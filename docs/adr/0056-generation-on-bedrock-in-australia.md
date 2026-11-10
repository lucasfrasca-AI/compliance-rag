# ADR-0056 — Generation runs on Claude Sonnet 5 through Bedrock, in Australia

- Status: accepted. The human decided on 2026-11-10 (quoted below).
- Date: 2026-11-10
- Decision IDs: amends `D-07` and `D-16` (`DECISIONS.md`)
- Supersedes: ADR-0024's platform and region (Vertex, the US multi-region
  endpoint); the model, Sonnet 5, is unchanged. Also ADR-0023's offshore
  build-phase exception. ADR-0032's tester gate is retired with it, and its
  code is removed by #323
- Evidence: `docs/research/generation-in-australia-2026-11.md`
- Issues: #322 (the Bedrock adapter), #323 (removing the offshore paths)

## The human's decision

> Three weeks with no quota movement is enough — I'm not waiting on Google's
> queue any longer.

On AWS, which `SERVICES.md` listed as forbidden ("Wrong cloud for this
build"):

> 'wrong cloud for this build' … was a sensible default when both clouds
> worked, not a principle worth indefinite paralysis. I've now waited longer
> on a Vertex quota than a Bedrock adapter would take to write. If Bedrock
> gives pay-per-token Claude in Australia with no quota wait, we take it, and
> SERVICES.md gets amended rather than treated as binding.

On the gate:

> The residency gate's allowed-region list goes back to australia-southeast1
> only, and the US and offshore paths are removed entirely — no tester list,
> no offshore exception.

The fallback, had Bedrock not held up:

> If it doesn't hold up, go with option 1 — Gemini 3.5 Flash PAYG on us or
> global — and accept that residency stays as it is for the build phase.

## Context

- **The Vertex quota was never granted.** ADR-0024 chose `claude-sonnet-5` at
  the Vertex US multi-region endpoint. The first live call on 2026-10-10
  returned 429 on a quota that is effectively zero for this project. Three
  weeks later the increase had not moved. Phase 1's last exit criterion, the
  live run, waits on it.
- **Gemini in Sydney is a reservation, not a price per token.**
  `gemini-3.5-flash` is sold in `australia-southeast1` only as Single Zone
  Provisioned Throughput: 1 GSU minimum, about $2,970 a month on a 1-month
  commitment, ordered through an account representative. Its Standard PayGo
  is `global`, `us` and `eu` only. `gemini-2.5-flash`, the last regional
  Gemini in Sydney, retired on 2026-10-20.
- **Bedrock has a verified Australian path for the same model.** The Sonnet 5
  model card lists the geographic profile `au.anthropic.claude-sonnet-5`,
  source Region `ap-southeast-2` (Sydney), destinations `ap-southeast-2` and
  `ap-southeast-4` (Melbourne) only. AWS states that a geographic profile's
  requests "stay within that geography", and that any data stored for abuse
  detection is stored in the destination Region. Access is enabled on first
  use, after Anthropic's first-time-use form and an AWS Marketplace payment
  method. The default cross-region quota is 6,000,000 tokens a minute per
  Region, adjustable. The per-token prices equal the Vertex `us` rates.

Every quotation, with its URL, is in the research document.

## Decision

- **Generation runs on Claude Sonnet 5 on Amazon Bedrock**, through the
  Australian geographic inference profile `au.anthropic.claude-sonnet-5`,
  called from `ap-southeast-2`. Its only destinations are Sydney and
  Melbourne, so every request is processed in Australia.
- **Generation runs in Australia only.** The human's instruction was that
  "the residency gate's allowed-region list goes back to
  australia-southeast1 only". AWS names Sydney `ap-southeast-2`, where GCP
  names it `australia-southeast1`, so this ADR reads the instruction as a
  rule about geography, not one region string. The gate accepts exactly two
  configurations: `australia-southeast1` for the in-process demo generator,
  and `ap-southeast-2` with an `au.` inference profile whose destinations
  are Sydney and Melbourne for Bedrock. Every other region or profile is
  refused (#322 adds the Bedrock half; #323 removes the rest).
- **The model doesn't change.** It is still Sonnet 5, through the same
  Messages API, so the system prompt, the prompt contract, the draft schema
  and the pinned golden prompts carry over unchanged. The prices per 1M
  tokens are $2.20 input, $11.00 output, $2.75 for a 5-minute cache write and
  $0.22 for a cache read, the same as ADR-0024's, so ADR-0054's cost model
  and its 30k prefix cap stand.
- **No long-lived AWS keys.** On Cloud Run, the runtime service account
  `compliance-rag-runtime` exchanges its Google-signed ID token through STS
  `AssumeRoleWithWebIdentity` for an IAM role whose trust policy names the
  `accounts.google.com` principal and that account's unique id. The role may
  only invoke the AU profile and the foundation model in its two destination
  Regions. Locally, a named AWS profile (IAM Identity Center or SSO) is used.
  No code reads `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` (#322).
- **The profile is checked, not trusted by its name.** AWS says not to rely
  on the geographic prefix alone, so setup reads the profile with
  `GetInferenceProfile` and expects exactly Sydney and Melbourne.
- **AWS joins the allowed services**, for generation only: a Bedrock row and
  an IAM role row in `SERVICES.md`. Neptune and OpenSearch stay forbidden.
- **The billable guard covers `aws`.** Once #322 lands, any `aws` command
  that creates, modifies or attaches IAM, Bedrock, Marketplace or Service
  Quotas resources is denied without an approval line, as `gcloud` is.
  Read-only `get`, `list` and `describe` stay open. Until then the protocol
  applies to those commands by hand.

## Consequences

- **A second cloud, and a second account.** The human creates the AWS
  account, completes the first-time-use form, sets up Marketplace payment and
  creates the IAM role. Billing arrives through AWS Marketplace, outside the
  GCP budget alert, so the $200 ceiling now spans two bills.
- **Generation is no longer an exception to residency.** Once the adapter
  (#322) is live, every query-time byte is processed in Australia except the
  graph's, which stays on Neo4j Aura Free under D-09's build exception, as
  before. D-16's generation exception is removed.
- **The offshore code is removed by #323, and only after the first live
  Bedrock call succeeds** in `ap-southeast-2` with the profile's destinations
  confirmed. The human's fallback, quoted above, is Gemini 3.5 Flash PAYG at
  `us` or `global` with residency as it was for the build phase, and that
  fallback needs the offshore gate (ADR-0023, ADR-0032). Removing the gate
  before Bedrock is proven on this account would remove the fallback with it.
  Until #323 the Vertex `us` path stays in code, dormant: it is not called,
  except as this fallback. After #323, `GENERATION_REGIONS` holds
  only the two Australian configurations above, and generation outside
  Australia has no code path. *Build-phase tester* leaves `CONTEXT.md` with it.
- **The request-path egress audit** (a Phase 3 deliverable) now includes the
  Bedrock runtime endpoint and the STS endpoint, both in `ap-southeast-2`.
- **Telemetry stays in Cloud Logging, Sydney.** Bedrock model invocation
  logging is off by default and stays off: it would write prompts and answers
  to a second sink. Spans keep their current attributes, with
  `region=ap-southeast-2` and the inference profile.
- **Phase 5's "AU-resident generation" item is resolved** for Claude at
  pay-per-token pricing. Gemini on Provisioned Throughput stays recorded only
  as the GCP alternative.
- **Phase 1's live run** now waits on the human's AWS setup and the account's
  granted quota, which are checked live and recorded in `BUILD_LOG.md`, along
  with Sonnet 5's retention mode, which AWS doesn't document.
