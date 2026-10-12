"""Recall@k over the golden set (issue #28), per channel and fused, measured
through the same `route()` production uses — and with no LLM anywhere in it.

A query's recall@k is the share of its expected clauses found among the first
k distinct clause ids a list returns. Exact-ID queries go to the router's
keyword lookup, so their only channel is `keyword`; every other query runs
`retrieve()` and is measured on the dense list, the sparse list, and fusion.

`NoRerank` keeps fusion's order. It is the gate's reranker so the gate
measures retrieval rather than the cross-encoder, needs no model artefact, and
is the "no-rerank" arm of the reranker benchmark (#32).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from compliance_rag.adapters.interfaces import Candidate, LawScope, Reranker, ScoredDocument
from compliance_rag.eval.golden import Category, GoldenQuery, GoldenSet
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace

K = 10
FUSED = "fused"
RERANKED = "reranked"

EXIT_BARS: Mapping[Category, float] = {
    Category.EXACT_ID: 1.0,
    Category.PARAPHRASE: 0.9,
    Category.CROSS_CLAUSE: 0.8,
}
"""PHASES.md Phase 1 exit criteria: recall@10 per category — the starting bar,
revised with evidence."""

RECALL_BASELINE_PATH = (
    Path(__file__).resolve().parents[3] / "corpus" / "golden" / "recall_baseline.json"
)


WHOLE_LIBRARY = LawScope.unscoped(
    "Phase 1 retrieval eval: recall is measured over the whole library, with no tenant profile"
)


class NoRerank(Reranker):
    """Fusion's order, unchanged: score is the negated fused rank."""

    async def rerank(
        self, query: str, documents: tuple[str, ...], *, top_k: int
    ) -> tuple[ScoredDocument, ...]:
        kept = range(min(top_k, len(documents)))
        return tuple(ScoredDocument(index=i, score=-float(i)) for i in kept)


class UnsignedGoldenSet(ValueError):
    """The labels haven't been signed by a reviewer, so they aren't labels (D-01)."""


def recall_at_k(expected: tuple[str, ...], ranked: Iterable[Candidate], k: int = K) -> float:
    top: list[str] = []
    for candidate in ranked:
        if candidate.clause_id not in top:
            top.append(candidate.clause_id)
        if len(top) == k:
            break
    return sum(1 for clause_id in expected if clause_id in top) / len(expected)


@dataclass(frozen=True, slots=True)
class QueryRecall:
    query_id: str
    category: Category
    by_list: dict[str, float]
    """recall@k keyed by channel value (`dense`, `sparse`, `keyword`),
    `fused`, and `reranked` (fusion reordered by the pipeline's reranker)."""

    @property
    def gated(self) -> float:
        """What the exit bar is measured on: fusion, or the router's lookup
        for an exact-ID query (which is its fused list)."""
        return self.by_list[FUSED]


def measure_trace(query: GoldenQuery, trace: RetrievalTrace, k: int = K) -> QueryRecall:
    expected = query.expected_clause_ids
    by_list = {
        channel.value: recall_at_k(expected, candidates, k)
        for channel, candidates in trace.per_channel.items()
    }
    by_list[FUSED] = recall_at_k(expected, trace.fused, k)
    by_list[RERANKED] = recall_at_k(expected, trace.ranked_candidates(), k)
    return QueryRecall(query.id, query.category, by_list)


async def measure(
    pipeline: RetrievalPipeline, golden: GoldenSet, k: int = K
) -> tuple[QueryRecall, ...]:
    if not golden.is_signed_off:
        raise UnsignedGoldenSet("the golden set is not signed off; its labels are only a draft")
    results: list[QueryRecall] = []
    for query in golden.queries:
        trace = await pipeline.route(
            query.query, query.tenant_id, query.as_of, law=WHOLE_LIBRARY, top_k=k
        )
        results.append(measure_trace(query, trace, k))
    return tuple(results)


def _grouped(results: Iterable[QueryRecall]) -> dict[Category, list[QueryRecall]]:
    grouped: dict[Category, list[QueryRecall]] = {}
    for result in results:
        grouped.setdefault(result.category, []).append(result)
    return grouped


def by_category(results: Iterable[QueryRecall]) -> dict[Category, dict[str, float]]:
    """Mean recall per category, per list."""
    grouped = _grouped(results)
    summary: dict[Category, dict[str, float]] = {}
    for category, members in grouped.items():
        lists = sorted({name for m in members for name in m.by_list})
        summary[category] = {
            name: sum(m.by_list.get(name, 0.0) for m in members) / len(members)
            for name in lists
            if all(name in m.by_list for m in members)
        }
    return summary


def below_exit_bar(results: Iterable[QueryRecall]) -> dict[Category, float]:
    """Categories whose gated recall@k is under PHASES.md's bar, with the value."""
    means = {c: sum(m.gated for m in ms) / len(ms) for c, ms in _grouped(results).items()}
    return {c: mean for c, mean in means.items() if mean < EXIT_BARS[c]}


Baseline = Mapping[str, Mapping[str, float]]
"""query id -> list name -> recall@k, for every list but `reranked` (which,
under `NoRerank`, is fusion again)."""

_TOLERANCE = 1e-9


def regressions(
    results: Iterable[QueryRecall], baseline: Baseline
) -> dict[str, tuple[float, float]]:
    """Every list of every query whose recall@k fell below its checked-in
    baseline, as `"GQ-18/dense" -> (baseline, now)` — a dense or sparse
    collapse that fusion happens to mask still fails. A query absent from the
    baseline is new, not a regression; `test_the_baseline_covers_every_golden_query`
    stops one being dropped from it."""
    dropped: dict[str, tuple[float, float]] = {}
    for result in results:
        for name, before in baseline.get(result.query_id, {}).items():
            now = result.by_list.get(name, 0.0)
            if now < before - _TOLERANCE:
                dropped[f"{result.query_id}/{name}"] = (before, now)
    return dropped


def load_baseline(path: Path = RECALL_BASELINE_PATH) -> dict[str, dict[str, float]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(query_id): {str(name): float(v) for name, v in lists.items()}
        for query_id, lists in raw["recall_at_10"].items()
    }


def write_baseline(results: Iterable[QueryRecall], path: Path = RECALL_BASELINE_PATH) -> None:
    """Only a person updates the baseline, locally, as a reviewed diff: never
    in CI, where it would let a drop rewrite the bar it is measured against."""
    if os.environ.get("CI"):
        raise RuntimeError("the recall baseline is never rewritten in CI")
    document = {
        "note": "Per-query recall@10 for every list. Written only by "
        "UPDATE_RECALL_BASELINE=1 pytest tests/test_recall_gate.py, locally — "
        "a deliberate, reviewed change, never to make a drop pass.",
        "recall_at_10": {
            r.query_id: {n: v for n, v in r.by_list.items() if n != RERANKED} for r in results
        },
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
