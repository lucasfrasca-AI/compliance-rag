# ADR-0008 — Stack: Qdrant, Neo4j Aura, self-hosted cross-encoder, Claude on Vertex

- Status: accepted. Amended by ADR-0046 (#177): Qdrant scores dense and
  sparse, and the adapter fuses them with RRF, not Qdrant's native RRF
- Date: 2026-09-20
- Decision ID: `D-07` (`DECISIONS.md`)

## Context

The build needs a real managed hybrid retriever, real Cypher, and a real cross-encoder, under USD 100/month, with every stage inspectable and every query-time byte in Australia. Managed rerankers are either offshore or bill an always-on replica.

## Decision

Qdrant Cloud in `australia-southeast1` for dense, sparse and native RRF; Neo4j AuraDB for Cypher; a self-hosted cross-encoder on Cloud Run in Sydney for reranking; Claude on Vertex for generation; Cloud Run for services; Cloud SQL for the ledger; OpenTelemetry to Cloud Logging in Sydney as the only telemetry sink.

## Consequences

- Every retrieval stage can be inspected and A/B tested independently.
- The reranker is ours to operate, including its cold starts.
- Aura Free has no Australian region, which is why D-09 limits it to public law during the build.
- `SERVICES.md` is the hard allowlist; anything outside it is a change request, not a decision at call time.
