# ADR-0055 — Four Phase 3 resources are unlocked early, to deploy Ask

- Status: accepted. The human decided on 2026-11-10: "ADR-0055 is the right
  call: record the early unlock explicitly and leave the phase line at 1."
- Date: 2026-11-10
- Departs from: `PHASES.md` rule "Billable services unlock per phase …
  Nothing from a later phase is created early", for exactly the resources
  named below
- Related: ADR-0053 (Ask first), ADR-0023 and ADR-0024 (offshore generation
  during the build), #292 (the demo generator), #293 (the ledger runner and
  the Acme seed)

## Context

Phase 1 is open on one criterion only: the live run on Claude, which waits on
the US quota, now three weeks outstanding. Phase 2 is complete. Phase 3's
code is being built under ADR-0053, and Ask, its first surface, works end to
end on the in-process demo generator.

The human asked on 2026-11-08 to deploy Ask before the quota lands:
"a deployed Ask on the fake generator is something I can show someone, and
it exercises the whole production path." The resources a deployment needs
are `SERVICES.md` rows unlocked in Phase 3, while the phase line reads 1.

## Decision

The human unlocks these four rows now, and nothing else from Phase 3:

| Resource | Name | Region |
|---|---|---|
| Cloud Run service | `compliance-rag-api` | australia-southeast1 |
| Cloud SQL Postgres `db-f1-micro` | `compliance-rag-ledger` | australia-southeast1 |
| Secret Manager | `database-url`, `session-secret`, `qdrant-url`, `qdrant-api-key`, `neo4j-password` | australia-southeast1 (user-managed replication) |
| Firebase Authentication | on project `compliance-rag-508904` | global (Firebase) |

Each resource is still created only through its own approval line under the
billable protocol. This ADR records why the phase didn't block them; it
approves nothing by itself.

**The phase line stays at 1.** Phase 1's exit criterion is unmet, and moving
the line would claim otherwise. Every other Phase 3 unlock in `PHASES.md`
waits for the phase, as before: hosting, the web search key and Document AI.

## Consequences

- The deployment runs `GENERATOR=demo`. Every page and every turn the demo
  generator wrote says so (#292). No model is called, so nothing goes
  offshore.
- About $12–15 a month is added, almost all of it Cloud SQL. The
  `SERVICES.md` row for Cloud SQL was understated (it said ~$9–10, leaving
  out storage), and is corrected to $11–14 in the same change.
- Before the API was deployed, its logs had to be kept in Sydney. Cloud
  Run's stdout, stderr and request logs are routed to `compliance-rag-logs`
  and excluded from `_Default` (BUILD_LOG 2026-11-08, *residency*).
- The URL is public (`--allow-unauthenticated`). The members allowlist is
  the control, and it fails closed (#319). Firebase's sign-up toggle is a
  console setting and not relied on.
