# ADR-0002 — Both graphs are deterministic

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-01` (`DECISIONS.md`)

## Context

The regulatory graph decides which obligations apply to a company. If an obligation in that graph is wrong, every assessment built on it is wrong, and the error is invisible: a hallucinated obligation looks exactly like a real one. LLM extraction over legislation is accurate enough to be tempting and not accurate enough to be authoritative.

## Decision

Both graphs are deterministic. The regulatory graph is hand-curated YAML in `corpus/mappings/`, reviewed by PR, and nothing in it is LLM-extracted. Tenant policy-to-law mappings are the single exception: an LLM may *propose* one, but it is stored as `status: proposed` and is not traversed until a human approves it.

## Consequences

- Building the corpus is slow and manual. That is the cost of the product claim.
- Every obligation and crosswalk has an author, a reviewer and a date.
- Coverage grows one instrument at a time, which constrains how fast new frameworks can be added.
- The approval queue is a product surface, not an internal tool.
