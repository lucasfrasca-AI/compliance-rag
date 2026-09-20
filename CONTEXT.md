# CONTEXT — shared language

Use these words exactly, in code, docs, commits and chat. If a concept isn't here and you need it more than once, add it via `/grill-with-docs`.

## Corpus

- **Instrument** — a law, regulation or standard as a whole (Privacy Act 1988, EU AI Act, ISO 27001). Has versions.
- **Instrument version** — one compilation of an instrument. Identified by the register's own ID (compilation number, CELEX, catalogue revision).
- **Clause** — the smallest citable unit of an instrument: an APP sub-clause, an article paragraph, an Annex A control. Has a **stable ID**: `{instrument_id}:{version_id}:{clause_path}`.
- **Canonical text** — a clause's text after canonicalisation (NFC, whitespace, quotes normalised). The verifier compares canonical to canonical. Lives in Cloud Storage. Never edited.
- **Chunk** — a retrieval unit derived from a clause, ≤500 tokens. One chunk belongs to at most one obligation. Chunks live in Qdrant; clauses live in Cloud Storage.
- **Policy** — a company's own document, treated exactly like an instrument: versions, clauses, canonical text.
- **Corpus version** — a git tag over `corpus/` plus the set of instrument versions loaded. Recorded on every assessment.

## Graph

- **Obligation** — a graph node for a clause that imposes a requirement. Not every clause is an obligation (recitals, definitions aren't).
- **Condition** — a node that gates an obligation (turnover > $3M, actor is deployer, jurisdiction is EU). Evaluated against the **profile**.
- **Crosswalk** — a `CROSSWALKS_TO` edge between obligations or controls across instruments, authored in **mapping YAML**.
- **Mapping YAML** — hand-authored files in `corpus/mappings/`, reviewed via PR. The only source of crosswalks and obligation metadata.
- **Bitemporal** — every node carries `valid_from`, `valid_to`, `status`, `recorded_at`. **as_of** is the mandatory date parameter on every traversal.
- **Supersession** — the `SUPERSEDES` edge from a new version node to the old. Nodes are never deleted or mutated.
- **Tenant subgraph** — the small graph of one company's profile (`Organisation`, sectors, jurisdictions, actors, policy clauses). Built per request from the ledger; not persisted during build.

## Retrieval

- **Track A** — graph traversal from profile to applicable obligations. Deterministic, complete, unranked.
- **Track B** — hybrid retrieval over chunks, filtered to Track A's obligation IDs. Probabilistic, ranked.
- **Channel** — one retrieval signal: dense (cosine), sparse (BM25), keyword (exact `clause_id`).
- **Fusion** — RRF over channels inside Qdrant, producing ~50 candidates.
- **Rerank** — second-stage scoring of fused candidates to top 10. Default: the **reranker service** (cross-encoder on Cloud Run, Sydney). Alternative: **ColBERT rescoring** (late-interaction multivectors inside Qdrant).
- **Router** — the single fixed decision: clause-ID regex → keyword channel, skip rerank; else full pipeline.
- **Scope** — which corpus partition a chunk belongs to: `public` (law), `tenant` (company policies), `user` (one person's private additions).

## Assessment

- **Profile** — a tenant's structured facts (jurisdictions, sector, size bands, actor roles, data categories, regulators). Lives in the ledger. Never embedded.
- **Claim** — a statement extracted from an uploaded document ("data will be retained for 7 years"). Provenance inferred until the user confirms.
- **Finding** — one claim against one obligation or policy clause: `consistent` / `conflicts` / `not_addressed` / `ambiguous`, with citations and provenance.
- **Provenance tier** — how a fact entered the system: `parsed` (deterministic), `asserted` (a user said so), `inferred` (extracted from prose), `external` (web fallback, unverified against the corpus).
- **Coverage** — for an assessment, which obligations were assessable, which weren't, and why.
- **Verifier** — the deterministic check that every citation exists in the retrieved or cached set and its span matches canonical text.
- **Repair** — the targeted regeneration of only the failing citation. Never a full regeneration.
- **Evidence** — a confirmed claim or questionnaire answer that satisfies or fails a condition. Documents inform; humans assert.
- **CAG prefix** — the cached prompt prefix: system prompt + obligation digest + tenant policy corpus.

## Loops

- **Loop A** — corpus maintenance: poll → parse → version → diff → PR → load → regression gate.
- **Loop B** — assessment: sign in → upload/ask → claims → Track A → Track B → rerank → generate → verify → answer → record.
- **Golden set** — labelled queries and profiles with known-correct answers. The regression gate.

## Stores

- **Ledger** — Postgres. The one non-derivable store: assessments, profile, evidence, manifest, chats, usage.
- **Derived store** — Qdrant and Neo4j. Rebuildable from Cloud Storage + git.
