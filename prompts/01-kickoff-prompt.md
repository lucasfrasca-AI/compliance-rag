# Kickoff prompt — paste after the setup PR is merged (Sonnet is fine)

Verify that every account, credential and connection this project needs is in place. This session **creates nothing billable and enables nothing**. Read-only calls and free-tier writes to a throwaway test collection/label are allowed; delete them before you finish. If a check needs something that isn't there, report it and continue — do not try to fix account-level problems yourself.

Produce a single Markdown report at `docs/handoffs/0001-kickoff-checks.md` with one row per check: **check / result (pass, fail, skipped) / detail / what the human must do**. Then summarise in chat: number passing, and the exact list of human actions.

Read `.env` only via `settings.py` (never print a secret; print the last 4 characters at most).

## A. Local and repo

1. `uv sync` succeeds; `uv run pytest` green; `uv run ruff check .` and `uv run pyright` clean.
2. `git remote -v` shows the GitHub repo; `gh repo view` succeeds; `main` is protected and requires the `ci` check (`gh api repos/{owner}/{repo}/branches/main/protection`).
3. `docker compose up -d postgres` starts; apply `db/ledger/0001_init.sql`; confirm every table has a `tenant_id` column; `docker compose down`.
4. `.env` has every key from `.env.example` (report missing keys by name).

## B. Google Cloud (read-only)

5. `gcloud config get-value project` matches `GCP_PROJECT_ID` (a string, not the number) and `gcloud projects describe $GCP_PROJECT_ID --format='value(projectNumber)'` returns `GCP_PROJECT_NUMBER`; `gcloud config get-value run/region` is `australia-southeast1`.
6. `gcloud auth list` shows an active account; `gcloud auth application-default print-access-token` returns a token.
7. `gcloud services list --enabled` — report which of these are enabled: `run`, `cloudbuild`, `artifactregistry`, `sqladmin`, `aiplatform`, `secretmanager`, `cloudscheduler`, `workflows`, `storage`, `iam`, `cloudtrace`, `logging`. Report `documentai` as **should be disabled**.
8. Billing: `gcloud billing projects describe <project>` shows a linked account. `gcloud billing budgets list --billing-account=<id>` shows a USD 200 budget with alerts (if the command needs a permission you lack, mark skipped and ask the human to confirm in console).
9. Service account `compliance-rag-runtime@…` exists; list its roles; confirm it has **no** user-managed keys (`gcloud iam service-accounts keys list --managed-by=user`).
10. Claude on Vertex: make one minimal `messages.create` call with `max_tokens=8` in `australia-southeast1` using `anthropic[vertex]` and the model ID in `.env`. Report the model ID, region, latency, and the `usage` block. (This is a per-token call costing a fraction of a cent; it is the one intentional spend in this session.)
11. Vertex embeddings: embed the string `"APP 11.1"` with `gemini-embedding-001` at `output_dimensionality=1536`; confirm vector length 1536.
12. Vertex Ranking API (CI-only): rank three short strings against `"encryption at rest"` in `us-central1` to prove credentials. Record that it is **not** available in `australia-southeast1` and is gated to `TENANT_ID=dev-tenant`.
12a. Reranker prerequisites: confirm `onnxruntime` and `optimum` install under `uv`; download `BAAI/bge-reranker-v2-m3` tokenizer + ONNX export to the local HF cache (no cloud resources); score three (query, document) pairs locally and report latency on this machine. This proves the model path before the Cloud Run service exists.
12b. Artifact Registry: `gcloud artifacts repositories list --location=australia-southeast1` — confirm no repository exists yet (it is a Phase 1 ask).
13. Cloud Storage: `gcloud storage buckets list` — confirm the corpus bucket does **not** exist yet (it is a Phase 1 ask).

## C. Neo4j AuraDB Free

14. Connect with the `neo4j` driver using `NEO4J_URI`/`NEO4J_USERNAME`/`NEO4J_PASSWORD`. If the instance is paused, report it and tell the human to resume it in the console.
15. Confirm the URI host matches `NEO4J_INSTANCE_ID` (`5b790f49`). `CALL dbms.components()` → version. `SHOW CONSTRAINTS` → count. Create a node `(:KickoffCheck {stable_id:'kickoff', recorded_at:datetime()})`, read it back, delete it.
16. Report node and relationship counts (`MATCH (n) RETURN count(n)`, `MATCH ()-[r]->() RETURN count(r)`) against the free-tier limits (200k / 400k).

## D. Qdrant Cloud

17. Connect with `qdrant-client` using `QDRANT_URL`/`QDRANT_API_KEY`; `get_collections()` succeeds. Report the cluster region from the URL and confirm it is `australia-southeast1`; note the cluster name in `.env` (`QDRANT_CLUSTER_NAME`) for the report.
18. Create a throwaway collection `kickoff_check` with a 1536-dim cosine dense vector, a sparse vector named `bm25`, and a multivector named `colbert` (128 dims, MaxSim, HNSW `m=0`); insert one point using cloud inference (`Document(text=..., model="Qdrant/bm25")`) for the sparse side and a zero vector for the dense side; run one `query_points` with a prefetch on each channel and RRF fusion; then delete the collection. Also insert a `colbert` multivector via cloud inference (`Document(text=..., model="colbert-ir/colbertv2.0")`) and run one query that prefetches on `bm25` then rescores on `colbert`. If cloud inference is unavailable for either model, record which (**"client-side fastembed path applies"** for BM25; **"ColBERT A/B unavailable on free tier"** for ColBERT) and complete the test with hand-built vectors.
19. Create and delete a payload index on `clause_id` (keyword) to prove permissions.

## E. OpenTelemetry and Cloud Logging

20. Run the hello-world traced pipeline from Phase 0 (`uv run python -m compliance_rag.telemetry --selftest`) with `OTEL_SINKS=stdout` and confirm structured span JSON with a `trace_id` prints. Then `gcloud logging buckets list --location=australia-southeast1` — confirm no regional bucket exists yet (it is the first Phase 0 billable ask, $0).
20a. Startup assertion: set `OTEL_EXPORTER_OTLP_ENDPOINT=https://example.com` temporarily and run the selftest; confirm the app refuses to start because the endpoint is outside `australia-southeast1`. Unset it.

## F. Guardrails

21. Confirm the `PreToolUse` hook is active by attempting `gcloud storage buckets create gs://kickoff-should-not-exist --location=australia-southeast1` and **declining** the prompt. Record that the prompt fired. Then attempt `gcloud spanner instances list` and record that it was denied.
22. Confirm `.env` is gitignored and not in `git ls-files`.

## G. Close

23. Append to `BUILD_LOG.md`: `<date> | change | 0 | Kickoff checks: <n>/24 pass; see docs/handoffs/0001-kickoff-checks.md | — | $0 | prompts/01-kickoff-prompt.md`. Add a `residency` line: Qdrant in `australia-southeast1`; reranker to be self-hosted in Sydney; Ranking API CI-only.
24. If all of A–F pass, state the first Phase 0 ticket you would open and stop. Do not start it.
