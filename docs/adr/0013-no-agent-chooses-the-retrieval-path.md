# ADR-0013 — No agent chooses the retrieval path

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-12` (`DECISIONS.md`)

## Context

An agent given the option to skip the graph will sometimes skip it, because the retrieved text looks sufficient. That is precisely the failure D-03 is built to prevent, reintroduced one layer up.

## Decision

The pipeline is fixed. The only routing decision is a regex: a clause-ID query goes to the keyword channel and skips reranking; everything else runs the full pipeline.

## Consequences

- The path a query took is predictable and reproducible from the query alone.
- Some queries run more pipeline than they strictly need.
- Optimisations must be proven on the golden set, not delegated to a model's judgement at request time.
