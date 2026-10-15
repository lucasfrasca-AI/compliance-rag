# ADR-0045 — A clause is read and delivered in its provision

- Status: accepted, the mechanism only. The human's decision on #207
  (2026-10-15), "merge the mechanism, not the metric", is quoted under
  Decision 6. The PR first proposed moving ADR-0043's targets to the delivered
  list. The human declined that, so the targets stay on the reranked list
- Date: 2026-10-15
- Issue: #200
- Amends: D-04 and ADR-0022 (what the cross-encoder reads, and what
  `retrieve()` delivers beyond its top 10)
- Relates to: ADR-0043 (the gate gains a separate metric, delivered-recall,
  with floors and no target; ADR-0043's targets, and its one open target, are
  unchanged), ADR-0035 (the same containment, by path segment), ADR-0044
  (what travels with an obligation, which this leaves as it was)

## Context

ADR-0043 measured the recall gate on the whole compilation and left one open
target: law cross-clause recall@10, 0.444 on the reranked top 10, against 0.8.
The human:

> "0.444 cross-clause needs work, but I want the bar to be a real target, not
> a rubber stamp."

Four of the seven expected clauses were missed:

| query | missed | why (ADR-0043) |
|---|---|---|
| GQ-18 | APP 11.1(a), (b) | **Fragments.** (a) is "from misuse, interference and loss; and". Its meaning is in 11.1's chapeau, which is ranked 1st |
| GQ-19 | APP 10.1 | Ranking: 18th by dense, absent from sparse's top 50, 36th fused |
| GQ-20 | `s6/1/agency` | **Fragment.** "agency means:", whose content is its 15 paragraphs |

The parser stores one provision of the law as several clauses, split at its
chapeau: APP 11.1's sentence is three clauses, and the definition of
"agency" is sixteen. Each is its own chunk, ranked alone. So the ranking can
return a chapeau without its list, or a paragraph without the words that give
it meaning. ADR-0035 already says that a citation of a clause is a citation of
its text, which is the clause and every clause inside it, but only for the law
scope. Retrieval and the cross-encoder never applied it.

## Measured

2026-10-15. The gate corpus (779 clauses and the six Acme policies), local
Qdrant v1.19.1, the cached `gemini-embedding-001` vectors, local BM25 with IDF,
and the int8 cross-encoder over the top 20 fused candidates. No cloud call.

Two metrics per option, never one number. **Ranked recall@10** is recall@10
on the reranked top 10, the retrieval metric. **Delivered-recall** is the
share of the expected clauses in what the option delivers, which may be more
than ten clauses (a separate metric, decision 6). The GQ columns are
delivered-recall:

| option | ranked recall@10, law cross-clause | GQ-18 | GQ-19 | GQ-20 | delivered-recall, law cross-clause | other cells | delivered clauses per question (mean / max) |
|---|---|---|---|---|---|---|---|
| today: the reranked top 10, delivered as ranked | 0.444 | 0.333 | 0.5 | 0.5 | 0.444 | as ADR-0043 | 10 / 10 |
| (A) within k: a ranked clause's paragraphs after it, list cut at 10 | 0.444 | 1.0 | 0.5 | 0.5 | 0.667 | unchanged | 10 / 10 |
| (A) within k, its chapeaux before it too | 0.444 | 1.0 | 0.5 | 0.5 | 0.667 | unchanged | 10 / 10 |
| (A) beyond k: its paragraphs added | 0.444 | 1.0 | 0.5 | 0.5 | 0.667 | unchanged | 14.7 / 32 |
| (A) beyond k: its chapeaux added, no paragraphs | 0.444 | 0.333 | 0.5 | 0.5 | 0.444 | unchanged | 11.8 / 17 |
| (A) beyond k: chapeaux and paragraphs | 0.444 | 1.0 | 0.5 | 0.5 | 0.667 | unchanged | 16.5 / 36 |
| (B), the half that needs no re-embed: chunk text carries its chapeaux, for BM25 and the cross-encoder (dense vectors kept), delivered as ranked | 0.500 | 1.0 | 0.5 | **0.0** | 0.500 | ranked paraphrase law **0.833** (GQ-10 lost); policy cross-clause 1.0 | 10 / 10 |
| that (B), delivered with chapeaux and paragraphs | 0.500 | 1.0 | 0.5 | 0.5 | 0.667 | unchanged | — |
| the cross-encoder reads each candidate in its provision; within k | 0.444 | 1.0 | 0.5 | 0.5 | 0.667 | unchanged | 10 / 10 |
| the cross-encoder reads each candidate in its provision; paragraphs delivered beyond k, no chapeaux | 0.444 | 1.0 | 0.5 | 0.5 | 0.667 | unchanged | 13.9 / 30 |
| **the cross-encoder reads each candidate in its provision; chapeaux and paragraphs delivered beyond k (this decision)** | **0.444** | **1.0** | 0.5 | **1.0** | **0.833** | **unchanged** | **15.7 / 34** |
| that, delivering a ranked paragraph's whole provision (its siblings too) | 0.444 | 1.0 | 0.5 | 1.0 | 0.833 | unchanged | 23.5 / 73 |

"Other cells" are exact-ID 1.0 and paraphrase 1.0 for law and policy, and
policy cross-clause 0.9, in both metrics. Locally the reranked list's cells
are ADR-0043's in every row but (B)'s. **No option raises ranked recall@10
for law cross-clause above 0.444 except (B)'s half, which costs GQ-10 and
GQ-20.** Delivered sizes are over the 17 questions that aren't exact ids.

What decided it:

- **Delivery alone recovers GQ-18, in delivered-recall, and nothing else.** No clause of the
  definition of "agency" is in GQ-20's top 10, so there is nothing to deliver
  it with.
- **But two of its paragraphs are in the top 20 the cross-encoder sees:**
  `s6/1/agency/c` fused 18th and `/ca` 20th. Read alone, (ca) is "a body
  (whether incorporated or not), or a tribunal, established for a public
  purpose by or under a law … of a State or Territory as in force in an
  external Territory …". Nothing in it says it defines "agency", and the
  cross-encoder ranked it 16th of the 20. Read after its chapeau, "agency
  means: a body …", it ranks 7th, 0.91 logit inside the cut, and its chapeau
  is the clause GQ-20 expects.
- **Neither half is enough alone.** Read in provision, GQ-20's (ca) is
  ranked, but without the chapeaux it is delivered as the bare limb and the
  clause GQ-20 expects, "agency means:", isn't delivered: 0.667. Delivered
  whole but read bare, (ca) isn't ranked: 0.667.
- **Within k drops what the ranker ranked.** Cutting the list back to ten
  after delivery pushes ranked clauses out to make room for paragraphs: read
  in provision, GQ-20's (ca) is ranked 7th and lands past the tenth, behind
  the paragraphs and chapeaux of `s6c/1` and `s6c/3`, ranked above it.
- **(B) isn't needed, and its half that needs no re-embed hurts.** Changing
  the chunk text moves BM25 for every clause. GQ-10's APP 11.2 leaves the
  ranked list, and the paragraphs of `s6c/1` and `s6c/3`, now carrying their
  chapeaux, crowd out `s6c/3` itself. The dense half needs a cache refresh on
  Vertex and wasn't run. Reading in provision gives the cross-encoder what (B)
  would, and changes no chunk, vector or BM25 weight.

## Decision

**1. A provision is a clause and the clauses inside it with only
subsections, paragraphs and subparagraphs between them** (`in_same_provision`,
`corpus/models.py`). Containment is by path segment, as ADR-0035's, and within
one instrument version. A **Definition is a provision of its own**: `s6/1`,
"In this Act, unless the contrary intention appears:", contains all 176
definitions and 246 of their paragraphs, but none is in its provision. The
path grammar is `display_form`'s: a segment that isn't a subsection, paragraph
or subparagraph is a defined term's slug. On C2026C00227, `s6/1` is the only
clause that contains a clause outside its provision (a test checks all 779).

- **Chapeaux** are the clauses of a clause's provision that contain it: APP
  11.1 for 11.1(a); "agency means:" and (c) for the definition's (c)(i).
- **Paragraphs** are the clauses of its provision inside it.

**2. The cross-encoder reads each fused library-law candidate in its
provision:** its chapeaux' text, outermost first, then its own
(`read_in_provision`). It is the same twenty candidates, and the reranker
still keeps ten. Nothing it reads is cited or delivered as read, and every
clause keeps its own canonical text in the chunk, the vector, BM25, the
context and the verifier. On C2026C00227 the longest text read is 115 reranker
tokens, inside the 420 a chunk is allowed (`CHUNK_MAX_TOKENS`). A test checks
every clause of the compilation, so the reranker service never refuses one
for its length.

**3. The reranked top 10 are delivered whole:** each library-law clause with
its chapeaux before it and its paragraphs after it, each clause once, in rank
order (`RetrievalTrace.delivered()`). So does the router's exact hit, which
skips the reranker as before (D-04).

- **Added beyond the ten, never ranked.** The reranker's top 10 and their order
  are what they were (`ranked_candidates()`), and nothing ranked is pushed
  out. `answer()` gives the model the delivered list. The owed exceptions and
  qualifiers (ADR-0044) are then added to it as before, by exact id.
- **Never the whole provision of a ranked paragraph.** It brings its chapeaux,
  not its sibling paragraphs (below).

**4. One read, through every filter.** `RetrievalStore.lookup_lineage(ids)`
returns each clause's **lineage**: the clauses that contain one of `ids` or are
inside one, whatever lies between. Which of them share its provision is
decided by the pipeline, not the store. For the fused
candidates it is one Qdrant scroll on `clause_id` and the `ancestors` payload
(ADR-0035), through the same `_build_filter` as every read. So the law scope,
subscriptions and validity bind what comes with a clause exactly as they bind
the clause: under a scope that cites only APP 11.1(a), no chapeau is read or
delivered with it. The fake applies the same rule, and a contract holds both
to it. The deployed `clauses` collection already carries `ancestors` (#144,
loaded after #163), so it needs no change.

**5. Library law only.** The policy parser emits flat clauses, so nothing would
change for a tenant's document. A tenant's provision structure, and its
privacy rules, get their own decision when a parser produces one.

**6. The gate measures the delivered list** (`eval/recall.py`, `DELIVERED`)
beside the reranked one:

- **The targets move to the delivered list**, which is what generation is
  given. ADR-0043 already put them on "what `retrieve()` delivers"; this makes
  that literal.
- **The reranked list keeps its floors.** A ranking regression fails there even
  where delivery would mask it. One is lowered, by this decision and for a
  reason it causes: law paraphrase, 1.0 to 5/6. On CI's platform (the first
  run of this change's PR) GQ-10's APP 11.2 left the reranked ten, below its
  own paragraphs, which the cross-encoder now reads with 11.2's text. One of
  them, (b) "no longer needs the information", answers the question ("If we
  no longer need someone's personal details") more exactly, and locally it
  ranks first. 11.2 is delivered as their chapeau, so the delivered
  cell, which carries the target, stays 1.0. Locally (int8 numerics on
  arm64) 11.2 stays in the ten. A floor holds on both platforms, so it is the
  lower. It is the only reranked floor this decision changes, and nothing
  else of the reranked list may drop.
- **An expected clause counts on the delivered list** if it was ranked in the
  top 10 or is in the provision of one that was, never because anything was
  ranked deeper. The delivered list is longer than ten, and the gate prints how
  long, query by query.
- **The per-query baseline gains `delivered`** for every query: fusion's top 10
  delivered whole, so a change to delivery fails the `ci` job by query id with
  no model. No existing value in it changed, and the corpus is the same.
- **What a ranked clause can bring is pinned.** Delivered recall counts every
  clause delivered, so a rule that widened unseen, say a looser
  `in_same_provision`, could raise recall with no better ranking. On the
  whole compilation, 497 clauses have a chapeau (two at most), 152 have
  paragraphs (22 at most, "enforcement body"), 593 pairs in all, and a test
  holds those numbers, so a change is made deliberately. The pipeline's
  shape is pinned on fixtures: a ranked paragraph brings its chapeaux and not
  its siblings. The size of each query's delivered list isn't pinned:
  fusion's tenth candidate can move at a tie straddling a channel's cut
  (#177), and GQ-18's list under fusion was 31 clauses in three of four runs
  on fresh collections and 32 in the fourth, its recall unchanged.
- **This changes what the bar counts.** "recall@10" on the delivered list is
  the top 10 ranked, each with its provision: up to 34 clauses for one
  question here. The number is honest only with that said beside it, which
  the gate's report and this ADR do. Whether the human accepts the target
  being met on that list is the human's call (Status).

## Floors and targets after this decision

| cell | reranked floor | delivered floor | target (delivered) | state |
|---|---|---|---|---|
| exact-ID, law / policy | 1.000 / 1.000 | 1.000 / 1.000 | 1.0 | met |
| paraphrase, law / policy | **0.833** (was 1.000) / 1.000 | 1.000 / 1.000 | 0.9 | met |
| cross-clause, policy | 0.900 | 0.900 | 0.8 | met |
| **cross-clause, law** | 0.444 | **0.833** (5/6) | 0.8 | **met** (was open at 0.444) |

The fused floors are unchanged. Every delivered floor is at or above its
target, so `OPEN_TARGETS` is empty, and the strict xfail has nothing left to
expect. Two local reranked runs on fresh collections gave the same cells.
CI's platform gave the same delivered cells. In the reranked list it gave law
paraphrase 0.833 (above) and policy cross-clause 1.0, since GQ-21's 1/1 ranks
in there. Each floor is the lower of the two platforms.

Met in clauses: one of the seven law cross-clause clauses is missing, the
most the target allows (ADR-0043). Every expected clause delivered is brought
by a ranked clause at least 0.83 logit above the 11th: GQ-20's (ca) by 0.91,
and GQ-23's 3/1, unchanged, by 0.83. GQ-21's 1/1 stays 12th, as ADR-0043
found.

## What remains, and why it isn't tuned around

- **GQ-19's APP 10.1 is still missed, and it is not a ranking problem.** The
  question names "APP 10". No chunk's text carries a clause's reference: the
  display form is derived from the path and never stored (CONTEXT.md:
  Display form), and the parser keeps no heading (every `heading` in
  `clauses.json` is null), so neither APP 10's number nor its title, "quality
  of personal information", is anywhere a channel or the cross-encoder can
  match. Without them the question is about "collection versus use or
  disclosure", where APPs 3, 5 and 6 are the better match on meaning. The
  router sends only a whole-query stable id to the keyword channel (issue
  #5: prose that mentions an id falls through). Two principled fixes, each a
  decision of its own and neither taken here (#206):
  - resolve a reference in a question's prose ("APP 10", "s 6C(3)") to its
    clauses by exact id, beside the full pipeline (D-12's regex router,
    widened). That needs a display-form parser per citation convention and a
    payload field for the clause path. With it, GQ-19 would be 1.0 and the
    cell 1.0;
  - or carry a clause's reference or heading in what is embedded and indexed.
    That changes chunks (CONTEXT.md: Chunk) and needs a re-embed.
- **GQ-20's delivered context holds one limb of the definition of "agency".**
  It is "agency means:" with paragraph (ca), not (a) to (l). The label, which
  is the definition's own clause, is met honestly: part of the definition was
  ranked, and the clause that names the term came with it. But an answer to
  "who counts as an agency" from this context is incomplete. Delivering a
  ranked paragraph's whole provision would give all fifteen. It was measured
  at the same recall, with 23.5 delivered clauses a question against 15.7, and
  73 for GQ-20 against 34. It wasn't taken: it also brings all 22 limbs of "enforcement
  body" for GQ-20, because one of them was ranked, and a scope decision of
  this size is the human's.

## Consequences

- **More law reaches the model.** Measured through `answer()` with fake
  generation, over the 25 golden questions, the retrieved fence grows from
  733 to 1,113 reranker-tokenizer tokens a question on average (a proxy for
  Claude's tokens), and at most from 1,173 to 2,889 (GQ-20). Over the nine law
  questions that aren't exact ids it grows from 965 to 1,799. At
  `claude-sonnet-5`'s $2.20/M input, that is $0.0008 more a question on
  average and $0.004 at most, or about $1.70 a month at PHASES.md's 2,000
  queries. The CAG prefix and the system prompt are unchanged.
- **Latency.** One more Qdrant scroll per retrieval, before the rerank, and
  slightly longer texts for the cross-encoder (at most 115 tokens). Locally,
  over the 17 ranked questions, the rerank took 4.52 s median (5.40 s max)
  reading in provision against 4.37 s (4.98 s) reading bare, about 3% more.
- **Fewer fetches by id.** A qualifier's paragraph the ranking left out now
  usually comes with its ranked chapeau, so `answer()` fetches it less often.
  The guarantee is unchanged: what is still missing is fetched, or the answer
  is refused.
- **The verifier needs no change.** A delivered chapeau or paragraph is in the
  context, so it is citable, and it is shown as its own canonical text.
- **What a chunk is doesn't change.** CONTEXT.md's Chunk budget now also bounds
  what the cross-encoder reads, a clause after its chapeaux, which a test
  holds on the compilation. A new compilation (Phase 4, #198) must keep it;
  the test moves with the gate corpus.
- **Spans:** `retrieval.read_in_provision` lists the fused candidates read with
  a chapeau, and `retrieval.chapeaux` and `retrieval.paragraphs` list what was
  delivered with the ranked ten.
- **Phase 1's retrieval criterion is met** in the sense ADR-0043 defines, with
  no open target, once CI's `reranker-fidelity` job confirms it on its
  platform.
