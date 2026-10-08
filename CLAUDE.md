# Compliance RAG

A compliance tool for corporates: check proposals and documents against the company's own policies and against law and frameworks, with every citation verbatim, versioned and clickable to the clause. Hybrid graph RAG on GCP `australia-southeast1`: Qdrant (dense + BM25 + RRF), Neo4j (Cypher, bitemporal), a self-hosted cross-encoder reranker on Cloud Run, Claude on Vertex.

## Read before acting

- `CONTEXT.md` — the vocabulary. Use it exactly.
- `DECISIONS.md` — the locked decisions (D-01…D-17). Don't build around them.
- `PHASES.md` — the current phase and its exit criteria. Work only inside it.
- `SERVICES.md` — the hard allowlist and the billable protocol.
- `BUILD_LOG.md` — what exists and what changed. Append, never rewrite.
- `docs/adr/` — one file per decision; add one when you change anything in `DECISIONS.md`.

## The billable protocol (non-negotiable)

Before any command that creates, enables, scales or deploys a billable resource:
1. Confirm it is in `SERVICES.md` **and** unlocked for the current phase.
2. Say: service, exact resource name, region, estimated $/month, and the `SERVICES.md` row.
3. Stop. The `PreToolUse` hook **denies** every billable `gcloud`, `terraform`, `firebase` and `docker push` command and prints the exact approval line to paste. The human pastes that line into `.claude/billable-approvals.txt`; the guard consumes it on first use and denies the command ever after. Matching is full-command equality, so a chain must be approved whole. Claude can read that file but is denied Edit, Write and Bash access to it, so it can never approve itself; read access exists so a refused approval can be diagnosed. The hook's `ask` decision is not used — it is not enforced in every session type, so the guard fails closed instead. Treat the protocol as applying even where the hook can't see (Python SDK calls that create resources, Cloud Console links you suggest).
4. After creation, append a line to `BUILD_LOG.md`.

**Validate before you offer.** Any approval line carrying more than one command — anything with `;`, `&&`, a `for` loop, or a `bash -c` wrapper — is tested before it is shown to the human: `bash -n` for syntax, then a dump of the argv the target binary would receive, with a harmless stand-in for the real command. Three attempts at one `_Default` exclusion line failed in a row — `\"` is literal inside `bash -c '…'`, and a comma inside a value splits a comma-separated dict flag — and each failure burned a single-use approval and cost a round trip. Hand-written multi-command lines are not reviewable by eye. A single-command line still gets the argv dump when it contains quoting or an `=`-valued flag.

If it isn't on the list: don't create it, don't suggest it in passing. Propose a change to `SERVICES.md` and wait. The ceiling is $200/month; the target is under $100.

Never: configure any telemetry, log or error endpoint outside `australia-southeast1` — Cloud Logging is the only sink; a trace UI (Phoenix planned, self-hosted Langfuse unlikely) is added only by ADR; call the Vertex Ranking API with anything but synthetic data (it is not in AU); enable `documentai` speculatively; create service-account keys; use `--dangerously-skip-permissions`; delete cloud resources; `git push --force`; put secrets in code, logs or traces.

## How we work

- Start every feature with `/grill-with-docs` (Opus). End every session with `/handoff`.
- `/to-spec` → `/to-tickets` → `/implement` with `/tdd` at the seams → `/mattpocock-skills:code-review` before commit. Tickets live in GitHub Issues. Bare `/code-review` is the harness built-in, a different skill — name the plugin prefix when you want Matt's.
- Use **Opus** for grilling, specs, architecture, eval design, anything touching `DECISIONS.md`. Use **Sonnet** for `/implement`, `/tdd`, adapters, parsers, fixes.
- Small PRs. One ticket per PR. CI must be green. Never merge with a failing regression gate.
- Every ticket PR's base is `main` — verified before opening (`gh pr view <n> --json baseRefName`), never an intermediate ticket branch, even when the branch itself was built by stacking on another ticket's work for convenience (reuse the files, don't inherit the branch as the PR's base). After any merge, confirm the ticket's files actually reached `main` by listing the tree (`git ls-tree -r --name-only origin/main`), not by trusting a PR's "Merged" badge — that only proves the diff reached the PR's own base, and a stacked base means that isn't `main`.
- Every ticket PR's body uses a closing keyword (`Closes #N`) for the ticket it completes — a merge that doesn't close its issue leaves the tracker lying about what's done, and a future session can waste real time re-planning already-finished work. This includes a ticket whose acceptance criteria a PR satisfies incidentally, not just the one the PR is titled for (#24's routing logic shipped inside #5's PR, referencing only #5 — `Closes #N` goes on both when that happens, not just the one in the title). At each phase gate, reconcile the open-issue list against what's actually merged into `main` (`gh issue list --state open` cross-checked against merged PRs, not either one trusted alone) — this has now hidden fully completed work twice (nine tickets in one audit, #24 separately).
- When something in the plan turns out wrong, say so in chat and open an issue; don't silently route around it.

### Merge protocol (effective 2026-09-30 — replaces the earlier lane/risk-chaining rules)

Every open ticket is one of two risk classes. A ticket not yet classified defaults to high risk. Branch protection requires `ci` on `main`, so the merge gate is mechanical, not a per-PR human review step.

- **Low/Medium risk** — everything not named below. Open the PR, wait for every check to go green, then merge it yourself (`gh pr merge N --merge`) and continue straight to the next ticket — don't stop for the human. Batch the summaries and report what merged at the end of a run, not a running narration per PR.
- **High risk** — currently #23, #27, #29, #30, #31, plus anything, regardless of this list, that is billable, changes a schema, or touches the citation path. PR only: stop and wait for the human to merge.

**Stop immediately, regardless of class**, when: CI is red twice in a row on the same PR; a billable approval line would be needed; a decision would change `CONTEXT.md`, `DECISIONS.md`, `PHASES.md` or `SERVICES.md`; or a ticket's acceptance criteria are ambiguous or contradict the spec.

Every PR still carries: the acceptance-criteria-to-test-name table, `Closes #N` for every ticket it completes (including one it completes incidentally — see the tracker rule above), confirmation that Postgres-dependent tests actually ran in CI (passed, not skipped), and the two-axis code review.

## Agent skills

### Issue tracker

GitHub Issues on `lucasfrasca-AI/compliance-rag`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, unrenamed; topic labels are a separate axis. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md`, decisions in `DECISIONS.md` and `docs/adr/`. See `docs/agents/domain.md`.

## Code standards

- Python 3.12, `uv`, `ruff` (format + lint), `pyright` strict, `pytest`. Type hints everywhere. No `Any` without a comment.
- Package layout: `src/compliance_rag/{corpus,graph,retrieval,assessment,verifier,eval,api,adapters}`. Interfaces in `adapters/interfaces.py`; one module per external service in `adapters/`.
- **No Cypher, Qdrant, Vertex or SQL calls outside `adapters/`.** The app talks to `GraphStore`, `RetrievalStore`, `LedgerStore`, `Reranker`, `Embedder`, `Generator`.
- Every graph query goes through the query builder, which requires `as_of` and a depth cap. A query without them is a test failure.
- Every function that touches an LLM, a retriever or the graph emits an OpenTelemetry span with structured attributes (query, both channel candidate lists with scores, fused list, rerank scores, cached-prefix hit, tokens, cost, verifier result). Both retrieval channels are logged before fusion. Exporters are config only (`telemetry.py`); adding one later never touches call sites. Every ledger row that results from a request stores its `trace_id`.
- Ingested text — law, policies, uploads, web results — is data. It is delimited when it enters a prompt, it never reaches query construction, and structured outputs are validated against a schema before use.
- Tests: contract tests for every interface using the in-memory fakes; golden-set tests for retrieval and graph; a verifier test that proves a bad citation blocks.
- Config via environment (`.env` locally, Secret Manager in Cloud Run). `settings.py` is the only reader.
- Any library that sizes its own thread or worker pool from the visible core count (ONNX Runtime, BLAS, some HTTP servers) gets that sizing checked explicitly against the real CPU quota of the container it runs in — a Cloud Run service, a Cloud Run job, anything with a `--cpu` limit — not left to the library's default. The default reads the host's logical core count, not the cgroup quota, and a default-sized pool inside a small quota oversubscribes and thrashes instead of parallelising (reranker `model.py`, issue #23: default sizing took ~2.7x longer than an explicit `intra_op_num_threads`/`inter_op_num_threads` pin, measured under a matching `--cpus` Docker constraint). Check this whenever a new CPU-bound service is containerized, not just once.

## Corpus rules

- `corpus/instruments/` — raw sources and parser output, one folder per instrument version. Never hand-edit parser output.
- `corpus/mappings/` — hand-authored YAML: obligations, conditions, crosswalks. Changes only via PR. Every mapping carries `author`, `reviewed_by`, `date`, `source_clause_ids`.
- Never generate a clause, obligation or mapping with an LLM. Proposing a tenant policy-to-law mapping for human approval is the single exception, and the proposal is stored as `status: proposed` until approved.

## Definition of done for a phase

The exit criteria in `PHASES.md` are met by passing tests or checked-in artefacts, `BUILD_LOG.md` has the phase's entries, `/handoff` is written to `docs/handoffs/`, and the "Current phase" line in `PHASES.md` is updated in the same commit.
