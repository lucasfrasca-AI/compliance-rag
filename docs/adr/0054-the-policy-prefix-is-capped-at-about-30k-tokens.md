# ADR-0054 — The policy prefix is capped at about 30k tokens

- Status: accepted. The human accepted the cap as the default on 2026-11-08
  and asked for this ADR "explicitly, since 200k to 30k is a material change".
- Date: 2026-11-08
- Amends: D-11 / ADR-0012 (the CAG prefix, "capped at 200k tokens")
- Part of: ADR-0053 (the Phase 3 design session, decision U3)

## Context

ADR-0012 put the system prompt, the obligation digest and the tenant's policy
corpus in one cached prefix, capped at 200k tokens, and required a tenant
over the cap to fail loudly rather than be silently truncated. No code
enforces that cap today.

Phase 3's first exit criterion is a full document check under $0.50. At
`claude-sonnet-5`'s US multi-region rates (per million tokens: $2.20 input,
$2.75 for a 5-minute cache write, $0.22 for a cache read), a 20-claim check
makes about twenty findings calls over the same prefix:

- **A 200k prefix can't fit.** Twenty cached reads of 200k tokens cost $0.88
  on a warm cache, before anything else.
- **The work other than the prefix costs about $0.19** for Acme's 20-claim
  example, which leaves about $0.31 for the prefix.
- **What fits depends on how the cache is warmed.** If the first findings
  call writes the cache before the rest start, the prefix can be about 44k
  tokens; on an already-warm cache, about 71k; if all twenty start together
  on a cold cache, each pays the write (a cache entry exists only once the
  first response has begun), and the prefix can be about 6k.

Acme's six policies are about 3,200 tokens.

## Decision

1. **The CAG prefix (system prompt, obligation digest and policy corpus) is
   capped at 30,000 tokens.** That leaves headroom under the 44k a warmed
   check can afford, for the estimate's error and for the digest growing
   with the mapping.
2. **The cache is warmed by one call before parallel calls start.** A check
   makes its first findings call alone, and starts the rest once that call's
   response has begun.
3. **Above the cap, the request fails loudly** (ADR-0012): a named error
   saying the policy corpus is too large to read in full, with the measured
   size and the cap, before any model call. Never silent truncation, and
   never a quiet switch to a subset.
4. **The size is counted locally,** by a conservative estimate stated in
   code, not by a token-counting API: that would be a call outside Australia
   carrying the tenant's policy text.

## Consequences

- **D-11's row in DECISIONS.md** now reads ≤30k tokens.
- **A real customer with a large policy set is refused today.** The human:
  "Fail loudly is right for us, but it can't be the answer for a real
  customer with a large policy set." What replaces the failure (a
  most-relevant subset of policies, or retrieving policies instead of
  prefixing them, which would itself amend D-11 and ADR-0033) is not decided
  here. It is recorded as an issue (#274).
- **The cap is revisited** once a real tenant's policy corpus is measured.
- **The cap is enforced** wherever the prefix is built, Ask included, with a
  test that a corpus over the cap fails before any generator call.
