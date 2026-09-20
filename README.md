# Compliance RAG — kickoff kit

Copy the contents of this folder into the root of a new, empty git repo. Then work through the files in this order.

| Order | File | Who acts | What it does |
|---|---|---|---|
| 1 | `SETUP.md` | You | Accounts, CLIs, and the manual console settings for GCP, Neo4j Aura, Qdrant, GitHub, VS Code — with a status block for what's already done |
| 2 | `prompts/00-setup-prompt.md` | Claude Code | Installs the skills plugin, wires hooks and permissions, scaffolds the repo |
| 3 | `prompts/01-kickoff-prompt.md` | Claude Code | Verifies every connection and account, creates nothing billable, writes the first build-log entry |
| — | `CLAUDE.md` | Claude Code reads on every session | Project rules, the billable-service protocol, what to read before acting |
| — | `SERVICES.md` | Both | The hard allowlist. If a service isn't here, Claude Code may not create it |
| — | `PHASES.md` | Both | Five phases with entry gates and exit criteria; the current phase is marked |
| — | `DECISIONS.md` | Both | Locked architecture decisions with the reason each is locked |
| — | `CONTEXT.md` | Both | Shared vocabulary. Read before writing code or docs |
| — | `BUILD_LOG.md` | Claude Code appends, you review | Every service created, every notable change, one line each |
| — | `.claude/settings.json`, `.claude/hooks/billable_guard.py` | Harness | Permission rules and the hook that forces an ask before anything billable |
| — | `.env.example` | You | Every credential the project needs, none of the values |
| — | `.env` | You | Pre-filled with your instance names and project; add the secrets. **Gitignored — never commit it** |
| — | `.gitignore` | Harness | Keeps `.env`, `.venv`, local settings and raw corpus out of git from the first commit |

Model guidance: use **Opus** for `/grill-with-docs`, `/to-spec`, architecture and eval design; use **Sonnet** for `/implement`, `/tdd`, and routine changes. `CLAUDE.md` restates this so the agent applies it itself.
