# ADR-0018 — Policy clauses are projected into the graph, never persisted

- Status: accepted
- Date: 2026-09-22
- Decision ID: `D-14` (`DECISIONS.md`)
- Supersedes: ADR-0015

## Context

ADR-0015 recorded D-14 as "documents never get graph nodes", flatly. Three
things in the design need that qualified.

`CONTEXT.md` defines the **tenant subgraph** as including policy clauses.
Phase 2 makes approved policy-to-law mappings traversable, and a mapping whose
target is not a node is not traversable. And D-01 permits an LLM to *propose* a
tenant policy-to-law mapping for human approval — an approved mapping must
reach something.

Read flatly, D-14 forbids all three. Read loosely, it permits a persisted
policy node, which is the thing it exists to prevent: one tenant's document
becoming part of the authoritative graph, where a mistake in it is
indistinguishable from law.

## Decision

A policy clause may be a graph node only inside the ephemeral tenant subgraph,
projected per request from the ledger at `as_of` and discarded when the request
ends. It is never written to the persistent graph, never carries `SUPERSEDES`,
and never survives the request that built it. A policy's version history lives
in the ledger on `instrument_version.supersedes`.

Uploaded documents — proposals, user-private additions — get no node at all,
projected or persisted. They are retrieval targets and citation sources only.

## Consequences

- An approved policy-to-law mapping is traversable, so Phase 2 can answer
  policy-to-law in one hop.
- The persistent graph still contains nothing a tenant authored, so rebuilding
  it from git plus Cloud Storage stays complete (D-09).
- Projection cost is paid per request and scales with the tenant's policy
  corpus, which is bounded by the CAG cap in D-11.
- "Never persisted" has to be enforced in the adapter, not by convention. A
  graph write touching a policy clause is a bug, and the loader needs a test
  that proves it refuses.
- The tenant subgraph is now load-bearing rather than a convenience, so it
  needs its own eval fixtures in Phase 2.
