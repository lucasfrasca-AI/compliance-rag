# ADR-0020 — A curated library, with per-tenant subscriptions

- Status: accepted
- Date: 2026-10-08
- Decision ID: `D-17` (`DECISIONS.md`)

## Context

Until now every public-scope chunk was visible to every tenant: the retrieval
filter was "scope is public, or tenant_id matches". That fits one tenant
reading one statute. It doesn't fit the product as it actually is: the admin
maintains a library of legal instruments, and each tenant picks the ones that
apply to it. A health-sector tenant shouldn't get answers grounded in a
financial-services regulation it never selected, and the chatbot must answer
only from what is in that tenant's environment.

Library law is shared. The same Privacy Act points serve every tenant that
selected it. So "removing" an instrument from one tenant's environment can't
delete anything — another tenant is reading the same points.

## Decision

A ledger table, `tenant_instrument (tenant_id, instrument_id, subscribed_at,
subscribed_by)`, records which library instruments each tenant has selected.
Retrieval sees a tenant's own documents plus library chunks whose
`instrument_id` is in that tenant's subscriptions, and nothing else.

The filter is built inside the retrieval adapter, from the ledger, on every
query path — `QdrantRetrievalStore._build_filter` reads subscriptions itself.
No method accepts a caller-supplied filter or subscription list, so a call site
cannot forget it, and nothing a caller passes can widen it. `instrument_id` is
its own payload field and keyword index, because Qdrant cannot filter on a
substring of `clause_id`.

Unsubscribing deletes the row and nothing else. Resubscribing inserts a new row
and re-embeds nothing: the points were never touched.

`_public` holds no subscription rows. It cannot be a row in `tenant` (the 0002
CHECK), and it owns every library chunk, so the ownership branch of the filter
already gives it the whole library — including instruments added later, which
seeded rows would miss.

Only library ids can be subscribed to, enforced twice: in the application
(`validate_library_instrument_id`) and in the DDL (a CHECK that rejects any
tenant-prefixed id). A tenant's own documents need no subscription, and another
tenant's are never reachable.

## Consequences

- **Every real tenant starts with no library access.** Before this, every
  tenant saw all public law; now a tenant sees none until it subscribes. That
  includes `Settings.tenant_id`'s default `dev-tenant`. Nothing breaks today
  only because no runtime caller retrieves as a real tenant yet — the first
  one that does must subscribe first, or run as `_public` for library-only
  work (the golden set's public-law queries do exactly that).
- Proven against the real adapter on a real Qdrant server, with the same
  shared contract the in-memory fake runs: an unsubscribed tenant gets no
  library clause in the dense channel, the sparse channel, the fused list or an
  exact-ID lookup. A mutant filter that ignores subscriptions fails that
  contract.
- Every retrieval call reads the ledger once. Acceptable at the planned
  volume; cache per request if it ever shows in a trace.
- `tenant_id` passed to retrieval must come from authentication, never from
  request input — querying as `_public` sees the whole library. That is a
  Phase 3 API constraint, recorded here so it isn't rediscovered.
- `tenant_instrument.instrument_id` is a validated string, not a foreign key,
  because library law can't currently be inserted into `instrument` at all
  (issue #67). It becomes a real FK when that is fixed.
- There is no Postgres `LedgerStore` adapter yet; subscriptions are
  implemented on the interface and the in-memory fake, and the DDL is proven
  against real Postgres. The adapter lands with the first ticket that needs
  the ledger at runtime.
- Points upserted before `instrument_id` was a payload field don't carry it
  and are invisible to subscribed tenants until re-upserted (upsert is
  idempotent on the deterministic point id). `ensure_collection` adds the
  missing index to an existing collection without re-creating it.
- The subscription UI (checkboxes over the library) is Phase 3.
