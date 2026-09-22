# ADR-0016 — Reranking runs in Australia or not at all

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-15` (`DECISIONS.md`)

## Context

The reranker sees the query and the candidate chunks. For this product that means customer policy text and whatever the user typed. Every managed reranking API worth using — Cohere, Voyage, Jina, and Vertex's own Ranking API — is hosted outside Australia.

## Decision

Reranking runs in `australia-southeast1` or not at all. The default is a self-hosted cross-encoder on Cloud Run in Sydney, scale-to-zero. ColBERT rescoring inside Qdrant is the sanctioned alternative. The Vertex Ranking API is CI-only in `us-central1`, hard-gated to `TENANT_ID == dev-tenant` and synthetic data. No managed reranker outside Australia ever sees real data.

## Consequences

- Cold starts are ours to manage; if p95 breaks, min-instances 1 is a separate billable ask.
- Model choice is ours, and the benchmark is checked in.
- The Ranking API gate is a correctness requirement with a test, not a convention.
- If ColBERT wins the golden set, the Cloud Run service is deleted and the monthly bill drops.
