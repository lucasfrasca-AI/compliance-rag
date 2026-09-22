# ADR-0015 — Facts live in the ledger and the graph; documents live in storage and Qdrant

- Status: superseded by ADR-0018
- Date: 2026-09-20
- Decision ID: `D-14` (`DECISIONS.md`)

## Context

If applicability is computed from document similarity, a company that mentions a sector in passing acquires that sector's obligations. If a user's private upload can change applicability, two people at the same company get different answers to the same question.

## Decision

Company facts — the profile: jurisdictions, sector, size bands, actor roles, data categories, regulators — live in the ledger and the graph and are never embedded. Company documents live in Cloud Storage and Qdrant and never get graph nodes. User-private additions are citable but can never change applicability.

## Consequences

- Applicability is auditable: it came from a stated fact, not a cosine score.
- Changing the profile is an explicit, logged act with a provenance tier.
- A document asserting something the profile contradicts surfaces as a finding rather than silently updating the profile.
- The `scope` field (`public` / `tenant` / `user`) must be enforced in the adapters, not the callers.
