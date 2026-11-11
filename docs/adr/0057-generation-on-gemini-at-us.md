# ADR-0057 — Generation runs on Gemini 3.5 Flash at the Vertex `us` multi-region during the build

- Status: accepted. The human decided on 2026-11-10 and 2026-11-11 (quoted below).
- Date: 2026-11-11
- Decision IDs: amends `D-07` and `D-16` (`DECISIONS.md`)
- Supersedes: ADR-0056 entirely (Claude Sonnet 5 on Amazon Bedrock in
  Australia), and ADR-0024's choice of model (`claude-sonnet-5`) as the
  default; Claude on Vertex stays a dormant configuration. ADR-0024's
  stance continues, now for Gemini: build-phase generation at the US
  multi-region endpoint, `global` refused. ADR-0023's gate and ADR-0032's
  tester gate, which ADR-0056 retired, stand again.
- Evidence: `docs/research/generation-in-australia-2026-11.md`, section
  *2026-11-11: Gemini at `us`*
- Issues: #326 (the Gemini adapter; removes the Bedrock adapter and its AWS
  code, and keeps the Vertex Claude adapter as a dormant configuration). #323
  (removing the offshore paths) is closed as not planned.

## The human's decision

On 2026-11-10, the fallback recorded in ADR-0056:

> If it doesn't hold up, go with option 1 — Gemini 3.5 Flash PAYG on us or
> global — and accept that residency stays as it is for the build phase.
> Unblocking Phase 1 beats waiting, and the offshore gate already handles it
> honestly.

On 2026-11-11, after saying that nothing was ever built in AWS, only GCP:

> we were going to use a Claude model in GCP but it hasnt gottena pproval so
> we are using a google gemini model instead [sic]

## Context

- **Claude on Vertex hasn't got its quota.** ADR-0024 chose `claude-sonnet-5`
  at the Vertex US multi-region endpoint. The first live call on 2026-10-10
  returned 429 on a request quota that is effectively zero for this project,
  and in three weeks the increase never moved. Phase 1's last exit
  criterion, the live run, has waited on it since. The request is pending,
  not refused: in the human's words, it "hasnt gottena pproval" [sic].
- **Gemini in Sydney is a reservation, not a price per token.**
  `gemini-3.5-flash` is sold in `australia-southeast1` only as Single Zone
  Provisioned Throughput: 1 GSU minimum, about $2,970 a month on a 1-month
  commitment, about fifteen times the $200 ceiling. Its Standard PayGo is
  `global`, `us` and `eu` only.
- **Bedrock is withdrawn.** ADR-0056 moved generation to Claude on Amazon
  Bedrock through its Australian inference profile. Claude Code proposed
  Bedrock; the human accepted it on that proposal (2026-11-10: "If Bedrock
  gives pay-per-token Claude in Australia … we take it"); Claude Code didn't
  check whether the human had an AWS account before choosing it — that was
  the error. The human has none and keeps this build on GCP only. The
  Bedrock adapter was built in code only (#322, PR #325); no AWS account,
  role, subscription or other AWS resource was ever created, and nothing was
  billed.
- **Gemini 3.5 Flash at `us` needs nothing requested.** Verified against
  Google's raw pages on 2026-11-11:
  - GA, released May 19, 2026, and sold at `us` as Standard PayGo with usage
    tiers. In the model page's consumption-options table, the row "Fixed
    quota" reads "Not supported".
  - There is no per-project quota to raise and no Model Garden enablement:
    a usage tier is assigned automatically from the organization's 30-day
    spend, and a 429 "doesn't indicate that you've hit a fixed quota", only
    contention in a shared pool. The Vertex API is already enabled on
    `compliance-rag-508904`.
  - At `us`, "ML processing stays within" the United States. `global`
    processes "anywhere globally".
  - Prices per 1M tokens at non-global endpoints, in force since July 1,
    2026: $1.65 input, $0.165 cached input, $9.90 output (thinking
    included). There is no premium above 200k tokens.
  - Structured output (a response schema with JSON) and implicit and
    explicit context caching are supported. Both caching modes need a prefix
    of at least 4,096 tokens.

## Decision

- **Generation runs on Gemini 3.5 Flash (`gemini-3.5-flash`, GA) on Vertex
  AI**, project `compliance-rag-508904`, at the **`us` multi-region
  endpoint** (`aiplatform.us.rep.googleapis.com`), on Standard PayGo, during
  the build phase only.
- **Not `global`.** The human allowed "us or global"; this ADR takes `us`.
  `global` can route a request to any country, and Google says it provides
  no residency guarantee. `us` at least pins processing to one named
  jurisdiction, as ADR-0024 reasoned for Claude. `global` stays refused when
  `Settings` loads and when the generator is built.
- **Generation stays offshore during the build, behind the gates that already
  exist**, unchanged:
  - ADR-0023: offshore generation is refused at startup and before every
    request while the store holds any tenant chunk not registered synthetic,
    and a real tenant upload is refused at ingest;
  - ADR-0032: per answer, a question from a member who isn't a registered
    build-phase tester is refused, and so is any tenant text read from the
    bucket (the CAG prefix, a clause to repair) that isn't registered
    synthetic.
- **Only the Bedrock and AWS code is removed** by #326: the Bedrock adapter,
  its AWS credentials and its settings. The billable guard's `aws` rules
  stay: they cost nothing and still protect.
- **Claude on Vertex stays in the code as a dormant configuration.** The
  Vertex Claude adapter (`claude-sonnet-5` at `us`, ADR-0024) is kept,
  selectable with `GENERATION_BACKEND=claude`, behind the same offshore
  gates. It isn't called while Gemini is the default. If the quota is ever
  granted, switching back is the human's decision, recorded then by an ADR.
- **Caching: implicit is recommended**, and #326 confirms the choice and
  records why in its PR. Implicit caching is on by default, bills the cache
  write at the plain input price and stores nothing that has to be created.
  An explicit cache is a stored, per-token-hour-billed resource that code
  would create, so it would need its own `SERVICES.md` row first.
- **The schema is a hint, never trusted.** The reply is requested as JSON
  with a response schema, and `draft.py` still validates it strictly; the
  verifier still blocks any citation that doesn't match (D-02).

## Consequences

- **The build-phase offshore exception is kept.** D-16 goes back to its
  wording before ADR-0056, with Gemini in place of Claude: generation is the
  single exception to residency, only while the corpus is public law plus
  synthetic data, and only behind ADR-0023's and ADR-0032's gates. Aura Free
  stays under D-09's separate build exception, as before. D-07 names Gemini
  3.5 Flash at `us` and says the same. *Build-phase tester* is a current term
  again in `CONTEXT.md`.
- **AWS is not adopted.** Bedrock goes back to `SERVICES.md`'s forbidden
  table as "Not adopted: no AWS account". The Bedrock and IAM role rows are
  removed. The Vertex Claude row stays, marked dormant until the human
  decides otherwise.
- **"Gemini for generation" is no longer forbidden.** That row ("allowed only
  as an explicit A/B in the eval harness, never as the default") is replaced
  by the Gemini 3.5 Flash row and a note pointing here.
- **Everything model-specific is re-validated**, by #326 with stubs and by
  the live run (#29, #31) against the real model:
  - the system prompt, the prompt contract and the pinned golden prompts,
    which carry over byte-identical unless the system prompt names Claude;
  - the draft schema, now also sent as a response schema;
  - the injection test set;
  - the prefix-cap estimate (`assessment/prefix_cap.py`), calibrated for
    Claude's tokenizer;
  - the cost model.
- **The cost model moves to Gemini's prices.** At the Phase 1 cost model's
  ~2,000 uncached, ~5,000 cached and ~400 output tokens a query, 2,000
  queries a month cost about $16 with every prefix cached and $31 with none.
  Each 1,000 thinking tokens a query adds about $20. That puts the row at
  about $16–51 a month, against Claude's ~$20. ADR-0054's 30k prefix cap
  stands. Its $0.50 document-check budget was worked out at Claude's rates;
  at Gemini's, the same check costs less unless thinking is heavy, which the
  live run measures.
- **A short prefix may not be cached.** A question's prefix (system prompt and
  policies, no digest) is about 4,000 tokens for Acme, at or under the
  4,096-token caching minimum. The no-cache figure above is the bound.
- **A 429 means contention, not a quota.** The adapter maps it to
  `GenerationUnavailable(QUOTA)` as before and retries with backoff. The
  tier an organization gets below Tier 1's $10 of 30-day spend isn't
  published, so the first live call (#29) is the check.
- **Phase 1's live run is unblocked** once #326 merges. Nothing new has to be
  created: the Vertex API is enabled, and Gemini needs no enablement.
- **Phase 5's AU-resident generation stays open.** Before the first real
  customer document, generation has to move to Australia. The recorded
  options are Gemini on Single Zone Provisioned Throughput in Sydney (about
  $2,970 a month at the 1-GSU minimum), or Claude on Bedrock through the AU
  inference profile, which would need an AWS account and its own ADR.
- **Telemetry is unchanged.** Spans keep their attributes, with the region
  `us` and the cache-hit count from `cachedContentTokenCount`; Cloud Logging
  in Sydney stays the only sink.
