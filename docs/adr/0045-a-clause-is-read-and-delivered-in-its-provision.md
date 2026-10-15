# ADR-0045 — A clause is read and delivered in its provision

- Status: accepted, the mechanism only. The human's decision on #207
  (2026-10-15), "merge the mechanism, not the metric", is quoted under
  Decision 6. The PR first proposed moving ADR-0043's targets to the delivered
  list. The human declined that, so the targets stay on the reranked list.
  The same decision asked for GQ-10's lowered floor to be chased rather than
  accepted. Its root cause was a chapeau read bare, so decision 2 now reads a
  chapeau with its paragraphs too (GQ-10 on CI, below). That part was made
  after the human's review and is reported for it
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
| the cross-encoder reads each candidate after its chapeaux; within k | 0.444 | 1.0 | 0.5 | 0.5 | 0.667 | unchanged | 10 / 10 |
| the cross-encoder reads each candidate after its chapeaux; paragraphs delivered beyond k, no chapeaux | 0.444 | 1.0 | 0.5 | 0.5 | 0.667 | unchanged | 13.9 / 30 |
| the cross-encoder reads each candidate after its chapeaux; chapeaux and paragraphs delivered beyond k (this PR's first version) | 0.444 | 1.0 | 0.5 | 1.0 | 0.833 | unchanged on arm64; on x86, ranked paraphrase law **0.833** (GQ-10 lost, below) | 15.7 / 34 |
| that, delivering a ranked paragraph's whole provision (its siblings too) | 0.444 | 1.0 | 0.5 | 1.0 | 0.833 | unchanged | 23.5 / 73 |
| **the cross-encoder reads each candidate whole, its chapeaux, itself, then its paragraphs; chapeaux and paragraphs delivered beyond k (this decision)** | **0.444** | **1.0** | 0.5 | **1.0** | **0.833** | **unchanged, on arm64 and on x86** | **17.0 / 41** |

"Other cells" are exact-ID 1.0 and paraphrase 1.0 for law and policy, and
policy cross-clause 0.9, in both metrics. Locally the reranked list's cells
are ADR-0043's in every row but (B)'s. **No option raises ranked recall@10
for law cross-clause above 0.444 except (B)'s half, which costs GQ-10 and
GQ-20.** Delivered sizes are over the 17 questions that aren't exact ids.
The last row was measured after the human's decision on #207, to fix GQ-10
(decision 2). Every other row is the PR's first measurement.

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
  means: a body …", it ranks 7th (8th read whole), 0.91 logit inside the cut, and its chapeau
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

**2. The cross-encoder reads each fused library-law candidate whole, in its
provision:** its chapeaux' text, outermost first, then its own, then its
paragraphs' in clause order (`read_in_provision`). That is the order
delivering it gives the model (decision 3), so a candidate is never read as a
fragment of the law: neither a paragraph without the words that introduce it,
nor a chapeau without its list. It is the same twenty candidates, and the
reranker still keeps ten. Nothing it reads is cited or delivered as read, and
every clause keeps its own canonical text in the chunk, the vector, BM25, the
context and the verifier.

- **Bounded by the chunk budget.** Paragraphs are added whole while the
  reading stays within the 420 tokens a chunk is allowed (`CHUNK_MAX_TOKENS`,
  by the reranker's own tokenizer). The reranker service refuses an
  over-long pair rather than truncating it (#14), so it never refuses a
  candidate. On C2026C00227 a clause after its chapeaux is 115 tokens at
  most. Read whole, APP 8.2 is the longest at 388, and APP 5.2 is the one
  provision too long to read whole: it is read to its last paragraph that
  fits. A test checks every clause of the compilation.
- **Why whole, and not after its chapeaux only, as the PR first had it.** Read
  only after its chapeaux, a paragraph carries its chapeau's whole text, but
  the chapeau itself is read bare. So a chapeau competes with its own
  paragraphs, each of which reads as the chapeau and more. That cost GQ-10 on
  CI (below).

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

**6. The gate reports delivered-recall as a metric of its own**
(`eval/recall.py`, `DELIVERED`), beside recall@10 and never combined with it.
The PR first moved ADR-0043's targets to the delivered list, which would have
closed law cross-clause at 0.833. The human's decision on #207:

> "merge the mechanism, not the metric. Reranking post-chapeau and delivering
> clauses whole are both right. But 0.833 counting a 35-clause delivered list
> while the ranked top 10 is still 0.444 measures a different thing —
> retrieval's job is to rank, and a longer delivery list is a separate
> mechanism. Report them separately: recall@10 on the ranked list stays the
> retrieval metric and stays at 0.444; add delivered-recall as its own metric
> with its own floor. #180 existed to stop the gate flattering itself, and one
> merged number would undo that. Also chase GQ-10's dropped floor on CI rather
> than accepting it."

- **recall@10 on the reranked list stays the retrieval metric, and the
  targets stay on it** (ADR-0043, unchanged). Law cross-clause there is 0.444
  against 0.8, an open target, a strict xfail in `OPEN_TARGETS`, as ADR-0043
  had it. This decision lowers no reranked floor.
- **Delivered-recall** is the share of a query's expected clauses in the
  delivered list. It has its own floor in every cell and **no target**, so it
  can neither meet nor close one. An expected clause counts there if it was
  ranked in the top 10 or is in the provision of one that was, never because
  anything was ranked deeper. The delivered list is longer than ten, so
  delivered-recall is not a recall@10, and the gate prints the list's size
  beside it. The gate tests and `scripts/measure_recall_on_cloud.py` report
  the two metrics apart.
- **A word that changed meaning.** ADR-0043 and ADR-0022 call the reranked
  top 10 "the delivered list", "what `retrieve()` delivers". Since this ADR
  that name means the ten delivered whole. Read in those ADRs, it means the
  reranked list, which is where their targets are.
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

## GQ-10 on CI, and why a chapeau is read with its paragraphs

The PR's first version read a candidate after its chapeaux only. On CI's x86
runner GQ-10's APP 11.2 then fell out of the reranked ten, and the PR lowered
the reranked law-paraphrase floor to 5/6. The human asked for the root cause
instead. The 20 candidates' scores for GQ-10, on each platform:

| reading | numerics | APP 11.2's rank | its score | against the cut |
|---|---|---|---|---|
| bare (before this ADR) | arm64 | 4th (ADR-0043) | | inside by 0.8 logit or more |
| after its chapeaux | arm64 | 9th | -7.469 | +0.177 over the 11th |
| after its chapeaux | x86-64, AVX2 without VNNI (emulated) | **13th** | -8.362 | -0.836 under the 10th |
| after its chapeaux | x86-64, ORT's U8U8 precision mode | **12th** | -7.962 | -0.093 under the 10th |
| after its chapeaux | arm64, graph optimisations off | **13th** | -8.159 | -0.318 under the 10th |
| **whole (this decision)** | arm64 | **1st** | -4.013 | **+3.452** over the 11th |
| **whole (this decision)** | x86-64 (emulated) | **1st** | -3.694 | **+3.627** over the 11th |

The x86 rows ran in the production reranker image (`reranker:953dc6f`: the
same int8 artefact by sha256, ORT 1.20.1) under Docker's `linux/amd64`
emulation on the Mac, which reports AVX2 and no AVX-512 or VNNI. **It
reproduced CI exactly**: reranked law paraphrase 0.833 (GQ-10 lost) and
policy cross-clause 1.0 (GQ-21's 1/1 10th there, 12th on arm64).

The three hypotheses, tested in order:

- **(a) U8S8 saturation: not the cause.** On x86 with AVX2 and no VNNI, ORT
  computes U8S8 integer matmuls with `VPMADDUBSW`, which can saturate.
  `session.x64quantprecision` makes ORT use U8U8 there instead. It moves every
  score (by 0.23 logit on average), and 11.2 is still 12th.
- **(b) A near tie: the cause.** The int8 model's scores move by about 0.2
  logit on average, and by up to 1.3, under any change of kernel. Over all
  17 ranked questions' candidates, x86 against arm64 gives 0.22 mean and 1.03
  at most. U8U8 against arm64 gives 0.22 and 1.29. Arm64 with ORT's graph
  fusions off against on gives 0.19 and 1.28: the same integer arithmetic in
  a different float order. A margin of 0.18 is inside that, so where 11.2
  lands is a coin flip on any platform. Neither the Mac's side of it nor CI's
  is the right one.
- **(c) Truncation: not the cause.** The longest pair in GQ-10's batch is 128
  tokens of the reranker's 512.

**Why the margin was 0.18.** Read bare, 11.2 was 4th. Read after its
chapeaux, each of 11.2's paragraphs reads as 11.2's text plus one condition.
11.2's stored text is "If: the entity must take such steps as are reasonable
in the circumstances to destroy the information or to ensure that the
information is de-identified": the obligation without its conditions. Read
bare, that is a fragment. Its paragraph (b), "the entity no longer needs the
information …", read with that text, ranks 1st, and 11.2 fell to 9th. Read
whole, 11.2 is its sentence, conditions included. It ranks 1st on both
platforms, 3.5 logit clear of the cut, more than twice the largest movement
any kernel change produced.

## Floors and targets after this decision

| cell | recall@10 floor (reranked) | target (reranked) | delivered-recall floor | delivered-recall target |
|---|---|---|---|---|
| exact-ID, law / policy | 1.000 / 1.000 | 1.0 | 1.000 / 1.000 | none |
| paraphrase, law / policy | 1.000 / 1.000 | 0.9 | 1.000 / 1.000 | none |
| cross-clause, policy | 0.900 | 0.8 | 0.900 | none |
| **cross-clause, law** | **0.444** | **0.8: open** | 0.833 (5/6) | none |

The fused floors are unchanged, and no reranked floor changed. `OPEN_TARGETS`
is law cross-clause on the reranked list, as ADR-0043 had it. The final
version was measured on arm64 through the gate, and on x86 by scoring the
same 17 batches in the production image under emulation. The two gave the
same cells, except policy cross-clause, which was 0.9 on arm64 and 1.0 on x86
in both metrics: GQ-21's 1/1 is 12th on arm64 and 10th on x86. Each floor is
the lower of the two platforms.

In clauses, law cross-clause misses four of its seven expected clauses in
ranked recall@10, as in ADR-0043, and one (GQ-19's APP 10.1) in
delivered-recall. Every law clause found on either list is brought by a
ranked clause at least 0.89 logit above the 11th, on both platforms. The
closest is GQ-20's "agency means:", via (ca), at 0.91 on arm64 and 0.90 on
x86. GQ-21's 1/1 is the gate's one coin flip: 0.04 logit inside the cut on
x86, and 0.14 outside it on arm64, as ADR-0043 found.

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
  is the definition's own clause, is counted honestly in delivered-recall: part of the definition was
  ranked, and the clause that names the term came with it. But an answer to
  "who counts as an agency" from this context is incomplete. Delivering a
  ranked paragraph's whole provision would give all fifteen. It was measured
  at the same recall in the first version, with 23.5 delivered clauses a question against 15.7, and
  73 for GQ-20 against 34. It wasn't taken: it also brings all 22 limbs of "enforcement
  body" for GQ-20, because one of them was ranked, and a scope decision of
  this size is the human's.

## Consequences

- **More law reaches the model.** Measured through `answer()` with fake
  generation, over the 25 golden questions, the retrieved fence grows from
  735 to 1,179 reranker-tokenizer tokens a question on average (a proxy for
  Claude's tokens), and at most from 1,153 to 3,318 (GQ-20). Over the nine law
  questions that aren't exact ids it grows from 970 to 1,983. At
  `claude-sonnet-5`'s $2.20/M input, that is $0.001 more a question on
  average and $0.005 at most, or about $2 a month at PHASES.md's 2,000
  queries. The CAG prefix and the system prompt are unchanged.
- **Latency: the rerank is slower, mostly at the tail.** One more Qdrant
  scroll per retrieval, before the rerank, and longer texts for the
  cross-encoder. The service pads a batch to its longest pair, so one long
  provision among the twenty slows the whole batch. Locally (arm64, the
  service's two threads, the 17 ranked questions twice, interleaved), the
  rerank took:

  | reading | median | mean | max |
  |---|---|---|---|
  | bare (before this ADR) | 4.98 s | 5.01 s | 6.23 s |
  | after its chapeaux (the PR's first version) | 5.25 s | 5.52 s | 10.25 s |
  | whole (this decision) | 6.40 s | 7.60 s | 17.71 s (GQ-20, padded to 316 tokens) |

  Cloud Run's times weren't measured. The reranker's per-request cost is
  mostly its cold start (~37 s, SERVICES.md), so at 2,000 queries a month
  this narrows the free-tier headroom rather than adding a bill. PHASES.md's
  60 s p95 for a full document check has to hold with it.
- **Fewer fetches by id.** A qualifier's paragraph the ranking left out now
  usually comes with its ranked chapeau, so `answer()` fetches it less often.
  The guarantee is unchanged: what is still missing is fetched, or the answer
  is refused.
- **The verifier needs no change.** A delivered chapeau or paragraph is in the
  context, so it is citable, and it is shown as its own canonical text.
- **What a chunk is doesn't change.** CONTEXT.md's Chunk budget now also bounds
  what the cross-encoder reads, a clause whole in its provision.
  `read_in_provision` enforces it by counting with the reranker's vendored
  tokenizer, and a test holds it on the compilation. Wherever `retrieve()`
  runs, that tokenizer file (`vendor/bge-reranker-v2-m3-tokenizer/`) must ship
  with it. A new compilation (Phase 4, #198) must keep the budget; the test
  moves with the gate corpus.
- **Spans:** `retrieval.read_in_provision` lists the fused candidates read with
  a chapeau or a paragraph, and `retrieval.chapeaux` and `retrieval.paragraphs`
  list what was delivered with the ranked ten.
- **Phase 1's retrieval criterion is unchanged:** law cross-clause recall@10
  is 0.444 against 0.8, an open target (ADR-0043). Delivered-recall doesn't
  count toward it.
- **The production reranker's numerics are its own.** It is the same image,
  but its CPU wasn't checked (no cloud call). Any change of kernel moves the
  int8 model's scores by about 0.2 logit, so at a near tie, such as GQ-21's
  1/1, production may rank either way. `scripts/measure_recall_on_cloud.py`
  ran the reranker in-process on the Mac, so its "cloud equals container"
  finding (#180) covers the deployed Qdrant collection, not the deployed
  reranker's arithmetic. Making the model less sensitive (per-channel or
  reduced-range weights, or static quantisation) means a new artefact and a
  redeploy: a decision for the human, not taken here.
