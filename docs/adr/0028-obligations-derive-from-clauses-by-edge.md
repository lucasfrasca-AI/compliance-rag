# ADR-0028 — Obligations derive from clauses by edge, one per compilation

- Status: accepted
- Date: 2026-10-12
- Issue: #113 (from the #93 design session, Q6)
- Touches: CONTEXT Obligation, D-05, the graph schema

## Context

An obligation carried its clause as a property, `clause_id`. A clause's
stable ID includes the compilation (`privacy-act-1988:C2026C00227:sch1/app11/1`),
so every new compilation mints a new clause ID for unchanged text. APP 11.1's
words haven't changed in years, yet under a property each recompilation would
force a new obligation id or a mutation. And the query builder can't check that
a property's clause is in force on `as_of`; it checks only nodes on the path.

## Decision

**`DERIVED_FROM` is an edge, one per compilation the item is grounded in.**
It runs from Obligation, Condition, Actor and Control to Clause.

- A recompilation of unchanged law adds an edge to the new compilation's
  clause. The old clause retires with its version (`amend`), so on any `as_of`
  the path `Obligation -DERIVED_FROM-> Clause` returns exactly the text in
  force: the text the citation needs.
- A node's grounding is checked by the loader:
  - every clause it derives from is in the graph and in force at some time
    within the node's interval, so a node may list every compilation it
    spans;
  - on its last visible day at least one of them is in force.

  An item may not outlive the text it was read from.
- An item's own `valid_from` records when the obligation existed in law, and
  may predate the earliest compilation loaded. Before that compilation it has
  no text in force, so Track A reports it as unassessable for want of text,
  never as applicable (#115).
- `SUPERSEDES` on an obligation is now needed only when the *obligation*
  changes meaning, not when the compilation changes.

## Consequences

- Recompilation costs an edge, not a re-authored obligation.
- The citation set of an answer is the set of clause nodes on the evaluated
  paths, each checked `as_of` by the builder.
- Mapping YAML's `clause_id` becomes `clause_ids`, and every grounded item has
  one.
