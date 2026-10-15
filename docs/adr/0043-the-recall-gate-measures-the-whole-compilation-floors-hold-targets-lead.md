# ADR-0043 — The recall gate measures the whole compilation: floors hold today's numbers, targets say what Phase 1 needs

- Status: accepted, merged under the merge protocol (PR #201, 2026-10-15). The
  gate corpus is the human's decision on #180; the bars are the model's
  proposal, implemented so they are concrete, and the human can change them.
  Two questions are put to the human: whether Phase 1 may exit with #200's
  target open, and whether fused paraphrase should carry a target. Amended by
  ADR-0045 (#200): the targets are on the delivered list, the reranked ten
  delivered whole; the reranked list keeps its floors; law cross-clause is
  0.833 there, and its target is met and closed
- Date: 2026-10-15
- Issue: #180
- Amends: ADR-0022 (which corpus the bars are measured on, on which list, and
  how they are enforced) and PHASES.md's Phase 1 retrieval exit criterion
- Revises: D-04's rationale only (`DECISIONS.md`), which said the top 20
  "clears every recall@10 exit bar"; that was on the excerpt. The decision,
  rerank the top 20, stands (below)
- Relates to: ADR-0035 (its numbers stay on the excerpt)

## Context

The recall gate measured retrieval over the golden corpus: the 75-clause
Privacy Act excerpt and the six Acme policies. Production's `clauses`
collection holds the whole compilation's 779 selected clauses (#144), ten
times the distractors. Measured there, recall was lower (#180). The human's
decision:

> "#180 — yes, gate on the full compilation. A gate measuring a 75-clause
> excerpt is measuring an easier problem than production. Set the bars from
> the real numbers rather than carrying the excerpt's: propose bars you think
> are honest for 779 clauses and say why. 0.444 cross-clause needs work, but I
> want the bar to be a real target, not a rubber stamp."

**The gate corpus.** The raw compilation is never committed, so its parse is:
`corpus/instruments/privacy-act-1988/C2026C00227/clauses.json`, the 779
clauses the selection manifest puts in scope, exactly as the parser emits
them. `scripts/export_compilation_clauses.py` is its only writer, from the
pinned bytes (sha256 checked). With the source at hand,
`PRIVACY_ACT_COMPILATION_HTML` turns on a test that a fresh parse renders to
the file byte for byte. In CI, the clauses the excerpt and constructs fixtures
also hold (75 and 100) are checked against their fresh parse. The excerpt's 75
are a subset, with the same ids and text. With the Acme policies, the gate
loads 822 chunks. The embedding cache holds real `gemini-embedding-001`
vectors for all 824 texts (756 distinct law texts, 43 policy chunks, 25
queries). It stays exact float32 JSON, 6.8 MB.

## The measurement

2026-10-15, local Qdrant v1.19.1, cached vectors, local BM25 with IDF, and
the int8 cross-encoder over the top 20 fused candidates. Six fused runs and
three reranked runs on fresh collections were identical, query for query.
recall@10, per category and **document class**: law is the `_public`
queries over the library, and policy is Acme's queries over its own six
policies.

| category | class | n | excerpt fused | excerpt reranked | **gate fused** | **gate reranked** |
|---|---|---|---|---|---|---|
| exact-ID | law | 6 | 1.000 | 1.000 | 1.000 | 1.000 |
| exact-ID | policy | 2 | 1.000 | 1.000 | 1.000 | 1.000 |
| paraphrase | law | 6 | 1.000 | 1.000 | **0.833** | 1.000 |
| paraphrase | policy | 3 | 1.000 | 1.000 | 1.000 | 1.000 |
| cross-clause | law | 3 | 0.722 | 0.722 | **0.444** | **0.444** |
| cross-clause | policy | 5 | 0.900 | 1.000 | 0.900 | **0.900** |
| *paraphrase, all 9* | | | *1.000* | *1.000* | *0.889* | *1.000* |
| *cross-clause, all 8* | | | *0.833* | *0.896* | *0.729* | *0.729* |

The gate's law numbers are the deployed collection's (#180, after #179:
paraphrase 0.833 fused and 1.000 reranked, cross-clause 0.444 and 0.444). The
container now measures what users get.

Why each miss happens, from each expected clause's rank per channel:

- **GQ-10** (paraphrase, law): APP 11.2 is 6th by dense and absent from sparse's
  top 50, which fills with short s6 definitions; fusion puts it 11th. The
  reranker sees it among the 20 and puts it 4th.
- **GQ-18** (cross-clause, law): APP 11.1(a) is "from misuse, interference and
  loss; and"; 11.1(b) is "from unauthorised access, modification or
  disclosure." Their meaning is in 11.1's chapeau, which they don't carry. (a) is
  in neither channel's top 50; (b) is 25th by dense and 46th fused. 11.1 itself is
  1st.
- **GQ-19** (cross-clause, law): APP 10.1 is a full sentence, 18th by dense,
  absent from sparse's top 50, 36th fused; APPs 5, 3 and 6 outrank it. This is a
  ranking problem, not a fragment.
- **GQ-20** (cross-clause, law): `s6/1/agency` is "agency means:". The
  definition's own clause is three words, and its content is its paragraphs. It
  is in neither channel's top 50, and it was never found on the excerpt either.
- **GQ-21** (cross-clause, policy): Acme sees only its own policies, yet adding
  law moved it. BM25's IDF is computed over the whole collection, so 779 law
  clauses changed the sparse scores of Acme's chunks (top score 29.8 → 42.8)
  and the fused top 20 with them. The reranker then puts data-security-policy
  1/1 12th, 0.14 logit under the 10th. Production holds the same mix.
- **GQ-23** (cross-clause, policy): fused 0.5 on both corpora; the reranker
  recovers it.

Three of the four law clauses still missed after reranking are **fragments**:
a paragraph without its chapeau, and a definition without its paragraphs. None
of the four is in the fused top 20, so the cross-encoder never sees them.

More candidates don't help. Reranking the top 50 instead
(`scripts/benchmark_reranker_recall.py`, now on the gate corpus) leaves law
cross-clause at 0.444: APP 10.1 is fused 36th and enters the 50, but the
cross-encoder doesn't lift it into the top 10. It also loses GQ-10's APP
11.2 (paraphrase 0.889), and recovers only GQ-21 (cross-clause 0.792
overall). Median local rerank time is 11.5 s against 4.5 s. D-04's top 20
stands on the whole compilation.

## Decision

1. **The gate corpus is the whole compilation's selected clauses** with the
   Acme policies, as above. The excerpt stays for unit and contract tests and
   for numbers pinned on it (ADR-0035's). The two recall gates, the reranker
   benchmarks and `scripts/measure_recall_on_cloud.py` move to the gate corpus.
2. **A bar is per cell:** one list's mean recall@10 over one category's queries
   of one document class. Law and policy are kept apart because on
   cross-clause they are 0.444 and 0.900, and a category mean of 0.729 lets
   the policy half carry the law half. That already happened once: the
   excerpt's 0.896 "met" the cross-clause bar while its law half was 0.722.
   Only the law half can be measured on the deployed collection, too.
3. **Floors, enforced: every cell at what it achieves today** (`FLOORS` in
   `eval/recall.py`), fused and reranked, so any drop fails. The fused gate
   also holds every list of every query to its baseline, re-recorded on the
   gate corpus. The baseline now carries the corpus's sha256, so it can't be
   compared against a corpus it wasn't measured on.
4. **Targets: what Phase 1 needs.** These are PHASES.md's bars, kept: exact-ID
   1.0, paraphrase 0.9, cross-clause 0.8. They apply on the delivered list
   (reranked; for an exact-ID query, the router's lookup), for law and for
   policy each. Five of the six cells meet theirs today. One is open:
   **cross-clause law, 0.444 against 0.8.**
5. **An open target is a strict xfail** (`raises=AssertionError`, so a broken
   measurement errors rather than passing as expected), generated from the
   data: every cell whose floor is under its target. The day the target is
   met, the test passes, strict xfail fails the build, and the fix is to raise
   the cell's floor to the new value. That removes the xfail, so the target is
   ratcheted into a floor. The mechanism was checked by lowering the open
   target under today's value: the build failed.

| cell | floor (enforced) | target | state |
|---|---|---|---|
| exact-ID, law / policy — fused and reranked | 1.000 | 1.0 (reranked) | met |
| paraphrase, law — fused | 0.833 | none | floor only |
| paraphrase, policy — fused | 1.000 | none | floor only |
| paraphrase, law / policy — reranked | 1.000 | 0.9 | met |
| cross-clause, law — fused | 0.444 | none | floor only |
| cross-clause, policy — fused | 0.900 | none | floor only |
| cross-clause, policy — reranked | 0.900 | 0.8 | met |
| **cross-clause, law — reranked** | **0.444** | **0.8** | **open** |

## The argument for each number

The sample is small. There are 3 law and 5 policy cross-clause queries, and
6 and 3 paraphrase ones. One law cross-clause query moves its cell by a
third, and one expected clause by a sixth or a ninth. So the bars below are
counts of clauses as much as rates, and they are argued that way.

The targets keep PHASES.md's numbers, but not because the excerpt had them.
A target is what a reader needs, and that doesn't depend on how many
distractors the corpus holds. So each number was re-read as a count of
clauses in its cell on the whole compilation. Each one survived as the right
count; none was lowered to fit today's results:

| cell | queries / expected clauses | the target, in clauses |
|---|---|---|
| exact-ID, law / policy | 6 / 6, 2 / 2 | none missing |
| paraphrase, law / policy | 6 / 6, 3 / 3 | none missing: one miss is 0.833 or 0.667 |
| cross-clause, law | 3 / 7 | at most one of the seven missing |
| cross-clause, policy | 5 / 10 | at most two of the ten missing (each miss is 0.1) |

- **Exact-ID: 1.0, floor and target.** The router's keyword lookup either
  finds the id or has a bug. Unchanged from the excerpt.
- **Paraphrase, delivered: floor 1.000, target 0.9, met.** Every paraphrase
  question finds its clause in what `retrieve()` returns, in both halves. With
  six and three queries per cell, 0.9 already means no miss: one miss gives
  0.833 or 0.667. The floor (1.0) and the target therefore agree today. 0.9 is
  kept as the target rather than raised to 1.0 for when the golden set grows
  past ten queries in a cell; it is then one miss in ten.
- **Paraphrase, fused: floor 0.833 (law) and 1.000 (policy), no target.** The
  excerpt gated paraphrase on fusion at 0.9, and fusion is now 0.833 for law
  (GQ-10). The target is retired on fusion, not lowered, because nothing
  delivers fusion's order: `retrieve()` always reranks, and an exact-ID query
  skips fusion. ADR-0022 already moved cross-clause to the delivered list for
  this reason; this moves paraphrase with it. The fused gate's job is to catch a
  regression on every build, platform-exact and with no model, and the floor
  and the per-query baseline do that. GQ-10's clause is fused 11th, inside
  the 20 the reranker sees. If it fell past 20, the reranked floor (1.0)
  would fail. *The alternative, if the human prefers:* keep fused
  paraphrase 0.9 as a second open target, so GQ-10's sparse weakness stays
  on the board. I didn't, because I'd keep targets to what the product needs.
- **Cross-clause, law, delivered: floor 0.444, target 0.8, open.** Three
  queries hold seven expected clauses, so **≥ 0.8 means at most one of the
  seven is missing** from the top 10. One miss gives 0.833 or 0.889; two
  misses give at most 0.778. Today four are missing; the excerpt missed two.
  - *Why 0.8 and not lower.* A reader who asks for "all the specific ways"
    APP 11.1 requires protection, and gets 11.1 without (a) and (b), gets an
    answer that is wrong by omission. Two of seven missing is not good enough
    for a compliance answer. Parity with the excerpt (0.722) would certify
    that the 704 extra clauses cost nothing, while still missing two. 0.8 is
    also the bar the excerpt's category mean met while its law half sat at
    0.722, so this is the first time law is actually held to it.
  - *Why not 1.0.* `s6/1/agency` is "agency means:". No retriever can rank
    three words on meaning, so getting it means changing what a definition's
    clause carries: a corpus decision, not a gate number. 0.8 leaves room for
    exactly that one.
  - *What meeting it takes.* Recovering GQ-19's APP 10.1 alone gives 0.611.
    Reaching 0.8 also needs both of GQ-18's fragments, (a) and (b). So it
    can't be met by tuning around the edges. It needs the fragment problem
    addressed: for example, a paragraph's chunk text carrying its chapeau (a
    change to what a chunk is, `CONTEXT.md` **Chunk**, with a re-embed), or
    retrieval admitting a retrieved chapeau's paragraphs, as ADR-0035 does for
    scope. Tracked in #200.
- **Cross-clause, policy, delivered: floor 0.900, target 0.8, met.** Five
  queries hold ten expected clauses, two each, so each miss costs 0.1. The
  target allows two misses and today has one. 0.8 is one bar for both
  halves: in clauses, it allows one of law's seven and two of policy's ten. I
  kept it equal rather than tightening policy, because no reader need
  separates the two. The floor is
  stricter than the target: today's 0.900, so a second miss fails now, even
  though the target would allow it. Today's miss, GQ-21's 1/1 at 12th, is the
  gate's nearest thing to a coin flip: 0.14 logit under the 10th. On another
  platform's int8 numerics it may come back as 1.0, which passes.
- **Category means are not gated separately.** The cells imply them: with
  law at its target and policy at its floor, cross-clause is ≥ 0.863.

## What Phase 1's "met" means now

The retrieval criterion is met when no open target is left: every reranked
cell at or above its target, in CI. Today it is **not met**: cross-clause law
is 0.444 against 0.8. Every other cell meets its target. The 2026-10-08 "met"
(ADR-0022) was on the excerpt, and its law half was below the bar.

Whether Phase 1 can exit with this target still open is the human's call.
This ADR keeps it as a Phase 1 exit criterion, because the human asked for a
real target, and a target that a phase gate doesn't wait for is easy to
forget. The alternative is to move the open target to a later phase, by
issue and a `PHASES.md` edit, keeping the strict xfail in CI either way.

## Consequences

- The `ci` job upserts ~822 chunks into its service Qdrant instead of ~118,
  once per gate module. Locally the fused gate module takes ~4.5 s. The
  reranked gate reranks the same 17 queries × 20 candidates as before, now
  measured once for its two tests.
- No per-query baseline for the reranked list, still. The int8 model
  quantises activations per batch, so a score depends on the other
  candidates (GQ-21) and possibly on the platform. The floors are per cell,
  and the reranked cells' margins were checked: every expected clause that
  is found sits at least 0.8 logit inside the cut, except GQ-21's, which is
  outside it.
- A corpus change now takes four steps: a parser or manifest change, a
  re-export of `clauses.json`, a cache refresh (the human, on Vertex), and a
  deliberate re-baseline. The corpus-sha256 test makes the last one
  impossible to skip.
- Floors are raised by hand when recall improves. An improvement short of
  its target passes without forcing a ratchet. The next re-baseline is the
  place to raise them.
- `scripts/measure_recall_on_cloud.py` compares the deployed collection with
  the container on the same corpus. Until the human runs #178 part 2's
  correction (ADR-0042), `clauses` holds the previous parse, in which seven
  selected clauses differ, so a small difference is expected.

## Appendix: every query, every list (2026-10-15)

recall@10 on the gate corpus. `dense`, `sparse` and `fused` are from the fused
gate (`NoRerank`) and match `corpus/golden/recall_baseline.json`; `reranked`
is the int8 cross-encoder over the top 20. An exact-ID query has one list:
the router's keyword lookup.

| query | category | class | dense | sparse | fused | reranked | excerpt fused | excerpt reranked |
|---|---|---|---|---|---|---|---|---|
| GQ-01–06 | exact-ID | law | – | – | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-07–08 | exact-ID | policy | – | – | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-09 | paraphrase | law | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-10 | paraphrase | law | 1.000 | 0.000 | **0.000** | 1.000 | 1.000 | 1.000 |
| GQ-11 | paraphrase | law | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-12 | paraphrase | law | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-13 | paraphrase | law | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-14 | paraphrase | law | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-15 | paraphrase | policy | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-16 | paraphrase | policy | 1.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-17 | paraphrase | policy | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-18 | cross-clause | law | 0.333 | 0.333 | **0.333** | **0.333** | 0.667 | 0.667 |
| GQ-19 | cross-clause | law | 0.000 | 0.500 | **0.500** | **0.500** | 1.000 | 1.000 |
| GQ-20 | cross-clause | law | 0.500 | 0.500 | 0.500 | 0.500 | 0.500 | 0.500 |
| GQ-21 | cross-clause | policy | 1.000 | 1.000 | 1.000 | **0.500** | 1.000 | 1.000 |
| GQ-22 | cross-clause | policy | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-23 | cross-clause | policy | 1.000 | 0.500 | 0.500 | 1.000 | 0.500 | 1.000 |
| GQ-24 | cross-clause | policy | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| GQ-25 | cross-clause | policy | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

Per category, all queries in a category together: dense 1.000 / 0.729,
sparse 0.778 / 0.729 (paraphrase / cross-clause).
