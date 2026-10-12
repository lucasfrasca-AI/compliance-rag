# ADR-0027 — Applicability is an actor qualified by a dated condition tree

- Status: accepted
- Date: 2026-10-12
- Issue: #113 (from the #93 design session, Q1, Q2 and Q3)
- Touches: D-01, D-03, D-05, D-06; CONTEXT Obligation, Condition, Actor, Profile, Evidence, Tenant subgraph

## Context

"Does the Privacy Act apply to us?" is not a threshold. The APPs bind APP
entities (s15). An APP entity is an agency or organisation (s6(1)), and an
organisation is any legal form that is not a small business operator
(s6C(1)). Whether a business is a small business operator is a nested rule:
- the threshold, s6D(1) and (2);
- an operator test, s6D(3);
- six exceptions, s6D(4)(a)–(f);
- four carve-backs, s6D(5)–(8);
- a related-bodies rule, s6D(9);
- seven ways back in: s6E(1), (1A)–(1D) and (2), and the s6EA opt-in.

It also reads history: s6D(4)(a) asks about *any* financial year since the
business started. The README's single `turnover > 3000000` condition per
obligation would get it wrong, and the profile's self-declared
`actor_roles: [apn_entity]` would have the tenant answer the legal question
itself, with no reasons and no citations.

## Decision

1. **Applicability attaches to an Actor.** An `Actor` is a role an instrument
   defines, with stable ID `{instrument_id}/actor/{slug}` (for example
   `privacy-act-1988/actor/app-entity`). An obligation `APPLIES_TO` one or more
   actors. Each actor is `QUALIFIED_BY` the root of one condition tree,
   authored once and shared by every obligation that binds that role.
   `ACTS_AS` is the *result* of evaluating the tree for a tenant on a date,
   projected per request (CONTEXT: Tenant subgraph). It is never stored and
   never declared.
2. **A condition is a tree.** Each node is exactly one of `all_of`, `any_of`
   or `not` over other conditions, or a **leaf**. The node stores its form
   (`operator`); its children are `HAS_OPERAND` edges. The design document's
   proposed `ALL_OF` / `ANY_OF` / `NOT` edge types are collapsed into one edge
   plus the operator on the node, so a node can't carry mixed child edges and
   the builder needs one edge type to read a tree. Trees may share subtrees
   but never loop.
3. **A leaf tests one profile fact for one period:**
   - `current`;
   - the previous or current financial year;
   - `any_ended_financial_year` since a date field, for s6D(4)(a).

   It carries the question to ask and the unassessable reason to report when
   the fact is missing (D-06). Evaluation is three-valued, so a missing fact is
   *unknown*, never assumed (#115).
4. **Conditions and actors carry their own valid time (Q1).** Each is derived
   from its clauses and dated like an obligation, and anything a node refers
   to must be in force for the whole of its interval. A change to a gate, such
   as exemption reform, is one dated retirement. `as_of` then answers both
   sides of the change.
5. **Jurisdictions and sectors are reference nodes (Q2).** They use ISO
   3166-1/2 codes (`AU`, `AU-NSW`) and ANZSIC divisions
   (`anzsic-2006:Q`), each naming its `source`. An obligation may be narrowed
   to them through `APPLIES_TO`.
6. **Controls are crosswalk targets (Q3).** A `Control` node is derived from a
   standard's clause and carries our own `descriptor`, never the standard's
   title (PHASES.md licensing note). Obligations `CROSSWALKS_TO` controls.
   `REQUIRES` a control is kept for an instrument that mandates one, which the
   Privacy Act doesn't: APP 11.3's "technical and organisational measures" is
   a partial crosswalk at most. The real control list (#12) is Phase 4.
7. **Tenant policy mappings stay out of the graph (Q5).** They move to a ledger
   table projected per request in Phase 3.

## Consequences

- The answer to "does it apply?" explains itself. The deciding leaves' clauses
  *are* the citation chain, and every unknown leaf is a named question.
- Track A needs a small evaluator, in Python, not Cypher and not an LLM (#115).
  It also needs dated profile facts (#114).
- The loader refuses an item that refers to something not in force for its
  whole life. It also refuses a loaded node whose operands, qualification,
  `applies_to` or `requires` changed. A change of meaning is a new id
  (D-05; #120 covers cosmetic corrections).
- `schema.cypher` gains `QUALIFIED_BY` and `HAS_OPERAND` edges and drops the
  `Condition.kind` and `Actor.role` indexes that the old shape assumed.
