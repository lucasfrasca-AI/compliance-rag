# ADR-0036 — User scope is private to its owner

- Status: accepted (the human's decision on #171, 2026-10-14)
- Date: 2026-10-14
- Issue: #171
- Related: ADR-0020 (the tenant filter this adds the owner to), ADR-0033 (the
  cached prefix, now stated to be tenant scope only)

## Context

`CONTEXT.md` defines the `user` scope as "one person's private additions".
`Chunk` carried no owner, and every retrieval path admitted a user-scoped chunk
by **tenant** only. `QdrantRetrievalStore._build_filter` let `{tenant, user}`
through for the asking tenant, `lookup_clause` searched public, tenant and user
scope by tenant, and `RetrievalPipeline.route()` ignored `scopes` on a router
hit. So one member's private addition would have been retrievable by every
member of the tenant, through `answer()` as well, and by an exact clause-id
question.

Nothing writes user-scoped chunks yet (there is no upload API), so nothing has
leaked. #166 made the residency gate count them as customer data; this is the
isolation half of the same gap.

#171 offered two ways out: (a) give `Chunk` an owner and filter by it on every
channel, or (b) drop `user` scope until there is a product need. The human chose
(a): "user-private means private."

## Decision

**A user-scoped chunk or clause names its owner, and is admitted to that member
of that tenant and to no one else.**

- **The owner is `user_id`.** `Chunk` and `ClauseRecord` carry `user_id`.
  It is required and non-blank when `scope` is `user`, and must be `None` for
  any other scope (`check_user_owner`, one rule for both models). A user-scoped
  record also needs a real tenant. The Qdrant payload carries `user_id`, with a
  keyword payload index. `ensure_collection` adds the index to an existing
  collection, as it did `instrument_id` (ADR-0020).
- **The asking member reaches the store.** `user_id: str | None` is a
  keyword-only argument of `RetrievalStore.search` and `lookup_clause`,
  `ClauseStore.get_clause`, `RetrievalPipeline.retrieve` and `route`,
  `AssessmentPipeline.answer`, and the verifier's `check` and `check_all`. The
  API passes the signed-in member's uid.
- **The filter is the rule.** A user-scoped chunk is admitted only when its
  `scope` is `user`, its `tenant_id` is the asking tenant, and its `user_id` is
  the asking member. It is built in `_build_filter`, the one place a Qdrant
  filter is built, and mirrored in the fake's `_visible`, so one contract runs
  against both. The tenant's other documents are admitted by tenant alone, as
  before. No later stage of retrieval re-checks it; the clause store's read of
  canonical text applies the same rule on its own (below).
- **No member admits nothing.** `user_id=None` matches no user-scoped chunk, so
  the argument defaults to `None` on the store, pipeline and `answer`: leaving
  it out can only narrow what comes back, never widen it. A point that is
  user-scoped but carries no `user_id` (stored before the field, or by hand)
  matches nothing either; it fails closed.
- **An exact lookup is a channel too.** `lookup_clause` now takes `scopes`, and
  `route()` passes its own on a router hit, as `retrieve()` does for the full
  pipeline. It is required, like `search`'s, so no caller can leave it out, and
  it was the missing parameter that let a router hit ignore scopes.
- **`answer()` asks for user scope.** It retrieves from public, tenant and user
  scope (`ANSWER_SCOPES`): the filter, not the list, keeps user scope private,
  and a member's own additions are citable by them.
- **The verifier reads canonical text for the owner only.** `Verifier.check`
  passes the member to `ClauseStore.get_clause`, which returns `None` for a
  user-scoped clause to anyone else, in the fake and in the Cloud Storage
  adapter. Retrieval's filter already keeps another's clause out of the
  context; this holds when it did not, as the ledger's version checks hold
  for a retired clause. It finds the clause's tenant from the instrument id's
  prefix, as the verifier always has, so it does not itself know the asking
  tenant: retrieval's filter is what binds the tenant.
- **The write path carries the owner.** `CorpusLifecycle._check_payload` checks
  every chunk and clause against `check_user_owner` before any store is touched,
  because `model_copy` skips model validation, and requires a version to have
  one scope and one owner: a version is retired, withdrawn and purged as a
  unit, by tenant and instrument, never by owner, so one spanning two people
  would let either's removal reach the other's.
- **A private addition is never a policy.** An internal policy is the cached
  prefix every member's answer reads (D-11), so `publish_version` and
  `replace_in_place` refuse user-scoped text for one. The prefix is therefore
  tenant scope only (`PolicyCorpus.excludes` already named tenant scope only),
  and `tenant_policies` reads as no member, so a private clause that got into a
  policy's manifest is an error rather than a leak.

### Which member id

`Member.uid`, the stable id the sign-in carries (the Firebase `sub`), not the
email.

- **It does not change when the email does.** An email can be reassigned to
  another person, who would then inherit the previous holder's private
  additions. A uid cannot be reissued.
- **It is already what the verifier proves.** `FirebaseTokenVerifier` requires a
  1-128 character `sub` on every accepted token, so the id is always present
  and bounded.
- **Its cost is orphaning, which fails closed.** If the identity provider is
  ever replaced the uids change, and the old private additions reach no one
  until migrated. That is safe. Email would survive the move and could instead
  hand them to a stranger.

Other records in the ledger key a person by email (`AssessmentRecord.user_id`,
`asserted_by`, `subscribed_by`). Those are audit trails of who did something;
this is an access key, and an access key should not be reassignable. This ADR
does not change them.

### Alternatives considered

- **(b) Drop `user` scope** (refuse it on the write path, remove it from the
  default scopes). Smallest surface, and the recommendation on the issue. Not
  taken: the human decided private additions are part of the product, and the
  scope is already in `CONTEXT.md`, the residency gate (#166) and the models.
- **Post-filter in the pipeline.** Retrieve by tenant, then drop other members'
  chunks. The reranker and fusion's limit would then spend places on chunks the
  member may not see, and a missed call site would leak. The filter inside the
  store is the single point, as for tenants (ADR-0020) and the prefix (ADR-0033).
- **A collection per member.** Makes isolation structural but replaces the one
  configured collection (`Settings.qdrant_collection`) with one to create,
  index and bootstrap per person. A payload filter with an index is how tenants
  are separated already.
- **An access list per chunk** (share with named members). Sharing is a product
  question this ADR does not answer; an owner field is the minimal truth, and a
  list can be added beside it later.
- **A required `user_id` argument.** Safer against forgetting, but a forgotten
  one narrows rather than widens, and it would rewrite every existing caller,
  most of them library questions with no member.

## Consequences

- **A member's private additions are theirs alone**, by every channel (dense,
  sparse, fused, exact lookup), by the router's two paths, in the verifier's
  read of canonical text, and in the cached prefix. The leakage contract runs
  on the fake and on a real Qdrant server, with the owner as the positive
  control for every route.
- **`lookup_clause` gained a required `scopes`.** It used to search public,
  tenant and user scope whatever the caller wanted, which is the narrowing:
  the existing callers now pass public and tenant, and a caller that wants a
  member's own additions must say so.
- **`answer()` now retrieves a member's own private additions** (`ANSWER_SCOPES`).
  Before, only its exact-id lookup could reach user scope, and for any member.
  This is a capability for the owner, not part of the isolation fix; the
  isolation is the filter.
- **Points stored before `user_id` existed**, with scope `user`, fail closed.
  Nothing writes them today and the cloud collection is empty (#144), so nothing
  needs re-upserting.
- **A version has no owner of its own yet.** Manifests, `source.html` and the
  ledger's version rows are keyed by tenant and instrument, and nothing a
  member can call reads them with an id they choose. Before an upload path
  writes user-scoped documents, the version needs an owner too, so those reads
  and the removal states (withdraw, purge) can be owner-only. That is its own
  ticket.
- **The `user_id` is not put in traces.** Spans record whether a member asked
  (`retrieval.member_present`), not who.
