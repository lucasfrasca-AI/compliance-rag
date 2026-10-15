"""Fusion as every `RetrievalStore` applies it (#160, #177, ADR-0046).

Two steps, both pure, shared by `FakeRetrievalStore` and
`QdrantRetrievalStore` so the two can't disagree about either:

1. **The channel cut** (`cut`): a channel's candidates in `(-score, chunk_id)`
   order, then its first `limit`. A tie straddling the cut keeps its lowest
   `chunk_id`s, and tied candidates take their places in `chunk_id` order,
   whichever order the store found them in.
2. **Reciprocal rank fusion** (`reciprocal_rank_fusion`) over those cut lists,
   in the same order and cut the same way. A candidate's fused score comes
   from its places in the cut lists, so it depends on the order of step 1 and
   on nothing else.

Fusion used to run inside Qdrant (D-07's "native RRF"). Qdrant's own order
among equal channel scores decided which tied candidate entered a channel's
list and at which place, so it decided fused scores too (#177). Qdrant still
scores each channel; the order among equal scores is now ours.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from compliance_rag.adapters.interfaces import Candidate, Channel

RRF_K = 2
"""A candidate scores 1 / (`RRF_K` + its place) from each list it is in,
counting places from 0: 1/2 for a list's first. This is Qdrant's own default
for its native RRF, measured on v1.19.1, kept when fusion moved out of Qdrant
(ADR-0046). A fused list with no tie in either channel therefore keeps the
order native RRF gave it."""


def in_order(candidate: Candidate) -> tuple[float, str]:
    """The `RetrievalStore` order: score descending, then `chunk_id` ascending."""
    return (-candidate.score, candidate.chunk_id)


def cut(candidates: Iterable[Candidate], limit: int) -> tuple[Candidate, ...]:
    """The first `limit` of `candidates` in `in_order`. Given every candidate
    that ties with the one at the cut, the result doesn't depend on the order
    `candidates` came in."""
    return tuple(sorted(candidates, key=in_order)[: max(limit, 0)])


def reciprocal_rank_fusion(
    per_channel: Mapping[Channel, tuple[Candidate, ...]], limit: int
) -> tuple[Candidate, ...]:
    """RRF over channel lists already `cut`, then `cut` to `limit`. Every
    candidate in any list is scored, so a tie at the fused cut is broken with
    every tied candidate in view."""
    scores: dict[str, float] = {}
    first_seen: dict[str, Candidate] = {}
    for candidates in per_channel.values():
        for place, candidate in enumerate(candidates):
            scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1.0 / (RRF_K + place)
            first_seen.setdefault(candidate.chunk_id, candidate)
    return cut(
        (
            Candidate(
                chunk_id=chunk_id,
                clause_id=first_seen[chunk_id].clause_id,
                text=first_seen[chunk_id].text,
                score=score,
                channel=None,
            )
            for chunk_id, score in scores.items()
        ),
        limit,
    )
