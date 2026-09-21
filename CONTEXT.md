# CONTEXT — shared language

Use these words exactly, in code, docs, commits and chat. If a concept isn't here and you need it more than once, add it via `/grill-with-docs`.

## Corpus

- **Instrument** — a law, regulation or standard as a whole (Privacy Act 1988, EU AI Act, ISO 27001). Has versions.
- **Instrument version** — one compilation of an instrument. Identified by the register's own ID (compilation number, CELEX, catalogue revision).
- **Tenant id** — the identifier for one company. Contains neither `:` nor `/`, because both are separators inside a stable ID. `_public` is reserved for public law and is never a real tenant's id.
- **Clause** — the smallest citable unit of an instrument: an APP sub-clause, an article paragraph, an Annex A control. Has a **stable ID**: `{instrument_id}:{version_id}:{clause_path}`.
- **Instrument id** — globally unique on its own. A tenant-scoped instrument is prefixed with its tenant — `acme/hr-policy` — so two companies cannot both claim `hr-policy`. Public instruments are unprefixed. A stable ID that is unique only given out-of-band context is not stable: the ID is what a citation carries and what the verifier looks up.
- **Canonical text** — a clause's text after canonicalisation. The verifier compares canonical to canonical. Lives in Cloud Storage. Never edited. _Avoid_: normalised text, cleaned text.
- **Canonicalisation** — the exact transformation producing canonical text, in order: NFC normalisation; `‘ ’` → `'`; `“ ”` → `"`; en dash and em dash → `-`; non-breaking space → space; every whitespace run collapsed to one space; ends stripped. This defines what counts as a matching citation, so it is a definition, not an implementation detail.
- **Canonicalisation version** — an integer identifying one canonicalisation. Every citation records the version it was verified under, so changing canonicalisation cannot silently invalidate citations already issued. A change to the transformation is a new version, never an edit to an existing one.
- **Chunk** — a retrieval unit derived from a clause, ≤500 tokens. One chunk belongs to at most one obligation. Chunks live in Qdrant; clauses live in Cloud Storage.
- **Policy** — a company's own document, treated exactly like an instrument: versions, clauses, canonical text.
- **Corpus version** — a git tag over `corpus/instruments/`, identifying which instrument versions were loaded. Recorded on every assessment.
- **Mapping version** — a git tag over `corpus/mappings/`, identifying which obligations, conditions and crosswalks were in force. Moves independently of the corpus version: a mapping correction changes no clause text, and a new compilation changes no mapping. Recorded on every assessment; an assessment is reproducible only with both.

## Graph

- **Obligation** — a graph node for a clause that imposes a requirement. Not every clause is an obligation (recitals, definitions aren't). Carries its own stable id, independent of `clause_id`: an obligation is a human claim *about* a clause and survives a new instrument version via `SUPERSEDES`, where the clause's stable ID does not.
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
- **Router** — the single fixed decision: clause-ID regex → keyword channel, skip rerank; else full pipeline. The regex accepts a tenant-prefixed instrument id, so a policy clause ID is routed like any other.
- **Scope** — which corpus partition a chunk belongs to: `public` (law), `tenant` (company policies), `user` (one person's private additions). Scope `public` implies the reserved tenant id `_public`; `tenant_id` always names an owner, never nobody.

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

- **Ledger** — Postgres. The one store whose contents cannot be rebuilt: assessments, profile, evidence, manifest, chats, usage. The corpus tables in the same database (`instrument`, `instrument_version`, `clause`, `clause_version`) are a derived index kept there for join convenience; they are not the ledger and are rebuildable like any other derived store.
- **Derived store** — Qdrant and Neo4j. Rebuildable from Cloud Storage + git.
