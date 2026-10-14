"""Loading an ingested version into the retrieval store (#144): idempotent by
what is already stored, never by writing over it, on the fakes. The real
adapter's half (`count_version_chunks`, batched upserts) is in
`tests/test_qdrant_retrieval_store.py`."""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Chunk, ClauseRecord, DocumentClass
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.corpus.vector_load import (
    PartialVersion,
    VectorLoad,
    load_version_vectors,
    read_version_clauses,
)
from compliance_rag.eval.golden_corpus import LAW_INSTRUMENT_ID, LAW_VERSION_ID, golden_clauses
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION

EFFECTIVE = dt.date(2026, 6, 4)
LAW = tuple(c for c in golden_clauses() if c.tenant_id == PUBLIC_TENANT_ID)[:6]


def _words(text: str) -> int:
    return len(text.split())


class CountingStore(FakeRetrievalStore):
    def __init__(self, *, ledger: FakeLedgerStore) -> None:
        super().__init__(ledger=ledger)
        self.upserts = 0

    async def upsert_chunks(self, chunks: tuple[Chunk, ...]) -> None:
        self.upserts += 1
        await super().upsert_chunks(chunks)


async def _clause_store(clauses: tuple[ClauseRecord, ...] = LAW) -> FakeClauseStore:
    store = FakeClauseStore()
    for clause in clauses:
        await store.put_clause(clause)
    return store


async def _load(
    store: CountingStore, ledger: FakeLedgerStore, clauses: tuple[ClauseRecord, ...] = LAW
) -> VectorLoad:
    lifecycle = CorpusLifecycle(
        ledger=ledger,
        retrieval=store,
        clauses=await _clause_store(clauses),
        generation_region=BUILD_PHASE_OFFSHORE_REGION,
        synthetic_registry=SyntheticRegistry(keys=frozenset(), source="none: public law only"),
    )
    return await load_version_vectors(
        clauses,
        count_tokens=_words,
        lifecycle=lifecycle,
        retrieval=store,
        ledger=ledger,
        instrument_id=LAW_INSTRUMENT_ID,
        version_id=LAW_VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        effective_from=EFFECTIVE,
        document_class=DocumentClass.LIBRARY_LAW,
    )


async def _stored(store: FakeRetrievalStore) -> int:
    return await store.count_version_chunks(
        instrument_id=LAW_INSTRUMENT_ID, tenant_id=PUBLIC_TENANT_ID, version_id=LAW_VERSION_ID
    )


async def _validity(ledger: FakeLedgerStore) -> tuple[dt.date, dt.date | None] | None:
    return await ledger.version_validity(
        tenant_id=PUBLIC_TENANT_ID, instrument_id=LAW_INSTRUMENT_ID, version_id=LAW_VERSION_ID
    )


async def test_a_first_load_publishes_every_chunk_and_records_the_version() -> None:
    ledger = FakeLedgerStore()
    store = CountingStore(ledger=ledger)
    load = await _load(store, ledger)
    assert load == VectorLoad(len(LAW), 0, published=True, recorded_in_ledger=True)
    assert await _stored(store) == len(LAW)
    assert await _validity(ledger) == (EFFECTIVE, None)


async def test_a_rerun_changes_nothing() -> None:
    ledger = FakeLedgerStore()
    store = CountingStore(ledger=ledger)
    await _load(store, ledger)
    again = await _load(store, ledger)
    assert again == VectorLoad(len(LAW), len(LAW), published=False, recorded_in_ledger=False)
    assert store.upserts == 1
    assert await _stored(store) == len(LAW)


async def test_vectors_loaded_before_the_ledger_existed_are_recorded_in_it_only() -> None:
    """The deferred-ledger run, then a run against the real ledger (Phase 3's
    Cloud SQL): the version is recorded, nothing is embedded or upserted."""
    store = CountingStore(ledger=FakeLedgerStore())
    await _load(store, FakeLedgerStore())  # a ledger discarded afterwards
    later = FakeLedgerStore()
    load = await _load(store, later)
    assert load == VectorLoad(len(LAW), len(LAW), published=False, recorded_in_ledger=True)
    assert store.upserts == 1
    assert await _validity(later) == (EFFECTIVE, None)


async def test_a_partly_stored_version_is_refused_and_left_alone() -> None:
    ledger = FakeLedgerStore()
    store = CountingStore(ledger=ledger)
    await _load(store, ledger, LAW[:4])  # a load that stopped part-way
    with pytest.raises(PartialVersion, match="holds 4 chunks, the clause store 6"):
        await _load(store, ledger)
    assert store.upserts == 1
    assert await _stored(store) == 4


async def test_a_ledger_that_disagrees_on_the_start_date_is_refused() -> None:
    ledger = FakeLedgerStore()
    store = CountingStore(ledger=ledger)
    await _load(store, FakeLedgerStore())
    await ledger.record_version(
        tenant_id=PUBLIC_TENANT_ID,
        instrument_id=LAW_INSTRUMENT_ID,
        version_id=LAW_VERSION_ID,
        valid_from=dt.date(2026, 1, 1),
        supersedes=None,
        document_class=DocumentClass.LIBRARY_LAW,
    )
    with pytest.raises(ValueError, match="in force from 2026-01-01, not 2026-06-04"):
        await _load(store, ledger)


async def test_the_clause_store_is_read_back_in_full() -> None:
    clauses = await read_version_clauses(
        await _clause_store(),
        instrument_id=LAW_INSTRUMENT_ID,
        version_id=LAW_VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
    )
    assert sorted(c.clause_id for c in clauses) == sorted(c.clause_id for c in LAW)


async def test_a_clause_the_manifest_names_but_the_store_lacks_is_an_error() -> None:
    class LosesOne(FakeClauseStore):
        async def get_clause(self, clause_id: str, *, tenant_id: str) -> ClauseRecord | None:
            return (
                None
                if clause_id == LAW[2].clause_id
                else await super().get_clause(clause_id, tenant_id=tenant_id)
            )

    store = LosesOne()
    for clause in LAW:
        await store.put_clause(clause)
    with pytest.raises(LookupError, match="in the manifest but not in the clause store"):
        await read_version_clauses(
            store,
            instrument_id=LAW_INSTRUMENT_ID,
            version_id=LAW_VERSION_ID,
            tenant_id=PUBLIC_TENANT_ID,
        )


async def test_a_version_the_clause_store_has_never_held_is_an_error() -> None:
    with pytest.raises(LookupError, match="no manifest"):
        await read_version_clauses(
            FakeClauseStore(),
            instrument_id=LAW_INSTRUMENT_ID,
            version_id=LAW_VERSION_ID,
            tenant_id=PUBLIC_TENANT_ID,
        )
