# ADR-0012 — CAG for tenant policies, RAG for law

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-11` (`DECISIONS.md`)

## Context

A tenant's policy corpus is small, stable within a session, and read on nearly every request — ideal for a cached prompt prefix. The law corpus is the opposite: larger than any context window, and amended on a schedule nobody controls.

## Decision

The cached prefix (the CAG prefix) holds the system prompt, the obligation digest and the tenant policy corpus, capped at 200k tokens. Law is always retrieved. The law corpus is never cached into a prefix.

## Consequences

- Policy-to-law answers avoid re-sending the policy corpus every turn.
- Cached-prefix hit rate is a traced attribute and a cost lever.
- A tenant whose policies exceed 200k tokens needs a different strategy, and the cap must fail loudly rather than silently truncate.
- Invalidating the prefix on policy upload is a correctness requirement.
