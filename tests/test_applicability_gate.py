"""The applicability gate (issue #117): golden profiles in, expected
obligation outcomes out, checked through `GraphStore` — the fake always, and
Track A over Neo4j where a server is available (CI). The gate's own
machinery is tested on the invented `test-act` labels; the corpus labels
(`corpus/golden/applicability.yaml`) are checked whenever they exist.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from compliance_rag.adapters.fakes import FakeGraphStore
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.assessment.coverage import Outcome
from compliance_rag.corpus.models import ClauseRecord, Scope
from compliance_rag.eval.applicability import (
    APPLICABILITY_PATH,
    ApplicabilityGoldenSet,
    UnsignedLabels,
    check,
    load_applicability_set,
)
from compliance_rag.graph.loader import load_instrument_version, load_mappings
from compliance_rag.graph.mappings import MappingSet, load_mapping_set
from compliance_rag.graph.track_a import GraphTrackA
from tests.test_neo4j_graph import graph as graph
from tests.test_neo4j_graph import needs_neo4j

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_LABELS = ROOT / "tests" / "fixtures" / "golden" / "test-applicability.yaml"
FIXTURE_MAPPINGS = ROOT / "tests" / "fixtures" / "mappings" / "test-applicability.yaml"
CORPUS_MAPPINGS = ROOT / "corpus" / "mappings"


def _clause_ids(mappings: MappingSet) -> set[str]:
    return {c for _, item in mappings.grounded for c in item.clause_ids}


def fake_for(mappings: MappingSet, labels: ApplicabilityGoldenSet) -> FakeGraphStore:
    return FakeGraphStore(mappings, clause_validity=labels.clause_validity(_clause_ids(mappings)))


async def graph_for(
    graph: Neo4jGraph, mappings: MappingSet, labels: ApplicabilityGoldenSet
) -> GraphTrackA:
    """The mappings loaded into a real graph over skeleton clauses: the graph
    stores no clause text, only ids, validity and a hash, so placeholder text
    is enough for Track A (and never leaves the test database)."""
    await graph.apply_schema()
    by_version: dict[tuple[str, str], list[str]] = {}
    for clause_id in sorted(_clause_ids(mappings)):
        instrument_id, version_id, path = clause_id.split(":")
        by_version.setdefault((instrument_id, version_id), []).append(path)
    for (instrument_id, version_id), paths in by_version.items():
        effective = labels.compilations[f"{instrument_id}:{version_id}"]
        records = tuple(
            ClauseRecord.build(
                instrument_id=instrument_id,
                version_id=version_id,
                clause_path=path,
                text=f"(placeholder for {path})",
                scope=Scope.PUBLIC,
                tenant_id="_public",
            )
            for path in paths
        )
        await load_instrument_version(
            graph,
            records,
            instrument_since=dt.date(1901, 1, 1),
            effective=effective,
            supersedes=None,
        )
    await load_mappings(graph, mappings, today=max(c.as_of for c in labels.cases))
    return GraphTrackA(graph)


# ---- the machinery, on invented labels -------------------------------------------


async def test_the_fixture_labels_pass_on_the_fake() -> None:
    labels = load_applicability_set(FIXTURE_LABELS)
    mappings = load_mapping_set([FIXTURE_MAPPINGS])
    assert await check(fake_for(mappings, labels), labels) == ()


async def test_a_wrong_label_is_reported_by_case_and_obligation() -> None:
    labels = load_applicability_set(FIXTURE_LABELS)
    first = labels.cases[0]
    wrong = first.model_copy(
        update={"expected": first.expected.model_copy(update={"otherwise": Outcome.UNASSESSABLE})}
    )
    mismatches = await check(
        fake_for(load_mapping_set([FIXTURE_MAPPINGS]), labels),
        labels.model_copy(update={"cases": (wrong,)}),
    )
    assert [str(m) for m in mismatches] == [
        "GA-01 OB-T-KEEP outcome: expected 'unassessable', got 'applicable'",
        "GA-01 OB-T-KEEP-AU outcome: expected 'unassessable', got 'applicable'",
    ]


@pytest.mark.parametrize(
    ("update", "what"),
    [
        ({"obligation_count": 4}, "obligation count"),
        ({"outcomes": {"not_applicable": ["OB-T-HEALTH", "OB-T-TYPO"]}}, "OB-T-TYPO outcome"),
        ({"acts_as": {"test-act/actor/keeper": "unknown"}}, "acts_as test-act/actor/keeper"),
        ({"cites": {"OB-T-KEEP": ["test-act:v1:s9/9"]}}, "OB-T-KEEP cites"),
    ],
)
async def test_every_kind_of_wrong_label_is_reported(update: dict[str, object], what: str) -> None:
    labels = load_applicability_set(FIXTURE_LABELS)
    first = labels.cases[0]
    expected = type(first.expected).model_validate(first.expected.model_dump(mode="json") | update)
    case = first.model_copy(update={"expected": expected})
    mismatches = await check(
        fake_for(load_mapping_set([FIXTURE_MAPPINGS]), labels),
        labels.model_copy(update={"cases": (case,)}),
    )
    assert [m.what for m in mismatches] == [what]


async def test_unsigned_labels_are_refused_unless_a_draft_is_asked_for() -> None:
    labels = load_applicability_set(FIXTURE_LABELS)
    draft = labels.model_copy(update={"reviewed_by": "PENDING — reviewer"})
    store = fake_for(load_mapping_set([FIXTURE_MAPPINGS]), labels)
    with pytest.raises(UnsignedLabels):
        await check(store, draft)
    assert await check(store, draft, allow_draft=True) == ()


def test_a_clause_of_a_compilation_the_labels_do_not_name_is_refused() -> None:
    labels = load_applicability_set(FIXTURE_LABELS)
    with pytest.raises(ValueError, match="test-act:v2"):
        labels.clause_validity({"test-act:v2:s1/1"})


async def test_a_question_the_tree_never_needs_is_a_mismatch() -> None:
    labels = load_applicability_set(FIXTURE_LABELS)
    second = labels.cases[1]
    extra = second.expected.model_copy(
        update={"questions": (*(second.expected.questions or ()), "COND-T-UNDER-THRESHOLD")}
    )
    case = second.model_copy(update={"expected": extra})
    mismatches = await check(
        fake_for(load_mapping_set([FIXTURE_MAPPINGS]), labels),
        labels.model_copy(update={"cases": (case,)}),
    )
    assert [m.what for m in mismatches] == ["questions"]


@needs_neo4j
async def test_the_fixture_labels_pass_on_neo4j(graph: Neo4jGraph) -> None:
    labels = load_applicability_set(FIXTURE_LABELS)
    store = await graph_for(graph, load_mapping_set([FIXTURE_MAPPINGS]), labels)
    assert await check(store, labels) == ()


# ---- the gate, on the corpus labels -----------------------------------------------

REQUIRED = bool(os.environ.get("REQUIRE_APPLICABILITY_GATE"))
"""Set in CI: a missing labels file fails the gate instead of skipping it, as
REQUIRE_RECALL_GATE does for recall, and unsigned labels are refused (D-01).
Without it — a local run over a model-drafted change — pending labels are
checked as a draft: that the draft agrees with the code, never that it's
right."""

corpus_labels = pytest.mark.skipif(
    not APPLICABILITY_PATH.exists() and not REQUIRED,
    reason="no corpus/golden/applicability.yaml yet",
)


@corpus_labels
async def test_the_corpus_labels_pass_on_the_fake() -> None:
    labels = load_applicability_set()
    mappings = load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml")))
    mismatches = await check(fake_for(mappings, labels), labels, allow_draft=not REQUIRED)
    assert mismatches == (), "\n".join(str(m) for m in mismatches)


@corpus_labels
@needs_neo4j
async def test_the_corpus_labels_pass_on_neo4j(graph: Neo4jGraph) -> None:
    labels = load_applicability_set()
    store = await graph_for(graph, load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
    mismatches = await check(store, labels, allow_draft=not REQUIRED)
    assert mismatches == (), "\n".join(str(m) for m in mismatches)
