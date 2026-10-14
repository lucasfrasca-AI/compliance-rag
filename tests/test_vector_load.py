"""Loading an ingested version into the retrieval store (#144): idempotent on
the chunk ids, never by writing over a stored chunk, on the fakes. The real
adapter's half (`version_chunk_ids`, batched upserts) is in
`tests/test_qdrant_retrieval_store.py` and the shared lifecycle contract."""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.postgres_ledger_store import LedgerUnreachable, PostgresLedgerStore
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Chunk, ClauseRecord, DocumentClass
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.corpus.vector_load import (
    DifferentClauseSet,
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
        self.upserted: list[str] = []

    async def upsert_chunks(self, chunks: tuple[Chunk, ...]) -> None:
        self.upserted.extend(c.chunk_id for c in chunks)
        await super().upsert_chunks(chunks)


async def _clause_store(clauses: tuple[ClauseRecord, ...] = LAW) -> FakeClauseStore:
    store = FakeClauseStore()
    for clause in clauses:
        await store.put_clause(clause)
    return store


def _lifecycle(store: CountingStore, ledger: FakeLedgerStore) -> CorpusLifecycle:
    return CorpusLifecycle(
        ledger=ledger,
        retrieval=store,
        clauses=FakeClauseStore(),
        generation_region=BUILD_PHASE_OFFSHORE_REGION,
        synthetic_registry=SyntheticRegistry(keys=frozenset(), source="none: public law only"),
    )


async def _load(
    store: CountingStore,
    ledger: FakeLedgerStore,
    clauses: tuple[ClauseRecord, ...] = LAW,
    document_class: DocumentClass = DocumentClass.LIBRARY_LAW,
) -> VectorLoad:
    return await load_version_vectors(
        clauses,
        count_tokens=_words,
        lifecycle=_lifecycle(store, ledger),
        retrieval=store,
        ledger=ledger,
        instrument_id=LAW_INSTRUMENT_ID,
        version_id=LAW_VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        effective_from=EFFECTIVE,
        document_class=document_class,
    )


async def _stored(store: FakeRetrievalStore) -> frozenset[str]:
    return await store.version_chunk_ids(
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
    assert load == VectorLoad(len(LAW), 0, written=len(LAW), recorded_in_ledger=True)
    assert await _stored(store) == {c.clause_id for c in LAW}
    assert await _validity(ledger) == (EFFECTIVE, None)


async def test_a_rerun_changes_nothing() -> None:
    ledger = FakeLedgerStore()
    store = CountingStore(ledger=ledger)
    await _load(store, ledger)
    again = await _load(store, ledger)
    assert again == VectorLoad(len(LAW), len(LAW), written=0, recorded_in_ledger=False)
    assert len(store.upserted) == len(LAW)


async def test_a_load_that_stopped_part_way_is_finished_without_rewriting_what_is_stored() -> None:
    ledger = FakeLedgerStore()
    store = CountingStore(ledger=ledger)
    first_four = tuple(
        Chunk(chunk_id=c.clause_id, clause_id=c.clause_id, text=c.text) for c in LAW[:4]
    )
    await store.upsert_chunks(first_four)
    store.upserted.clear()
    load = await _load(store, ledger)
    assert load == VectorLoad(len(LAW), 4, written=2, recorded_in_ledger=True)
    assert store.upserted == [c.clause_id for c in LAW[4:]]
    assert await _stored(store) == {c.clause_id for c in LAW}
    assert await _validity(ledger) == (EFFECTIVE, None)


async def test_a_stored_chunk_the_clause_store_lacks_is_refused_and_nothing_is_written() -> None:
    ledger = FakeLedgerStore()
    store = CountingStore(ledger=ledger)
    await _load(store, ledger, LAW[:4])
    with pytest.raises(DifferentClauseSet, match="holds 2 chunks the clause store doesn't"):
        await _load(store, ledger, LAW[2:])
    assert len(store.upserted) == 4


async def test_vectors_loaded_before_the_ledger_existed_are_recorded_in_it_only() -> None:
    """The deferred-ledger run, then a run against the real ledger (Phase 3's
    Cloud SQL, #174): the version is recorded, nothing is embedded."""
    store = CountingStore(ledger=FakeLedgerStore())
    await _load(store, FakeLedgerStore())  # a ledger discarded afterwards
    later = FakeLedgerStore()
    load = await _load(store, later)
    assert load == VectorLoad(len(LAW), len(LAW), written=0, recorded_in_ledger=True)
    assert len(store.upserted) == len(LAW)
    assert await _validity(later) == (EFFECTIVE, None)


async def test_the_ledger_only_record_keeps_the_lifecycle_class_check() -> None:
    """Recorded through `CorpusLifecycle`, not the ledger directly: library
    law can't be recorded under another class."""
    store = CountingStore(ledger=FakeLedgerStore())
    await _load(store, FakeLedgerStore())
    later = FakeLedgerStore()
    with pytest.raises(ValueError, match="internal_policy"):
        await _load(store, later, document_class=DocumentClass.INTERNAL_POLICY)
    assert await _validity(later) is None


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
        async def get_clause(
            self, clause_id: str, *, tenant_id: str, user_id: str | None = None
        ) -> ClauseRecord | None:
            if clause_id == LAW[2].clause_id:
                return None
            return await super().get_clause(clause_id, tenant_id=tenant_id, user_id=user_id)

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


async def test_an_unreachable_ledger_is_named_by_where_it_is_never_by_who() -> None:
    """The loader checks the ledger before writing anything elsewhere."""
    ledger = PostgresLedgerStore("postgresql://someone:hunter2@127.0.0.1:1/ledger")
    with pytest.raises(LedgerUnreachable) as refused:
        await ledger.ping()
    assert "127.0.0.1:1/ledger" in str(refused.value)
    assert "hunter2" not in str(refused.value) and "someone" not in str(refused.value)
    assert refused.value.__cause__ is None
