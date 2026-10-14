# ADR-0037 — An obligation names its exceptions, and they travel with it

- Status: accepted
- Date: 2026-10-14
- Issue: #164
- Amends: ADR-0030 (Track B filters library law to the clauses Track A cites)
  and ADR-0035 (a cited clause admits the clauses inside it)

## Context

The signed mapping makes one obligation per APP subclause that imposes a
requirement. Exceptions, permissions and definitional subclauses are clauses,
not obligations. That is right for applicability, but nothing cited them. So
for every tenant, Track B could never return an obligation's exceptions
(ADR-0030 limits it to what Track A cites). An answer citing APP 8.1 could
never see APP 8.2.

On 2026-10-14 the human chose #164's option (a): an obligation names its
exceptions, in reviewed mapping content. They called it a correctness bug,
not a modelling nicety. An answer that states a rule without its exceptions
overstates it, and the reader can't tell.

## Decision

**1. The mapping model gains exceptions.** An exception is a clause that says
when an obligation does not apply, or is displaced. Mapping YAML names them
under `exceptions:`, one entry per obligation, keyed by its id:

```yaml
exceptions:
  - obligation_id: OB-PA-APP8-1
    clause_ids: [privacy-act-1988:C2026C00227:sch1/app8/2]
```

- **A file of its own.** The entry is keyed by the obligation's id, not
  written on it, so the signed `privacy-act-1988.yaml` gains exceptions from
  `privacy-act-1988-exceptions.yaml` and is never edited. Editing a signed
  file without re-review would break D-01. The new file has its own front
  matter, its own review, and the same pending refusal as every mapping file.
- **An edge.** Each exception loads as `EXCEPTED_BY` from the obligation to
  the clause, one per compilation like `DERIVED_FROM` (ADR-0028).
- **It grows and never shrinks.** Once loaded, an obligation's `EXCEPTED_BY`
  set may gain a clause and never lose one (D-05). That is how a signed
  obligation gains exceptions reviewed later. Fixing a wrong exception means
  superseding the obligation.
- **Refused before any write:**
  - an unknown obligation;
  - a clause outside the file's `source_clause_ids`, not in the graph, or
    never in force while the obligation is;
  - the obligation's own clause, or one containing it (`sch1/app8` for APP
    8.1 would be option (b), the whole APP, by another name);
  - a second entry for the same obligation;
  - a pending file. `scripts/load_mappings.py` already refused one; CI now
    does too (`test_no_corpus_mapping_awaits_review`). Before this, the tests
    that load `corpus/mappings/` would have run on a pending file.

**2. Track A's scope includes them (amends ADR-0030).** For every obligation
that applies or might, `LawScope.of(coverage)` cites its exceptions as well
as its clauses and the reasoning's. Not applicable means not retrieved, for
its exceptions too. An exception cited this way admits the clauses inside it
(ADR-0035), so its paragraphs are retrievable. Exceptions decide nothing
about whether an obligation applies: they are kept out of its `citations`
and the ledger's coverage row, and carried as `exception_clause_ids`. The
fake reads them from the YAML and `GraphTrackA` reads `EXCEPTED_BY` through
the query builder. A test holds the two equal.

**3. A guarantee, not a ranking chance.** Being in scope isn't enough: Track
B's top 10 can still rank an exception out. So `LawScope` carries the pairs
(an obligation's clause, an exception to it). When `answer()` assembles the
context, it adds each exception owed to a clause there, or to a paragraph
inside one:

- **Marked if the ranking returned it.** Otherwise it is fetched by id
  (`RetrievalPipeline.fetch`), through the lookup's filters.
- **The marker names the clause it excepts:**
  `[B clause <exception> excepts <clause>]`. The system prompt says an
  answer relying on that clause must say the exception exists and cite it.
- **Citable.** A delivered exception is in the verifier's context.
- **The ids come from Track A, never from retrieved text** (#29). What
  retrieval returned decides only which ids are owed.
- **An owed exception that can't be fetched refuses the answer before
  generation** (`ExceptionUndelivered`). The graph would then name a clause
  the retrieval store doesn't hold.

## Consequences

- The real Act's exceptions are a separate, drafted mapping file awaiting
  the human's signature. Until it is signed, no obligation has exceptions
  and nothing changes for any tenant.
- **APP 11.3 is not an exception.** #164 and the human's decision named it as
  one. Its parsed text is "For the purposes of subclauses 11.1 and 11.2,
  without limiting those subclauses or any other provision of this Act, such
  steps include technical and organisational measures." That says what the
  steps include, not when 11.1 or 11.2 doesn't apply. Under this model it
  stays out of scope, and GQ-14's recall under Acme's scope stays 0. The same
  gap holds for every definitional subclause an obligation's own text depends
  on (APP 5.2 for 5.1, 7.6 for 7.7, 11.3 for 11.1 and 11.2). Whether to cite
  those too, as a second kind of edge, is a separate decision, not taken here.
- **What is not guaranteed:**
  - The paragraphs inside an exception are in scope but not guaranteed: the
    guarantee delivers the clause the mapping names, which for most APP
    exceptions is a chapeau ending "if:". The answer can say the exception
    exists and cite it. Its conditions reach the model only if retrieval
    returns them. To guarantee a paragraph, the mapping names it.
  - An unscoped call (`LawScope.unscoped`, no Track A) knows no exceptions.
    The product path always passes Track A's scope. Only scripts and the
    Phase 1 eval don't.
- One more exact-id lookup per owed exception missing from the ranking, run
  concurrently. A cached prefix carries no exceptions, so its bytes, and its
  cache hits, are unchanged.
- The mapping version moves (ADR-0034): the new file is under
  `corpus/mappings/`, and the loader and Track A changed under
  `src/compliance_rag/graph/`.
