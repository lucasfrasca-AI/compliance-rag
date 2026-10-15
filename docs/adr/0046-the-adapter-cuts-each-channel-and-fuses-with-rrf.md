# ADR-0046 — The adapter cuts each channel by `chunk_id` on a tie and fuses with RRF itself

- Status: accepted, merged under the merge protocol
- Date: 2026-10-15
- Issue: #177 (and #205, below)
- Amends: D-07's "hybrid, native RRF" (`DECISIONS.md`, ADR-0008) and
  ADR-0005's "RRF inside Qdrant". Qdrant still scores both channels; fusion
  moves into the adapter. D-04 (fuse first, rerank the top 20) is unchanged
- Relates to: #160 (the fused order), ADR-0043 (the gate), ADR-0045 (the
  delivered list's size, which a tie used to move)

## Context

#160 made the order of `fused` ours: the adapter asked Qdrant for the whole
fused union, sorted it by `(-score, chunk_id)` and cut it. Two things upstream
of that were still Qdrant's (#177):

1. **Which tied candidates enter a channel's list.** Each channel was cut at
   `limit` by Qdrant, which keeps whichever members of a tie at the cut it
   likes.
2. **Their places in it.** Native RRF scores a candidate by its place in each
   channel's list, and among equal channel scores that place was Qdrant's
   internal order. So the fused **scores** depended on it, not only ties
   between them.

Measured on the gate corpus (local Qdrant v1.19.1), four fresh collections
with `main`'s adapter, 17 ranked queries:

- a channel's order among equal scores moved between collections for 10;
- three channels have a tie straddling the 20-cut (GQ-09 dense and sparse,
  GQ-24 sparse), and Qdrant's pick changed the channel's set for GQ-09 and
  GQ-24;
- the fused order and fused scores moved for 10, and the fused top 10 for
  three (GQ-16, GQ-17, GQ-18); GQ-18's delivered list was 31 or 32 clauses
  (ADR-0045).

No recall@10 moved in those four runs, by luck of where the expected clauses
sat. With this decision, on four more fresh collections, none of these moved
for any query.

On a constructed five-way tie (the contract case below), `main`'s adapter
kept the two **highest** `chunk_id`s at a cut of 3, and at a cut of 10 put
them in the order 3, 4, 2, 1, 5, so `t-tied-3` got fused 0.667 and
`t-tied-1` 0.333. It failed the contract 20 times in 20.

Native RRF can't be given an order among equal scores: its places come from
inside Qdrant. So the fix can't keep it.

## Decision

1. **Each channel is its own query, read past its cut.** The adapter asks
   for `limit + 1`, and twice as many again while the last candidate read
   ties with the one at the cut. Every member of a tie at the cut is then in
   hand. The usual cost is one extra point's payload per channel; a tie at the
   cut costs a second request.
2. **Each channel's list is `cut` in `(-score, chunk_id)` order**
   (`adapters/fusion.py`): a tie straddling the cut keeps its lowest
   `chunk_id`s, and tied candidates take their places in `chunk_id` order.
3. **RRF runs in the adapter over those lists** (`reciprocal_rank_fusion`),
   with Qdrant's own k: 1 / (2 + place), places from 0, measured on v1.19.1.
   A fused list with no tie in either channel therefore keeps the order native
   RRF gave it. The fused list is `cut` the same way.
4. **`FakeRetrievalStore` uses the same two functions.** Its k was 60; it is
   now Qdrant's. No test's expectation changed with it.
5. **The `per_channel` lists returned are the fusion's input**, not separate
   queries beside it. Both are logged before fusion on a new `qdrant.search`
   span, with how deep each channel was read (`retrieval.channel_depth.*`);
   the `retrieve` span still logs them too.
6. **The contract says so.** `RetrievalStore.search`'s docstring no longer
   carves channel ties out. `assert_channel_cut_contract` builds a five-way
   tie in both channels, cuts it at 3, and asserts the members, their places
   and the fused scores `2 / (RRF_K + place)`, on the fake and on real Qdrant.
   Three requests per search become two.

## Consequences

- **The gate's numbers.** Every cell, fused, reranked and delivered, law and
  policy, each category, is unchanged, and so is every per-query value in
  `corpus/golden/recall_baseline.json`. The baseline is not re-recorded. What
  changed is which tie wins, now the same on every collection: listed query by
  query in the PR (#177).
- **Equal scores are exact.** A tie is two channel scores that compare equal
  as Qdrant returns them. Qdrant's BM25 scores can differ in the last float32
  place between two collections holding the same points (seen on the gate
  corpus: 15.639612 against 15.639613 for one chunk), presumably from the
  order a segment sums in. Two chunks can tie on one collection and not on
  another. The order no longer depends on Qdrant's internal order, but the
  scores it is taken from can still differ by one unit in the last place. In
  four fresh collections that happened in the sparse lists of 7 queries and
  moved no channel order and no fused list. Rounding scores before comparing
  would only move the boundary, so it isn't done; #223 tracks it.
- **Exact search assumed.** "Every member of the tie is in hand" holds while
  Qdrant searches the channel exactly, as it does for collections under its
  indexing threshold (the gate's and production's, at ~800 points). Under HNSW
  a channel is approximate anyway.
- **D-07's stack is unchanged.** Qdrant Cloud in Sydney, the same collection,
  the same vectors; nothing is re-embedded or re-deployed.
