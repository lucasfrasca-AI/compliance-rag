"""A tenant's policy corpus: `answer()`'s cached prefix (D-11, #148).

The tenant's internal policies in force on a date, from the ledger. That
means never an external upload (retrieval-only), never a withdrawn or purged
version, never a retired one. Each is read clause by clause from `ClauseStore`
in document order. Same tenant and date, same prefix, byte for byte: that's
what lets the prefix cache hit.

Document order comes from the clause paths, not the manifest. A manifest
promises no order (`GCSClauseStore` writes it sorted as strings, so `10/1`
before `9/1`). A policy's paths are its section and subsection numbers
(`{N}/{M}`, `policy_parser`), so their numeric order is the document's.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable

from compliance_rag.adapters.interfaces import ClauseStore, LedgerStore
from compliance_rag.assessment.prompt import SourceClause
from compliance_rag.corpus.models import ClauseRecord, stable_id

READ_CONCURRENCY = 16
"""Clause reads in flight at once: one store round trip per clause, so not
in sequence, and not all at once either."""


def document_order(clause_path: str) -> tuple[tuple[int, int | str], ...]:
    """`9/1` before `10/1`: numeric segments by number, anything else after,
    by text."""
    return tuple((0, int(s)) if s.isdigit() else (1, s) for s in clause_path.split("/"))


async def _bounded[T](reads: list[Awaitable[T]], limit: int) -> list[T]:
    gate = asyncio.Semaphore(limit)

    async def one(read: Awaitable[T]) -> T:
        async with gate:
            return await read

    return list(await asyncio.gather(*(one(r) for r in reads)))


async def tenant_policies(
    ledger: LedgerStore, clauses: ClauseStore, *, tenant_id: str, as_of: dt.date
) -> tuple[SourceClause, ...]:
    clause_ids: list[str] = []
    for version in await ledger.policy_versions(tenant_id, as_of=as_of):
        manifest = await clauses.get_manifest(
            version.instrument_id, version.version_id, tenant_id=tenant_id
        )
        clause_ids += [
            stable_id(version.instrument_id, version.version_id, path)
            for path in sorted(manifest, key=document_order)
        ]
    records: list[ClauseRecord | None] = await _bounded(
        [clauses.get_clause(c, tenant_id=tenant_id) for c in clause_ids], READ_CONCURRENCY
    )
    missing = [c for c, r in zip(clause_ids, records, strict=True) if r is None]
    if missing:
        raise LookupError(f"{missing} are in their version's manifest but not stored")
    return tuple(SourceClause(r.clause_id, r.canonical_text) for r in records if r is not None)
