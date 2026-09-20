# ADR-0011 — Scope: proposals and documents against policy and law

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-10` (`DECISIONS.md`)

## Context

Adjacent features are easy to bolt on — AI-system registers, IaC scanning, live cloud posture — and each competes with an established, often free, product. The graph is the defensible part.

## Decision

v1 checks proposals and documents against company policy and law, offers chat over both, and opens every citation at the clause. No AI-system registration, no IaC scanning, no live cloud state. Multi-tenancy is deferred but keyed: `tenant_id` is present on every table and payload from the start.

## Consequences

- Effort concentrates on the part competitors cannot copy quickly.
- Prospects asking for a CSPM feature get a no.
- Retrofitting `tenant_id` later would touch every table and index, so it is carried from day one even while unused.
- Out-of-scope ideas go to GitHub Issues with the `later` label.
