# ADR-0003 — The LLM never authors a citation

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-02` (`DECISIONS.md`)

## Context

The product's claim is that every citation is verbatim, versioned and clickable to the clause. A model asked to quote a clause will sometimes produce text that is plausible, well-formed and not what the instrument says. No amount of prompting removes this.

## Decision

Clause text is retrieved verbatim from canonical storage; the LLM composes around it but never writes it. A deterministic verifier compares every citation's span against the canonical source after canonicalisation (NFC, whitespace, quotes) and blocks any answer whose span does not match. A failure triggers a targeted repair of that citation only, never a full regeneration.

## Consequences

- A bad citation blocks the answer rather than shipping it.
- Canonicalisation rules are load-bearing and must be identical on the ingest and verify paths.
- Latency grows by the verifier pass plus any repair.
- The verifier needs a test that proves a deliberately bad citation blocks; without it the guarantee is unproven.
