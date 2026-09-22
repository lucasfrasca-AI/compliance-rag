# ADR-0009 — Repository interfaces between the app and every external service

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-08` (`DECISIONS.md`)

## Context

Four of the external services in this stack are chosen under build-phase constraints — free tiers, regional availability, a price ceiling. Those constraints change at Phase 5. If Cypher and Qdrant calls are scattered through the application, changing any of them is a rewrite.

## Decision

The app talks only to `GraphStore`, `RetrievalStore`, `LedgerStore`, `Reranker`, `Embedder` and `Generator`. No Cypher, Qdrant, Vertex or SQL call exists outside `adapters/`. A pgvector adapter is kept as a fallback for `RetrievalStore`.

## Consequences

- Migrating a store is writing one adapter and passing its contract tests.
- Every interface has in-memory fakes, so most tests need no network.
- Interfaces must stay narrow enough not to leak one vendor's model; a Qdrant-shaped `RetrievalStore` would defeat the point.
- A little indirection is paid on every call path.
