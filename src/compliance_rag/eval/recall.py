"""Recall@k over the golden set (issue #28), per channel and fused, measured
through the same `route()` production uses — and with no LLM anywhere in it.

A query's recall@k is the share of its expected clauses found among the first
k distinct clause ids a list returns. Exact-ID queries go to the router's
keyword lookup, so their only channel is `keyword`; every other query runs
`retrieve()` and is measured on the dense list, the sparse list, fusion and
the reranked top k. **recall@10 on the reranked list is the retrieval metric**:
retrieval's job is to rank.

**Delivered-recall** is a separate metric (ADR-0045), never combined with
recall@k into one number. It is the share of the expected clauses in the
**delivered list**: the reranked top k, each library-law clause delivered
whole with the chapeaux and paragraphs of its provision, which is what
generation is given. That list holds more than k clauses, so delivered-recall
is not a recall@k, and its size is reported beside it
(`QueryRecall.delivered_clauses`). An expected clause counts there only if it
was ranked in the top k or is in the provision of one that was, never because
anything was ranked deeper. It measures a delivery mechanism, not the ranking.

`NoRerank` keeps fusion's order. It is the gate's reranker so the gate
measures retrieval rather than the cross-encoder, needs no model artefact, and
is the "no-rerank" arm of the reranker benchmark (#32).

The bars (ADR-0043) are per *cell*: one list's mean over one category's
queries of one document class — library law (the `_public` queries, over the
whole compilation) or internal policy (Acme's, over its own six policies). The
two halves are kept apart because they differ by half a point on cross-clause
and only the law half can be measured on the deployed collection. Each cell
has an enforced **floor**, what the gate achieves today: it stops
regressions. A **target** names where a cell is going: PHASES.md's bars on the
reranked list, and 0.95 on fused paraphrase (ADR-0043). A cell whose floor is
under its target is an open target: CI expects it to fail, strictly, so the
day it is met CI fails until its floor is raised to meet it. A target is not a
floor, so an open one doesn't hold a phase's exit (ADR-0043). Delivered-recall
has floors and no target (ADR-0045), so it can neither meet nor close one.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from compliance_rag.adapters.interfaces import Candidate, LawScope, Reranker, ScoredDocument
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Chunk, DocumentClass
from compliance_rag.eval.golden import Category, GoldenQuery, GoldenSet
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace

K = 10
FUSED = "fused"
RERANKED = "reranked"
DELIVERED = "delivered"

LAW = DocumentClass.LIBRARY_LAW
POLICY = DocumentClass.INTERNAL_POLICY

_TOLERANCE = 1e-9
"""Means are sums of fractions like 1/3: compared with this much slack."""


def _under(value: float, bar: float) -> bool:
    return value < bar - _TOLERANCE


class Cell(NamedTuple):
    """A gated number: one list's mean recall@10 over one category's queries
    whose documents are of one class."""

    list_name: str
    category: Category
    document_class: DocumentClass


TARGETS: Mapping[Category, float] = {
    Category.EXACT_ID: 1.0,
    Category.PARAPHRASE: 0.9,
    Category.CROSS_CLAUSE: 0.8,
}
"""What Phase 1 needs (PHASES.md exit criteria, ADR-0043): recall@10 per
category on the reranked list (`reranked`; for an exact-ID query, the router's
lookup), for law and for policy queries each. PHASES.md's numbers, each
re-argued in clauses on the whole compilation in ADR-0043. Never on the
delivered list: delivered-recall is a separate metric with no target
(ADR-0045)."""

FUSED_TARGETS: Mapping[Category, float] = {Category.PARAPHRASE: 0.95}
"""Where fusion is going (the human's decision on ADR-0043): paraphrase
recall@10 on the fused list, for law and for policy queries each. Nothing
delivers fusion's order, so this isn't a Phase 1 bar. It keeps GQ-10's sparse
weakness on the board: law is 5/6 (GQ-10's APP 11.2 is fused 11th), so the
target is open. Policy is 1.0, and its floor already holds the value."""

FLOORS: Mapping[Cell, float] = {
    # The fused gate (CI's `ci` job, no model): platform-exact.
    Cell(FUSED, Category.EXACT_ID, LAW): 1.0,
    Cell(FUSED, Category.EXACT_ID, POLICY): 1.0,
    Cell(FUSED, Category.PARAPHRASE, LAW): 5 / 6,  # target 0.95: open (GQ-10's 11.2 fused 11th, #221)
    Cell(FUSED, Category.PARAPHRASE, POLICY): 1.0,
    Cell(FUSED, Category.CROSS_CLAUSE, LAW): 4 / 9,  # GQ-18 1/3, GQ-19 1/2, GQ-20 1/2
    Cell(FUSED, Category.CROSS_CLAUSE, POLICY): 0.9,  # GQ-23 1/2
    # The reranked gate (CI's `reranker-fidelity` job): the cross-encoder's
    # top 10, recall@10, the retrieval metric. The targets are here.
    Cell(RERANKED, Category.EXACT_ID, LAW): 1.0,
    Cell(RERANKED, Category.EXACT_ID, POLICY): 1.0,
    Cell(RERANKED, Category.PARAPHRASE, LAW): 1.0,
    Cell(RERANKED, Category.PARAPHRASE, POLICY): 1.0,
    Cell(RERANKED, Category.CROSS_CLAUSE, LAW): 4 / 9,  # target 0.8: open (#200, #206)
    Cell(RERANKED, Category.CROSS_CLAUSE, POLICY): 0.9,  # GQ-21 1/2
    # The same job: delivered-recall, a separate metric with floors and no
    # target (ADR-0045). The reranked top 10, each delivered whole: more than
    # ten clauses, so never a recall@10 and never combined with one.
    Cell(DELIVERED, Category.EXACT_ID, LAW): 1.0,
    Cell(DELIVERED, Category.EXACT_ID, POLICY): 1.0,
    Cell(DELIVERED, Category.PARAPHRASE, LAW): 1.0,
    Cell(DELIVERED, Category.PARAPHRASE, POLICY): 1.0,
    # GQ-18's (a) and (b) come with APP 11.1, and GQ-20's "agency means:" with
    # its paragraph (ca). Still missed: GQ-19's APP 10.1 (#206).
    Cell(DELIVERED, Category.CROSS_CLAUSE, LAW): 5 / 6,
    Cell(DELIVERED, Category.CROSS_CLAUSE, POLICY): 0.9,  # GQ-21 1/2
}
"""What the gate achieves on the whole compilation (ADR-0043, measured
2026-10-15; the delivered cells ADR-0045, the same day): enforced, so any drop
in any cell fails. Raised deliberately when recall improves, never lowered to
let a drop pass."""


def target_of(cell: Cell) -> float | None:
    """A cell's target: PHASES.md's bar on the reranked list, where retrieval
    ranks, or fusion's own on fused paraphrase (`FUSED_TARGETS`). Any other
    fused cell has none, and a delivered cell has none, since delivered-recall
    measures delivery, not ranking (ADR-0045). Such a cell is held to its floor
    (and fusion to the per-query baseline), and nothing more."""
    if cell.list_name == RERANKED:
        return TARGETS[cell.category]
    if cell.list_name == FUSED:
        return FUSED_TARGETS.get(cell.category)
    return None


OPEN_TARGETS: tuple[Cell, ...] = tuple(
    cell
    for cell, floor in FLOORS.items()
    if (target := target_of(cell)) is not None and _under(floor, target)
)
"""Cells whose target is above what the gate achieves (ADR-0043): each is a
strict xfail in the gate that measures its list, the fused one in
`test_recall_gate.py` and the reranked one in `test_recall_gate_reranked.py`.
Not a floor, so none holds a phase's exit."""


def open_targets_on(list_name: str) -> tuple[Cell, ...]:
    """The open targets one gate measures."""
    return tuple(cell for cell in OPEN_TARGETS if cell.list_name == list_name)


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
    `fused` and `reranked` (fusion reordered by the pipeline's reranker);
    and, under `delivered`, delivered-recall, the separate metric of those k
    each delivered whole (ADR-0045), which is not a recall@k."""
    delivered_clauses: int = 0
    """How many clauses the delivered list holds: k, plus the chapeaux and
    paragraphs that make them whole. The size of what generation reads,
    reported beside delivered-recall. 0 only for a result built by hand, with no
    trace measured. What a ranked clause can bring is pinned on the whole
    compilation instead (`test_delivered_whole.py`), since fusion's tenth
    candidate, and with it this size, can move at a tie (#177)."""


def document_class_of(query: GoldenQuery) -> DocumentClass:
    """A golden query asked as `_public` searches the library; one asked as
    Acme searches Acme's own policies, since Acme subscribes to nothing there."""
    return LAW if query.tenant_id == PUBLIC_TENANT_ID else POLICY


def recall_delivered(expected: tuple[str, ...], trace: RetrievalTrace) -> float:
    """Delivered-recall: the share of `expected` in what `retrieve()`
    delivers, the reranked top k (already cut to k by the reranker), each with
    the chapeaux and paragraphs of its provision (ADR-0045). Every delivered
    clause counts, however many the k bring; none comes from ranking deeper
    than k. A separate metric from recall@k, never combined with it."""
    delivered = {c.clause_id for c in trace.delivered()}
    return sum(1 for clause_id in expected if clause_id in delivered) / len(expected)


def measure_trace(query: GoldenQuery, trace: RetrievalTrace, k: int = K) -> QueryRecall:
    expected = query.expected_clause_ids
    by_list = {
        channel.value: recall_at_k(expected, candidates, k)
        for channel, candidates in trace.per_channel.items()
    }
    by_list[FUSED] = recall_at_k(expected, trace.fused, k)
    by_list[RERANKED] = recall_at_k(expected, trace.ranked_candidates(), k)
    by_list[DELIVERED] = recall_delivered(expected, trace)
    return QueryRecall(
        query.id,
        query.category,
        document_class_of(query),
        by_list,
        delivered_clauses=len(trace.delivered()),
    )


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


def by_cell(results: Iterable[QueryRecall], list_name: str) -> dict[Cell, float]:
    """Mean recall@k of `list_name` per category and document class."""
    grouped: dict[Cell, list[float]] = {}
    for result in results:
        cell = Cell(list_name, result.category, result.document_class)
        grouped.setdefault(cell, []).append(result.by_list[list_name])
    return {cell: sum(values) / len(values) for cell, values in grouped.items()}


def below_floor(results: Iterable[QueryRecall], list_name: str) -> dict[Cell, tuple[float, float]]:
    """Every cell of `list_name` under its floor, as `cell -> (floor, now)`.
    A cell with no floor is a failure too: a category or document class the
    golden set gained without a deliberate floor would otherwise go unguarded."""
    return {
        cell: (FLOORS.get(cell, 1.0), now)
        for cell, now in by_cell(results, list_name).items()
        if cell not in FLOORS or _under(now, FLOORS[cell])
    }


def describe_cell(cell: Cell, now: float) -> str:
    """One report line: the cell's value beside its floor and its target."""
    floor = FLOORS.get(cell)
    target = target_of(cell)
    return (
        f"{cell.list_name:<8} {cell.category.value:<13} {cell.document_class.value:<16} "
        f"{now:.3f}  floor {'none' if floor is None else f'{floor:.3f}'}, "
        f"target {'none' if target is None else f'{target:.2f}'}"
    )


def short_of_target(results: Iterable[QueryRecall], cell: Cell) -> float | None:
    """`cell`'s value if it is under its target, else None."""
    target = target_of(cell)
    now = by_cell(results, cell.list_name)[cell]
    return now if target is not None and _under(now, target) else None


Baseline = Mapping[str, Mapping[str, float]]
"""query id -> list name -> recall@k, for every list but `reranked` (which,
under `NoRerank`, is fusion again), and delivered-recall under `delivered`:
under `NoRerank` that is fusion's top k delivered whole, so a change to
delivery fails the fused gate by query id on every build, with no model
(ADR-0045)."""


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
            if _under(now, before):
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
        "note": "Per-query recall@10 for every list, and delivered-recall under "
        "`delivered`, a separate metric that is not a recall@10 (ADR-0045), on the gate "
        "corpus (the whole compilation's selected clauses and the Acme policies, "
        "ADR-0043). Written only "
        "by UPDATE_RECALL_BASELINE=1 pytest tests/test_recall_gate.py, locally — "
        "a deliberate, reviewed change, never to make a drop pass.",
        "corpus_sha256": corpus,
        "recall_at_10": {
            r.query_id: {n: v for n, v in r.by_list.items() if n != RERANKED} for r in results
        },
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
