"""The #93 ontology in mapping YAML and the graph (issue #113, ADR-0027,
ADR-0028): condition trees, actors, controls, reference nodes and
`DERIVED_FROM`, validated before any write and traversable through the
query builder.

Fixtures are invented (`tests/fixtures/mappings/test-obligations.yaml`, the
`test-act` instrument). Graph tests run on a local Neo4j (`NEO4J_TEST_URI`).
"""

from __future__ import annotations

import copy
import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from compliance_rag.adapters.graph_query import Direction, GraphQuery
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.graph.loader import load_instrument_version, load_mappings
from compliance_rag.graph.mappings import MappingError, load_mapping_file, load_mapping_set
from tests.test_graph_loader import (
    FIXTURE,
    JUN,
    MAR,
    SEP,
    SINCE,
    V2,
    clause_records,
    fixture_document,
    load_test_corpus,
    write_yaml,
)
from tests.test_neo4j_graph import graph as graph
from tests.test_neo4j_graph import needs_neo4j


def _condition(**fields: Any) -> dict[str, Any]:  # Any: YAML
    return {
        "id": "COND-T",
        "description": "d",
        "clause_ids": ["test-act:v1:s2/1"],
        "valid_from": "2026-01-01",
    } | fields


LEAF = {"field": "opted_in", "test": "eq", "value": True}
ASK = {"question": "q?", "unassessable_reason": "r"}
TWO = ["COND-TEST-LARGE", "COND-TEST-OPTED-IN"]


# ---- validation, no server ---------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        (_condition(), "exactly one of"),
        (_condition(leaf=LEAF, any_of=TWO), "exactly one"),
        (_condition(leaf=LEAF), "question"),
        (_condition(any_of=TWO, **ASK), "question"),
        (_condition(all_of=["COND-TEST-LARGE"]), "two or more"),
        (_condition(leaf=LEAF | {"test": "in", "value": "x"}, **ASK), "list"),
        (_condition(leaf=LEAF | {"test": "gt", "value": "big"}, **ASK), "number"),
        (_condition(leaf=LEAF | {"test": "before", "value": "soon"}, **ASK), "date"),
        (_condition(leaf=LEAF | {"period": "any_ended_financial_year"}, **ASK), "since_field"),
        (_condition(leaf=LEAF | {"field": "Opted In"}, **ASK), "snake_case"),
    ],
)
def test_a_condition_has_one_well_formed_shape(
    tmp_path: Path,
    condition: dict[str, Any],  # Any: YAML
    message: str,
) -> None:
    document = fixture_document()
    document["conditions"].append(condition)
    with pytest.raises(MappingError, match=message):
        load_mapping_file(write_yaml(tmp_path, document))


def test_a_condition_cycle_is_refused(tmp_path: Path) -> None:
    document = fixture_document()
    document["conditions"].append(_condition(id="COND-LOOP", **{"not": "COND-TEST-COVERED"}))
    document["conditions"][0]["any_of"] = ["COND-TEST-LARGE", "COND-LOOP"]
    with pytest.raises(MappingError, match="cycle"):
        load_mapping_set([write_yaml(tmp_path, document)])


def test_a_referenced_node_must_be_in_force_for_all_of_its_referrer(tmp_path: Path) -> None:
    """An actor qualified by a condition that ends before it does would be
    unqualified for part of its life."""
    document = fixture_document()
    document["conditions"][0] |= {"valid_to": "2026-06-01", "end_reason": "repealed"}
    with pytest.raises(MappingError, match="not in force for all of it"):
        load_mapping_set([write_yaml(tmp_path, document)])


def test_an_obligation_names_an_actor(tmp_path: Path) -> None:
    document = fixture_document()
    document["obligations"][2]["applies_to"] = {"actors": []}
    with pytest.raises(MappingError, match="at least one actor"):
        load_mapping_file(write_yaml(tmp_path, document))


def test_an_actor_id_is_namespaced_by_its_instrument(tmp_path: Path) -> None:
    document = fixture_document()
    document["actors"][0]["id"] = "keeper"
    with pytest.raises(MappingError, match="test-act/actor/"):
        load_mapping_file(write_yaml(tmp_path, document))


def test_a_list_names_each_target_once(tmp_path: Path) -> None:
    document = fixture_document()
    document["obligations"][2]["applies_to"]["actors"] *= 2
    with pytest.raises(MappingError, match="listed twice"):
        load_mapping_file(write_yaml(tmp_path, document))


# ---- in the graph -------------------------------------------------------------


def _paths(rows: tuple[Any, ...]) -> set[tuple[str, ...]]:  # Any: GraphRow
    return {tuple(r.path) for r in rows}


def _hop(start: str, edge: str, to: str, *, as_of: dt.date = SEP) -> GraphQuery:
    return (
        GraphQuery(as_of=as_of, max_depth=1)
        .start(start)
        .follow((edge,), to, direction=Direction.OUT)
    )


@needs_neo4j
async def test_the_ontology_is_traversable_through_the_builder(graph: Neo4jGraph) -> None:
    """Every edge of the ontology, read back as_of a date through the query
    builder (D-05) — the shape Track A reads."""
    await load_test_corpus(graph)
    await load_mappings(graph, load_mapping_set([FIXTURE]), today=SEP)
    assert _paths(await graph.read(_hop("Obligation", "APPLIES_TO", "Actor").build())) == {
        ("OB-TEST-1B", "test-act/actor/keeper"),
        ("OB-TEST-2", "test-act/actor/keeper"),
    }
    assert _paths(await graph.read(_hop("Actor", "QUALIFIED_BY", "Condition").build())) == {
        ("test-act/actor/keeper", "COND-TEST-COVERED")
    }
    assert _paths(await graph.read(_hop("Condition", "HAS_OPERAND", "Condition").build())) == {
        ("COND-TEST-COVERED", "COND-TEST-LARGE"),
        ("COND-TEST-COVERED", "COND-TEST-OPTED-IN"),
    }
    assert ("OB-TEST-2", "test-act:v1:s2/1") in _paths(
        await graph.read(_hop("Obligation", "DERIVED_FROM", "Clause").build())
    )
    (leaf,) = await graph.read(
        GraphQuery(as_of=SEP, max_depth=1).start("Condition", stable_id="COND-TEST-LARGE").build()
    )
    assert {k: leaf.node[k] for k in ("operator", "field", "period", "test", "value")} == {
        "operator": "leaf",
        "field": "annual_turnover_aud",
        "period": "previous_financial_year",
        "test": "gt",
        "value": 1000,
    }


@needs_neo4j
async def test_an_obligation_survives_a_recompilation_by_gaining_an_edge(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    """ADR-0028: v2 recompiles the Act; the same items derive from both
    versions' clauses, and as_of picks the text in force."""
    await load_test_corpus(graph)
    await load_instrument_version(graph, V2, instrument_since=SINCE, effective=JUN, supersedes="v1")
    document = fixture_document()
    document["source_clause_ids"] += ["test-act:v2:s1/1", "test-act:v2:s2/1"]
    for item in [*document["conditions"], *document["actors"], *document["obligations"]]:
        v1 = item["clause_ids"][0]
        v2 = v1.replace(":v1:", ":v2:")
        if str(item["valid_from"]) >= "2026-06-01":  # starts with v2: v2's text only
            item["clause_ids"] = [v2]
        elif item.get("valid_to") is None:  # spans both: gains v2's clause
            item["clause_ids"] = [v1, v2]
    await load_mappings(graph, load_mapping_set([write_yaml(tmp_path, document)]), today=SEP)
    for as_of, version in ((MAR, "v1"), (SEP, "v2")):
        query = (
            GraphQuery(as_of=as_of, max_depth=1)
            .start("Obligation", stable_id="OB-TEST-2")
            .follow(("DERIVED_FROM",), "Clause", direction=Direction.OUT)
        )
        assert _paths(await graph.read(query.build())) == {
            ("OB-TEST-2", f"test-act:{version}:s2/1")
        }


@needs_neo4j
async def test_changed_operands_of_a_loaded_condition_are_refused_before_any_write(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await load_test_corpus(graph)
    await load_mappings(graph, load_mapping_set([FIXTURE]), today=SEP)
    before = await graph.counts()
    document = fixture_document()
    document["conditions"].append(_condition(id="COND-TEST-NEW", leaf=LEAF, **ASK))
    document["conditions"][0]["any_of"] = ["COND-TEST-LARGE", "COND-TEST-NEW"]
    with pytest.raises(MappingError, match="COND-TEST-COVERED is already loaded with different"):
        await load_mappings(graph, load_mapping_set([write_yaml(tmp_path, document)]), today=SEP)
    assert await graph.counts() == before


@needs_neo4j
async def test_jurisdictions_sectors_and_controls_load_and_crosswalk(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await load_test_corpus(graph)
    document = fixture_document()
    document["jurisdictions"] = [
        {"id": "AU", "name": "Australia", "source": "ISO 3166-1", "valid_from": "1901-01-01"}
    ]
    document["sectors"] = [
        {
            "id": "anzsic-2006:Q",
            "name": "Health",
            "source": "ANZSIC 2006",
            "valid_from": "2006-01-01",
        }
    ]
    document["controls"] = [
        {
            "id": "test-act/control/c1",
            "clause_ids": ["test-act:v1:s2/1"],
            "descriptor": "An invented control.",
            "valid_from": "2026-01-01",
        }
    ]
    document["obligations"][2]["applies_to"] |= {
        "jurisdictions": ["AU"],
        "sectors": ["anzsic-2006:Q"],
    }
    document["crosswalks"].append(
        {"from": "OB-TEST-2", "to": "test-act/control/c1", "kind": "partial", "note": "n"}
    )
    await load_mappings(graph, load_mapping_set([write_yaml(tmp_path, document)]), today=SEP)
    for label, edge, expected in (
        ("Jurisdiction", "APPLIES_TO", ("OB-TEST-2", "AU")),
        ("Sector", "APPLIES_TO", ("OB-TEST-2", "anzsic-2006:Q")),
        ("Control", "CROSSWALKS_TO", ("OB-TEST-2", "test-act/control/c1")),
    ):
        query = (
            GraphQuery(as_of=SEP, max_depth=1)
            .start("Obligation", stable_id="OB-TEST-2")
            .follow((edge,), label, direction=Direction.OUT)
        )
        assert _paths(await graph.read(query.build())) == {expected}


# ---- review of #122: one retirement, many compilations, no silent change ------


def test_an_operand_may_end_before_its_parent(tmp_path: Path) -> None:
    """ADR-0027, Q1: repealing one gate is one retirement. The parent keeps
    its id; on later dates the repealed operand counts as false."""
    document = fixture_document()
    document["conditions"][2] |= {"valid_to": "2026-06-01", "end_reason": "repealed"}
    mappings = load_mapping_set([write_yaml(tmp_path, document)])
    assert mappings.conditions_by_id["COND-TEST-COVERED"].valid_to is None


def test_a_crosswalk_between_items_never_in_force_together_is_refused(tmp_path: Path) -> None:
    document = fixture_document()
    document["crosswalks"][0] |= {"from": "OB-TEST-1", "to": "OB-TEST-1B"}
    with pytest.raises(MappingError, match="never in force at the same time"):
        load_mapping_set([write_yaml(tmp_path, document)])


@needs_neo4j
async def test_an_item_may_derive_from_three_compilations(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    """ADR-0028: v2 is in force on neither the item's first day nor today,
    and is still a valid grounding — each clause only has to overlap."""
    v3 = clause_records(
        "v3", {"s1/1": "Keep records safe and say how.", "s2/1": "Destroy records."}
    )
    await load_test_corpus(graph)
    await load_instrument_version(graph, V2, instrument_since=SINCE, effective=MAR, supersedes="v1")
    await load_instrument_version(graph, v3, instrument_since=SINCE, effective=JUN, supersedes="v2")
    document = fixture_document()
    document["source_clause_ids"] += [f"test-act:{v}:s{n}/1" for v in ("v2", "v3") for n in (1, 2)]
    document["obligations"][2]["clause_ids"] = [f"test-act:{v}:s2/1" for v in ("v1", "v2", "v3")]
    for item in [*document["conditions"]]:
        item["clause_ids"] = [f"test-act:{v}:s2/1" for v in ("v1", "v2", "v3")]
    document["actors"][0]["clause_ids"] = [f"test-act:{v}:s1/1" for v in ("v1", "v2", "v3")]
    del document["obligations"][:2]
    document["crosswalks"] = []
    await load_mappings(graph, load_mapping_set([write_yaml(tmp_path, document)]), today=SEP)
    for as_of, version in ((dt.date(2026, 2, 1), "v1"), (dt.date(2026, 4, 1), "v2"), (SEP, "v3")):
        query = (
            GraphQuery(as_of=as_of, max_depth=1)
            .start("Obligation", stable_id="OB-TEST-2")
            .follow(("DERIVED_FROM",), "Clause", direction=Direction.OUT)
        )
        assert _paths(await graph.read(query.build())) == {
            ("OB-TEST-2", f"test-act:{version}:s2/1")
        }


@pytest.mark.parametrize(
    ("section", "index", "edit", "message"),
    [
        ("obligations", 2, {"requires": {"conditions": ["COND-TEST-LARGE"]}}, "REQUIRES"),
        ("obligations", 2, {"valid_from": "2026-02-01"}, "valid_from"),
        ("actors", 0, {"qualified_by": "COND-TEST-LARGE"}, "QUALIFIED_BY"),
    ],
)
@needs_neo4j
async def test_a_loaded_item_cannot_change_meaning(
    graph: Neo4jGraph,
    tmp_path: Path,
    section: str,
    index: int,
    edit: dict[str, object],
    message: str,
) -> None:
    await load_test_corpus(graph)
    await load_mappings(graph, load_mapping_set([FIXTURE]), today=SEP)
    before = await graph.counts()
    document = fixture_document()
    document[section][index] |= edit
    with pytest.raises(MappingError, match=message):
        await load_mappings(graph, load_mapping_set([write_yaml(tmp_path, document)]), today=SEP)
    assert await graph.counts() == before


@needs_neo4j
async def test_a_loaded_item_cannot_lose_a_clause_or_rewrite_a_crosswalk(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await load_test_corpus(graph)
    await load_instrument_version(graph, V2, instrument_since=SINCE, effective=JUN, supersedes="v1")
    grown = fixture_document()
    grown["source_clause_ids"].append("test-act:v2:s2/1")
    grown["obligations"][2]["clause_ids"].append("test-act:v2:s2/1")
    grown["obligations"][1]["clause_ids"] = ["test-act:v2:s2/1"]
    grown["source_clause_ids"].append("test-act:v2:s1/1")
    for item in [*grown["conditions"], *grown["actors"]]:
        item["clause_ids"].append(item["clause_ids"][0].replace(":v1:", ":v2:"))
        grown["source_clause_ids"].append(item["clause_ids"][-1])
    grown["source_clause_ids"] = sorted(set(grown["source_clause_ids"]))
    await load_mappings(graph, load_mapping_set([write_yaml(tmp_path, grown, "a.yaml")]), today=SEP)

    shrunk = copy.deepcopy(grown)
    shrunk["obligations"][2]["clause_ids"] = ["test-act:v2:s2/1"]
    with pytest.raises(MappingError, match="a clause was removed"):
        await load_mappings(
            graph, load_mapping_set([write_yaml(tmp_path, shrunk, "b.yaml")]), today=SEP
        )

    rekinded = copy.deepcopy(grown)
    rekinded["crosswalks"][0]["kind"] = "equivalent"  # a crosswalk's kind is meaning
    with pytest.raises(MappingError, match="crosswalk to OB-TEST-2"):
        await load_mappings(
            graph, load_mapping_set([write_yaml(tmp_path, rekinded, "c.yaml")]), today=SEP
        )
    renoted = copy.deepcopy(grown)
    renoted["crosswalks"][0]["note"] = "A different note."  # its note is wording (ADR-0031)
    with pytest.raises(MappingError, match=r"wording changed in loaded items \['OB-TEST-1B'\]"):
        await load_mappings(
            graph, load_mapping_set([write_yaml(tmp_path, renoted, "d.yaml")]), today=SEP
        )
