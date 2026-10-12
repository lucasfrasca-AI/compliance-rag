# ADR-0006 — The graph is bitemporal and every traversal takes as_of

- Status: accepted; status wording amended by ADR-0029
- Date: 2026-09-20
- Decision ID: `D-05` (`DECISIONS.md`)

## Context

Law changes, and a report issued last quarter was correct against the law as it stood then. If amendments overwrite nodes, re-running an old assessment silently produces a different answer, and there is no way to show what the system believed at the time.

## Decision

Nodes are immutable. An amendment creates a new version node, a `SUPERSEDES` edge to the old, and sets `valid_to` on the old. Every node carries `valid_from`, `valid_to`, `status` and `recorded_at`, with three statuses: `in_force`, `not_yet_commenced`, `repealed`. Every traversal takes `as_of`, enforced by the query builder — a query without it is a test failure.

## Consequences

- Any assessment can be reproduced as of its own date.
- The graph only grows; node counts must be watched against tier limits.
- `as_of` is mandatory everywhere, including in ad-hoc queries, which is why it is enforced in the builder rather than by convention.
- Commencement dates become test fixtures.
