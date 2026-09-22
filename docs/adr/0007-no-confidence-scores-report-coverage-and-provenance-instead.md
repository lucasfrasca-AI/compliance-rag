# ADR-0007 — No confidence scores; report coverage and provenance instead

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-06` (`DECISIONS.md`)

## Context

A number like 0.87 next to a compliance finding implies a calibrated probability. Nothing here produces one: the mapping underneath was written by a person. Presenting human judgement as a computed score misleads exactly the reader who most needs to know where the claim came from.

## Decision

No confidence scores anywhere in the output. Each assessment reports coverage — which obligations were assessable, which were not, and why — and each finding carries a provenance tier: `parsed`, `asserted`, `inferred` or `external`.

## Consequences

- A reader can tell a parsed fact from an inferred one at a glance.
- 'Unassessable, and here is why' is a first-class output, not a failure.
- Ranking scores stay internal, in traces and evals, never in the answer.
- Comparison against competitors showing confidence numbers needs explaining.
