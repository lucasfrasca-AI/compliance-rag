# ADR-0044 — A qualifier travels with its obligation, and so does what is inside an exception or a qualifier

- Status: accepted; its sign-off's APP 12.6 is removed as an erratum (ADR-0050)
- Date: 2026-10-15
- Issue: #182
- Amends: ADR-0037 (an obligation names its exceptions, and they travel with
  it), and through it ADR-0030's law scope

## Context

ADR-0037 made an obligation's exceptions travel with it: named in reviewed
mapping content, in Track A's law scope, and given to every answer that is
given the obligation's clause. It left two gaps, recorded there and in #182.

- **APP 11.3 is not an exception.** Its parsed text: "For the purposes of
  subclauses 11.1 and 11.2, without limiting those subclauses or any other
  provision of this Act, such steps include technical and organisational
  measures." It says what 11.1's and 11.2's steps include, not when they
  don't apply. Under ADR-0037 nothing cited it, so no tenant's answer about
  11.1 could see it. GQ-14 expects it, and scored 0.0 under Acme's scope. The
  same holds for APP 5.2 (the matters 5.1 requires notice of) and 7.6 (the
  requests 7.7 gives effect to).
- **An exception's paragraphs.** The mapping names APP 8.2, the lead-in to
  (a)–(f). The paragraphs are clauses of their own: in scope (ADR-0035), but
  reaching the model only if the ranking returned them. So an answer could
  say an exception exists and not how far it reaches.

On 2026-10-14 the human decided:

> "#182 — yes, a qualifying subclause travels with its obligation. The
> principle is the same as #164: an answer citing APP 11.1 without 11.3 is
> incomplete in a way the reader can't detect. Extend the exceptions
> mechanism to cover qualifiers, and distinguish them in the data model and
> in the output — an exception says when the obligation doesn't apply, a
> qualifier says how it's satisfied. Those are different things to a
> reader."

## Decision

**1. A qualifier is a clause that says how an obligation is satisfied, or
what it requires.** Mapping YAML names it under `qualifiers:`, one entry per
obligation, keyed by its id, exactly as `exceptions:` is:

```yaml
qualifiers:
  - obligation_id: OB-PA-APP11-1
    clause_ids: [privacy-act-1988:C2026C00227:sch1/app11/3]
```

- **Its own section and its own edge.** A file may carry `exceptions:`,
  `qualifiers:` or both; a file with only `exceptions:` (#188's draft) is
  valid as it was. A qualifier loads as `READ_WITH`, from the obligation to
  the clause, never as `EXCEPTED_BY`.
- **Why `READ_WITH`.** `QUALIFIED_BY` already exists, Actor → Condition: the
  condition tree that decides who holds a role. A qualifier is the opposite
  kind of thing, a clause of the law that is part of what an obligation
  says. `HAS_QUALIFIER` or any name built on "qualif-" would read as the
  same relation in a Cypher query or a conversation. `READ_WITH` was #182's
  own suggestion, and says what the edge does: the obligation is read with
  that clause. CONTEXT.md's **Qualifier** entry distinguishes the two senses
  of "qualifies".
- **The same refusals as an exception, before any write:** an unknown
  obligation; a clause outside the file's `source_clause_ids`, not in the
  graph, or never in force while the obligation is; the obligation's own
  clause, or one containing it; a second entry for the same obligation; a
  pending file.
- **And one more: never both.** A clause can't be an exception to an
  obligation and a qualifier of the same one. Nor can an exception and a
  qualifier of one obligation contain one another, since the clauses inside
  both would then travel as both (decision 2). A clause may be an exception
  to one obligation and a qualifier of another.
- **It grows and never shrinks,** as `EXCEPTED_BY` does (D-05).

**2. What is inside an exception or a qualifier travels with it, by exact
id.** The loader expands each named exception and qualifier to itself and
every clause inside it in the graph, as further `EXCEPTED_BY` or `READ_WITH`
edges. APP 8.2 brings 8.2(a)–(f) and their subparagraphs; 5.2 brings
(a)–(j).

- **Read from the graph's clause nodes, before any write.** A clause's
  paragraphs are clauses of its own instrument version, loaded and retired
  with it. So `check_mappings` reads, through the query builder, the
  version's clauses in force on the named clause's first day, and keeps
  those whose path runs through it (`clauses_inside`, ADR-0035's
  containment by path segment: `s1/30` is not inside `s1/3`). One read per
  version and first day, which is one per version, since a version's clauses
  share its validity. The writes use that read (`CheckedMappings.inside`), so they rest
  on what the checks saw.
- **The fake mirrors it from its clause validity,** through the same
  `clauses_inside`, among the clauses in force on the named clause's first
  day, as the loader reads them. A test holds Track A over Neo4j equal to the
  fake.
- **Why the loader.** #182 offered three options:
  - The reviewer lists every paragraph. That is error-prone, and a reviewer
    signs legal judgements, not the parser's structure.
  - A `RetrievalStore` method returning a clause and the clauses inside it.
    That is an interface change, and it would let the retrieval store, not
    the graph, decide what travels with an obligation.
  - Track A expanding on each read. The builder has no path-prefix read, so
    that would read every clause of the version on every coverage call.

  The loader pays the read once per load, and Track A's read stays one hop.

**3. The law scope and the guarantee carry both kinds.**

- `ObligationCoverage.qualifier_clause_ids` sits beside
  `exception_clause_ids`. Both are kept out of `citations` and the ledger's
  coverage row, because they decide nothing about whether the obligation
  applies.
- `LawScope.qualifiers` (`QualifierOf` pairs) sits beside
  `LawScope.exceptions` (`ExceptionOf`).
- `LawScope.owed` returns, for each clause a context owes, an `Owed`: the
  clauses it excepts and the clauses it qualifies.
- `answer()` delivers both, fetched by exact id if the ranking left them
  out, until nothing more is owed.
- One that can't be fetched refuses the answer before generation.
  `ObligationExceptionUndelivered` becomes `OwedClauseUndelivered`, whose
  message names each missing clause's kind.

**4. The output keeps them apart.**

- **The marker** glosses each relation in plain words:
  - `[B clause <id> excepts <id> (when it does not apply)]`;
  - `[B clause <id> qualifies <id> (how it is satisfied)]`;
  - both, in that order, on a clause that is an exception to one obligation
    and a qualifier of another.

  A fence with neither keeps the boundary it had, so the cached prefix's
  bytes don't change (D-11).
- **The system prompt** describes each, with a rule for each:
  - an answer relying on a clause an exception names must say the exception
    exists and state when the obligation does not apply, because a rule
    without its exceptions overstates it;
  - one relying on a clause a qualifier names must state how the obligation
    is satisfied as the qualifier says, because a rule without its
    qualifiers is incomplete;
  - never describe one as the other.
- **The structured output:** `Answer.qualifiers`, beside `Answer.exceptions`.
- **The spans:** `answer.qualifiers` and `answer.qualifiers_fetched` beside
  the exception attributes, and `track_a.qualifiers` ("<clause> read with
  <qualifier>") beside `track_a.exceptions`.
- **Not changed:**
  - The verifier's context is a set of clause ids, marking nothing, so a
    delivered qualifier is citable like any delivered clause.
  - The API's citation shape carries no exception marking today, so there
    was nothing to extend. Showing a client which cited clause excepted or
    qualified which is a feature of its own, not taken here.

## Consequences

- **The exception marker's text changed** from `excepts <id>` to `excepts
  <id> (when it does not apply)`. That is in the retrieved fence, not the
  cached prefix.
- **More text reaches the model, by design.** Expanded, #188's 19 exception
  pairs are 112 `EXCEPTED_BY` edges, and the qualifiers draft's 7 pairs are
  24 `READ_WITH` edges. That was measured by loading the whole selected
  compilation (779 clauses) into a local Neo4j with both drafts, where Track
  A over Neo4j equalled the fake for every applicability profile (GA-01 to
  GA-07).
  - An answer citing APP 8.1 is now given 19 clauses with it (8.2, 8.3 and
    what is inside them), fetched concurrently where the ranking left them
    out.
  - The graph grows by 136 edges once both drafts are signed and loaded
    (Aura Free: under 0.1% of its relationship limit).
- **The real Act's qualifiers are a separate, drafted mapping file** awaiting
  the human's signature. Until it is signed, no obligation has qualifiers.
  The paragraph expansion applies to #188's exceptions as soon as that file
  is signed and loaded.
- **GQ-14, measured** (real Qdrant, cached vectors, no reranker, Acme under
  GA-01's scope, its policies as the prefix). Recall@10 of the expected law
  over the nine law questions:

  | Mapping set | GQ-14 | Mean |
  |---|---|---|
  | Signed only | 0.0 | 0.852 |
  | + #188's exceptions | 0.0 | 0.852 |
  | + the qualifiers draft | 1.0 | 0.963 |
  | + both | 1.0 | 0.963 |

  With the qualifiers, 11.3 is also in what `answer()` gives the model
  (marked as qualifying 11.1 and 11.2), whatever the ranking does. The
  baseline mean is above ADR-0035's 0.796 because GQ-20 now scores 1.0 (the
  ranking has changed since, #190). Run ad hoc, never committed, so no test
  consumes unsigned content.
- **The mapping version moves** (ADR-0034): the loader, Track A and the
  mapping model changed under `src/compliance_rag/graph/`.

## Sign-off, 2026-10-15

The human signed both mapping files, with three decisions that sharpen where
each category ends.

- **A scope limit is an exception.** The reviewer added APP 3.7 ("This
  principle applies to the collection of personal information that is
  solicited by an APP entity") to the exceptions file: "a scope limit
  disapplies the obligation for material outside the scope, which is what an
  exception does". As with 6.7 and 7.8, it is listed for every obligation of
  its principle: OB-PA-APP3-1, 3-2, 3-3, 3-5 and 3-6. The model's draft had
  called 3.7 definitional, which was wrong.
- **A permission is neither.** The reviewer removed APP 4.2 and 12.10 from the
  qualifiers: "They're permissions, not qualifiers; 'you may do X' doesn't
  change how an obligation is satisfied, it creates a separate allowance, and
  including them dilutes a category that has to mean something precise to a
  reader." APP 12.6 stayed, as signed. It also says "may", but (the model's
  reading) it names a way of giving the access 12.5 requires, so it says how
  12.5 is satisfied rather than granting a separate allowance. **Superseded
  on 2026-10-20 by ADR-0050:** the human trimmed 12.6 as a permission like
  4.2 and 12.10, and its `READ_WITH` edge is removed as an acknowledged
  erratum, since it never qualified 12.5 at any valid time.
- **A deeming rule is a possible third category.** APP 6.6 stays out of both:
  "note deeming rules as a possible third category rather than forcing them
  into one of two". A deeming rule says what counts as what. Whether it
  travels with its obligation, labelled as such, is #210 (later).

As signed: **24 exception pairs over 15 obligations**, and **5 qualifier
pairs** (5.2→5.1, 7.6→7.7, 11.3→11.1, 11.3→11.2, 12.6→12.5); after ADR-0050's
erratum, 4 (12.6→12.5 removed). The edge counts
as loaded into Aura are in `BUILD_LOG.md`.
