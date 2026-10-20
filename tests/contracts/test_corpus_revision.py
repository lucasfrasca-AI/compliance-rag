"""A corpus revision's store-level writes, as shared contracts (ADR-0042).

`assert_clause_store_revision_contract` runs here against `FakeClauseStore`
and `GCSClauseStore` over the versioned in-memory bucket;
`assert_revise_chunks_contract` against `FakeRetrievalStore` here and against
the real adapter on a real Qdrant in `tests/test_qdrant_retrieval_store.py`.
One definition each, so the stores can't drift. The fixture is
`tests/test_corpus_revision.py`'s (the deployed text of the seven clauses, and
the corrected parse). The graph's half is on a real Neo4j only
(`tests/test_corpus_revision_stores.py`): `Neo4jGraph` is the one `GraphAdmin`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.interfaces import ClauseStore, RetrievalAdmin
from compliance_rag.adapters.revision_authorization import (
    RevisionAuthorization,
    _issue,  # pyright: ignore[reportPrivateUsage] — the stores' half of the contract
)
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    ClauseRecord,
    Incompleteness,
    Scope,
    canonical_sha256,
)
from compliance_rag.corpus.revision import RevisedClause, RevisionDrift
from tests.test_corpus_revision import (
    INSTRUMENT,
    VERSION,
    authorization,
    corrected_clauses,
    deploy_bucket,
    deployed_clauses,
    revised_clauses,
    words,
)
from tests.test_gcs_clause_store import _FakeBucket  # pyright: ignore[reportPrivateUsage]
from tests.test_incomplete_clauses import (
    S6DA,
    _parsed_constructs,  # pyright: ignore[reportPrivateUsage] — the shared fixture
    _state_record,  # pyright: ignore[reportPrivateUsage]
)


async def assert_clause_store_revision_contract(store: ClauseStore) -> None:
    """Every `ClauseStore`: the record and the new hashes on the manifest, the
    new text in the objects, idempotent, and a stale or foreign write refused."""
    await deploy_bucket(store)
    granted = authorization()
    with pytest.raises(PermissionError, match="its hash differs"):
        await store.record_revision(granted, deployed_clauses()[:1] + revised_clauses()[1:])
    assert await store.revisions(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID) == ()
    with pytest.raises(RevisionDrift, match="no text was given"):
        await store.record_revision(granted, revised_clauses()[1:])

    await store.record_revision(granted, revised_clauses())
    manifest = await store.get_manifest(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID)
    assert manifest == {
        c.clause_path: canonical_sha256(c.canonical_text) for c in corrected_clauses()
    }
    for clause in revised_clauses():
        assert await store.get_clause(clause.clause_id, tenant_id=PUBLIC_TENANT_ID) == clause
    assert await store.revisions(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID) == (
        granted.record,
    )

    # Again, the same revision: nothing more is recorded.
    await store.record_revision(granted, revised_clauses())
    assert await store.revisions(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID) == (
        granted.record,
    )
    # Another correction of what is already corrected changes nothing: refused,
    # so the manifest never records a revision that didn't happen.
    with pytest.raises(RevisionDrift, match="changes nothing stored"):
        await store.record_revision(authorization("r2"), revised_clauses())
    # One whose previous hashes aren't stored: drift.
    other = _issue(
        replace(
            granted.record,
            revision_id="r3",
            clauses=(RevisedClause("s6/1", "0" * 64, "1" * 64),),
        )
    )
    with pytest.raises(RevisionDrift, match="neither the previous"):
        await store.record_revision(other, ())


async def test_the_gcs_clause_store_satisfies_the_revision_contract() -> None:
    await assert_clause_store_revision_contract(GCSClauseStore(_FakeBucket()))


async def test_the_fake_clause_store_satisfies_the_revision_contract() -> None:
    await assert_clause_store_revision_contract(FakeClauseStore())


async def assert_revise_chunks_contract(store: RetrievalAdmin) -> None:
    """Every `RetrievalAdmin`: the text (and vectors) of a stored chunk the
    revision names change, and nothing else; a chunk not stored as this
    version's library law, or text the revision doesn't record, is refused
    before anything is written."""
    granted = authorization()
    fixed = chunk_clauses(revised_clauses(), count_tokens=words)

    async def texts() -> dict[str, str]:
        return await store.stored_chunk_texts(
            instrument_id=INSTRUMENT, tenant_id=PUBLIC_TENANT_ID, version_id=VERSION
        )

    # Nothing stored: a correction never adds a chunk.
    with pytest.raises(PermissionError, match="never adds one"):
        await store.revise_chunks(granted, fixed)
    assert await texts() == {}
    # A tenant's chunk under the same id is not this version's library law.
    await store.upsert_chunks(
        (fixed[0].model_copy(update={"tenant_id": "acme", "scope": Scope.TENANT}),)
    )
    with pytest.raises(PermissionError, match="never adds one"):
        await store.revise_chunks(granted, fixed[:1])

    deployed = chunk_clauses(deployed_clauses(), count_tokens=words)
    effective = dt.date(2026, 6, 4)
    await store.upsert_chunks(
        tuple(c.model_copy(update={"valid_from": effective}) for c in deployed)
    )
    before = await texts()
    assert before == {c.chunk_id: c.text for c in deployed}
    old = {c.chunk_id: c for c in deployed}
    with pytest.raises(PermissionError, match="its hash differs"):
        await store.revise_chunks(granted, (*fixed[:-1], old[fixed[-1].chunk_id]))
    assert await texts() == before  # refused before any write

    await store.revise_chunks(granted, fixed)
    assert await texts() == {**before, **{c.chunk_id: c.text for c in fixed}}
    assert await store.version_chunk_ids(
        instrument_id=INSTRUMENT, tenant_id=PUBLIC_TENANT_ID, version_id=VERSION
    ) == frozenset(before)


async def test_the_fake_retrieval_store_satisfies_the_revise_chunks_contract() -> None:
    store = FakeRetrievalStore(ledger=FakeLedgerStore())
    await assert_revise_chunks_contract(store)
    # Nothing but the text changed: the valid time is the stored chunk's.
    revised = store._chunks[f"{INSTRUMENT}:{VERSION}:s6/1"]  # pyright: ignore[reportPrivateUsage]
    assert revised.valid_from == dt.date(2026, 6, 4)


# ---- an incomplete clause (#211, ADR-0047): the state is part of the form ----


def _state_change() -> tuple[ClauseRecord, ClauseRecord, RevisionAuthorization]:
    """s6da/2 as deployed (complete) and as parsed (incomplete), and the
    authorised revision between them: the same text, a new state."""
    marked = next(c for c in S6DA.select(_parsed_constructs()) if c.clause_path == "s6da/2")
    return marked.model_copy(update={"incomplete": ()}), marked, _issue(_state_record())


async def assert_clause_store_state_contract(store: ClauseStore) -> None:
    """Every `ClauseStore`: a clause's incompleteness is listed on the manifest
    with its hash, and a revision moves it, writing the object, the record and
    the new state, refusing a clause given in a form it doesn't record."""
    unmarked, marked, granted = _state_change()
    await store.put_clauses((unmarked,))
    await store.put_manifest(
        INSTRUMENT,
        VERSION,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=2,
        source_html="<p/>",
    )
    assert await store.get_incomplete(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID) == {}
    with pytest.raises(PermissionError, match="records it incomplete"):
        await store.record_revision(granted, (unmarked,))
    await store.record_revision(granted, (marked,))
    assert await store.get_incomplete(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID) == {
        "s6da/2": marked.incomplete
    }
    assert await store.get_clause(marked.clause_id, tenant_id=PUBLIC_TENANT_ID) == marked
    assert await store.revisions(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID) == (
        granted.record,
    )
    # Again: nothing more is recorded.
    await store.record_revision(granted, (marked,))
    assert len(await store.revisions(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID)) == 1


async def test_the_gcs_clause_store_satisfies_the_state_contract() -> None:
    await assert_clause_store_state_contract(GCSClauseStore(_FakeBucket()))


async def test_the_fake_clause_store_satisfies_the_state_contract() -> None:
    await assert_clause_store_state_contract(FakeClauseStore())


async def assert_chunk_state_contract(store: RetrievalAdmin) -> None:
    """Every `RetrievalAdmin`: each stored chunk's incompleteness reads back,
    `()` when complete, and a revision of state alone changes it and leaves
    the text as it was; a chunk given in a form the revision doesn't record
    is refused."""
    unmarked, marked, granted = _state_change()
    [stored] = chunk_clauses((unmarked,), count_tokens=words)
    await store.upsert_chunks((stored,))

    async def held() -> dict[str, tuple[Incompleteness, ...]]:
        return await store.stored_chunk_incomplete(
            instrument_id=INSTRUMENT, tenant_id=PUBLIC_TENANT_ID, version_id=VERSION
        )

    assert await held() == {stored.chunk_id: ()}
    [revised] = chunk_clauses((marked,), count_tokens=words)
    with pytest.raises(PermissionError, match="records it incomplete"):
        await store.revise_chunks(granted, (stored,))
    await store.revise_chunks(granted, (revised,))
    assert await held() == {stored.chunk_id: marked.incomplete}
    texts = await store.stored_chunk_texts(
        instrument_id=INSTRUMENT, tenant_id=PUBLIC_TENANT_ID, version_id=VERSION
    )
    assert texts == {stored.chunk_id: stored.text}


async def test_the_fake_retrieval_store_satisfies_the_chunk_state_contract() -> None:
    await assert_chunk_state_contract(FakeRetrievalStore(ledger=FakeLedgerStore()))
