# Generation in Australia — November 2026

2026-11-10 · decision: **ADR-0056** (Claude Sonnet 5 on Amazon Bedrock through the Australian geographic inference profile) · issues #322, #323

## Question

The Vertex US multi-region quota for Claude (ADR-0024) went three weeks with no movement. Where can generation run in Australia, at pay-per-token pricing, without waiting on a quota?

Two candidates were checked: Gemini on Vertex in `australia-southeast1`, and Claude on Amazon Bedrock through its Australian geographic inference profile. Every quotation below was copied from the raw page downloaded on 2026-11-10, not from a summary. Statements without quotation marks are summaries of the same pages.

## Gemini on Vertex in Sydney

| Fact | Source |
|---|---|
| `gemini-3.5-flash` in Sydney is Provisioned Throughput only: "Note: In asia-northeast1, asia-south1, asia-southeast1, australia-southeast1, and europe-west2, only Single Zone Provisioned Throughput is supported." | Gemini 3.5 Flash model page, https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-5-flash (the documentation now sits under `docs.cloud.google.com/gemini-enterprise-agent-platform/...`) |
| Standard PayGo is not offered in Sydney. The Standard PayGo row reads: "Global: global; Multi-region: us, eu" | Same page |
| Launch stage GA; retirement "May 19, 2027 or later" | Same page |
| `gemini-2.5-flash`, the last Gemini model with a regional Sydney endpoint, has already retired: "Retirement date†: October 20, 2026" | Gemini 2.5 Flash model page, same documentation set |
| Single Zone Provisioned Throughput: 1 GSU minimum, $4.068493151 per GSU-hour on a 1-month, non-global commitment, about **$2,970 a month** (× 730 hours). Ordered through an account representative | Vertex AI pricing, https://cloud.google.com/vertex-ai/generative-ai/pricing |

**Reading.** The only Gemini model sold in Sydney is sold as a committed reservation at about fifteen times the $200 ceiling. Pay-per-token Gemini runs only at `us`, `eu` or `global`, which is no closer to Australia than the Vertex Claude path it would replace. It was the human's fallback had Bedrock not held up: Gemini 3.5 Flash PAYG at `us` or `global`, behind the existing offshore gate (ADR-0023, ADR-0032).

## Claude on Amazon Bedrock in Australia

### Routing and residency

| Fact | Source |
|---|---|
| The Sonnet 5 model card lists an Australian geographic profile: "Geo: AU Geo inference ID: au.anthropic.claude-sonnet-5 \| Source Region \| Destination Regions \| ap-southeast-2 (Sydney) \| ap-southeast-2 (Sydney), ap-southeast-4 (Melbourne)" | https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5.html |
| In `ap-southeast-2`: In-Region not supported, Geo supported, Global supported. The model is reachable from Sydney only through a profile; the AU profile is the one that stays in Australia | Same page |
| "Cross-Region inference requests to an inference profile tied to a geography (such as US, EU, and APAC) stay within that geography. Your data remains in the AWS Regions where it originally resides." | https://docs.aws.amazon.com/bedrock/latest/userguide/geographic-cross-region-inference.html |
| "To the extent we store data for abuse detection, your input prompts and output results will be stored in the destination region." With the AU profile the destination is Sydney or Melbourne | Same page |
| "Don't rely on the geographic prefix in an inference profile ID alone to determine where requests can be routed." So the adapter's setup reads the profile's destinations live with `GetInferenceProfile` (#322) rather than trusting the `au.` prefix | https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html |

### Access

| Fact | Source |
|---|---|
| "In commercial AWS Regions, access to most models is enabled on first use and there is no access request to submit on that page." | https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html |
| "For Anthropic models, you must complete the First Time Use (FTU) form before invoking the model." | Same page |
| The account also needs AWS Marketplace subscribe permissions and a payment method. Anthropic models on Bedrock are billed through AWS Marketplace | Same page |

### Quotas

| Fact | Source |
|---|---|
| "Cross-region model inference tokens per minute for Anthropic Claude Sonnet 5 \| Each supported Region: 6,000,000 \| Yes" (the last column: adjustable) | https://docs.aws.amazon.com/general/latest/gr/bedrock.html |
| "Cross-Model Max Tokens Per Day \| Each supported Region: 700,000,000 \| No" | Same page |
| Account quotas "might be lower than the default values listed" | https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html |
| Output tokens burn down the tokens-per-minute quota at 10× | Same page |

At the planning volume (about 2,000 queries a month) the default is far above need; the risk is that this account's granted value is lower than the default, which is checked live.

### Pricing

Per 1M tokens, from https://aws.amazon.com/bedrock/pricing/, for Claude Sonnet 5:

| | Geo or In-Region (the AU profile) | Global profile |
|---|---|---|
| Input | $2.20 | $2.00 |
| Output | $11.00 | $10.00 |
| Cache write, 5-minute TTL | $2.75 | about 10% less |
| Cache write, 1-hour TTL | $4.40 | about 10% less |
| Cache read | $0.22 | about 10% less |

The AU profile's rates are the same as the Vertex `us` rates in ADR-0024 ($2.20 / $11.00 / $2.75 / $0.22), so ADR-0054's cost model and the 30k prefix cap carry over unchanged.

### Features the generator uses

| Fact | Source |
|---|---|
| Prompt caching is explicit: a cache checkpoint must be placed, minimum 1,024 tokens per checkpoint, at most 4 checkpoints, TTL 5 minutes or 1 hour. Implicit caching is not supported | https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html |
| Structured outputs are supported. They are incompatible with Claude's built-in citations feature, which this project doesn't use (D-02: the model never authors a citation) | Bedrock user guide, Claude structured outputs |
| Model invocation logging is off by default | https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html |
| Sonnet 5's data retention mode is not documented. The retention-mode statement appears only on the Fable 5 and later model pages | Model cards, as above |

## Unverified, checked live after setup

These can't be read from documentation. Each is checked on the account once the human's AWS setup is done (`docs/setup/aws-bedrock.md`, #322), and the result goes in `BUILD_LOG.md`.

- **The quota actually granted.** Service Quotas, `ap-southeast-2`: "Cross-region model inference tokens per minute for Anthropic Claude Sonnet 5". The documented default is 6,000,000; an account's value may be lower.
- **The profile's destinations.** `aws bedrock get-inference-profile --inference-profile-identifier au.anthropic.claude-sonnet-5 --region ap-southeast-2` must list exactly `ap-southeast-2` and `ap-southeast-4`. Anything else stops the adapter (#322), and #323 waits on it.
- **Melbourne's opt-in status.** `ap-southeast-4` is an opt-in Region. Whether the account has opted in doesn't change residency, since both destinations are Australian; it may change which destination serves a request.
- **Sonnet 5's retention mode.** Undocumented for Sonnet 5 (above). Checked on the account and recorded.
