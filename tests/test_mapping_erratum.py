"""A mapping edge that was never true is removed as an acknowledged erratum
(#245, ADR-0050).

The human's decision: "APP 12.6: option (b), remove it as an acknowledged
erratum under a short ADR. It was my error to sign a batch containing a
permission, and your draft flagged it. Superseding from today would leave a
qualifier relationship in force for every earlier date that was never correct
in the first place — an erratum is what this is, so record it as one." So:

- a grow-only edge (`DERIVED_FROM`, `EXCEPTED_BY`, `READ_WITH`) the mappings
  no longer give is removed only when an `ErratumAcknowledgement` names
  exactly those edges, with who and why; missing or extra refuses the load;
- all the removals land in one transaction, or none do;
- the source node's `corrections` trail records when, by whom, why,
  `"erratum": true` and the edges; the edge is never loaded again;
- never a node, a meaning edge or a property;
- Track A over Neo4j after the erratum equals the fake over the trimmed YAML.

The mechanism on the invented `test-act` fixture; the real case, APP 12.6
read with 12.5, on the corpus mappings.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncGenerator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from compliance_rag.adapters.fakes import FakeGraphStore
from compliance_rag.adapters.graph_schema import (
    ERRATUM_EDGES,
    EdgeRef,
    GraphWriteRefused,
    NodeRef,
)
from compliance_rag.adapters.interfaces import SourceKind, SourceVersion
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.assessment.evidence import Facts
from compliance_rag.eval.applicability import load_applicability_set
from compliance_rag.graph.loader import (
    GROW_ONLY,
    MEANING,
    ErratumAcknowledgement,
    ErratumEdge,
    load_mappings,
)
from compliance_rag.graph.mappings import MappingError, MappingSet, load_mapping_set
from compliance_rag.graph.track_a import GraphTrackA
from tests.test_applicability import AS_OF, FIXTURE
from tests.test_applicability_gate import fake_for, graph_for
from tests.test_graph_loader import write_yaml
from tests.test_neo4j_graph import cypher, needs_neo4j
from tests.test_neo4j_graph import graph as graph
from tests.test_obligation_exceptions import EXCEPTIONS
from tests.test_obligation_qualifiers import (
    OVER,
    QUALIFIER,
    QUALIFIER_A,
    QUALIFIER_B,
    QUALIFIERS,
    VALIDITY,
    edges,
    load_corpus,
    qualifiers_document,
    with_both,
)

ROOT = Path(__file__).resolve().parents[1]
CORPUS_MAPPINGS = ROOT / "corpus" / "mappings"
CORPUS_QUALIFIERS = CORPUS_MAPPINGS / "privacy-act-1988-qualifiers.yaml"
APP12_5 = "OB-PA-APP12-5"
APP12_6 = "privacy-act-1988:C2026C00227:sch1/app12/6"
BY = "Lucas Frasca"
WHY = "a permission, not a qualifier: never true at any valid time"

KEEP_QUALIFIER = [
    ErratumEdge("OB-T-KEEP", "READ_WITH", c) for c in (QUALIFIER, QUALIFIER_A, QUALIFIER_B)
]
"""OB-T-KEEP's qualifier as loaded: s1/3 and the paragraphs inside it."""


def erratum(*named: ErratumEdge) -> ErratumAcknowledgement:
    return ErratumAcknowledgement(frozenset(named), by=BY, reason=WHY)


def without_keeps_qualifier(tmp_path: Path) -> MappingSet:
    """The qualifiers fixture with OB-T-KEEP's entry trimmed."""
    document = qualifiers_document()
    document["qualifiers"] = [
        q for q in document["qualifiers"] if q["obligation_id"] != "OB-T-KEEP"
    ]
    document["source_clause_ids"].remove(QUALIFIER)
    return load_mapping_set([FIXTURE, EXCEPTIONS, write_yaml(tmp_path, document)])


async def trail(graph: Neo4jGraph, stable_id: str) -> list[dict[str, Any]]:  # Any: JSON
    (row,) = await cypher(
        graph, f"MATCH (n {{stable_id: '{stable_id}'}}) RETURN n.corrections AS c"
    )
    entries = cast("list[str]", row["c"] or [])
    return [json.loads(e) for e in entries]


# ---- the acknowledgement ----------------------------------------------------------


def test_an_erratum_removes_exactly_the_grow_only_edges_never_a_meaning_edge() -> None:
    assert frozenset(GROW_ONLY) == ERRATUM_EDGES
    assert not MEANING & ERRATUM_EDGES


@pytest.mark.parametrize(
    ("named", "by", "why", "message"),
    [
        ((), BY, WHY, "names the edges it removes"),
        ((ErratumEdge("OB-T-KEEP", "READ_WITH", QUALIFIER),), " ", WHY, "who acknowledged it"),
        ((ErratumEdge("OB-T-KEEP", "READ_WITH", QUALIFIER),), BY, "", "who acknowledged it"),
        (
            (ErratumEdge("OB-T-KEEP", "APPLIES_TO", "test-act/actor/keeper"),),
            BY,
            WHY,
            r"removes only .* edges, not \['OB-T-KEEP APPLIES_TO",
        ),
        (
            (ErratumEdge("test-act:v1:s1/1", "PART_OF", "test-act:v1"),),
            BY,
            WHY,
            "any other change of meaning is a new id",
        ),
    ],
    ids=["no-edges", "no-one", "no-reason", "a-meaning-edge", "a-clause-structure-edge"],
)
def test_an_erratum_names_grow_only_edges_who_and_why(
    named: tuple[ErratumEdge, ...], by: str, why: str, message: str
) -> None:
    with pytest.raises(MappingError, match=message):
        ErratumAcknowledgement(frozenset(named), by=by, reason=why)


# ---- the loader, on Neo4j ----------------------------------------------------------


@needs_neo4j
async def test_a_removed_qualifier_is_refused_without_the_erratum(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    """The refusal names every edge the load would remove, with what to do."""
    await load_corpus(graph)
    await load_mappings(graph, with_both(), today=AS_OF)
    before = await graph.counts()
    with pytest.raises(MappingError, match="erratum acknowledged exactly") as refused:
        await load_mappings(graph, without_keeps_qualifier(tmp_path), today=AS_OF)
    for edge in KEEP_QUALIFIER:
        assert str(edge) in str(refused.value)
    assert await graph.counts() == before


@needs_neo4j
async def test_the_erratum_must_name_exactly_the_edges_removed(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    """The paragraphs inside a qualifier travel as edges of their own, so an
    erratum names them too; and naming an edge the load keeps is refused."""
    await load_corpus(graph)
    await load_mappings(graph, with_both(), today=AS_OF)
    trimmed = without_keeps_qualifier(tmp_path)
    before = await graph.counts()
    with pytest.raises(MappingError, match=r"s1/3/a.*s1/3/b"):  # missing: the paragraphs
        await load_mappings(graph, trimmed, today=AS_OF, errata=erratum(KEEP_QUALIFIER[0]))
    kept = ErratumEdge("OB-T-HEALTH", "READ_WITH", "test-act:v1:s2/2")
    with pytest.raises(MappingError, match=r"acknowledged \['OB-T-HEALTH READ_WITH"):  # extra
        await load_mappings(graph, trimmed, today=AS_OF, errata=erratum(*KEEP_QUALIFIER, kept))
    assert await graph.counts() == before


@needs_neo4j
async def test_an_acknowledged_erratum_removes_the_edges_and_audits_them(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await load_corpus(graph)
    await load_mappings(graph, with_both(), today=AS_OF)
    before = await graph.counts()
    trimmed = without_keeps_qualifier(tmp_path)
    await load_mappings(graph, trimmed, today=AS_OF, errata=erratum(*KEEP_QUALIFIER))
    assert await edges(graph, "READ_WITH") == {("OB-T-HEALTH", "test-act:v1:s2/2")}
    after = await graph.counts()
    assert (after.nodes, after.edges) == (before.nodes, before.edges - 3)
    (entry,) = await trail(graph, "OB-T-KEEP")
    assert entry["erratum"] is True
    assert (entry["by"], entry["reason"]) == (BY, WHY)
    assert dt.datetime.fromisoformat(entry["at"]).tzinfo is not None
    assert entry["removed"] == [
        {"edge_type": "READ_WITH", "target": c} for c in (QUALIFIER, QUALIFIER_A, QUALIFIER_B)
    ]
    # Nothing else on the node changed, and its exceptions are where they were.
    (row,) = await cypher(
        graph, "MATCH (o:Obligation {stable_id:'OB-T-KEEP'}) RETURN o.summary AS s"
    )
    assert row["s"]
    assert ("OB-T-KEEP", "test-act:v1:s1/2") in await edges(graph, "EXCEPTED_BY")


@needs_neo4j
async def test_a_rerun_is_a_no_op_with_or_without_the_erratum(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await load_corpus(graph)
    await load_mappings(graph, with_both(), today=AS_OF)
    trimmed = without_keeps_qualifier(tmp_path)
    await load_mappings(graph, trimmed, today=AS_OF, errata=erratum(*KEEP_QUALIFIER))
    after = await graph.counts()
    again = await load_mappings(graph, trimmed, today=AS_OF, errata=erratum(*KEEP_QUALIFIER))
    plain = await load_mappings(graph, trimmed, today=AS_OF)
    assert (again.nodes_created, again.edges_created) == (0, 0)
    assert (plain.nodes_created, plain.edges_created) == (0, 0)
    assert await graph.counts() == after
    assert len(await trail(graph, "OB-T-KEEP")) == 1


@needs_neo4j
async def test_an_edge_an_erratum_removed_is_never_loaded_again(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    """Loading the file as it was signed would give the edge back, undoing
    the erratum without anyone deciding to."""
    await load_corpus(graph)
    await load_mappings(graph, with_both(), today=AS_OF)
    await load_mappings(
        graph, without_keeps_qualifier(tmp_path), today=AS_OF, errata=erratum(*KEEP_QUALIFIER)
    )
    before = await graph.counts()
    with pytest.raises(MappingError, match="was removed as an erratum"):
        await load_mappings(graph, with_both(), today=AS_OF)
    assert await graph.counts() == before


@needs_neo4j
async def test_track_a_after_the_erratum_equals_the_fake_over_the_trimmed_yaml(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await load_corpus(graph)
    await load_mappings(graph, with_both(), today=AS_OF)
    trimmed = without_keeps_qualifier(tmp_path)
    await load_mappings(graph, trimmed, today=AS_OF, errata=erratum(*KEEP_QUALIFIER))
    facts = Facts.of(OVER)
    got = await GraphTrackA(graph).coverage(facts, tenant_id="acme", as_of=AS_OF)
    fake = FakeGraphStore(trimmed, clause_validity=VALIDITY)
    assert got == await fake.coverage(facts, tenant_id="acme", as_of=AS_OF)
    assert got.obligation("OB-T-KEEP").qualifier_clause_ids == ()
    # At every date: an erratum is not a supersession.
    earlier = await GraphTrackA(graph).coverage(
        facts, tenant_id="acme", as_of=VALIDITY[QUALIFIER][0]
    )
    assert earlier.obligation("OB-T-KEEP").qualifier_clause_ids == ()


# ---- the adapter, on Neo4j ---------------------------------------------------------


KEEP = NodeRef("Obligation", "OB-T-KEEP")


def read_with(target: str) -> EdgeRef:
    return EdgeRef("READ_WITH", KEEP, NodeRef("Clause", target))


@needs_neo4j
async def test_the_adapter_removes_only_grow_only_edges_from_a_mapping_item_to_a_clause(
    graph: Neo4jGraph,
) -> None:
    await load_corpus(graph)
    await load_mappings(graph, with_both(), today=AS_OF)
    before = await graph.counts()
    refused = [
        EdgeRef("APPLIES_TO", KEEP, NodeRef("Actor", "test-act/actor/keeper")),
        EdgeRef(
            "PART_OF", NodeRef("Clause", QUALIFIER), NodeRef("InstrumentVersion", "test-act:v1")
        ),
        EdgeRef("DERIVED_FROM", NodeRef("Clause", QUALIFIER), NodeRef("Clause", QUALIFIER_A)),
    ]
    for edge in refused:
        with pytest.raises(GraphWriteRefused, match="an erratum removes"):
            await graph.remove_by_erratum([edge], by=BY, reason=WHY)
    with pytest.raises(GraphWriteRefused, match="who acknowledged it and why"):
        await graph.remove_by_erratum([read_with(QUALIFIER)], by=BY, reason=" ")
    assert await graph.counts() == before
    assert await trail(graph, "OB-T-KEEP") == []


@needs_neo4j
async def test_the_adapters_removals_land_together_or_not_at_all(graph: Neo4jGraph) -> None:
    """An edge that isn't there, with no erratum recording its removal,
    refuses the whole erratum: the edge that is there stays."""
    await load_corpus(graph)
    await load_mappings(graph, with_both(), today=AS_OF)
    before = await graph.counts()
    with pytest.raises(GraphWriteRefused, match=r"no edge .* to remove"):
        await graph.remove_by_erratum(
            [read_with(QUALIFIER), read_with("test-act:v1:s1/1")], by=BY, reason=WHY
        )
    assert await graph.counts() == before
    assert await trail(graph, "OB-T-KEEP") == []
    assert await graph.remove_by_erratum([read_with(QUALIFIER)], by=BY, reason=WHY) == 1
    assert await graph.remove_by_erratum([read_with(QUALIFIER)], by=BY, reason=WHY) == 0  # re-run
    assert len(await trail(graph, "OB-T-KEEP")) == 1


# ---- the script: a dry run by default ----------------------------------------------


@asynccontextmanager
async def _the(graph: Neo4jGraph) -> AsyncGenerator[Neo4jGraph]:
    yield graph


@pytest.fixture
def script(
    graph: Neo4jGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, Path]]:  # Any: the script module
    from scripts import load_mappings as module

    directory = tmp_path / "mappings"
    directory.mkdir()
    for path in (FIXTURE, EXCEPTIONS, QUALIFIERS):
        (directory / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    def resolved(kind: SourceKind, *, repo: Path) -> SourceVersion:
        assert kind is SourceKind.MAPPING
        return SourceVersion(SourceKind.MAPPING, "e" * 40)

    def opened(_settings: object) -> AbstractAsyncContextManager[Neo4jGraph]:
        return _the(graph)

    monkeypatch.setattr(module, "MAPPINGS", directory)
    monkeypatch.setattr(module, "resolve_source_version", resolved)
    monkeypatch.setattr(module.Neo4jGraph, "from_settings", opened)
    yield module, directory


@needs_neo4j
async def test_the_script_dry_runs_an_erratum_then_applies_it(
    graph: Neo4jGraph,
    script: tuple[Any, Path],  # Any: the script module
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, directory = script
    await load_corpus(graph)
    await module.main(AS_OF, None)  # the qualifiers as signed
    document = qualifiers_document()
    document["qualifiers"] = [
        q for q in document["qualifiers"] if q["obligation_id"] != "OB-T-KEEP"
    ]
    document["source_clause_ids"].remove(QUALIFIER)
    (directory / QUALIFIERS.name).write_text(yaml.safe_dump(document), encoding="utf-8")
    loads = "MATCH (l:Load) RETURN count(l) AS n"
    (recorded,) = await cypher(graph, loads)
    before = await graph.counts()
    capsys.readouterr()

    await module.main(AS_OF, None, erratum(*KEEP_QUALIFIER))
    printed = capsys.readouterr().out
    assert "dry run of mapping version untagged @ eeeeeee" in printed
    for edge in KEEP_QUALIFIER:
        assert f"  {edge}\n" in printed
    assert "nothing written" in printed
    assert await graph.counts() == before
    assert await cypher(graph, loads) == [recorded]  # no load recorded

    await module.main(AS_OF, None, erratum(*KEEP_QUALIFIER), apply=True)
    assert "removed as an erratum" in capsys.readouterr().out
    assert ("OB-T-KEEP", QUALIFIER) not in await edges(graph, "READ_WITH")
    await module.main(AS_OF, None, erratum(*KEEP_QUALIFIER))  # dry run after: a re-run
    assert "already removed by an erratum" in capsys.readouterr().out


@needs_neo4j
async def test_the_script_dry_run_prints_a_refusal(
    graph: Neo4jGraph,
    script: tuple[Any, Path],  # Any: the script module
) -> None:
    module, directory = script
    await load_corpus(graph)
    await module.main(AS_OF, None)
    document = qualifiers_document()
    document["qualifiers"] = [
        q for q in document["qualifiers"] if q["obligation_id"] != "OB-T-KEEP"
    ]
    document["source_clause_ids"].remove(QUALIFIER)
    (directory / QUALIFIERS.name).write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(SystemExit, match=r"refused: .*s1/3/a"):
        await module.main(AS_OF, None, erratum(KEEP_QUALIFIER[0]))


# ---- APP 12.6, on the corpus mappings ------------------------------------------------


def signed_qualifiers(tmp_path: Path) -> Path:
    """The qualifiers file as signed on 2026-10-15 (`mappings-2026.10.15`):
    today's, with 12.6 read with 12.5."""
    document = yaml.safe_load(CORPUS_QUALIFIERS.read_text(encoding="utf-8"))
    document["source_clause_ids"].append(APP12_6)
    document["qualifiers"].append({"obligation_id": APP12_5, "clause_ids": [APP12_6]})
    return write_yaml(tmp_path, document, CORPUS_QUALIFIERS.name)


def corpus_set(qualifiers: Path | None = None) -> MappingSet:
    paths = sorted(CORPUS_MAPPINGS.glob("*.yaml"))
    if qualifiers is not None:
        paths = [qualifiers if p.name == qualifiers.name else p for p in paths]
    return load_mapping_set(paths)


async def test_track_a_no_longer_reads_12_5_with_12_6() -> None:
    mappings = corpus_set()
    assert APP12_5 not in mappings.qualifiers_by_obligation
    assert APP12_6 not in mappings.clause_ids
    labels = load_applicability_set()
    coverage = await fake_for(mappings, labels).coverage(
        Facts.of([]), tenant_id="acme", as_of=max(c.as_of for c in labels.cases)
    )
    assert coverage.obligation(APP12_5).qualifier_clause_ids == ()


@needs_neo4j
async def test_the_12_6_erratum_on_the_corpus_mappings(graph: Neo4jGraph, tmp_path: Path) -> None:
    """Loaded as Aura was (the signed files), then the trimmed file: refused
    without the erratum, accepted with exactly that one edge, and then Track A
    over Neo4j equals the fake over the trimmed YAML, 12.5 read with nothing."""
    labels = load_applicability_set()
    today = max(c.as_of for c in labels.cases)
    signed = corpus_set(signed_qualifiers(tmp_path))
    await graph_for(graph, signed, labels)
    assert (APP12_5, APP12_6) in await edges(graph, "READ_WITH")
    trimmed = corpus_set()
    with pytest.raises(MappingError, match=f"{APP12_5} READ_WITH {APP12_6}"):
        await load_mappings(graph, trimmed, today=today)
    the_edge = ErratumEdge(APP12_5, "READ_WITH", APP12_6)
    report = await load_mappings(graph, trimmed, today=today, errata=erratum(the_edge))
    assert (report.nodes_created, report.edges_created) == (0, 0)
    assert (APP12_5, APP12_6) not in await edges(graph, "READ_WITH")
    (entry,) = await trail(graph, APP12_5)
    assert entry["removed"] == [{"edge_type": "READ_WITH", "target": APP12_6}]
    facts = Facts.of([])
    got = await GraphTrackA(graph).coverage(facts, tenant_id="acme", as_of=today)
    assert got == await fake_for(trimmed, labels).coverage(facts, tenant_id="acme", as_of=today)
    assert got.obligation(APP12_5).qualifier_clause_ids == ()
