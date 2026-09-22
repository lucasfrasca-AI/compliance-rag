# ADR-0004 — Two-track retrieval: traversal and ranking never mix

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-03` (`DECISIONS.md`)

## Context

Applicability is a deterministic question — does this obligation bind this company, as of this date — and relevance is a probabilistic one. If both flow into a single ranked pool, an applicable obligation can be pushed out of the top-k by a topically similar paragraph that binds nobody.

## Decision

Track A traverses the graph from the tenant profile and returns every applicable obligation, unranked and untruncated. Track B runs hybrid retrieval over chunks, filtered to Track A's obligation IDs, to find supporting text. Graph results never enter a ranking pool.

## Consequences

- Completeness on applicability, ranking only on supporting prose.
- Track A's output size is bounded by the law, not by a k parameter, so the coverage report must carry it.
- Track B can return nothing for an applicable obligation; that is a coverage gap to report, not a reason to widen the filter.
