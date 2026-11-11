# Generation in Australia — November 2026

2026-11-10 · decision: **ADR-0056** (Claude Sonnet 5 on Amazon Bedrock through the Australian geographic inference profile) · issues #322, #323

2026-11-11 · **superseded by ADR-0057**: the human has no AWS account, so Bedrock was withdrawn. Generation runs on Gemini 3.5 Flash at the Vertex `us` multi-region endpoint during the build, behind the offshore gate. The evidence for that is the last section, *2026-11-11: Gemini at `us`*. The Bedrock sections below are kept as the record of what was checked; nothing in them was ever created.

## Question

The Vertex US multi-region quota for Claude (ADR-0024) went three weeks with no movement. Where can generation run in Australia, at pay-per-token pricing, without waiting on a quota?

Two candidates were checked: Gemini on Vertex in `australia-southeast1`, and Claude on Amazon Bedrock through its Australian geographic inference profile. Every quotation below was copied from the raw page downloaded on 2026-11-10, not from a summary. Statements without quotation marks are summaries of the same pages.

## Gemini on Vertex in Sydney

| Fact | Source |
|---|---|
| `gemini-3.5-flash` in Sydney is Provisioned Throughput only: "Note: In asia-northeast1, asia-south1, asia-southeast1, australia-southeast1, and europe-west2, only Single Zone Provisioned Throughput is supported." | Gemini 3.5 Flash model page, https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models/gemini/3-5-flash (the documentation now sits under `docs.cloud.google.com/gemini-enterprise-agent-platform/...`) |
| Standard PayGo is not offered in Sydney. The Standard PayGo row lists two items, "Global: global" and "Multi-region: us, eu" | Same page |
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
| Sonnet 5's data retention mode is not documented. A retention-mode statement appears on some model cards of Claude Fable 5 and later, not on all of them: the Opus 5.5 and Sonnet 5.5 cards lack it, and so does Sonnet 5's | Model cards, as above |
| The data-retention page says: "There is no data retention change to Claude models released before Claude Fable 5". That sentence doesn't cover Sonnet 5, which was released on 30 June 2026, after Fable 5 (9 June 2026). So Sonnet 5's retention is neither stated nor excluded, and is checked live | Bedrock data-retention page (downloaded as `data-retention.txt`) |

## Unverified, checked live after setup

These can't be read from documentation. Each is checked on the account once the human's AWS setup is done (`docs/setup/aws-bedrock.md`, #322), and the result goes in `BUILD_LOG.md`.

- **The quota actually granted.** Service Quotas, `ap-southeast-2`: "Cross-region model inference tokens per minute for Anthropic Claude Sonnet 5". The documented default is 6,000,000; an account's value may be lower.
- **The profile's destinations.** `aws bedrock get-inference-profile --inference-profile-identifier au.anthropic.claude-sonnet-5 --region ap-southeast-2` must list exactly `ap-southeast-2` and `ap-southeast-4`. Anything else stops the adapter (#322), and #323 waits on it.
- **Melbourne's opt-in status.** `ap-southeast-4` is an opt-in Region. Whether the account has opted in doesn't change residency, since both destinations are Australian; it may change which destination serves a request.
- **Sonnet 5's retention mode.** Its model card says nothing, and the data-retention page's "released before Claude Fable 5" sentence doesn't reach it (above). Checked on the account and recorded.

None of these was ever checked: the human has no AWS account (ADR-0057).

## 2026-11-11: Gemini at `us`

Decision: **ADR-0057**, Gemini 3.5 Flash on Vertex AI at the `us` multi-region endpoint, Standard PayGo, during the build only. Adapter: #326.

Every quotation below was copied from the raw page, downloaded with `curl -sL` on 2026-11-11 and stripped to text, not from a summary. The `docs.cloud.google.com/vertex-ai/...` URLs now redirect to `docs.cloud.google.com/gemini-enterprise-agent-platform/...`, and the pricing page to `cloud.google.com/model-garden/pricing`. Every documentation page below says "Last updated 2026-11-10 UTC"; the pricing page carries no date.

### The model and the endpoint

| Fact | Source |
|---|---|
| Model ID `gemini-3.5-flash`. "Launch stage: GA", "Release date: May 19, 2026", "Retirement date†: May 19, 2027 or later" | Model page, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-flash |
| Standard PayGo locations: "Global: global" and "Multi-region: us, eu". ML processing lists "Multi-region: us, eu" | Same page |
| Consumption options: Pay-as-you-go is "Standard PayGo with Usage Tiers, Flex PayGo, Priority PayGo", Supported; "Fixed quota", Not supported | Same page |
| Capabilities: system instructions, structured output, and "Implicit context caching, explicit context caching" are all Supported. Context window 1,048,576 tokens, maximum output 65,536 | Same page |
| Security controls for online prediction and for context caching: data residency, CMEK, VPC-SC and AXT all marked Supported | Same page |
| The `us` endpoint's host: "Multi-region endpoints (such as us and eu on aiplatform.us.rep.googleapis.com and aiplatform.eu.rep.googleapis.com) constrain machine learning processing to specific jurisdictional boundaries." | Standard PayGo, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/standard-paygo |

### Access: no quota request, no Model Garden enablement

| Fact | Source |
|---|---|
| Gemini 3.5 Flash is on the list of models that "support Standard PayGo with Usage Tiers" | Standard PayGo page |
| No fixed per-project quota: "If you receive a 429 error, it doesn't indicate that you've hit a fixed quota. It indicates temporary high contention for a specific shared resource." | Same page |
| Tiers are assigned automatically from spend: "Your usage tier is automatically determined by your organization's total spend on eligible Agent Platform services over a rolling 30-day period." The spend counted includes all Gemini model families' predictions | Same page |
| Flash tiers (org-level prioritized TPM): Tier 1, "$10 - $250", 2,000,000; Tier 2, "$250 - $2,000", 4,000,000; Tier 3, 10,000,000; Tier 4, 50,000,000. "There's no separate requests-per-minute (RPM) limit for each tier." | Same page |
| The thresholds are stated for `global`, not `us`: "The throughput thresholds apply to requests sent to the global endpoint." Multi-region endpoints "operate from separate regional capacity pools and don't share the global endpoint's worldwide throughput capacity." | Same page |
| Traffic over a threshold isn't refused outright: "Your traffic isn't strictly capped at your tier's TPM threshold." But "High instantaneous traffic can lead to throttling even if your average per-minute usage is within your tier's TPM threshold." | Same page |
| The only setup the Gemini 3.5 Flash guide names: "Before you begin, you must authenticate to Agent Platform using Application Default Credentials (ADC) or an API key." There is no Model Garden enablement step, unlike a partner model such as Claude. The model page adds that its example app "requires a Google Cloud project with billing and Agent Platform API enabled" | Gemini 3.5 Flash guide, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/guides/gemini-3-5-flash; model page |
| Checked live, read-only, 2026-11-11: `aiplatform.googleapis.com` is enabled on `compliance-rag-508904` (the embeddings already use it), and the project's parent is an organization (`812386781552`), which is what usage tiers are computed over | `gcloud services list --enabled`, `gcloud projects describe` |

**Reading.** Nothing has to be requested: no quota increase, no model enablement, no form. That is the difference from Claude on Vertex, whose quota was never granted.

Two things can't be read from the documentation:

- **The tier below $10.** Tier 1 starts at $10 of 30-day spend. This organization's is under that (embeddings cost under $1), and the page doesn't say what applies below it.
- **The `us` pool's throughput.** The published thresholds are for `global`.

At ~20 parallel findings calls of ~7,000 tokens each, about 140,000 tokens arrive in a burst, far under Tier 1's 2,000,000 TPM. But the page warns that second-level spikes can be throttled whatever the average. So a 429 is possible under contention, but it is not a quota wall. The adapter retries with exponential backoff, as the page says to, and ADR-0054's warm-one-call-first rule already spaces the burst. The first live call (#29) is the check.

### Prices, `us` (non-global), Standard

Per 1M tokens, from the "Gemini 3" table, Standard tab, of https://cloud.google.com/vertex-ai/generative-ai/pricing (redirects to `cloud.google.com/model-garden/pricing`):

| Gemini 3.5 Flash, Non-global | ≤ 200K input tokens | > 200K input tokens |
|---|---|---|
| Input (text, image, video, audio) | $1.65 | $1.65 |
| Cached input | $0.165 | $0.165 |
| Text output (response and reasoning) | $9.90 | $9.90 |

- **Against global.** Global is $1.50 input, $0.15 cached, $9.00 output; non-global is 10% more.
- **No long-context premium** for this model: the > 200K column equals the ≤ 200K one. ADR-0054 caps the prefix at 30k tokens anyway.
- **Effective date:** "For non-global endpoints, pricing will go into effect for the Generally available Gemini 3 and later families of all Google models starting on July 1, 2026. Before July 1, 2026, Global endpoint pricing applies to Non-global endpoints." So the non-global rates above are the ones in force now.
- **`us` is "Non-global".** The page calls every endpoint other than `global` that, and has no separate `us` column.
- **Output includes thinking.** The row is "Text output (response and reasoning)", and Gemini 3.5 Flash thinks by default: "If omitted, 3.5 Flash will use its default setting (MEDIUM)" (Gemini 3.5 Flash guide). Thinking tokens are billed as output.
- **Explicit cache storage** is $1.00 per 1M token-hours. The "Context Cache Storage price for Explicit Caching" table, Hourly tab, reads "Gemini 3.8 Flash Cyber, Gemini 3.8 Flash, Gemini 3.7 Flash, Gemini 3.6 Flash, Gemini 3.5 Flash, Gemini 3 Flash | $0.000001 | $0.000001" per token-hour.
- **#326's figures confirmed.** The issue recorded "$1.65 per 1M input, $0.165 cached input and $9.90 output".

**Cost at the planning volume.** The Phase 1 cost model (handoff 0002) assumes, per query, about 2,000 uncached input tokens, about 5,000 cached prefix tokens and about 400 output tokens, at 2,000 queries a month:

- **Every prefix cached:** 2,000 × $1.65 + 5,000 × $0.165 + 400 × $9.90 per 1M = $0.0033 + $0.000825 + $0.00396 = **$0.0081 a query, $16.17 a month**.
- **No prefix cached** (a miss, or a prefix under the 4,096-token minimum below): 7,000 × $1.65 + 400 × $9.90 per 1M = **$0.0155 a query, $31.02 a month**.
- **Thinking** adds $9.90 × 2,000 / 1M = **$19.80 a month for every 1,000 thinking tokens a query**. It is unmeasured until the live run.

So **about $16–51 a month** at 2,000 queries. The high end is no cache hits and 1,000 thinking tokens a query. Claude's estimate was ~$20. The live run (#29, #31) replaces the assumptions with measured tokens.

### Context caching

| Fact | Source |
|---|---|
| "Implicit caching: Automatic caching enabled by default that provides cost savings when cache hits occur." "All Google Cloud projects have implicit caching enabled by default. Implicit caching provides a 90% discount on cached tokens compared to standard input tokens." | Context caching overview, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview |
| "For both implicit and explicit caching, you're billed for the input tokens used to create the cache at the standard input token price. For explicit caching, there are also storage costs based on how long caches are stored. There are no storage costs for implicit caching." So there is no write premium, unlike Claude's 1.25× cache write | Same page |
| Implicit lifetime: "A cached prefix expires no later than 24 hours after it's created, and most entries are evicted earlier." Gemini 3.5 Flash is on the in-memory list. It is not on the durable-caching list, which is Gemini 4 and 3.8 Flash only | Same page |
| To get implicit hits: "Place large and common contents at the beginning of your prompt. Send requests with a similar prefix in a short amount of time." | Same page |
| Explicit: "Explicit caching offers more control and ensures a discount on input tokens that reference an existing context cache. On Gemini 2.5 or later models, this discount is 90%". The default TTL is 60 minutes; the minimum is 1 minute, and "There isn't a maximum cache duration" | Same page |
| Minimum size, both modes: "Gemini 3 (with exceptions) and Gemini 4 models: 4,096 tokens". Gemini 3.5 Flash isn't among the exceptions, which are 3.0 Flash Preview, 3.1 Pro Preview, 3.7 Flash and 3.8 Flash, at 6,144 "(implicit caching only)" | Same page |
| "For both implicit and explicit caching, the cachedContentTokenCount field in your response's metadata indicates the number of tokens in the cached part of your input." | Same page |
| "Google Cloud uses your project number to isolate cached data, and cached content is never shared across projects." | Same page |

**Recommendation: implicit caching**, the default. #326 confirms it and records the choice in its PR.

- **Nothing is created.** An explicit cache is a stored resource holding the tenant's policy text, created by code and billed per token-hour. That is a billable resource made through the SDK, and it would need its own `SERVICES.md` row first.
- **The write is cheap.** It costs the plain input price, and there is no storage charge.
- **A hit isn't guaranteed.** The cost is small either way: an explicit cache of a 5,000-token prefix, kept for a whole month, would be 5,000 × $0.000001 × 730 h = $3.65. The no-cache figure above, $31 a month, is the bound, and the `cachedContentTokenCount` span attribute shows the real hit rate.
- **The 4,096-token minimum matters for Ask.** A question's prefix has no obligation digest. It is the system prompt (~800 tokens) plus Acme's policies (~3,200), about 4,000, at or under the minimum, so it may never be cached at all. A document check's prefix (~5,500 with the digest) clears it. Gemini's tokenizer counts differ from Claude's, so #326 measures this offline if it can.

### Structured output

| Fact | Source |
|---|---|
| Gemini 3.5 Flash is on the supported-models list for controlled generation | Structured output, https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output |
| "To ensure 100% valid JSON objects, requests must include both a response_schema and response_mime_type set to application/json." Without a schema, JSON mode "acts only as a strong hint and carries a small risk of generating malformed JSON" | Same page |
| "Structured output supports a subset of the Gemini Enterprise Agent Platform schema reference." "A complex schema can result in an InvalidArgument: 400 error." "The size of your response schema counts towards the input token limit." | Same page |
| Descriptions, schemas or examples in the prompt "must present the same property ordering as is specified in the responseSchema" | Same page |

The schema only shapes the reply. `draft.py` still validates it strictly, because model output is never trusted before validation (CLAUDE.md *Code standards*), and the verifier still blocks any citation that doesn't match (D-02).

### Where `us` processes data

| Fact | Source |
|---|---|
| "Jurisdictional multi-region endpoints: When you use jurisdictional endpoints, ML processing stays within that specific geographical region (such as the United States or the European Union)." | Data residency, https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/data-residency |
| The ML-processing table marks Gemini 3.5 Flash (`gemini-3.5-flash`) Supported under "US multi-region". It is also marked under Australia, which is Sydney's Provisioned-Throughput-only location from the first section | Same page |
| "Global endpoints route and process data anywhere globally, without restricting it to a specific geographic region … they don't provide regional isolation or data residency guarantees." This is why `global` stays refused, as ADR-0024 refused it for Claude | Same page |

**Reading.** At `us`, a request is processed in the United States. That is offshore, exactly as Claude at `us` was under ADR-0024, so ADR-0023's and ADR-0032's gates apply unchanged: public law and synthetic data only, and only a build-phase tester's question.

### Unverified, checked on the first live call (#29)

- **The tier and throughput** an organization under $10 of 30-day spend actually gets at `us`. The Agent Platform dashboard shows it, given `roles/aiplatform.viewer` and `roles/billing.viewer`.
- **429s:** whether ~20 parallel findings calls draw them at `us`.
- **Thinking tokens and cache hits:** thinking tokens per answer at the default level, and the implicit-cache hit rate, both from the response's usage metadata.
