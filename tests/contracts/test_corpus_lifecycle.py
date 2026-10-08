"""Version-and-retire and the removal states, as one shared contract (ADR-0021).

`assert_corpus_lifecycle_contract` runs here against the in-memory fakes and in
`tests/test_qdrant_retrieval_store.py` against the real adapter on a real
Qdrant server — one definition, so the two can't drift.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable

import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle, RemovalRefused
from compliance_rag.adapters.fakes import (
    FakeCitationVerifier,
    FakeClauseStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.interfaces import (
    Candidate,
    Channel,
    ClauseStore,
    RemovalAction,
    RemovalOutcome,
    RetrievalAdmin,
    RetrievalStore,
)
from compliance_rag.adapters.removal_authorization import RemovalAuthorization
from compliance_rag.corpus.chunker import chunk_clause
from compliance_rag.corpus.models import CANONICALISATION_VERSION, Chunk, ClauseRecord, Scope
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace

NOW = dt.date(2026, 10, 8)
BETWEEN = dt.date(2026, 1, 1)
V1_FROM = dt.date(2025, 1, 1)
V2_FROM = dt.date(2026, 6, 1)

POLICY = "acme/retention-policy"
V1 = f"{POLICY}:v1:3/1"
V2 = f"{POLICY}:v2:3/1"
HANDBOOK = "acme/staff-handbook"
LAW = "privacy-act-1988"
LAW_CLAUSE = f"{LAW}:C2026C00227:sch1/app11/2"
BETA_DOC = "beta/retention-policy:v1:3/1"
QUERY = "how long are customer account records retained after closure"


def _clause(clause_id: str, text: str, *, tenant_id: str) -> ClauseRecord:
    instrument_id, version_id, clause_path = clause_id.split(":")
    public = tenant_id == "_public"
    return ClauseRecord.build(
        instrument_id=instrument_id,
        version_id=version_id,
        clause_path=clause_path,
        text=text,
        scope=Scope.PUBLIC if public else Scope.TENANT,
        tenant_id=tenant_id,
    )


V1_TEXT = "Customer account records are retained for seven years after closure."
V2_TEXT = "Customer account records are retained for five years after closure."


def _chunk(record: ClauseRecord) -> Chunk:
    return chunk_clause(record, count_tokens=lambda text: len(text.split()))


def _routes(trace: RetrievalTrace) -> dict[str, frozenset[str]]:
    def ids(candidates: tuple[Candidate, ...]) -> frozenset[str]:
        return frozenset(c.clause_id for c in candidates)

    return {
        "dense": ids(trace.per_channel.get(Channel.DENSE, ())),
        "sparse": ids(trace.per_channel.get(Channel.SPARSE, ())),
        "fused": ids(trace.fused),
        "reranked": ids(trace.ranked_candidates()),
    }


async def assert_corpus_lifecycle_contract(
    store: RetrievalStore,
    admin: RetrievalAdmin,
    ledger: FakeLedgerStore,
    clauses: ClauseStore,
    stored_chunk_ids: Callable[[], Awaitable[frozenset[str]]],
) -> None:
    lifecycle = CorpusLifecycle(ledger=ledger, retrieval=admin, clauses=clauses)
    pipeline = RetrievalPipeline(store, FakeReranker())
    verifier = FakeCitationVerifier()

    v1 = _clause(V1, V1_TEXT, tenant_id="acme")
    v2 = _clause(V2, V2_TEXT, tenant_id="acme")
    law = _clause(
        LAW_CLAUSE, "destroy the information or ensure it is de-identified", tenant_id="_public"
    )
    beta = _clause(BETA_DOC, "Beta keeps customer account records for ten years.", tenant_id="beta")
    for record in (v1, v2, law, beta):
        await clauses.put_clause(record)
    await admin.upsert_chunks((_chunk(law), _chunk(beta)))

    # --- Version and retire: v2 supersedes v1; both stay stored. ----------
    await lifecycle.publish_version(
        (_chunk(v1),),
        instrument_id=POLICY,
        tenant_id="acme",
        version_id="v1",
        effective_from=V1_FROM,
        supersedes=None,
    )
    await lifecycle.publish_version(
        (_chunk(v2),),
        instrument_id=POLICY,
        tenant_id="acme",
        version_id="v2",
        effective_from=V2_FROM,
        supersedes="v1",
    )
    assert {V1, V2} <= await stored_chunk_ids()

    # Mechanism 2: at today's as_of, v1 is in no channel, not fused, not
    # reranked — and v2 is in every one of them (the positive control).
    today = _routes(await pipeline.retrieve(QUERY, "acme", NOW))
    assert all(V2 in found and V1 not in found for found in today.values()), today
    assert await store.lookup_clause(V1, tenant_id="acme", as_of=NOW) is None
    # Before v2 took effect, v1 is the one returned: retirement is a date,
    # not a deletion.
    then = _routes(await pipeline.retrieve(QUERY, "acme", BETWEEN))
    assert all(V1 in found and V2 not in found for found in then.values()), then

    # Mechanism 3: a citation to the retired version names a clause outside
    # the retrieved set, so it fails — even though v1's text is still stored.
    retrieved = await pipeline.retrieve(QUERY, "acme", NOW)
    records: list[ClauseRecord] = []
    for candidate in retrieved.ranked_candidates():
        record = await clauses.get_clause(candidate.clause_id, tenant_id="acme")
        if record is not None:
            records.append(record)
    assert not await verifier.verify(
        V1, V1_TEXT, clauses=tuple(records), canonicalisation_version=CANONICALISATION_VERSION
    )
    assert await verifier.verify(
        V2, V2_TEXT, clauses=tuple(records), canonicalisation_version=CANONICALISATION_VERSION
    )

    # --- Library law and other tenants' documents are refused, audited. ---
    refusals = [
        lifecycle.purge(
            tenant_id="acme",
            instrument_id=LAW,
            version_id=None,
            actor="u1",
            reason="not relevant to us",
            acknowledged_findings_citing=0,
        ),
        lifecycle.withdraw(
            tenant_id="acme",
            instrument_id=LAW,
            version_id="C2026C00227",
            actor="u1",
            reason="not relevant to us",
        ),
        lifecycle.purge(
            tenant_id="acme",
            instrument_id="beta/retention-policy",
            version_id=None,
            actor="u1",
            reason="clean up",
            acknowledged_findings_citing=0,
        ),
        lifecycle.replace_in_place(
            (_chunk(law),),
            (law,),
            tenant_id="_public",
            instrument_id=LAW,
            version_id="C2026C00227",
            source_html="<p/>",
            actor="admin",
            reason="typo",
            acknowledged_findings_citing=0,
        ),
    ]
    for attempt in refusals:
        with pytest.raises(RemovalRefused):
            await attempt
    assert {LAW_CLAUSE, BETA_DOC} <= await stored_chunk_ids()
    assert await store.lookup_clause(LAW_CLAUSE, tenant_id="_public", as_of=NOW) is not None
    acme_refused = [
        e for e in await ledger.removal_audit("acme") if e.outcome is RemovalOutcome.REFUSED
    ]
    assert len(acme_refused) == 3
    assert any(e.outcome is RemovalOutcome.REFUSED for e in await ledger.removal_audit("_public"))
    with pytest.raises(PermissionError):
        RemovalAuthorization(
            tenant_id="acme", instrument_id=POLICY, version_id=None, audit_id="x", _issuer=object()
        )

    # --- Withdraw: out of retrieval at every as_of, text and vectors kept. -
    await lifecycle.withdraw(
        tenant_id="acme",
        instrument_id=POLICY,
        version_id="v2",
        actor="u1",
        reason="superseded by board resolution",
    )
    for as_of in (NOW, BETWEEN):
        found = _routes(await pipeline.retrieve(QUERY, "acme", as_of))
        assert all(V2 not in ids for ids in found.values()), found
    assert await store.lookup_clause(V2, tenant_id="acme", as_of=NOW) is None
    assert V2 in await stored_chunk_ids()
    still_stored = await clauses.get_clause(V2, tenant_id="acme")
    assert still_stored is not None
    assert await verifier.verify(
        V2, V2_TEXT, clauses=(still_stored,), canonicalisation_version=CANONICALISATION_VERSION
    )
    assert ledger.withdrawn[("acme", POLICY, "v2")] == "u1"
    with pytest.raises(RemovalRefused, match="withdrawn"):
        await lifecycle.replace_in_place(
            (_chunk(v2),),
            (v2,),
            tenant_id="acme",
            instrument_id=POLICY,
            version_id="v2",
            source_html="<p/>",
            actor="u1",
            reason="typo",
            acknowledged_findings_citing=0,
        )

    # --- Purge: the findings count is shown first and must be acknowledged.
    ledger.seed_finding("acme", V1)
    preview = await lifecycle.preview_removal(
        tenant_id="acme", instrument_id=POLICY, version_id="v1"
    )
    assert preview.findings_citing == 1
    whole = await lifecycle.preview_removal(tenant_id="acme", instrument_id=POLICY, version_id=None)
    assert whole.findings_citing == 1
    with pytest.raises(RemovalRefused, match="1 finding"):
        await lifecycle.purge(
            tenant_id="acme",
            instrument_id=POLICY,
            version_id="v1",
            actor="u1",
            reason="legal hold lifted",
            acknowledged_findings_citing=0,
        )
    assert V1 in await stored_chunk_ids()

    await lifecycle.purge(
        tenant_id="acme",
        instrument_id=POLICY,
        version_id="v1",
        actor="u1",
        reason="legal hold lifted",
        acknowledged_findings_citing=1,
    )
    stored = await stored_chunk_ids()
    assert V1 not in stored
    assert {V2, LAW_CLAUSE, BETA_DOC} <= stored  # other version, library, other tenant untouched
    assert await clauses.get_clause(V1, tenant_id="acme") is None
    assert await clauses.get_clause(V2, tenant_id="acme") is not None
    gone = _routes(await pipeline.retrieve(QUERY, "acme", BETWEEN))
    assert all(V1 not in ids for ids in gone.values()), gone
    assert ledger.purged[("acme", POLICY, "v1")] == ("u1", "legal hold lifted")
    purge_trail = [
        e.outcome
        for e in await ledger.removal_audit("acme")
        if e.action is RemovalAction.PURGE and e.instrument_id == POLICY
    ]
    assert purge_trail == [
        RemovalOutcome.REFUSED,
        RemovalOutcome.AUTHORIZED,
        RemovalOutcome.COMPLETED,
    ]

    # --- Replace in place: a typo fix to a live, uncited version. ---------
    typo = _clause(
        f"{HANDBOOK}:v1:1/1", "Staff must lock screens when away from ther desk.", tenant_id="acme"
    )
    fixed = _clause(
        f"{HANDBOOK}:v1:1/1", "Staff must lock screens when away from their desk.", tenant_id="acme"
    )
    await clauses.put_clause(typo)
    await lifecycle.publish_version(
        (_chunk(typo),),
        instrument_id=HANDBOOK,
        tenant_id="acme",
        version_id="v1",
        effective_from=V1_FROM,
        supersedes=None,
    )
    await lifecycle.replace_in_place(
        (_chunk(fixed),),
        (fixed,),
        tenant_id="acme",
        instrument_id=HANDBOOK,
        version_id="v1",
        source_html="<p>handbook</p>",
        actor="u1",
        reason="typo: ther -> their",
        acknowledged_findings_citing=0,
    )
    stored_text = await clauses.get_clause(fixed.clause_id, tenant_id="acme")
    assert stored_text is not None
    assert stored_text.text == fixed.text
    hit = await store.lookup_clause(fixed.clause_id, tenant_id="acme", as_of=NOW)
    assert hit is not None
    assert "their desk" in hit.text


async def test_fakes_satisfy_the_corpus_lifecycle_contract() -> None:
    ledger = FakeLedgerStore()
    store = FakeRetrievalStore(ledger=ledger)

    async def stored() -> frozenset[str]:
        return store.stored_chunk_ids

    await assert_corpus_lifecycle_contract(store, store, ledger, FakeClauseStore(), stored)


async def test_a_removal_that_fails_midway_is_audited_as_failed() -> None:
    ledger = FakeLedgerStore()

    class BrokenStore(FakeRetrievalStore):
        async def delete_chunks(self, authorization: RemovalAuthorization) -> None:
            raise RuntimeError("qdrant unavailable")

    lifecycle = CorpusLifecycle(
        ledger=ledger, retrieval=BrokenStore(ledger=ledger), clauses=FakeClauseStore()
    )
    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        await lifecycle.purge(
            tenant_id="acme",
            instrument_id=POLICY,
            version_id=None,
            actor="u1",
            reason="cleanup",
            acknowledged_findings_citing=0,
        )
    trail = [e.outcome for e in await ledger.removal_audit("acme")]
    assert trail == [RemovalOutcome.AUTHORIZED, RemovalOutcome.FAILED]
    assert ("acme", POLICY, None) not in ledger.purged


async def test_a_version_cannot_supersede_itself() -> None:
    ledger = FakeLedgerStore()
    lifecycle = CorpusLifecycle(
        ledger=ledger, retrieval=FakeRetrievalStore(ledger=ledger), clauses=FakeClauseStore()
    )
    with pytest.raises(ValueError, match="replace-in-place"):
        await lifecycle.publish_version(
            (),
            instrument_id=POLICY,
            tenant_id="acme",
            version_id="v1",
            effective_from=V1_FROM,
            supersedes="v1",
        )
