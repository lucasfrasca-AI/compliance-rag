"""Load an ingested instrument version into the retrieval store (#144): its
clauses and source read back from the clause store, and published through
`CorpusLifecycle` (ADR-0021), which checks the clause store holds exactly them
(#233: ingest wrote them, so it writes nothing there), chunks them with the
real reranker tokenizer, embeds and upserts them and records the version in
the ledger.

Idempotent on the deterministic chunk ids, never by writing over a stored
chunk:

- none of the version stored: publish it (vectors, then the ledger record);
- some of it stored, all of them the clause store's: a load that stopped
  part-way, so the missing chunks are upserted (`complete_version`);
- every chunk stored and the ledger has the version: nothing to do;
- every chunk stored, the ledger hasn't: record the version in the ledger
  only. That is how a ledger that comes later (Cloud SQL, Phase 3, #174)
  learns of a version whose vectors were loaded before it existed;
- a stored chunk the clause store doesn't have: refused. It is a different
  clause set, and writing over a stored version is a replace in place, which
  needs a person and `CorpusLifecycle`'s guardrails, not a re-run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from compliance_rag import telemetry
from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.interfaces import ClauseStore, LedgerStore, RetrievalAdmin
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import ClauseRecord, DocumentClass, stable_id

_CONCURRENT_READS = 16
"""Clause-store reads in flight at once: one object per clause, so a
779-clause version is 779 reads."""


class DifferentClauseSet(RuntimeError):
    """The store holds chunks of the version that the clause store doesn't.
    Nothing was written."""


@dataclass(frozen=True, slots=True)
class VectorLoad:
    """What a load found and did, against the ledger it was given."""

    chunks: int
    stored_before: int
    written: int
    """Chunks this run embedded and upserted."""
    recorded_in_ledger: bool
    """True if this run recorded the version in the given ledger."""


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


async def read_version_source(
    store: ClauseStore, *, instrument_id: str, version_id: str, tenant_id: str
) -> str:
    """The source ingest stored beside the version's clauses: what
    `load_version_vectors` hands the lifecycle, which checks it against the
    manifest. A version whose ingest never finished has none, and is an error."""
    source = await store.get_source_html(instrument_id, version_id, tenant_id=tenant_id)
    if source is None:
        raise LookupError(
            f"{instrument_id}:{version_id} has no stored source: its ingest didn't finish"
        )
    return source


async def load_version_vectors(
    clauses: tuple[ClauseRecord, ...],
    *,
    source_html: str,
    count_tokens: Callable[[str], int],
    lifecycle: CorpusLifecycle,
    retrieval: RetrievalAdmin,
    ledger: LedgerStore,
    instrument_id: str,
    version_id: str,
    tenant_id: str,
    effective_from: dt.date,
    document_class: DocumentClass,
    supersedes: str | None = None,
) -> VectorLoad:
    """Bring the store and the ledger up to the clause store's version (module
    docstring). Every write goes through `lifecycle`; `retrieval` and `ledger`
    are only read here, and must be the ones `lifecycle` writes to.
    `clauses` and `source_html` are the version as the clause store holds it
    (`read_version_clauses`, `read_version_source`).

    `supersedes` names the version this one retires from `effective_from`
    (an amendment, ADR-0021, ADR-0042): the lifecycle retires its chunks and
    ends it in the ledger when it records this one."""
    chunks = chunk_clauses(clauses, count_tokens=count_tokens)
    with telemetry.span(
        "vector_load.load_version",
        **{"load.version": f"{instrument_id}:{version_id}", "load.chunks": len(chunks)},
    ) as span:
        stored = await retrieval.version_chunk_ids(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        )
        span.set_attribute("load.stored_before", len(stored))
        foreign = stored - {c.chunk_id for c in chunks}
        if foreign:
            raise DifferentClauseSet(
                f"{instrument_id}:{version_id}: the store holds {len(foreign)} chunks the clause "
                f"store doesn't (e.g. {min(foreign)}). Writing over a stored version is a "
                "replace in place, so nothing was changed"
            )
        if not stored:
            await lifecycle.publish_version(
                clauses,
                count_tokens=count_tokens,
                source_html=source_html,
                instrument_id=instrument_id,
                tenant_id=tenant_id,
                version_id=version_id,
                effective_from=effective_from,
                supersedes=supersedes,
                document_class=document_class,
            )
            span.set_attribute("load.written", len(chunks))
            return VectorLoad(len(chunks), 0, written=len(chunks), recorded_in_ledger=True)
        written = 0
        if len(stored) < len(chunks):
            written = await lifecycle.complete_version(
                clauses,
                count_tokens=count_tokens,
                source_html=source_html,
                instrument_id=instrument_id,
                tenant_id=tenant_id,
                version_id=version_id,
                effective_from=effective_from,
                document_class=document_class,
            )
        span.set_attribute("load.written", written)
        recorded = await ledger.version_validity(
            tenant_id=tenant_id, instrument_id=instrument_id, version_id=version_id
        )
        if recorded is None:
            await lifecycle.record_published_version(
                instrument_id=instrument_id,
                tenant_id=tenant_id,
                version_id=version_id,
                effective_from=effective_from,
                supersedes=supersedes,
                document_class=document_class,
            )
            return VectorLoad(len(chunks), len(stored), written=written, recorded_in_ledger=True)
        valid_from, _ = recorded
        if valid_from != effective_from:
            raise ValueError(
                f"{instrument_id}:{version_id}: the ledger has it in force from {valid_from}, "
                f"not {effective_from}"
            )
        return VectorLoad(len(chunks), len(stored), written=written, recorded_in_ledger=False)
