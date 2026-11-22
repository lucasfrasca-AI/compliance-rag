"""One ledger `usage` row per generation call, from its real token counts and
keyed to the request's trace (#31). Shared by `answer()` (the answer and each
repair), claim extraction (#298) and a document check's findings (#299), so a
model call is recorded one way."""

from __future__ import annotations

import uuid

from compliance_rag.adapters.interfaces import Generation, LedgerStore, UsageRecord
from compliance_rag.telemetry import current_trace_id


def usage_of(tenant_id: str, generation: Generation) -> UsageRecord:
    """The row for one call, under the current request's trace. A findings run
    builds these as the calls finish and writes them with its assessment, in one
    transaction (`LedgerStore.record_findings`)."""
    return UsageRecord(
        tenant_id=tenant_id,
        usage_id=uuid.uuid4().hex,
        trace_id=current_trace_id(),
        model=generation.model,
        input_tokens=generation.input_tokens,
        output_tokens=generation.output_tokens,
        cached_prefix_hit=generation.cached_prefix_hit,
        cost_usd=generation.cost_usd,
    )


async def record_usage(ledger: LedgerStore, tenant_id: str, generation: Generation) -> None:
    await ledger.record_usage(usage_of(tenant_id, generation))
