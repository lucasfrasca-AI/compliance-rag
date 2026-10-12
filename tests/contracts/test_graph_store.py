"""The `GraphStore` contract (Track A, issue #115): one definition, run on
`FakeGraphStore` (mapping YAML in memory) and on `GraphTrackA` over a real
Neo4j loaded with the same YAML — so the two can't silently diverge.

The fixture is the invented `test-act` miniature
(`tests/fixtures/mappings/test-applicability.yaml`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from compliance_rag.adapters.fakes import FakeGraphStore
from compliance_rag.adapters.interfaces import GraphStore
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.assessment.coverage import Outcome
from compliance_rag.assessment.evidence import Facts, Period
from compliance_rag.graph.loader import load_instrument_version, load_mappings
from compliance_rag.graph.mappings import load_mapping_set
from compliance_rag.graph.track_a import GraphTrackA
from tests.test_applicability import AS_OF, CLAUSES, FIXTURE, LAST_FY, SMALL_HISTORY, fact
from tests.test_graph_loader import SINCE, V1
from tests.test_neo4j_graph import graph as graph
from tests.test_neo4j_graph import needs_neo4j

BEFORE = dt.date(2025, 12, 1)  # before test-act's compilation and every mapping


async def assert_graph_store_contract(store: GraphStore) -> None:
    assert isinstance(store, GraphStore)
    over = Facts.of([fact("annual_turnover_aud", 4000, LAST_FY)])
    under = Facts.of([fact("annual_turnover_aud", 900, LAST_FY)])
    small = Facts.of(
        [
            fact("annual_turnover_aud", 900, LAST_FY),
            *SMALL_HISTORY,
            fact("provides_health_service", False),
            fact("opted_in", False),
        ]
    )

    # D-06: every obligation, one of three outcomes, untruncated.
    covered = await store.coverage(over, tenant_id="acme", as_of=AS_OF)
    assert {o.obligation_id for o in covered.obligations} == {
        "OB-T-KEEP",
        "OB-T-KEEP-AU",
        "OB-T-HEALTH",
    }
    assert covered.obligation("OB-T-KEEP").outcome is Outcome.APPLICABLE
    assert covered.obligation("OB-T-KEEP").citations == ("test-act:v1:s1/1", "test-act:v1:s2/1")
    assert covered.actor("test-act/actor/keeper").truth.value == "true"

    possibly = await store.coverage(under, tenant_id="acme", as_of=AS_OF)
    assert possibly.obligation("OB-T-KEEP").outcome is Outcome.UNASSESSABLE
    assert {q.condition_id for q in possibly.questions} >= {
        "COND-T-EVER-OVER",
        "COND-T-HEALTH",
        "COND-T-OPTED-IN",
    }

    exempt = await store.coverage(small, tenant_id="acme", as_of=AS_OF)
    assert exempt.obligation("OB-T-KEEP").outcome is Outcome.NOT_APPLICABLE

    # The same question on two dates (PHASES.md Phase 2 exit): before the
    # compilation and the mappings, nothing is in force to apply.
    earlier = await store.coverage(over, tenant_id="acme", as_of=BEFORE)
    assert earlier.obligations == ()

    # Crosswalks are capped (D-05) — the fixture has none.
    assert await store.crosswalks("OB-T-KEEP", as_of=AS_OF, max_depth=2) == ()


async def test_the_fake_keeps_the_contract() -> None:
    await assert_graph_store_contract(
        FakeGraphStore(load_mapping_set([FIXTURE]), clause_validity=CLAUSES)
    )


@pytest.fixture
async def loaded(graph: Neo4jGraph) -> AsyncIterator[Neo4jGraph]:
    await graph.apply_schema()
    await load_instrument_version(
        graph, V1, instrument_since=SINCE, effective=dt.date(2026, 1, 1), supersedes=None
    )
    await load_mappings(graph, load_mapping_set([FIXTURE]), today=AS_OF)
    yield graph


@needs_neo4j
async def test_track_a_over_neo4j_keeps_the_contract(loaded: Neo4jGraph) -> None:
    await assert_graph_store_contract(GraphTrackA(loaded))


@needs_neo4j
async def test_neo4j_and_the_fake_agree_exactly(loaded: Neo4jGraph) -> None:
    """Not just the contract's assertions: the whole coverage, explanation
    trees and questions included, is identical from both sources."""
    fake = FakeGraphStore(load_mapping_set([FIXTURE]), clause_validity=CLAUSES)
    real = GraphTrackA(loaded)
    for evidence in (
        [fact("annual_turnover_aud", 900, LAST_FY)],
        [fact("annual_turnover_aud", 4000, Period.financial_year(2024)), *SMALL_HISTORY],
        [fact("operates_in", ("AU-VIC",)), fact("annual_turnover_aud", 4000, LAST_FY)],
    ):
        facts = Facts.of(evidence)
        assert await real.coverage(facts, tenant_id="acme", as_of=AS_OF) == await fake.coverage(
            facts, tenant_id="acme", as_of=AS_OF
        )


@needs_neo4j
async def test_neo4j_and_the_fake_agree_across_amendment_and_repeal(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    """Where the two read differently — operands and lineage from node
    properties versus YAML — on dates either side of a supersession and a
    repeal."""
    import yaml

    document = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    conditions = {c["id"]: c for c in document["conditions"]}
    conditions["COND-T-OPTED-IN"] |= {"valid_to": "2026-07-01", "end_reason": "repealed"}
    old = conditions["COND-T-UNDER-THRESHOLD"]
    old |= {"valid_to": "2026-07-01", "end_reason": "superseded"}
    document["conditions"].append(
        dict(
            old,
            id="COND-T-UNDER-2",
            valid_from="2026-07-01",
            valid_to=None,
            end_reason=None,
            supersedes="COND-T-UNDER-THRESHOLD",
            leaf=old["leaf"] | {"value": 5000},
        )
    )
    path = tmp_path / "amended.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    mappings = load_mapping_set([path])
    await graph.apply_schema()
    await load_instrument_version(
        graph, V1, instrument_since=SINCE, effective=dt.date(2026, 1, 1), supersedes=None
    )
    await load_mappings(graph, mappings, today=AS_OF)
    fake, real = FakeGraphStore(mappings, clause_validity=CLAUSES), GraphTrackA(graph)
    facts = Facts.of([fact("annual_turnover_aud", 4000, LAST_FY), fact("opted_in", True)])
    for as_of in (dt.date(2026, 6, 1), AS_OF):
        assert await real.coverage(facts, tenant_id="acme", as_of=as_of) == await fake.coverage(
            facts, tenant_id="acme", as_of=as_of
        )
