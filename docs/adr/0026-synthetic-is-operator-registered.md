# ADR-0026 — Synthetic is operator-registered, never self-attested

- Status: accepted
- Date: 2026-10-10
- Issue: #78
- Hardens: ADR-0023 / ADR-0024 (the build-phase residency gate)

## Context

The residency gate lets generation run offshore only while every tenant chunk
is synthetic, and fails closed: an unmarked tenant chunk counts as real. The
gate is only as good as the `synthetic` flag, and the flag was set by whoever
built the chunk. A careless or hostile ingester could mark a real policy
synthetic and have it answered from the US.

There were three options:
- **An admin-only flag in the ledger**, keyed by instrument version. The
  instrument ID is chosen by the uploader, so the key doesn't identify content,
  and it needs an admin surface that doesn't exist until Phase 3.
- **An allowlist of fixture source paths.** Paths name files on the build
  machine, not the content that reaches the write path.
- **A checked-in registry of content hashes.**

## Decision

**A checked-in registry of content hashes**, `corpus/synthetic/registry.json`:
- **What it holds:** sha256 over the owning tenant and the exact text of each
  synthetic chunk and each synthetic clause (its canonical text). Clauses are
  registered because clause text reaches the generator by itself: a repair
  prompt carries a clause's canonical text. Registered chunks paired with an
  unregistered clause on replace-in-place are therefore real data, and refused
  offshore.
- **Who writes it:** only `scripts/register_synthetic_fixtures.py`, from
  `docs/research/fixtures/synthetic-policies/`. It changes only by PR, like
  `corpus/mappings/`.
- **What the write path does:** `CorpusLifecycle` takes the registry as a
  required argument and derives every chunk's flag from it on publish and on
  replace-in-place. A chunk that arrives marked synthetic but isn't registered
  is refused before anything is written (`SyntheticClaimRefused`). It is
  refused rather than silently downgraded, so the attempt is visible.
- **How the key binds:** to the tenant as well as the words. The same text
  under another tenant is that tenant's real data.
- **The other writers are fenced.** Compilation ingest (`corpus/ingest.py`)
  now refuses a tenant document outright; it writes library law only. A test
  lists every module in `src/` that calls `upsert_chunks` or `put_clause`, so
  a new writer that bypasses the lifecycle fails CI until it is reviewed.

## Consequences

- An upload can no longer mark its own content synthetic
  (`tests/test_synthetic_registry.py`).
- Changing a fixture, the parser or the chunker changes the hashes.
  `test_the_registry_matches_the_fixtures` fails until the operator
  re-registers by PR.
- **Still open, and a Phase 3 blocker:** the Phase 3 upload API must not
  accept a `synthetic` field, and must reach the stores only through
  `CorpusLifecycle`. The store adapters (`upsert_chunks`) still take the flag
  as given, as they must for operator scripts such as the golden-set harness
  and the injection probes; the writer-list test is what keeps that list
  closed. #78 stays open for the API-level test.
- The registry is read from the source tree. A deploy that doesn't ship
  `corpus/synthetic/` fails at startup, which is closed, not open; the Phase 3
  container must include it.
