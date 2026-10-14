"""Recall@k over the golden set (issue #28), per channel and fused, measured
through the same `route()` production uses — and with no LLM anywhere in it.

A query's recall@k is the share of its expected clauses found among the first
k distinct clause ids a list returns. Exact-ID queries go to the router's
keyword lookup, so their only channel is `keyword`; every other query runs
`retrieve()` and is measured on the dense list, the sparse list, and fusion.

`NoRerank` keeps fusion's order. It is the gate's reranker so the gate
measures retrieval rather than the cross-encoder, needs no model artefact, and
is the "no-rerank" arm of the reranker benchmark (#32).

The bars (ADR-0043) are per *cell*: one list's mean recall@10 over one
category's queries of one document class — library law (the `_public`
queries, over the whole compilation) or internal policy (Acme's, over its own
six policies). The two halves are kept apart because they differ by half a
point on cross-clause and only the law half can be measured on the deployed
collection. Each cell has an enforced **floor**, what the gate achieves today,
and the delivered list has a **target**, what Phase 1 needs. A cell whose floor
is under its target is an open target: CI expects it to fail, strictly, so the
day it is met CI fails until its floor is raised to meet it.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from compliance_rag.adapters.interfaces import Candidate, LawScope, Reranker, ScoredDocument
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Chunk, DocumentClass
from compliance_rag.eval.golden import Category, GoldenQuery, GoldenSet
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace

K = 10
FUSED = "fused"
RERANKED = "reranked"

LAW = DocumentClass.LIBRARY_LAW
POLICY = DocumentClass.INTERNAL_POLICY

Cell = tuple[str, Category, DocumentClass]
"""A gated number: (list, category, document class) — one list's mean
recall@10 over that category's queries of that document class."""

TARGETS: Mapping[Category, float] = {
    Category.EXACT_ID: 1.0,
    Category.PARAPHRASE: 0.9,
    Category.CROSS_CLAUSE: 0.8,
}
"""What Phase 1 needs (PHASES.md exit criteria, ADR-0043): recall@10 per
category on the list `retrieve()` delivers (`reranked`; for an exact-ID query,
the router's lookup), for law and for policy queries each. PHASES.md's
starting bars, kept, now measured on the whole compilation."""

FLOORS: Mapping[Cell, float] = {
    # The fused gate (CI's `ci` job, no model): platform-exact.
    (FUSED, Category.EXACT_ID, LAW): 1.0,
    (FUSED, Category.EXACT_ID, POLICY): 1.0,
    (FUSED, Category.PARAPHRASE, LAW): 5 / 6,  # GQ-10: APP 11.2 fused 11th
    (FUSED, Category.PARAPHRASE, POLICY): 1.0,
    (FUSED, Category.CROSS_CLAUSE, LAW): 4 / 9,  # GQ-18 1/3, GQ-19 1/2, GQ-20 1/2
    (FUSED, Category.CROSS_CLAUSE, POLICY): 0.9,  # GQ-23 1/2
    # The reranked gate (CI's `reranker-fidelity` job): what `retrieve()` delivers.
    (RERANKED, Category.EXACT_ID, LAW): 1.0,
    (RERANKED, Category.EXACT_ID, POLICY): 1.0,
    (RERANKED, Category.PARAPHRASE, LAW): 1.0,
    (RERANKED, Category.PARAPHRASE, POLICY): 1.0,
    (RERANKED, Category.CROSS_CLAUSE, LAW): 4 / 9,  # target 0.8: open (#200)
    (RERANKED, Category.CROSS_CLAUSE, POLICY): 0.9,  # GQ-21 1/2
}
"""What the gate achieves on the whole compilation (ADR-0043, measured
2026-10-15): enforced, so any drop in any cell fails. Raised deliberately when
recall improves, never lowered to let a drop pass."""


def target_of(cell: Cell) -> float | None:
    """A cell's target: on the delivered list only. A fused cell has none —
    nothing delivers fusion's order, so it is held to its floor and to the
    per-query baseline, and nothing more."""
    list_name, category, _ = cell
    return TARGETS[category] if list_name == RERANKED else None


OPEN_TARGETS: tuple[Cell, ...] = tuple(
    cell
    for cell, floor in FLOORS.items()
    if (target := target_of(cell)) is not None and floor < target - 1e-9
)
"""Cells whose target is above what the gate achieves: what Phase 1 still
needs (ADR-0043)."""

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
    document_class: DocumentClass
    """What the query searches: library law (a `_public` query), or its
    tenant's own internal policies (the golden set's Acme queries)."""
    by_list: dict[str, float]
    """recall@k keyed by channel value (`dense`, `sparse`, `keyword`),
    `fused`, and `reranked` (fusion reordered by the pipeline's reranker)."""


def document_class_of(query: GoldenQuery) -> DocumentClass:
    """A golden query asked as `_public` searches the library; one asked as
    Acme searches Acme's own policies, since Acme subscribes to nothing there."""
    return LAW if query.tenant_id == PUBLIC_TENANT_ID else POLICY


def measure_trace(query: GoldenQuery, trace: RetrievalTrace, k: int = K) -> QueryRecall:
    expected = query.expected_clause_ids
    by_list = {
        channel.value: recall_at_k(expected, candidates, k)
        for channel, candidates in trace.per_channel.items()
    }
    by_list[FUSED] = recall_at_k(expected, trace.fused, k)
    by_list[RERANKED] = recall_at_k(expected, trace.ranked_candidates(), k)
    return QueryRecall(query.id, query.category, document_class_of(query), by_list)


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


_TOLERANCE = 1e-9


def by_cell(results: Iterable[QueryRecall], list_name: str) -> dict[Cell, float]:
    """Mean recall@k of `list_name` per category and document class."""
    grouped: dict[tuple[Category, DocumentClass], list[float]] = {}
    for result in results:
        grouped.setdefault((result.category, result.document_class), []).append(
            result.by_list[list_name]
        )
    return {(list_name, c, d): sum(v) / len(v) for (c, d), v in grouped.items()}


def below_floor(results: Iterable[QueryRecall], list_name: str) -> dict[Cell, tuple[float, float]]:
    """Every cell of `list_name` under its floor, as `cell -> (floor, now)`.
    A cell with no floor is a failure too: a category or document class the
    golden set gained without a deliberate floor would otherwise go unguarded."""
    return {
        cell: (FLOORS.get(cell, 1.0), now)
        for cell, now in by_cell(results, list_name).items()
        if cell not in FLOORS or now < FLOORS[cell] - _TOLERANCE
    }


def short_of_target(results: Iterable[QueryRecall], cell: Cell) -> float | None:
    """`cell`'s value if it is under its target, else None."""
    target = target_of(cell)
    now = by_cell(results, cell[0])[cell]
    return now if target is not None and now < target - _TOLERANCE else None


Baseline = Mapping[str, Mapping[str, float]]
"""query id -> list name -> recall@k, for every list but `reranked` (which,
under `NoRerank`, is fusion again)."""


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


def corpus_sha256(chunks: Iterable[Chunk]) -> str:
    """What a baseline was measured on: every chunk's id, owner and text. A
    re-parse, a manifest change or an edited policy fixture changes it, and
    the baseline must then be re-recorded deliberately (#180)."""
    rows = sorted((c.chunk_id, c.tenant_id, c.text) for c in chunks)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def load_baseline(path: Path = RECALL_BASELINE_PATH) -> dict[str, dict[str, float]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(query_id): {str(name): float(v) for name, v in lists.items()}
        for query_id, lists in raw["recall_at_10"].items()
    }


def load_baseline_corpus(path: Path = RECALL_BASELINE_PATH) -> str:
    """The `corpus_sha256` of the corpus the baseline was recorded on."""
    return str(json.loads(path.read_text(encoding="utf-8"))["corpus_sha256"])


def write_baseline(
    results: Iterable[QueryRecall], *, corpus: str, path: Path = RECALL_BASELINE_PATH
) -> None:
    """Only a person updates the baseline, locally, as a reviewed diff: never
    in CI, where it would let a drop rewrite the bar it is measured against.
    `corpus` is the measured corpus's `corpus_sha256`."""
    if os.environ.get("CI"):
        raise RuntimeError("the recall baseline is never rewritten in CI")
    document = {
        "note": "Per-query recall@10 for every list, on the gate corpus (the whole "
        "compilation's selected clauses and the Acme policies, ADR-0043). Written only "
        "by UPDATE_RECALL_BASELINE=1 pytest tests/test_recall_gate.py, locally — "
        "a deliberate, reviewed change, never to make a drop pass.",
        "corpus_sha256": corpus,
        "recall_at_10": {
            r.query_id: {n: v for n, v in r.by_list.items() if n != RERANKED} for r in results
        },
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
