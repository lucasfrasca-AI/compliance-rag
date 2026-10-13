"""A tenant's policy corpus: `answer()`'s cached prefix (D-11, #148).

The tenant's internal policies in force on a date, from the ledger. That
means never an external upload (retrieval-only), never a withdrawn or purged
version, never a retired one. Each is read clause by clause from `ClauseStore`
in its manifest's order, which is document order. Same tenant and date, same
prefix, byte for byte: that's what lets the prefix cache hit.
"""

from __future__ import annotations

import datetime as dt

from compliance_rag.adapters.interfaces import ClauseStore, LedgerStore
from compliance_rag.assessment.prompt import SourceClause
from compliance_rag.corpus.models import stable_id


async def tenant_policies(
    ledger: LedgerStore, clauses: ClauseStore, *, tenant_id: str, as_of: dt.date
) -> tuple[SourceClause, ...]:
    policy: list[SourceClause] = []
    for version in await ledger.policy_versions(tenant_id, as_of=as_of):
        manifest = await clauses.get_manifest(
            version.instrument_id, version.version_id, tenant_id=tenant_id
        )
        for clause_path in manifest:
            clause_id = stable_id(version.instrument_id, version.version_id, clause_path)
            record = await clauses.get_clause(clause_id, tenant_id=tenant_id)
            if record is None:
                raise LookupError(f"{clause_id} is in its version's manifest but not stored")
            policy.append(SourceClause(record.clause_id, record.canonical_text))
    return tuple(policy)
