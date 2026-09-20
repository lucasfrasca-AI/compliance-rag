# BUILD LOG

Append-only. One line per event. Newest at the bottom. Claude Code writes entries; the human reviews them at each phase exit.

Entry format:

```
YYYY-MM-DD | <type> | <phase> | <what> | <where/region> | <est $/mo> | <why / ref>
```

Types: `service` (billable resource created or enabled), `free` (free-tier resource created), `change` (notable code or schema change), `decision` (ADR added or superseded), `residency` (anything that affects data location), `cost` (measured cost checkpoint), `incident` (something broke or was rolled back).

Running cost estimate is recomputed at every `cost` entry and stated in the entry.

## Log

2026-09-17 | change | 0 | Kickoff kit committed (CLAUDE.md, SERVICES.md, PHASES.md, DECISIONS.md, CONTEXT.md, hooks) | repo | $0 | project start
2026-09-17 | residency | 0 | Vertex Ranking API confirmed unavailable in australia-southeast1; reranker will be a self-hosted cross-encoder on Cloud Run Sydney, ColBERT-in-Qdrant as A/B; Ranking API CI-only in us-central1 | australia-southeast1 | $0–15 | D-15
2026-09-17 | free | 0 | Qdrant Cloud free tier confirmed available in australia-southeast1 | australia-southeast1 | $0 | SETUP.md §4
2026-09-17 | decision | 0 | Telemetry: single sink = Cloud Logging regional bucket in Sydney via OpenTelemetry; no Langfuse Cloud at any phase; trace UI later by ADR only (Phoenix planned, self-hosted Langfuse unlikely) | australia-southeast1 | $0 | D-16
2026-09-17 | free | 0 | GCP project created (number 423977975910), region australia-southeast1; USD 200 budget with 50/75/90/100% alerts | australia-southeast1 | $0 | SETUP.md §2
2026-09-17 | service | 0 | Claude (Sonnet-class) enabled in Vertex Model Garden; model ID captured in .env | australia-southeast1 | usage-billed, $30–80 est. from Phase 1 | SERVICES.md
2026-09-17 | free | 0 | Neo4j AuraDB Free instance compliance-rag-neo (ID 5b790f49) created; credentials captured | offshore (Aura Free has no AU region) — public law only | $0 | SETUP.md §3, D-09
2026-09-17 | free | 0 | Qdrant Cloud Free cluster compliance-rag-quad created (1 GB / 0.5 vCPU / 4 GiB) | australia-southeast1 | $0 | SETUP.md §4
2026-09-20 | change | 0 | Billable guard rebuilt to fail closed. PreToolUse `ask` decisions are not enforced in this session type — verified live, two billable commands ran straight through an `ask` and were stopped only by their own argument errors. `deny` is enforced, so every billable command is now denied unless an unused line in `.claude/billable-approvals.txt` matches it; the guard marks the line `# used` on first use. Claude is denied Edit/Write/Bash on that file, so only the human grants approvals | repo | $0 | CLAUDE.md billable protocol step 3, SERVICES.md "Protocol for anything billable"
2026-09-20 | change | 0 | Repo scaffolded, skills plugin installed, hooks verified, CI added | repo | $0 | prompts/00-setup-prompt.md
2026-09-20 | decision | 0 | ADRs written for every row of DECISIONS.md, D-01 to D-16, plus ADR-0001 (record architecture decisions). The setup prompt's "D-01…D-14" was stale against a sixteen-row register and was corrected | repo | $0 | docs/adr/, prompts/00-setup-prompt.md
