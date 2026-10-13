# ADR-0023 — Build-phase generation runs in Singapore, behind a hard gate

- Status: accepted; model and region superseded by ADR-0024 (`claude-sonnet-5` at the US multi-region endpoint). The gate stands, extended to each answer by ADR-0032 (the question, the CAG prefix and a clause to repair)
- Date: 2026-10-09
- Decision IDs: corrects `D-07`, amends `D-16` (`DECISIONS.md`)
- Extends: D-09 (build-phase stores hold only public law and synthetic data)

## Context

D-07 assumed Claude on Vertex could run in `australia-southeast1`, with the
rationale "every query-time byte in Australia". It can't. Probes on
2026-10-08 and 2026-10-09 returned 404 for the publisher model there, and
Google's own documentation agrees. Verified against the raw pages rather than
a summary, on 2026-10-09:

- **[Vertex AI locations](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations).**
  No Anthropic model is marked Supported in the Sydney column.
- **[Claude Sonnet 5](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/sonnet-5) and Sonnet 5.5.**
  Model availability is the US multi-region, the EU multi-region and the
  global endpoint. `asia-southeast1` appears only under "ML processing", so
  there is no endpoint to call there.
- **[Claude Sonnet 4.6](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/sonnet-4-6) (and 4.5).**
  GA with a regional `asia-southeast1` endpoint. This is the nearest regional
  Claude endpoint to Sydney.

The Sydney-resident alternatives, from the same documentation and the
[Vertex AI pricing page](https://cloud.google.com/vertex-ai/generative-ai/pricing):

- **`gemini-3.5-flash`.** GA and listed in `australia-southeast1`, but "In …
  australia-southeast1 … only Single Zone Provisioned Throughput is
  supported". Standard PayGo is global, us and eu only. The minimum commitment
  is 1 GSU at $4.07/hour (1-month commit) or $3.01/hour (1-year), about
  **$2,200–3,000 a month**, against a $200 ceiling.
- **`gemini-2.5-flash`.** Regional in Sydney, but it retires on 2026-10-20.
- **The `global` endpoint, and `asia-southeast1`, kept as permanent residency
  choices.** Both are rejected: the generator is the one component that
  processes customer policy text and queries.

D-09 already draws the build/production line for stores. Free-tier stores
hold only public law and synthetic data during the build, and real customer
policies trigger the residency move. Today the corpus is public law plus six
invented Acme policies. No customer data is in the system.

## Decision

**During the build phase, generation runs on `claude-sonnet-4-6` at the
`asia-southeast1` regional endpoint, and only while the corpus is public law
plus synthetic data.** It is the single exception to Australian residency.
Telemetry, Cloud Storage, the ledger and Qdrant stay in `australia-southeast1`
unconditionally. Neo4j Aura Free remains under D-09's existing exception
(public law only).

The exception is closed by a gate in code, not by a note:

- **Allowed regions.** `vertex_claude_region` may be `australia-southeast1`
  or `asia-southeast1` and nothing else. The `global` endpoint and every
  other region are refused when `Settings` loads, and again when the
  generator is built.
- **Startup and every request.** Offshore, `VertexClaudeGenerator` refuses
  if the retrieval store holds any tenant-scoped chunk not marked
  `synthetic`. It asks when the client is built and again before every
  request, so data written later still stops the next request before it
  leaves Australia. That covers another process, or a path that skips
  `CorpusLifecycle`. The store is a required constructor argument.
- **Ingest.** `CorpusLifecycle` refuses to store a tenant-scoped,
  non-synthetic chunk while generation is offshore. The first real policy
  upload fails loudly at the door; it is never generated on offshore.
- **Fail closed.** `Chunk.synthetic` defaults to `False`. An unmarked tenant
  chunk, or a stored point that predates the flag, counts as real customer
  data and closes the gate. `vertex_claude_region` defaults to Sydney; the
  build phase opts into Singapore explicitly.

The marker sits on **chunks, not instruments**, on purpose. The gate asks the
store generation actually reads from, so it holds whichever path wrote the
data. An instrument-level flag in the ledger would describe intent; the chunk
flag describes what is stored. A half-marked instrument reads as real and
fails closed.

`synthetic` is **self-attested**: whoever ingests a document declares it.
That's acceptable while the only tenant data is the six fixtures in
`docs/research/synthetic-policies/`. It is not a control against a careless
or hostile ingester, and it has to be replaced with something stronger
before any external party can upload.

## Consequences

- **The model changes from Sonnet 5 to Sonnet 4.6.** Sonnet 5 has no
  `asia-southeast1` endpoint. The prompt contract (fenced verbatim clauses, a
  strict JSON draft, a fixed system prompt) is model-agnostic. It was proven
  with fakes, never against a live Sonnet 5, so nothing validated is lost. It
  still has to be validated live against Sonnet 4.6.
- **Pricing is checked against the published page.** Sonnet 4.6 regional
  rates are $3.30 input, $16.50 output, $4.13 for a 5-minute cache write and
  $0.33 for a cache hit, per 1M tokens: the global list price plus 10%, from
  the regional table on the
  [Vertex AI pricing page](https://cloud.google.com/vertex-ai/generative-ai/pricing),
  2026-10-09.
- **AU-resident generation is a real cost of this market, not a later
  surprise.** It is about $2.2–3k a month through Provisioned Throughput.
  That is what a first real customer has to fund, and it is recorded in
  SERVICES.md and PHASES.md Phase 5.
- **Moving generation to Sydney** means setting `vertex_claude_region`
  to `australia-southeast1` and pointing the generator at a model served
  there. That model would be a Gemini model on Provisioned Throughput, which
  is a new `Generator` adapter and an ADR of its own. The gate lifts itself:
  Sydney generation accepts real data.
