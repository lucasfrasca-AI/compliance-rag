# Compliance RAG

A compliance tool for corporates: check proposals and documents against the company's own policies and against law and frameworks, with every citation verbatim, versioned and clickable to the clause. Hybrid graph RAG on GCP `australia-southeast1`: Qdrant (dense + BM25 + RRF), Neo4j (Cypher, bitemporal), a self-hosted cross-encoder reranker on Cloud Run, Claude on Vertex.

## Read before acting

- `CONTEXT.md` — the vocabulary. Use it exactly.
- `DECISIONS.md` — sixteen locked decisions (D-01…D-16). Don't build around them.
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
- When something in the plan turns out wrong, say so in chat and open an issue; don't silently route around it.

### Ticket risk classes (Phase 1, effective 2026-09-23)

Every open ticket is one of three risk classes, which sets how much round-tripping with the human it needs. A ticket not yet classified defaults to high risk.

- **High** — one PR each, stop and wait for the human's merge before continuing to the next ticket. Currently: #10, #17, #19, #22, #23, #27, #29, #30, #31. Also anything, regardless of this list, that writes to a real store, spends money, touches the citation path, or changes `CONTEXT.md` / `DECISIONS.md`.
- **Medium** — one PR each, but chained: open the PR, wait for CI, and if it's green start the next ticket without waiting for the merge itself. Currently: #6, #13, #14, #18, #20, #21, #24, #26, #32.
- **Low** — batch several into one PR, reviewed once. Currently: #5, #7, #12, and any doc-only or fixture-only work.

Lanes sequence the work; a lane's next ticket never starts while its predecessor's CI is red.

- **Lane A** (law corpus, sequential, high-risk gates): #17 → #18 → #6 → #21 → #19. Stop for the human after #17 and after #19; #18, #6, #21 chain between them per the medium-risk rule.
- **Lane B** (low risk, one PR): #5 (router regex) + #7 (display form) + #12 (ISO control IDs and descriptors), batched.
- **Lane C** (policy side): #11 (synthetic policy set + conflict matrix) its own PR, then #20 (policy parser) chained.

Work order: Lane B, then Lane C, then Lane A.

**Stop and wait, no exceptions**, regardless of risk class or lane, when: CI goes red on any PR; a billable approval would be needed; a decision would change `CONTEXT.md`, `DECISIONS.md`, `PHASES.md` or `SERVICES.md`; a ticket's acceptance criteria are ambiguous or contradict the spec; or a scope increase is about to be absorbed rather than re-planned through the ticket.

Every PR still carries: the acceptance-criteria-to-test-name table, confirmation that Postgres-dependent tests actually ran in CI (passed, not skipped), and the two-axis code review. When work halts for any reason above, report back as one list of PRs ready to merge, one-line summary each, for batch review — not a running narration per PR.

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

## Corpus rules

- `corpus/instruments/` — raw sources and parser output, one folder per instrument version. Never hand-edit parser output.
- `corpus/mappings/` — hand-authored YAML: obligations, conditions, crosswalks. Changes only via PR. Every mapping carries `author`, `reviewed_by`, `date`, `source_clause_ids`.
- Never generate a clause, obligation or mapping with an LLM. Proposing a tenant policy-to-law mapping for human approval is the single exception, and the proposal is stored as `status: proposed` until approved.

## Definition of done for a phase

The exit criteria in `PHASES.md` are met by passing tests or checked-in artefacts, `BUILD_LOG.md` has the phase's entries, `/handoff` is written to `docs/handoffs/`, and the "Current phase" line in `PHASES.md` is updated in the same commit.
