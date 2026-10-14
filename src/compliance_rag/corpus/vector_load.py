"""Load an ingested instrument version into the retrieval store (#144): its
clauses read back from the clause store, chunked with the real reranker
tokenizer, and published through `CorpusLifecycle` (ADR-0021), which embeds
and upserts them and records the version in the ledger.

Idempotent by what is already stored, never by writing over it:

- no chunk of the version stored: publish it (vectors, then the ledger record);
- every chunk stored and the ledger has the version: nothing to do;
- every chunk stored, the ledger hasn't: record the version in the ledger only.
  That is how a ledger that comes later (Cloud SQL, Phase 3) learns of a
  version whose vectors were loaded before it existed;
- some chunks stored but not all, or more than the clause store holds:
  refused. Writing over a stored version is a replace in place, which needs a
  person and `CorpusLifecycle`'s guardrails, not a re-run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.interfaces import ClauseStore, LedgerStore, RetrievalAdmin
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import Chunk, ClauseRecord, DocumentClass, stable_id

_CONCURRENT_READS = 16
"""Clause-store reads in flight at once: one object per clause, so a
779-clause version is 779 reads."""


class PartialVersion(RuntimeError):
    """The store holds some of the version's chunks, but not exactly the
    clause store's set. Nothing was written."""


@dataclass(frozen=True, slots=True)
class VectorLoad:
    """What a load found and did."""

    chunks: int
    stored_before: int
    published: bool
    """True if this run embedded and upserted the version."""
    recorded_in_ledger: bool
    """True if this run recorded the version in the ledger."""


async def read_version_clauses(
    store: ClauseStore, *, instrument_id: str, version_id: str, tenant_id: str
) -> tuple[ClauseRecord, ...]:
    """Every clause the version's manifest lists, in manifest order. A clause
    the manifest names but the store doesn't hold is an error, not a gap."""
    paths = tuple(await store.get_manifest(instrument_id, version_id, tenant_id=tenant_id))
    if not paths:
        raise LookupError(f"{instrument_id}:{version_id} has no manifest in the clause store")
    reads = asyncio.Semaphore(_CONCURRENT_READS)

    async def read(path: str) -> ClauseRecord:
        clause_id = stable_id(instrument_id, version_id, path)
        async with reads:
            clause = await store.get_clause(clause_id, tenant_id=tenant_id)
        if clause is None:
            raise LookupError(f"{clause_id} is in the manifest but not in the clause store")
        return clause

    return tuple(await asyncio.gather(*(read(path) for path in paths)))


async def load_version_vectors(
    clauses: tuple[ClauseRecord, ...],
    *,
    count_tokens: Callable[[str], int],
    lifecycle: CorpusLifecycle,
    retrieval: RetrievalAdmin,
    ledger: LedgerStore,
    instrument_id: str,
    version_id: str,
    tenant_id: str,
    effective_from: dt.date,
    document_class: DocumentClass,
) -> VectorLoad:
    """Bring the store and the ledger up to the clause store's version (module
    docstring). `retrieval` and `ledger` are the ones `lifecycle` writes to."""
    chunks: tuple[Chunk, ...] = chunk_clauses(clauses, count_tokens=count_tokens)
    stored = await retrieval.count_version_chunks(
        instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
    )
    if stored == 0:
        await lifecycle.publish_version(
            chunks,
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=version_id,
            effective_from=effective_from,
            supersedes=None,
            document_class=document_class,
        )
        return VectorLoad(len(chunks), 0, published=True, recorded_in_ledger=True)
    if stored != len(chunks):
        raise PartialVersion(
            f"{instrument_id}:{version_id}: the store holds {stored} chunks, the clause store "
            f"{len(chunks)}. A load that stopped part-way, or a different clause set: writing "
            "over a stored version is a replace in place, so nothing was changed"
        )
    recorded = await ledger.version_validity(
        tenant_id=tenant_id, instrument_id=instrument_id, version_id=version_id
    )
    if recorded is None:
        await ledger.record_version(
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            valid_from=effective_from,
            supersedes=None,
            document_class=document_class,
        )
        return VectorLoad(len(chunks), stored, published=False, recorded_in_ledger=True)
    if recorded[0] != effective_from:
        raise ValueError(
            f"{instrument_id}:{version_id}: the ledger has it in force from {recorded[0]}, "
            f"not {effective_from}"
        )
    return VectorLoad(len(chunks), stored, published=False, recorded_in_ledger=False)
