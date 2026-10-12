"""Graph loaders (issue #89): corpus structure and mapping YAML, validated
before any write and idempotent on stable ID.

Fixtures are invented (`tests/fixtures/mappings/`, the `test-act`
instrument) — this ticket loads, it authors nothing. Graph tests run on a
local Neo4j (`NEO4J_TEST_URI`, see `tests/test_neo4j_graph.py`).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from compliance_rag.adapters.graph_query import Direction, GraphQuery
from compliance_rag.adapters.graph_schema import (
    Bitemporal,
    GraphCounts,
    GraphWriteRefused,
    NodeRef,
)
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.corpus.models import ClauseRecord, Scope
from compliance_rag.graph.loader import load_instrument_version, load_mappings
from compliance_rag.graph.mappings import (
    MappingError,
    load_mapping_file,
    load_mapping_set,
)
from tests.test_neo4j_graph import cypher, needs_neo4j
from tests.test_neo4j_graph import graph as graph

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "mappings" / "test-obligations.yaml"
README = ROOT / "corpus" / "mappings" / "README.md"
JAN, MAR, JUN, SEP = (dt.date(2026, m, 1) for m in (1, 3, 6, 9))
SINCE = dt.date(2000, 1, 1)


def fixture_document() -> dict[str, Any]:  # Any: YAML
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def _readme_document() -> dict[str, Any]:  # Any: YAML
    """Every YAML example in the README, joined into one document."""
    blocks = re.findall(r"```yaml\n(.*?)```", README.read_text(encoding="utf-8"), re.S)
    document: dict[str, Any] = {}  # Any: YAML
    for block in blocks:
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict):
            document |= parsed
    return document


def write_yaml(tmp_path: Path, document: dict[str, Any], name: str = "m.yaml") -> Path:  # Any: YAML
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def clause_records(version: str, texts: dict[str, str]) -> tuple[ClauseRecord, ...]:
    return tuple(
        ClauseRecord.build(
            instrument_id="test-act",
            version_id=version,
            clause_path=path,
            text=text,
            scope=Scope.PUBLIC,
            tenant_id="_public",
        )
        for path, text in texts.items()
    )


V1 = clause_records("v1", {"s1/1": "Keep records safe.", "s2/1": "Destroy records."})
V2 = clause_records("v2", {"s1/1": "Keep records safe and say how.", "s2/1": "Destroy records."})


# ---- validation, no server ---------------------------------------------------


def test_the_fixture_validates() -> None:
    mappings = load_mapping_set([FIXTURE])
    assert [o.id for o in mappings.obligations] == ["OB-TEST-1", "OB-TEST-1B", "OB-TEST-2"]
    assert [a.id for a in mappings.actors] == ["test-act/actor/keeper"]
    assert mappings.conditions_by_id["COND-TEST-COVERED"].operands == (
        "COND-TEST-LARGE",
        "COND-TEST-OPTED-IN",
    )


def test_the_readme_documented_shapes_validate(tmp_path: Path) -> None:
    """The README is the spec for mapping YAML: its examples, joined into
    one file, must validate — so the loader and the README can't drift.
    Every kind of the #93 ontology appears in it."""
    document = _readme_document()
    mappings = load_mapping_set([write_yaml(tmp_path, document)])
    assert all(mappings.by_label.values()), mappings.by_label
    assert mappings.crosswalks and mappings.policy_mappings


@pytest.mark.parametrize("field", ["author", "reviewed_by", "date", "source_clause_ids"])
def test_missing_front_matter_is_refused(tmp_path: Path, field: str) -> None:
    document = fixture_document()
    del document[field]
    with pytest.raises(MappingError, match=field):
        load_mapping_file(write_yaml(tmp_path, document))


@pytest.mark.parametrize(
    ("section", "index", "key", "value"),
    [
        ("obligations", 0, "end_reason", "revoked"),
        ("crosswalks", 0, "kind", "maybe"),
        ("policy_mappings", 0, "status", "pending"),
        ("policy_mappings", 0, "proposed_by", "agent"),
        ("obligations", 0, "colour", "blue"),  # unknown field
    ],
)
def test_unknown_values_are_refused(
    tmp_path: Path, section: str, index: int, key: str, value: str
) -> None:
    document = fixture_document()
    document[section][index][key] = value
    with pytest.raises(MappingError):
        load_mapping_file(write_yaml(tmp_path, document))


def test_an_unknown_leaf_test_is_refused(tmp_path: Path) -> None:
    document = fixture_document()
    document["conditions"][1]["leaf"]["test"] = "approximately"
    with pytest.raises(MappingError, match="test"):
        load_mapping_file(write_yaml(tmp_path, document))


def test_a_model_reviewer_is_refused(tmp_path: Path) -> None:
    document = fixture_document() | {"reviewed_by": "model: some model"}
    with pytest.raises(MappingError, match="never a model"):
        load_mapping_file(write_yaml(tmp_path, document))


def test_an_obligation_outside_its_sources_is_refused(tmp_path: Path) -> None:
    document = fixture_document()
    document["obligations"][2]["clause_ids"] = ["test-act:v1:s9/1"]
    with pytest.raises(MappingError, match="not in source_clause_ids"):
        load_mapping_file(write_yaml(tmp_path, document))


@pytest.mark.parametrize(
    ("section", "index", "key", "value", "message"),
    [
        ("obligations", 1, "supersedes", "OB-NOPE", "unknown obligation"),
        ("obligations", 0, "valid_to", "2026-05-01", "no gap or overlap"),
        ("policy_mappings", 0, "obligation_id", "OB-NOPE", "unknown obligation"),
        ("policy_mappings", 0, "status", "approved", "approved_by"),
        ("obligations", 2, "requires", {"conditions": ["COND-NOPE"]}, "unknown condition"),
        ("obligations", 2, "applies_to", {"actors": ["test-act/actor/nope"]}, "unknown actor"),
        ("actors", 0, "qualified_by", "COND-NOPE", "unknown condition"),
        ("conditions", 0, "any_of", ["COND-TEST-LARGE", "COND-NOPE"], "unknown condition"),
        ("crosswalks", 0, "to", "OB-TEST-TYPO", "unknown obligation or control OB-TEST-TYPO"),
    ],
)
def test_references_must_resolve(
    tmp_path: Path, section: str, index: int, key: str, value: object, message: str
) -> None:
    document = fixture_document()
    document[section][index][key] = value
    with pytest.raises(MappingError, match=message):
        load_mapping_set([write_yaml(tmp_path, document)])


def test_ids_are_unique_across_files(tmp_path: Path) -> None:
    first = write_yaml(tmp_path, fixture_document(), "a.yaml")
    second = fixture_document()
    second["obligations"] = [second["obligations"][2]]
    second["crosswalks"], second["policy_mappings"] = [], []
    with pytest.raises(MappingError, match="defined twice"):
        load_mapping_set([first, write_yaml(tmp_path, second, "b.yaml")])


def test_corpus_mappings_holds_no_authored_content() -> None:
    """Authoring obligations is the human's design session, not this ticket."""
    assert [p.name for p in (ROOT / "corpus" / "mappings").iterdir()] == ["README.md"]


# ---- corpus structure, on a real server -------------------------------------


def _version_at(as_of: dt.date) -> GraphQuery:
    return (
        GraphQuery(as_of=as_of, max_depth=1)
        .start("Instrument", stable_id="test-act")
        .follow(("VERSION_OF",), "InstrumentVersion", direction=Direction.IN)
    )


def _clauses_at(as_of: dt.date) -> GraphQuery:
    return GraphQuery(as_of=as_of, max_depth=1).start("Clause")


@needs_neo4j
async def test_the_corpus_loader_is_idempotent(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    first = await load_instrument_version(
        graph, V1, instrument_since=SINCE, effective=JAN, supersedes=None
    )
    again = await load_instrument_version(
        graph, V1, instrument_since=SINCE, effective=JAN, supersedes=None
    )
    assert (first.nodes_created, first.edges_created) == (4, 3)
    assert (again.nodes_created, again.edges_created) == (0, 0)
    assert again.counts == first.counts == await graph.counts() == GraphCounts(4, 3)


@needs_neo4j
async def test_a_new_version_supersedes_and_retires_its_clauses(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await load_instrument_version(graph, V1, instrument_since=SINCE, effective=JAN, supersedes=None)
    await load_instrument_version(graph, V2, instrument_since=SINCE, effective=JUN, supersedes="v1")
    await load_instrument_version(graph, V2, instrument_since=SINCE, effective=JUN, supersedes="v1")
    assert [r.node["stable_id"] for r in await graph.read(_version_at(MAR).build())] == [
        "test-act:v1"
    ]
    assert [r.node["stable_id"] for r in await graph.read(_version_at(SEP).build())] == [
        "test-act:v2"
    ]
    in_force = sorted(str(r.node["stable_id"]) for r in await graph.read(_clauses_at(SEP).build()))
    assert in_force == ["test-act:v2:s1/1", "test-act:v2:s2/1"]
    # nothing deleted: 1 instrument + 2 versions + 4 clauses; VERSION_OF x2, PART_OF x4, SUPERSEDES
    assert await graph.counts() == GraphCounts(7, 7)


@needs_neo4j
async def test_a_tenant_document_is_never_persisted(graph: Neo4jGraph) -> None:
    policy = ClauseRecord.build(
        instrument_id="acme/policy",
        version_id="v1",
        clause_path="1/1",
        text="Acme keeps records.",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    with pytest.raises(ValueError, match="only library law"):
        await load_instrument_version(
            graph, (policy,), instrument_since=SINCE, effective=JAN, supersedes=None
        )
    assert await graph.counts() == GraphCounts(0, 0)


# ---- mapping YAML, on a real server -----------------------------------------


async def load_test_corpus(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await load_instrument_version(graph, V1, instrument_since=SINCE, effective=JAN, supersedes=None)


@needs_neo4j
async def test_mappings_load_idempotently(graph: Neo4jGraph) -> None:
    await load_test_corpus(graph)
    mappings = load_mapping_set([FIXTURE])
    first = await load_mappings(graph, mappings, today=SEP)
    again = await load_mappings(graph, mappings, today=SEP)
    # 3 conditions, 1 actor, 3 obligations; DERIVED_FROM x7, HAS_OPERAND x2,
    # QUALIFIED_BY, APPLIES_TO x3, SUPERSEDES, CROSSWALKS_TO
    assert (first.nodes_created, first.edges_created) == (7, 15)
    assert (again.nodes_created, again.edges_created) == (0, 0)
    assert again.counts == await graph.counts() == GraphCounts(4 + 7, 3 + 15)


@needs_neo4j
async def test_a_superseded_obligation_is_retired_not_deleted(graph: Neo4jGraph) -> None:
    await load_test_corpus(graph)
    await load_mappings(graph, load_mapping_set([FIXTURE]), today=SEP)

    def obligation(as_of: dt.date) -> GraphQuery:
        return GraphQuery(as_of=as_of, max_depth=1).start("Obligation")

    def ids(rows: tuple[Any, ...]) -> list[str]:  # Any: GraphRow
        return sorted(str(r.node["stable_id"]) for r in rows)

    assert ids(await graph.read(obligation(MAR).build())) == ["OB-TEST-1", "OB-TEST-2"]
    assert ids(await graph.read(obligation(SEP).build())) == ["OB-TEST-1B", "OB-TEST-2"]
    (row,) = await cypher(
        graph,
        "MATCH (:Obligation {stable_id:'OB-TEST-1B'})-[:SUPERSEDES]->(o) RETURN o.stable_id AS id",
    )
    assert row["id"] == "OB-TEST-1"


@needs_neo4j
async def test_a_proposed_policy_mapping_never_reaches_the_graph(graph: Neo4jGraph) -> None:
    """Tenant content stays out of Aura (D-09), so a policy mapping — proposed
    or approved — is validated and reported, never written: it can't be
    traversed because it isn't there."""
    await load_test_corpus(graph)
    report = await load_mappings(graph, load_mapping_set([FIXTURE]), today=SEP)
    assert report.not_loaded == ("MAP-TEST-1",)
    rows = await cypher(
        graph,
        "MATCH (n) WHERE n.stable_id CONTAINS 'MAP-' OR n.stable_id STARTS WITH 'acme' "
        "RETURN count(n) AS c",
    )
    assert rows[0]["c"] == 0


@needs_neo4j
async def test_an_obligation_about_a_missing_clause_is_refused_before_any_write(
    graph: Neo4jGraph,
    tmp_path: Path,
) -> None:
    await load_test_corpus(graph)
    before = await graph.counts()
    document = fixture_document()
    document["source_clause_ids"].append("test-act:v1:s9/1")
    document["obligations"][2]["clause_ids"] = ["test-act:v1:s9/1"]
    with pytest.raises(MappingError, match="not in the graph"):
        await load_mappings(graph, load_mapping_set([write_yaml(tmp_path, document)]), today=SEP)
    assert await graph.counts() == before


# ---- review findings ----------------------------------------------------------


def test_one_obligation_has_one_successor(tmp_path: Path) -> None:
    document = fixture_document()
    twin = dict(document["obligations"][1], id="OB-TEST-1C")
    document["obligations"].append(twin)
    with pytest.raises(MappingError, match="superseded by both"):
        load_mapping_set([write_yaml(tmp_path, document)])


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("policy_clause_id", "acme-policy-without-segments", "policy_clause_id"),
        ("approved_by", "model: a model", "never a model"),
    ],
)
def test_policy_mapping_fields_are_checked(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    document = fixture_document()
    mapping = document["policy_mappings"][0]
    mapping[key] = value
    if key == "approved_by":
        mapping |= {"status": "approved", "approved_date": "2026-10-10"}
    with pytest.raises(MappingError, match=message):
        load_mapping_file(write_yaml(tmp_path, document))


def test_an_in_condition_may_list_numbers(tmp_path: Path) -> None:
    document = fixture_document()
    document["conditions"][2]["leaf"] = {"field": "size_band", "test": "in", "value": [1, 2, 3]}
    leaf = load_mapping_file(write_yaml(tmp_path, document)).conditions[2].leaf
    assert leaf is not None and leaf.value == (1, 2, 3)


@needs_neo4j
async def test_a_correction_in_place_is_refused_before_any_write(
    graph: Neo4jGraph,
    tmp_path: Path,
) -> None:
    """README: "correcting a mapping edits the existing entry". Once loaded,
    that would mutate a node (D-05) — refused before anything is written,
    not halfway through (#93 decides how corrections work)."""
    await load_test_corpus(graph)
    await load_mappings(graph, load_mapping_set([FIXTURE]), today=SEP)
    before = await graph.counts()
    document = fixture_document()
    document["obligations"][2]["summary"] = "Destroy the invented records promptly."
    document["obligations"].append(
        {
            "id": "OB-TEST-0",  # sorts first: would be written before the refusal
            "clause_ids": ["test-act:v1:s2/1"],
            "summary": "A new one.",
            "valid_from": "2026-01-01",
            "applies_to": {"actors": ["test-act/actor/keeper"]},
        }
    )
    with pytest.raises(MappingError, match="OB-TEST-2 is already loaded"):
        await load_mappings(graph, load_mapping_set([write_yaml(tmp_path, document)]), today=SEP)
    assert await graph.counts() == before


@needs_neo4j
async def test_an_obligation_may_not_outlive_its_clause(graph: Neo4jGraph) -> None:
    """v1's clauses are retired when v2 supersedes them in June; an open-ended
    obligation on a v1 clause would then point at law no longer in force."""
    await load_test_corpus(graph)
    await load_instrument_version(graph, V2, instrument_since=SINCE, effective=JUN, supersedes="v1")
    with pytest.raises(MappingError, match=r"end the \w+ with its clause"):
        await load_mappings(graph, load_mapping_set([FIXTURE]), today=SEP)
    assert await graph.counts() == GraphCounts(7, 7)


@needs_neo4j
async def test_superseding_a_version_not_in_force_writes_nothing(graph: Neo4jGraph) -> None:
    await load_test_corpus(graph)
    before = await graph.counts()
    with pytest.raises(ValueError, match="nothing to supersede"):
        await load_instrument_version(
            graph, V2, instrument_since=SINCE, effective=JUN, supersedes="v9"
        )
    assert await graph.counts() == before


@needs_neo4j
async def test_a_refused_supersession_leaves_the_old_clauses_in_force(graph: Neo4jGraph) -> None:
    """The old version and its clauses are retired inside `amend`'s one
    transaction: if the new version is refused, none of them is retired."""
    await load_test_corpus(graph)
    await graph.put_node(
        NodeRef("InstrumentVersion", "test-act:v2"),
        Bitemporal(valid_from=SEP),  # clashes with effective=JUN
    )
    with pytest.raises(GraphWriteRefused, match="immutable"):
        await load_instrument_version(
            graph, V2, instrument_since=SINCE, effective=JUN, supersedes="v1"
        )
    in_force = sorted(str(r.node["stable_id"]) for r in await graph.read(_clauses_at(SEP).build()))
    assert in_force == ["test-act:v1:s1/1", "test-act:v1:s2/1"]
