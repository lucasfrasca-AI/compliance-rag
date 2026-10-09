# ADR-0024 — Build-phase generation on Sonnet 5 at the US multi-region endpoint

- Status: accepted
- Date: 2026-10-10
- Decision IDs: amends `D-07` and `D-16` (`DECISIONS.md`); supersedes ADR-0023's choice of model and region, and keeps its gate unchanged

## Context

ADR-0023 chose `claude-sonnet-4-6` at `asia-southeast1`, because Sonnet 4.6
is the Claude model with a Singapore endpoint. Sonnet 5 has none:
`asia-southeast1` appears on its model page only as an ML processing
location, with no endpoint to call. Probes on 2026-10-09 and 2026-10-10
returned 404 for Sonnet 4.6 and 4.5 at `asia-southeast1`; Model Garden had
not enabled them for this project.

The human's decision (2026-10-10): use `claude-sonnet-5`, which is already
enabled for this project. Sonnet 5's endpoints are the US multi-region, the
EU multi-region and the global endpoint
([model page](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/sonnet-5)).

Singapore and the US are both outside Australia. ADR-0023's gate protects
either identically: offshore generation is allowed only while the corpus is
public law plus synthetic data, and it stops the moment any non-synthetic
tenant text exists.

## Decision

- **Model and endpoint.** During the build phase, generation runs on
  `claude-sonnet-5` at the **US multi-region endpoint** (`us`,
  `aiplatform.us.rep.googleapis.com`).
- **Allowed regions.** `vertex_claude_region` accepts `australia-southeast1`
  or `us`, and nothing else. `global`, `asia-southeast1`, `eu`, every
  single-zone US region and every other region are refused, both when
  `Settings` loads and when the generator is built. The `global` endpoint
  stays refused outright because it routes requests anywhere. `us` at least
  pins processing to one named jurisdiction.
- **The gate itself** is ADR-0023's and is unchanged:
  - refused at startup and before every offshore request when the store
    holds any tenant chunk not marked `synthetic`;
  - real tenant uploads refused at ingest while generation is offshore;
  - `synthetic` defaults to `False`;
  - the region defaults to Sydney.
- **Prices**, from the "US Multi-Region" tab of the
  [Vertex AI pricing page](https://cloud.google.com/vertex-ai/generative-ai/pricing),
  checked 2026-10-10, per 1M tokens:
  - input $2.20;
  - output $11.00;
  - 5-minute cache write $2.75;
  - cache hit $0.22.

  That is 10% above the global tab's $2.00 and $10.00.

## Consequences

- **This deliberately moves the build-phase exception to the US.** It is
  permitted only for public law and synthetic data, and the gate enforces it
  in code, not in prose.
- **Live calls are blocked on quota.** The first live calls on 2026-10-10
  returned 429: "Quota exceeded for
  `aiplatform.googleapis.com/us_multi_region_online_prediction_requests_per_base_model`
  with base model: anthropic-claude-sonnet. Please submit a quota increase
  request". The same response came back after 75 seconds idle. The model is
  enabled, but this project's US multi-region request quota for Claude is
  effectively zero until a person requests an increase. #29's live criterion
  and #31's live evaluation wait on that.
- **For Phase 5, not for now.** AWS Bedrock publishes an Australian geo
  inference profile (`au.anthropic.claude-sonnet-5`) that keeps processing in
  Australian regions on pay-per-token pricing. GCP's AU-resident option is
  `gemini-3.5-flash` on Provisioned Throughput, at about $2,200–3,000 a month.
  The comparison is recorded in PHASES.md Phase 5. This repository stays on
  GCP until a paying customer makes the residency decision real.
