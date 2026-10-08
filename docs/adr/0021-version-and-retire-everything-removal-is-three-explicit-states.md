# ADR-0021 — Version and retire everything; removal is three explicit states

- Status: accepted
- Date: 2026-10-08
- Decision ID: `D-18` (`DECISIONS.md`)
- Extends: D-05 (bitemporal law), D-17 (library and subscriptions)

## Context

D-05 already makes law bitemporal: an amendment is a new version, the old one
gets `valid_to`, nothing is deleted. Tenant documents had no stated rule. The
obvious implementation for "the tenant uploaded a new version of its retention
policy" is to replace the old one in place, and for "delete my policy" to delete
it. Both break the product claim: an assessment issued in March must still
verify against the policy as it read in March. A replaced or deleted version
can't.

## Decision

**Version and retire, for everything.** A new upload of any document — law,
external upload or internal policy — gets a new `version_id`. Its chunks start
at the effective date, the previous version's chunks get `valid_to` set to that
same date, and both stay where they are. Nothing is replaced in place and
nothing is deleted on update. One rule for law and policy means one code path,
and retention is already mandatory for law under D-05.

A retired version is unreachable by three independent mechanisms, not by
discipline:

1. **The filter.** The bitemporal clause lives in `_build_filter`, which every
   query path calls unconditionally; no public method accepts a caller-supplied
   filter, and nothing exposes the raw client.
2. **A test that proves it, against the real adapter.** Upsert v1 and v2 of the
   same clause on real Qdrant, retire v1, query at today's `as_of`: v1's
   clause ids appear in neither retrieval channel, the fused list, nor the
   reranked output. At an `as_of` before the retirement, v1 is the one returned
   — the positive control that keeps the negative from passing vacuously.
3. **The verifier.** Citations are compared by `clause_id`, which includes
   `version_id`, so a citation to a retired version names a different clause
   than one to its successor. For this to hold *even if retrieval leaked the
   retired clause*, the verifier must check, independently of the retrieved
   set, that the cited version is in force at the request's `as_of` and not
   withdrawn. That check belongs to the real verifier (issue #30) and is an
   acceptance criterion there, proven with a deliberately leaked clause. It is
   **not** proven by this change: asserting it here with only the clauses
   retrieval returned would just restate mechanism 2.

Deletion offers none of the third property: once the text is gone, there is
nothing to verify an old citation against, so an issued report silently stops
being checkable.

**Three removal states**, each a different operation with a different blast
radius:

- **Unsubscribe** (library law, ADR-0020) — the subscription row is removed.
  Nothing in any store is touched; prior citations still resolve.
- **Withdraw** (tenant-owned only) — the instrument version is marked
  withdrawn and excluded from retrieval at every `as_of`. Text and vectors are
  retained, so prior citations still verify. This is what "delete my policy"
  does by default.
- **Purge** (tenant-owned only) — actually deletes: Qdrant points by
  `instrument_id` + `tenant_id` (+ `version_id`); Cloud Storage objects,
  including every noncurrent object generation; nothing in Neo4j, because a
  tenant policy clause only ever exists in the ephemeral per-request subgraph
  (ADR-0018). The ledger row is kept, with a purged marker recording who, when
  and why.

**Purge guardrails live in the adapter, not the UI**, so no future UI can
bypass them. The adapter:

- refuses outright for library law (`scope=public`) — tenants unsubscribe from
  library law, never purge it;
- returns, before acting, the number of existing findings that cite the target,
  and refuses to act unless the caller acknowledges exactly that number — so a
  caller can't purge without having seen it, and a finding written in between
  forces a fresh look;
- writes an audit row whatever the outcome — `refused`, or `authorized` before
  any store is touched and then `completed` or `failed` — so even a crash
  mid-removal leaves a trace.

The store-level deletes that a purge drives (Qdrant points, Cloud Storage
objects) take a `RemovalAuthorization` that only the lifecycle adapter issues,
after those checks. That makes a bypass deliberate and visible — it means
importing a private issuer under a type-checker suppression — not impossible;
nothing inside one Python process is. Publishing over an existing version is
refused for the same reason: it would be a replace in place without the
guardrails.

"Library law" is decided by the instrument id, not the `scope` field: a
tenant-owned instrument is exactly one whose id carries that tenant's prefix
(CONTEXT.md: Instrument id), so the two are equivalent under the id scheme, and
the id is what every removal call names.

**Replace in place** (same `version_id`, old points deleted) exists only as an
explicit "typo fix, not a policy change" path. It applies the same guardrails
as purge, including the findings count, because it does break prior citations
to that version: their quoted text may no longer match. Law is never replaced
in place under any circumstances. A withdrawn version is refused too: replacing
it would re-upsert its chunks and silently put it back into retrieval.

## Consequences

- Storage grows with every version. At the planned corpus size (~30 MB) this is
  immaterial; purge exists for the cases where it isn't, or where retention is
  legally unwanted.
- **Cloud Storage soft delete.** The corpus bucket has a 7-day soft-delete
  retention (`retentionDurationSeconds: 604800`, read off the live bucket on
  2026-10-08). A purge deletes every live and noncurrent generation, but
  soft-deleted objects stay recoverable — and billed — for 7 days. That is also
  the undo for a mistaken purge. Whether "purge" should mean "gone immediately"
  is a decision about the bucket's policy, not about this code, and is left
  open deliberately.
- `publish_version` writes Qdrant only; recording the new `instrument_version`
  row and its `supersedes` in the ledger lands with the Postgres
  `LedgerStore` adapter (issue #69).
- There is no Postgres `LedgerStore` adapter yet. The findings count, the
  withdrawn and purged markers and the audit table are implemented on the
  interface and the in-memory fake; their DDL (`db/ledger/0005`) is proven
  against real Postgres, and the findings count is a SQL function
  (`count_findings_citing`) so the exact query the guardrail runs is proven
  there now, not first written inside a future adapter.
- Proven against the real adapter on real Qdrant by one shared contract that
  the fakes also run: version-and-retire (mechanism 2), re-publishing an
  existing version refused, every refusal, withdraw keeping the points, purge
  removing only its target, a malformed replacement refused before anything is
  touched, replace-in-place.
  "Stored" is read straight off the collection with the raw client, so it is
  Qdrant's actual points, not what the adapter chooses to show. Two filter
  mutants — no `withdrawn` exclusion, no `valid_to` bound — each fail it.
- Cloud Storage purge is tested against a versioned in-memory bucket, not the
  real one: CI's identity can read only the `reranker/` prefix.
- Withdrawal is a payload flag on the version's Qdrant points, set by the same
  adapter that records it in the ledger. A `must_not` on that flag is part of
  `_build_filter`, so it inherits the "every path, no caller filter" property.
- The UI for upload, withdraw and purge — including a typed confirmation for
  purge — is Phase 3. It adds no enforcement of its own.
