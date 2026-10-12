"""The #93 ontology in mapping YAML and the graph (issue #113, ADR-0027,
ADR-0028): condition trees, actors, controls, reference nodes and
`DERIVED_FROM`, validated before any write and traversable through the
query builder.

Fixtures are invented (`tests/fixtures/mappings/test-obligations.yaml`, the
`test-act` instrument). Graph tests run on a local Neo4j (`NEO4J_TEST_URI`).
"""

from __future__ import annotations

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
