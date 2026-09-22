# ADR-0010 — Free-tier stores hold only public law and synthetic data during the build

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-09` (`DECISIONS.md`)

## Context

Neo4j Aura Free is not offered in an Australian region, and the free tiers generally are not where customer data belongs. But the build needs real managed services, not local stand-ins, or the migration risk simply moves to the end of the project.

## Decision

During the build, free-tier stores hold public law and synthetic data only. The ledger is the single non-derivable store and is the first thing to be AU-resident and paid. Loading any real customer policy triggers the residency move, which is Phase 5.

## Consequences

- Every derived store can be rebuilt from Cloud Storage plus git in minutes, so losing one is an inconvenience.
- The ledger needs backups from the moment it holds anything real.
- 'No real policies yet' is a standing precondition, and the moment it stops being true is a gate, not a detail.
- The synthetic policy set has to be good enough to exercise the real paths.
