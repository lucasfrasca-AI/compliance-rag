# SETUP — steps only you can do

Everything here needs a browser login, a credit card, or a one-time secret that only you should see. Claude Code does everything else. Tick each box; the kickoff prompt will check them.

## Status as of 2026-09-17

Done:
- GCP project created (number `423977975910` — put the **project ID string**, not the number, in `.env` and `gcloud config`; `gcloud projects list` shows both), region `australia-southeast1`
- $200 budget with 50/75/90/100% alerts
- Claude (Sonnet-class) enabled in Vertex Model Garden; model ID captured
- Neo4j AuraDB Free instance `compliance-rag-neo` (ID `5b790f49`); URI, username and one-time password captured
- Qdrant Cloud Free cluster `compliance-rag-quad` in GCP `australia-southeast1` (1 GB RAM, 0.5 vCPU, 4 GiB disk); URL and API key captured. *(Name appears once as `compliance-rag-cluster` in notes — the console name wins; put whichever is real in `.env` as `QDRANT_CLUSTER_NAME`.)*

Remaining: §0 local tools, §1 GitHub, §2 API enablement + ADC login + runtime service account, §4 Qdrant inference-model check (ColBERT only — BM25 is client-side, issue #22), §7 fill `.env`, §8 open in VS Code.


## 0. Local machine

- [ ] Node.js 20+ (`node -v`), Python 3.12+ (`python3 --version`), `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- [ ] `jq` (used by the billable guard hook)
- [ ] Docker Desktop (local Postgres for Phase 0; Cloud Build does production images so this is optional later)
- [ ] `gcloud` CLI installed and updated (`gcloud components update`)
- [ ] `gh` CLI installed, `gh auth login` completed
- [ ] VS Code with the **Claude Code** extension from the marketplace, signed in to your Anthropic account
- [ ] Claude Code CLI works in the integrated terminal: `claude --version`

## 1. GitHub

- [ ] Create a **private** repo, e.g. `compliance-rag`. Do not initialise with a README.
- [ ] Clone it, copy this kit into the root, commit as `chore: kickoff kit`.
- [ ] Settings → Branches → protect `main`: require a pull request, require status checks (you'll name the check after Phase 0 creates it).
- [ ] Settings → Actions → allow GitHub Actions.
- [ ] Issues enabled (the skills plugin uses GitHub Issues as the tracker).

## 2. Google Cloud

Region for everything: **`australia-southeast1`**. Pick a project ID you can type, e.g. `compliance-rag-dev`.

- [x] Create the project. Link a billing account. *(done — project number `423977975910`)*
- [x] **Billing → Budgets & alerts**: budget `USD 200`, alert thresholds 50%, 75%, 90% (`$150`), 100%. *(done)* Claude Code will later wire the 90% alert to a Pub/Sub topic that disables Cloud Run.
- [ ] Enable these APIs (console or `gcloud services enable` — the kickoff prompt will do it after asking): `run`, `cloudbuild`, `artifactregistry`, `sqladmin`, `aiplatform`, `secretmanager`, `cloudscheduler`, `workflows`, `storage`, `iam`, `cloudtrace`, `logging`, `documentai` (leave `documentai` off until a scanned PDF appears).
- [x] **Vertex AI → Model Garden → Claude**: Sonnet-class model enabled, terms accepted. *(done)*
- [x] Exact model ID string captured for `.env`. *(done — paste it into `VERTEX_CLAUDE_MODEL`)*
- [ ] Ranking API: confirmed **not** available in `australia-southeast1`. Nothing to set up; the reranker is a Cloud Run service Claude Code builds in Phase 1. The Ranking API is used only by CI in `us-central1` with synthetic data.
- [ ] Artifact Registry: nothing manual — Claude Code creates the `compliance-rag` Docker repository in Phase 1 (after asking) to hold the reranker image.
- [ ] Local auth: `gcloud auth login`, then `gcloud auth application-default login`, then `gcloud config set project <id>` and `gcloud config set run/region australia-southeast1`.
- [ ] Create a service account `compliance-rag-runtime@<project>.iam.gserviceaccount.com`. Grant: Vertex AI User, Cloud SQL Client, Secret Manager Secret Accessor, Storage Object Admin (scoped to the corpus bucket later), Cloud Trace Agent, Logs Writer. **Do not create or download a key** — Cloud Run uses it directly; your laptop uses ADC.

## 3. Neo4j AuraDB Free

- [x] Sign in at console.neo4j.io → New instance → **AuraDB Free**. *(done)*
- [x] Name: `compliance-rag-neo` (instance ID `5b790f49`). *(done)* Cloud/region: whatever Free offers closest to you (Free is not offered in Australia — acceptable during build because the graph holds public law only; see `DECISIONS.md` D-09).
- [x] Credentials captured. *(done)* Aura downloads a credentials file when the instance is created. **It is authoritative for all five values**: copy `NEO4J_INSTANCE_NAME`, `NEO4J_INSTANCE_ID`, `NEO4J_URI`, `NEO4J_USERNAME` and `NEO4J_PASSWORD` from it into `.env`, and set `NEO4J_DATABASE` too. **On Aura Free the username and the database name are both the instance ID** (`5b790f49` here), not `neo4j`. The original kit defaulted both to `neo4j`, which was wrong from day one; it surfaced on 2026-10-11, the first time anything connected to the deployed instance, as an authentication failure that looked like a bad password.
- [x] Keep-alive *(added 2026-10-12, start of Phase 2)*. Aura Free pauses after 3 days idle and deletes after 30. `.github/workflows/aura-keepalive.yml` sends one statement a day to Aura's Query API that MERGEs a throwaway `:KeepAlive` node and DELETEs it in the same statement — a committed write, so it counts as activity whether Aura's idle timer watches reads or writes, and nothing is left behind. It needs two repository secrets, set once by the human (they hold the Aura password, so Claude Code doesn't set them):
  ```
  gh secret set AURA_KEEPALIVE_INSTANCE_ID   # the instance ID, e.g. 5b790f49
  gh secret set AURA_KEEPALIVE_PASSWORD      # from the Aura credentials file
  ```
  Then run it once by hand (`gh workflow run aura-keepalive`) to confirm. A failed run emails you: that is the signal the instance is paused, deleted or its password changed.
- [ ] Nothing else to configure. Constraints, indexes and the bitemporal schema are created by code (`db/neo4j/schema.cypher`, `Neo4jGraph.apply_schema`).

## 4. Qdrant Cloud

- [x] Sign in at cloud.qdrant.io → Create cluster → **Free**. *(done)*
- [x] Provider: **GCP**. Region: **`australia-southeast1`**. *(done — 1 GB RAM, 0.5 vCPU, 4 GiB disk)*
- [x] Name: `compliance-rag-quad`. *(done)*
- [x] API key captured. *(done)* Put `QDRANT_URL` (`https://xxxx.gcp.cloud.qdrant.io:6333`) and `QDRANT_API_KEY` in `.env`.
- [x] Cloud Inference: confirmed available on the cluster (2026-09-30), but not the model issue #26 was written against — the cluster offers `answerdotai/answerai-colbert-small-v1` (96 dims, 300-token context, 33M params, free), not `colbert-ir/colbertv2.0` (128 dims, 512-token context). The smaller window is meaningfully below this repo's 420-token chunk cap (chosen for the cross-encoder's 512-token budget, `CONTEXT.md`: Chunk); see issue #26 for the measured chunk-token distribution and the pending human decision on whether to run #26 with that handicap documented or defer it to #32. The sparse (BM25) channel needs no Cloud Inference check at all: issue #22 made client-side `fastembed` encoding the permanent path, not a fallback, specifically so CI and production run the identical code (SERVICES.md's "Qdrant Cloud" row and BUILD_LOG have the decision).
- [ ] Create the corpus collection once: `uv run python -m scripts.bootstrap_qdrant_collection` (#139; idempotent, prints what it found or made, never the URL or key). Until something deployed does it on start, nothing else creates it, and the `qdrant-keepalive` workflow fails with a 404 naming this command. The schema it creates, which you may want to recognise later: dense vector `1536` dims cosine (embedding model called with `output_dimensionality=1536`), sparse vector named `sparse` via client-side `fastembed` BM25 encoding (`Qdrant/bm25` model, no cloud inference — issue #22), payload indexes on `clause_id`, `tenant_id`, `scope`, `instrument_id`, `version_id` (keyword), `valid_from`, `valid_to` (integer, epoch days) and `withdrawn`, `synthetic` (bool). Law is scoped to Track A's clauses through `clause_id` (ADR-0030); chunks carry no obligation (#129).
- [ ] Free clusters are suspended when idle. If the keep-alive reports "no HTTP answer", resume the cluster in the console.

## 5. Telemetry (nothing manual)

- [ ] Nothing to do. OpenTelemetry → a Cloud Logging regional bucket `compliance-rag-logs` in `australia-southeast1` is the only sink; Claude Code creates the bucket in Phase 0 after asking. There is no Langfuse account to create. A trace UI (Phoenix on Cloud Run Sydney planned; self-hosted Langfuse unlikely) is a later ADR, not a setup step.

## 6. Web search fallback (optional until Phase 3)

- [ ] Brave Search API or Tavily free key → `WEB_SEARCH_API_KEY`.

## 7. Secrets

- [ ] Copy `.env.example` to `.env`, fill it in. `.env` is gitignored. Claude Code reads it locally; in Phase 3 it moves each value into Secret Manager (after asking).

## 8. Open the repo in VS Code

- [ ] Open the repo folder. Open the Claude Code panel. Paste `prompts/00-setup-prompt.md` as your first message.
