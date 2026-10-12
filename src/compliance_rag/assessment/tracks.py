"""Track A, then Track B, for one tenant on one date (D-03, issue #116).

Track A decides which obligations apply, might apply or don't
(`GraphStore.coverage`); its citations become Track B's law scope
(`LawScope.of`); Track B retrieves supporting text inside it. A clause
outside what Track A cites — an obligation that doesn't apply, a provision
nothing here derives from — is never retrieved, however well it matches.
Graph results never enter the ranking pool: Track A's output is a filter and
a report, not candidates.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from compliance_rag.adapters.interfaces import GraphStore, LawScope
from compliance_rag.assessment.coverage import Coverage, Outcome
from compliance_rag.assessment.evidence import Facts
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from compliance_rag.telemetry import span


@dataclass(frozen=True, slots=True)
class Tracked:
    """Both tracks' results, kept apart (D-03): the coverage report and the
    retrieval trace inside its scope."""

    coverage: Coverage
    law: LawScope
    retrieval: RetrievalTrace


async def tracked_retrieve(
    query: str,
    *,
    tenant_id: str,
    as_of: dt.date,
    facts: Facts,
    graph: GraphStore,
    retrieval: RetrievalPipeline,
) -> Tracked:
    with span("tracks", query=query, tenant_id=tenant_id, as_of=as_of.isoformat()) as current:
        coverage = await graph.coverage(facts, tenant_id=tenant_id, as_of=as_of)
        for outcome in Outcome:
            current.set_attribute(
                f"track_a.{outcome.value}",
                [o.obligation_id for o in coverage.with_outcome(outcome)],
            )
        law = LawScope.of(coverage)
        trace = await retrieval.retrieve(query, tenant_id, as_of, law=law)
        return Tracked(coverage, law, trace)
