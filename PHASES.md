# PHASES

Current phase: **1 — Corpus, retrieval, verifier** (update this line when a phase exits; the exit is a commit that changes it.)

Rules:
- Work only on the current phase. Ideas for later phases go into GitHub Issues with the `later` label, not into code.
- A phase exits only when every exit criterion is demonstrated by a passing test or a checked-in artefact, not by a claim in chat.
- Billable services unlock per phase (see `SERVICES.md`). Nothing from a later phase is created early.
- Each phase starts with `/grill-with-docs` on Opus and ends with `/handoff`.

## Phase 0 — Foundations

Goal: a repo that can't be built wrong. No cloud resources beyond free tiers.

Deliverables
- Python project (`uv`, `ruff`, `pytest`, `pyright`) with `src/compliance_rag/` package layout
- Repository interfaces: `GraphStore`, `RetrievalStore`, `LedgerStore`, `Reranker`, `Embedder`, `Generator` — abstract classes with no implementation beyond in-memory fakes for tests
- Ledger schema v1 (SQL migration): `instrument`, `instrument_version`, `clause`, `clause_version`, `tenant`, `user`, `assessment`, `finding`, `evidence`, `document_manifest`, `chat`, `usage`. Every table has `tenant_id`. `assessment` records `corpus_version`, `mapping_version`, `as_of`
- Canonical clause record model with stable ID scheme: `{instrument_id}:{version_id}:{clause_path}`
- OpenTelemetry bootstrap → Cloud Logging regional bucket `compliance-rag-logs` in Sydney (the only sink); one end-to-end trace on a hello-world pipeline, every span as a structured log entry keyed by `trace_id`. Startup assertion: any exporter endpoint outside `australia-southeast1` refuses to start
- CI: lint, type-check, tests on every PR; branch protection references the check
- `CONTEXT.md` glossary confirmed via `/grill-with-docs`; ADRs for D-01…D-10 in `docs/adr/`

Exit criteria
- `uv run pytest` green in CI
- A trace visible in the Sydney Cloud Logging bucket from a CI run, queryable by `trace_id` in Logs Explorer
- The CI trace check asserts **both** halves of the routing, not just the first: the span landed in `compliance-rag-logs`, **and** nothing matching the sink filter landed in `_Default`. A span that reaches Sydney while a copy also sits in the global bucket fails the criterion — `_Default` is global, and a leak there is a residency breach whatever arrives in Sydney (D-16)
- Interfaces have fakes and a contract test each

Unlocks: nothing billable.

## Phase 1 — Corpus, retrieval, verifier

Goal: ask a question about the Australian Privacy Act and get a verified, verbatim, versioned citation.

Deliverables
- **Format spike first** (`docs/research/legislation-gov-au-format.md`): fetch the actual ePub bytes and document the markup — what wraps an APP heading, what wraps a numbered sub-clause, whether clause numbers are separate elements. Blocks the parser ticket. If the HTML has no clause semantics, the parser estimate changes materially and we learn that before writing it, not during
- Parser for legislation.gov.au compilations → clause records → Cloud Storage (versioned). Source is the **ePub HTML**, which is where the clause hierarchy is machine-readable and where `canonical_text` comes from. Click-to-clause for law is an anchor into that stored HTML; PDF position capture is for uploaded documents only (`CONTEXT.md` **Position map**). Pinned compilation: **C2026C00227**, 4 June 2026, series `C2004A03712`, set in config — not a `latest` redirect, so a red regression gate is never ambiguous between a retrieval regression and a change in the law. Tracking latest is Phase 4's job, and it arrives with a diff and a PR
- The parser handles the whole compilation; **selection is a manifest in `corpus/`, not a special case in the parser**. Phase 1 chunks and embeds Schedule 1 (the APPs) plus ss 6, 6C, 6D and 6E. The definitions are needed because APP 11's "reasonable steps" is meaningless without the Act's definition of personal information, and the small-business exemption in ss 6C–6E decides whether the APPs apply at all — Track A needs it in Phase 2, and pulling it in later means a re-embed
- Synthetic company policy set (5–8 documents, MD + PDF) parsed the same way, designed against a matrix checked into `docs/research/synthetic-policy-set.md`: for each of APP 1, 5, 6, 8, 11, 12, 13, which document addresses it and whether it is consistent, conflicting, silent or ambiguous. Every verdict appears at least twice, at least one pair of policies contradicts each other, and the same document exists in both MD and PDF to exercise PDF position capture
- Cloud Storage layout: **one object per clause**, keyed `{instrument_id}/{version_id}/clauses/{clause_path}.json`, plus one `manifest.json` per instrument version listing every clause path with its `canonical_sha256`. D-02 puts this read path on the critical path of every answer, so the verifier does a single deterministic GET — no scan, no cache invalidation, no offset arithmetic. A verifier that is slow or clever is one people bypass. The per-version manifest gives Phase 4's diff something to compare without fetching every object
- Selection manifest: `corpus/instruments/{instrument_id}/{version_id}/manifest.yaml`, same `author`/`reviewed_by`/`date` front matter as mapping YAML, listing the clause paths in scope for chunking and embedding. It sits inside `corpus/instruments/`, so **changing it changes the corpus version** — correct, not incidental: adding a section changes what retrieval can find, so an assessment issued before the change is only reproducible if the version moved. Note the deliberate asymmetry with the golden set, which sits outside that tag because it changes what we measure, not what the system knows
- Chunker (clause-level, ≤420 tokens as counted by the reranker's tokenizer — see `CONTEXT.md` **Chunk** and issue #6; the cross-encoder's 512-token query+passage window binds, not the embedder), embedder (Vertex, 1536 dims), Qdrant adapter with dense + sparse + keyword payload and native RRF
- Router: clause-ID regex → keyword lookup, skip rerank; else hybrid → rerank
- **Reranker service** on Cloud Run (`australia-southeast1`): FastAPI + ONNX Runtime, `POST /rerank {query, documents[]}` → scores; models benchmarked: `BAAI/bge-reranker-v2-m3` (default), `mixedbread-ai/mxbai-rerank-base-v2`, `cross-encoder/ms-marco-MiniLM-L-6-v2`
- **ColBERT rescoring** in Qdrant (multivector, HNSW off) as the A/B; Vertex Ranking API adapter kept for CI only, hard-gated to `TENANT_ID == dev-tenant`
- Generator adapter (Claude on Vertex) with structured output; prompt caching on the system prefix
- **Citation verifier**: canonical-form comparison against Cloud Storage; fail → targeted repair
- **Eval harness**: 60+ labelled queries (exact ID, paraphrase, cross-clause); recall@k per channel and fused; verifier pass rate; runs in CI as a regression gate
- ISO 27001 Annex A **control IDs only** (`A.8.3`), each with our own one-line descriptor. Not ISO's titles: a title like "Information access restriction" is ISO's authored wording, not a fact, and reproducing all 93 is arguably substantial reproduction of a paid standard. An ID is a reference; our gloss is our text. A crosswalk targets the ID, so nothing is lost functionally. Real titles are something a licensed customer supplies. This is a flagged legal risk, not legal advice — if the titles are wanted, get a written view first

Exit criteria
- recall@10 on exact-ID queries = 1.0; on paraphrase ≥ 0.9 (numbers are the starting bar — revise with evidence)
- Verifier pass rate 100% on the golden set, with at least one deliberately bad citation proven to block
- Cost per query logged; projected monthly under $60 at 2,000 queries
- Reranker benchmark checked in (`docs/research/reranker-benchmark.md`): recall@10 and p95 latency for cross-encoder vs ColBERT vs no-rerank; the winner is the default, recorded in BUILD_LOG

Sequencing: **nothing writes to Cloud Storage or Qdrant until #2, #3 and #4 are merged.** Those three shape `ClauseRecord` and the stable-ID grammar, so a parser run before them produces records that must be rewritten and embeddings that must be paid for twice. Order: #3 → #4 → #2 → format spike → parser → #6 → chunker → #5 → router.

Unlocks: Cloud Storage bucket, Vertex Embeddings, Claude on Vertex, Cloud Run `reranker` service (min 0), Cloud Run jobs, Cloud Build + Artifact Registry (needed to build the reranker image), Cloud Trace/Logging, budget Pub/Sub wiring. Ranking API in `us-central1` for CI only.

## Phase 2 — Graph and Track A

Goal: "does this apply to us, as of this date" answered by traversal, not by the LLM.

Deliverables
- Neo4j schema: `Instrument`, `InstrumentVersion`, `Clause`, `Obligation`, `Condition`, `Actor`, `Control`, `Jurisdiction`, `Sector`; edges `PART_OF`, `VERSION_OF`, `SUPERSEDES`, `APPLIES_TO`, `REQUIRES`, `CROSSWALKS_TO`; bitemporal properties on every node
- Query builder that **requires** `as_of` and a path-depth cap — an unscoped query is a test failure
- Mapping YAML in `corpus/mappings/` → graph loader, idempotent on stable ID
- Tenant profile model → ephemeral tenant subgraph per request (`Organisation`, `HANDLES`, `OPERATES_IN`, `ACTS_AS`, `REGULATED_BY`)
- Track A returns the full applicable obligation set with `assessable / unassessable (reason)`; Track B filters on those IDs
- Graph eval: golden profiles → expected obligation sets; Privacy Act small-business threshold and tranche-1 commencement dates as fixtures

Exit criteria
- Same question with two `as_of` dates yields different, correct obligation sets
- Coverage report lists every unassessable obligation with its reason
- Aura node/edge counts logged; under 10% of limits

Unlocks: Neo4j Aura Free (human-created).

## Phase 3 — The product

Goal: a signed-in compliance lead uploads a proposal and gets findings against policy and law, with click-to-clause.

Deliverables
- Claim extraction (schema-validated, provenance inferred, user confirms)
- CAG layer: tenant policy corpus + obligation digest as the cached prefix; law via retrieval
- Findings taxonomy: consistent / conflicts / not addressed / ambiguous, each with provenance tier and citations
- Web fallback (external tier, shown after grounded answer, never blended)
- API on Cloud Run (SSE streaming); Cloud SQL ledger; Secret Manager; Firebase Auth (single tenant, `tenant_id` fixed)
- Frontend: chat with history, citation panel with click-to-clause (PDF.js + positions), admin panel (profile fields, policy upload/version, mapping approval queue, framework scope)
- Prompt-injection controls: delimiting, schema validation, no retrieved text in query construction; adversarial test set in CI
- Egress audit script checked into `docs/research/` proving no request-path byte leaves AU
- Trace UI decision point: if Logs Explorer has proven insufficient for a named debugging task, write the ADR and add Phoenix (planned) — otherwise stay on Logging

Exit criteria
- A full document check end to end under 60 s p95, under $0.50
- Every citation in the UI opens the source at the clause
- Admin can approve a policy-to-law mapping and see it change a finding

Unlocks: Cloud Run `compliance-rag-api` service, Cloud SQL micro, Secret Manager, Firebase Auth, hosting, web search key, Document AI on demand.

## Phase 4 — Loop A automation and second corpus

Goal: the corpus keeps itself current with a human in the loop.

Deliverables
- Feed poller (Federal Register, EUR-Lex Cellar) on Cloud Scheduler → Cloud Run job
- Clause-level diff → GitHub PR naming affected mappings
- Keep-alive job for Aura and Qdrant
- Regression gate re-runs golden set on corpus change; re-runs affected assessments
- EU AI Act + Regulation 2026/1744 ingested; Omnibus commencement shifts as eval fixtures

Exit criteria
- A simulated amendment produces a PR listing exactly the changed mappings and nothing else
- No manual keep-alive needed for 30 days

Unlocks: Cloud Scheduler, Cloud Workflows.

## Phase 5 — Hardening (outside build budget; decisions only)

Residency move for the graph (Aura Pro in AU — Qdrant is already in Sydney), trace UI (Phoenix on Cloud Run, or self-hosted Langfuse if prompt management is ever needed), real multi-tenancy (RLS, per-tenant labels, scope filters enforced in adapters), SSO via OIDC/SAML. Each is an ADR before it is a task.
