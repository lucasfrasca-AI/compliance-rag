# SERVICES — the hard allowlist

This is the complete list of services the project may use. If a service, API, tier or resource is not on this list, it is out of scope: do not create it, do not enable it, do not propose it in passing. Propose it explicitly as a change to this file and wait.

Ceiling: **USD 200/month**. Working target: **under $100**. Estimates are monthly at ~2,000 queries and a ~30 MB corpus.

## Protocol for anything billable

Before any command or API call that creates, enables, scales or deploys a billable resource:

1. Check the resource is on this list **and** unlocked for the current phase in `PHASES.md`.
2. State in chat: the service, the exact resource name, the region, the estimated monthly cost, and which row below authorises it.
3. Wait for a written approval. The `PreToolUse` guard (`.claude/hooks/billable_guard.py`) **denies** every billable `gcloud`, `terraform`, `firebase` and `docker push` command outright and prints the exact line to paste. Approval is that line, written by hand into `.claude/billable-approvals.txt`:

   ```
   <ISO date> | <exact full command>
   ```

   Matching is **full-command equality** after whitespace normalisation, so `<approved> && anything` is denied and a chain must be approved as the whole chain. A rejected command never consumes its approval. The guard marks the line `# used` the first time it authorises a command and denies every attempt after that — one line, one command, one run.

   Claude can **read** the approvals file and cannot **write** it. `Edit`, `Write` and any `Bash` command naming the path are denied; the `Read` tool is deliberately left open. Reading is what makes a refused approval diagnosable — a paste that never reached disk, a line the guard skipped, a whitespace difference — and reading grants nothing, because only a line already in the file can authorise a command. Writing is the capability that matters, and it is closed.

   The same applies to **`ci/wif-provider-flags.yaml`**. It holds the OIDC provider's attribute condition, which decides which GitHub workflows may mint a GCP token, so widening it is a grant of access that no approval line would show — the approval line names only the flags file. Claude is denied `Edit` and `Write` on it; changing it is a human edit, reviewed like any other change to this file.

   Rules match only at a **shell-segment boundary**: the start of the command, or immediately after `;`, `&&`, `||`, `|` or a newline, with leading environment assignments stripped. Prose that merely mentions a billable command — an ADR, a `BUILD_LOG` entry written through a heredoc — does not trip the guard. The file is gitignored; approvals are local and never shared. Console-only services (Neo4j, Qdrant) are created by the human and never by code.

   The guard does not use the hook `ask` decision, which raises a confirmation prompt. `ask` is silently not enforced in non-interactive sessions, where it collapses into an allow; `deny` is enforced everywhere, so the gate is built from `deny` and fails closed.
4. After it exists, append one line to `BUILD_LOG.md` with the date, resource, region, cost estimate and the reason.

Billable means: any GCP resource that is not free-tier-only, any paid tier of a third-party service, any API whose pricing is per-call (Vertex embeddings, ranking, Claude, Document AI), and any change that lifts a free tier to paid.

## Allowed

| Service | Purpose | Tier / size | Est. $/mo | Phase unlocked | Created by |
|---|---|---|---|---|---|
| GitHub (private repo, Actions, Issues) | Source of truth, CI, regression gate, corpus PRs | Free | $0 | 0 | Human |
| Local Postgres (Docker) | Ledger during Phase 0–1 | — | $0 | 0 | Claude Code |
| Cloud Storage bucket `<project>-corpus`, versioned, `australia-southeast1` | Canonical clause text, raw sources, position maps | Standard | <$1 | 1 | Claude Code (ask) |
| Vertex AI Embeddings (`gemini-embedding-001`, 1536 dims) | Dense vectors | Per call | <$1 | 1 | Claude Code (ask once) |
| **Cloud Run service `reranker`**, `australia-southeast1`, 2 vCPU / 2 GiB, min instances 0, max 1, concurrency 4 | Self-hosted cross-encoder (ONNX int8, CPU). Primary reranker — the only in-region option without an always-on bill | Scale-to-zero | $0–15 (min 1 only after measured need, separate ask) | 1 | Claude Code (ask) |
| Qdrant ColBERT rescoring (`colbertv2.0` multivectors via cloud inference, HNSW off) | Second-stage rescoring inside the Sydney cluster; A/B against the cross-encoder | Free tier | $0 | 1 | Claude Code |
| Vertex AI Ranking API (`semantic-ranker-default`), `us-central1` | **Build/CI only.** Not available in AU. Forbidden whenever `TENANT_ID != dev-tenant` or any real policy is loaded | Per 1k queries | ~$1 | 1 | Claude Code (ask once) |
| Claude on Vertex AI (Sonnet-class), `australia-southeast1` | Generation, claim extraction | Per token | $30–80 | 1 | Human enables; Claude Code calls |
| Qdrant Cloud | Vector + sparse store, RRF. Sparse (BM25) vectors are computed client-side via `fastembed`'s `Qdrant/bm25` model, not Qdrant Cloud's server-side inference — a deliberate choice (issue #22), not a fallback: the vector stored is identical either way, and this keeps CI (self-hosted Qdrant, no cloud account) and production running the exact same code path | **Free** (1 GB) | $0 | 1 | Human |
| Neo4j AuraDB | Graph | **Free** (200k nodes) | $0 | 2 | Human |
| Cloud Logging **regional log bucket** `compliance-rag-logs` in `australia-southeast1` (+ BigQuery sink when the eval harness needs SQL over spans) | **The only telemetry sink.** OpenTelemetry spans from every LLM, retrieval and graph call land here as structured logs. The `_Default` bucket is global and must not receive application logs | Free tier (50 GiB) | $0 | 0 | Claude Code (ask) |
| Arize Phoenix on Cloud Run `phoenix`, `australia-southeast1`, 1 vCPU / 1 GiB, min 1; Postgres database `phoenix` on the existing Cloud SQL instance | **Planned later, not now.** Trace UI added only via an ADR that names a debugging need Logs Explorer can't meet. OTel-native, so adding it is config only | Always-on 1 instance | ~$10–15 | 3+, by ADR | Claude Code (ask) |
| Workload Identity Federation: pool `github`, OIDC provider `github-provider`, both `global`; CI service account `compliance-rag-ci` (`logging.logWriter` + `logging.viewer` project-wide, plus a bucket-scoped `storage.objectViewer` on `<project>-corpus` conditioned to the `reranker/` prefix only — issue #14) | Keyless GitHub Actions to GCP auth. The only way to reach the Phase 0 trace criterion without a service-account key, which is hard-denied. The provider's attribute condition — pinned in `ci/wif-provider-flags.yaml` — is the security boundary. Storage access is deliberately narrow: CI can read the converted reranker artifact to re-verify its fidelity, and nothing else in the bucket (no clause text, no other tenant's objects) | Free | $0 | 0 (WIF); 1 (storage grant) | Claude Code (ask) |
| Workload Identity Federation, same pool/provider; **second, narrower** CI service account `compliance-rag-corpus-ci`, for future corpus-ingest and eval jobs (issue #10) | Exactly two bindings: `roles/logging.logWriter` project-wide (write-only) and `roles/iam.workloadIdentityUser` on itself for the same repo's `principalSet`. Deliberately **cannot read any log**, including `_Default` — no `logging.viewer`, no `_AllLogs` `viewAccessor` on either `compliance-rag-logs` or `_Default` (both of which `compliance-rag-ci` holds). No Storage, Vertex, Secret Manager or Cloud SQL role. Exists so a future ingest/eval job never runs as an identity that can read `_Default`, whatever else it needs | Free | $0 | 1 | Claude Code (ask) |
| Cloud Monitoring | Alerts, uptime | Free tier | $0 | 1 | Claude Code |
| Cloud Trace | Latency waterfalls only, metadata attributes only. **Regional storage unverified** — never receives prompts, chunks or answers until Google's data-residency docs confirm `australia-southeast1` storage | Free tier | $0 | 1, metadata-only | Claude Code |
| Secret Manager | Credentials | ≤6 secrets free | $0 | 3 | Claude Code (ask) |
| Cloud Run service `compliance-rag-api`, `australia-southeast1`, min instances 0, max 2, 1 vCPU, 1 GiB | API + streaming chat | Scale-to-zero | $0–8 | 3 | Claude Code (ask) |
| Cloud Run jobs (`ingest`, `eval`, `feed-poll`) | Batch work | Scale-to-zero | in above | 1 | Claude Code (ask) |
| Cloud Build + Artifact Registry | Images | Free tier | $0 | 3 | Claude Code (ask) |
| Cloud SQL Postgres `db-f1-micro`, `australia-southeast1`, 10 GB SSD, no HA, no read replica | Ledger, chat history, users | Shared-core | ~$9–10 | 3 | Claude Code (ask) |
| Cloud Scheduler (≤3 jobs) | Feed poll, keep-alive, nightly eval | Free | $0 | 4 | Claude Code (ask) |
| Cloud Workflows | Loop A orchestration | Free tier | $0 | 4 | Claude Code (ask) |
| Firebase Authentication | Login | Free | $0 | 3 | Human enables project; Claude Code configures |
| Firebase Hosting **or** Cloudflare Pages | Frontend | Free | $0 | 3 | Claude Code (ask) |
| Cloud Billing budget + Pub/Sub topic `budget-alerts` | Ceiling enforcement | Free | $0 | 1 | Human creates budget; Claude Code wires topic (ask) |
| Brave Search API **or** Tavily | Web fallback, external tier only | Free | $0–5 | 3 | Human |
| Document AI OCR | Scanned PDFs only | Per page | $0 unless used | 3, on demand | Claude Code (ask per batch) |

## Explicitly forbidden

These were evaluated and rejected. Do not use them, even "just to test".

| Service | Why |
|---|---|
| Vertex AI Vector Search | Index nodes bill hourly; ~$400–800/mo idle |
| Vertex AI Search / Agent Builder datastore | Black box on retrieval channels; conflicts with the visibility requirement |
| Spanner, Spanner Graph, AlloyDB | Minimum billing exceeds budget; GQL not Cypher |
| Neptune, OpenSearch, Bedrock | Wrong cloud for this build |
| Any GPU, any always-on VM, any Cloud SQL tier above `db-f1-micro`, HA or replicas | Budget |
| Neo4j Aura Professional, Qdrant paid tiers | Not in build phase. Residency upgrade is a Phase 5 decision |
| Langfuse Cloud (US/EU) | No Australian region. Not used at any phase, including with synthetic data — one sink, one region, from day one |
| Self-hosted Langfuse v3 (ClickHouse + Redis + VM) | Possible but unlikely later alternative to Phoenix: ~$25–40/mo and four components to operate. Only if prompt management or annotation queues become a demonstrated need, and only by ADR |
| Any error reporter, analytics or telemetry SaaS without an AU region (Sentry US/EU, PostHog Cloud, etc.) | Telemetry follows the same residency rule as data |
| Custom Vertex AI Prediction endpoint for the reranker | Bills a minimum replica hourly (~$50–70/mo idle) for the same model Cloud Run runs at ~$0 |
| Cohere, Voyage, Jina rerank APIs | Offshore; query and candidate chunks would leave Australia |
| LLM-based reranking (Gemini/Claude listwise) as the default path | Non-deterministic, slower, 10–20× the cost; permitted only as an eval baseline |
| Fine-tuning, LoRA, distillation | Out of scope by design |
| Gemini for generation | Allowed only as an explicit A/B in the eval harness, never as the default |

## Fallbacks that do not need approval

| If | Then |
|---|---|
| Reranker cold start breaches the p95 budget in eval | Raise `reranker` to min instances 1 — a separate billable ask (~$8–15/mo) |
| ColBERT rescoring beats the cross-encoder on the golden set | Make it the default and delete the `reranker` service — record as a `decision` in BUILD_LOG |
| Qdrant free tier unavailable in AU | Nearest GCP region; log as residency item |
