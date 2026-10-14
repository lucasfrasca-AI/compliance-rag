# ADR-0035 — A cited clause admits the clauses inside it

- Status: accepted
- Date: 2026-10-14
- Issue: #163
- Amends: ADR-0030 (Track B filters library law to the clauses Track A cites)

## Context

ADR-0030 limits Track B to "the clauses Track A cites". The mapping cites
subclauses (`sch1/app11/2`), but the parser stores each paragraph as its own
clause and chunk (`sch1/app11/2/a` … `/d`). `LawScope.admits` and the Qdrant
filter compared exact ids, so no paragraph of a cited subclause was ever
retrievable for a tenant.

APP 11.2's stored text is its chapeau, "If: the entity must take such steps as
are reasonable in the circumstances to destroy the information or to ensure
that the information is de-identified." Its conditions, (b) "no longer needs
the information" and (d) "not required … to retain", were out of scope for
every tenant. With GA-01's facts, none of the six paragraph-level Schedule 1
chunks in the golden corpus was admitted.

The Phase 1 recall gate never saw this, because it retrieves over the whole
library.

## Decision

**A citation of a clause is a citation of its text, which in the stored model
is the clause and every clause inside it.** `LawScope.admits` admits a public
clause if its id, or the id of any clause containing it, is cited.

- **Containment is by path segment.** `clause_ancestors("…:sch1/app11/2/b")`
  is `…:sch1`, `…:sch1/app11` and `…:sch1/app11/2`. So `s1/10` is never inside
  `s1/1`, although one string starts with the other.
- **Same instrument version only.** Ancestors share the instrument and
  version, so a paragraph is admitted only in the compilation it was cited in.
- **Qdrant stores each chunk's ancestors** in an `ancestors` payload field, with
  a keyword index. The law branch of the filter matches the cited ids against
  either `clause_id` or `ancestors`. The fake applies the same rule through
  `LawScope.admits`, so the two agree by construction (a contract runs on both).
- **A point stored before the field existed** matches on its own id only. That
  is narrower than the rule, never wider. The cloud collection holds nothing
  yet (#144), so nothing needs re-upserting.

#163 recommended expanding the scope's ids from the graph instead. That needs
the clause structure the fake graph doesn't hold, and it grows every scope
several times over. The path rule needs only the id.

## Measured

Real Qdrant, real cached `gemini-embedding-001` vectors, no reranker. The nine
golden questions whose labels name law were asked as Acme, under Track A's
scope for GA-01's facts, with its policies left out (ADR-0033):

- **Mean recall@10 of the expected law: 0.593 before, 0.796 after.** GQ-09 and
  GQ-13 go from 0 to 1, and GQ-18 from 0.333 to 0.667. Each gains APP
  11.1(a) or (b).
- **GQ-20 drops from 1.0 to 0.5.** Of its two expected clauses, the one that
  drops out is the definition of "agency" (`s6/1/agency`). That definition is
  cited, and its 15 paragraphs are now admitted with it, as are s6C(3)'s 17
  paragraphs. They compete for the ten places.
  - **0.5 is GQ-20's own figure in the recall gate's baseline,** which is
    measured over the whole library. The old 1.0 came from the scope being too
    narrow to hold the competing paragraphs.
  - **Whether the reranker restores it isn't measured here.**
- **APP 11.3 stays out for GQ-14,** as it should. The mapping lists it as a
  clause, not an obligation, so nothing cites it.

That last point is a wider gap, and a separate one (#164). An obligation's
exceptions (APP 7.2–7.6 for 7.1, 8.2 for 8.1, 11.3 for 11.2) are never cited,
so they are never retrievable either.

The measurement is a test:
`test_on_real_qdrant_acmes_scope_now_retrieves_the_paragraphs_its_labels_expect`.
- **What it pins:** GQ-09 and GQ-13 at 1.0, and the mean above 0.75.
- **What it prints:** the rest. A clause on an RRF tie at the tenth place can
  move from run to run (#160).

#156's real-Qdrant measurement (ADR-0033) retrieved over the whole library, so
this change leaves it as it was.

## Consequences

- **More law reaches the model for every tenant:** the paragraphs that carry
  most subclauses' conditions. #149's question on the fake now gets ten law
  clauses with the CAG prefix left out, against four before.
- **The verifier needs no change.** Its context is what was retrieved, and a
  retrieved paragraph is in it.
- **A scope can now admit many clauses for one cited id** (s6C(3) has 17
  paragraphs). That is the cited text, not a widening.
