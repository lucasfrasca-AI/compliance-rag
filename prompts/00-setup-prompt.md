# Setup prompt — paste as the first message in Claude Code (Opus)

You are setting up this repository for the Compliance RAG project. Read `CLAUDE.md`, `CONTEXT.md`, `DECISIONS.md`, `PHASES.md` and `SERVICES.md` in full before doing anything. This session creates **nothing billable** and touches **no cloud resources**. If any step below would require one, stop and tell me.

Work through these steps in order. After each, print a one-line status. Stop and ask if anything is ambiguous.

## 1. Tooling check

Run and report versions: `node -v`, `python3 --version`, `uv --version`, `gcloud --version`, `gh --version`, `docker --version`, `claude --version`. Do not install anything system-wide; list what is missing and stop if `uv`, `gcloud` or `gh` is absent.

## 2. Skills plugin

Install the skills plugin from Claude Code's marketplace: `claude plugins install mattpocock-skills` (or `/plugin install mattpocock-skills` from the session). Do **not** also run `npx skills add` — installing both duplicates every skill.

Then run `/setup-matt-pocock-skills` with these answers:
- Issue tracker: **GitHub Issues** (this repo)
- Triage labels: `bug`, `feature`, `corpus`, `eval`, `later`, `blocked`
- Docs location: `docs/` (ADRs in `docs/adr/`, handoffs in `docs/handoffs/`, research in `docs/research/`)

Confirm the following skills are available: `grill-with-docs`, `to-spec`, `to-tickets`, `implement`, `tdd`, `code-review`, `wizard`, `research`, `handoff`, `diagnosing-bugs`, `domain-modeling`, `writing-for-agents`.

## 3. Hooks and permissions

`.claude/settings.json` and `.claude/hooks/billable_guard.py` already exist. Verify the hook loads: run `claude hooks` or open `/hooks` and confirm the `PreToolUse` → `Bash` entry is listed. Then test it from this session by *attempting* `gcloud run services list` (should run silently) and `echo test && gcloud run deploy fake --region australia-southeast1` (should prompt you with the billable message — **decline the prompt**). Report both results.

Note: subagents may not inherit project hooks. Do not delegate any command that could be billable to a subagent; run those in the main session.

## 4. Repository scaffold

Create, commit on branch `chore/scaffold`, and open a PR:

```
.gitignore                 (already present in the kit — verify it covers python, uv, .env, .venv, .claude/settings.local.json, corpus/instruments/**/raw/)
pyproject.toml             (python 3.12, uv, ruff, pyright strict, pytest, deps below)
src/compliance_rag/__init__.py
src/compliance_rag/settings.py         (single env reader; pydantic-settings)
src/compliance_rag/adapters/interfaces.py   (GraphStore, RetrievalStore, LedgerStore, Reranker, Embedder, Generator — abstract)
src/compliance_rag/adapters/fakes.py        (in-memory implementations for tests)
src/compliance_rag/telemetry.py             (OpenTelemetry bootstrap → Cloud Logging structured-log exporter; refuses any endpoint outside australia-southeast1; sinks are config)
src/compliance_rag/corpus/models.py         (ClauseRecord, InstrumentVersion, stable ID scheme from CONTEXT.md)
tests/contracts/test_interfaces.py          (one contract test per interface, run against fakes)
tests/test_settings.py
db/ledger/0001_init.sql                     (schema from PHASES.md Phase 0, every table has tenant_id)
db/neo4j/schema.cypher                      (constraints + indexes only; stable_id unique per label; bitemporal props documented)
corpus/instruments/.gitkeep
corpus/mappings/README.md                   (YAML schema for obligations, conditions, crosswalks; required fields author, reviewed_by, date, source_clause_ids)
docs/adr/0001-record-architecture-decisions.md
docs/adr/                                   (one ADR per row of DECISIONS.md, D-01…D-16, short: context / decision / consequences)
docs/handoffs/.gitkeep
docs/research/.gitkeep
docker-compose.yml                          (postgres:16 only, for Phase 0–1 ledger)
.github/workflows/ci.yml                    (uv sync, ruff, pyright, pytest; name the check "ci")
```

Dependencies (pin majors): `fastapi`, `uvicorn`, `pydantic`, `pydantic-settings`, `qdrant-client`, `neo4j`, `anthropic[vertex]`, `google-cloud-aiplatform`, `google-cloud-storage`, `psycopg[binary]`, `pymupdf`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`, `google-cloud-logging`, `httpx`. Dev: `ruff`, `pyright`, `pytest`, `pytest-asyncio`.

Do not write any adapter that talks to a real service yet. Fakes only.

## 5. Vocabulary pass

Run `/grill-with-docs` on `CONTEXT.md` for **no more than 10 questions**, targeted at anything ambiguous in the Phase 0 deliverables. Update `CONTEXT.md` inline. Do not add concepts from later phases.

## 6. Close

Append to `BUILD_LOG.md`:
`<date> | change | 0 | Repo scaffolded, skills plugin installed, hooks verified, CI added | repo | $0 | prompts/00-setup-prompt.md`

Then run `/handoff` to `docs/handoffs/0000-setup.md` and tell me the PR number.
