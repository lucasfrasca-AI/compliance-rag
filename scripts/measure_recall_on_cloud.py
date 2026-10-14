"""One-off: the recall gate's measurement on the deployed Qdrant cluster (#144),
so its numbers come at least once from the store that will serve answers, not
only from a container.

Two measurements:

1. **The gate itself.** The golden corpus on a throwaway collection on the
   cloud cluster (uniquely named, dropped on exit, never `clauses`), fused and
   reranked, beside the container baseline (`corpus/golden/recall_baseline.json`).
2. **The deployed collection.** The golden set's law-only queries (tenant
   `_public`) against `clauses` itself, read only. That is the whole
   compilation (779 clauses), not the golden excerpt, so there are more
   distractors than the gate has. A difference from the gate is a finding to
   explain, not something to re-baseline away.

Query vectors come from the checked-in cache, so nothing calls Vertex; the
reranker is the int8 cross-encoder, in-process. Reads QDRANT_URL /
QDRANT_API_KEY through `Settings` and prints neither. Never run in CI: CI
holds no Qdrant Cloud credentials (`tests/test_keepalive_workflows.py`).

    uv run python -m scripts.measure_recall_on_cloud
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.corpus.models import PUBLIC_TENANT_ID
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden import Category, GoldenSet, load_golden_set
from compliance_rag.eval.golden_corpus import gate_chunks
from compliance_rag.eval.harness import golden_store, in_process_reranker
from compliance_rag.eval.recall import (
    EXIT_BARS,
    FUSED,
    RERANKED,
    QueryRecall,
    by_category,
    load_baseline,
    measure,
    regressions,
)
from compliance_rag.reranker_service.model import RerankerModel
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings


def _baseline_means(query_ids: Iterable[str], golden: GoldenSet) -> dict[Category, float]:
    """The container baseline's fused recall@10, as category means over
    `query_ids` — the comparison each cloud number is printed beside."""
    baseline = load_baseline()
    category = {q.id: q.category for q in golden.queries}
    grouped: dict[Category, list[float]] = {}
    for query_id in query_ids:
        grouped.setdefault(category[query_id], []).append(baseline[query_id][FUSED])
    return {c: sum(values) / len(values) for c, values in grouped.items()}


def _report(title: str, results: tuple[QueryRecall, ...], golden: GoldenSet) -> None:
    container = _baseline_means((r.query_id for r in results), golden)
    print(f"\n{title}: recall@10 by category")
    print(f"  {'category':<13} {'bar':>4}  {'fused':>6} {'container':>9}  {'reranked':>8}")
    for category, lists in sorted(by_category(results).items()):
        print(
            f"  {category.value:<13} {EXIT_BARS[category]:>4.1f}  {lists[FUSED]:>6.3f} "
            f"{container[category]:>9.3f}  {lists[RERANKED]:>8.3f}"
        )
    for query_id, (before, after) in sorted(regressions(results, load_baseline()).items()):
        print(f"  below the container baseline: {query_id} {before:.3f} -> {after:.3f}")
    for result in results:
        if result.by_list[RERANKED] < 1.0:
            print(f"  reranked miss {result.query_id} ({result.category.value}): {result.by_list}")


async def main() -> None:
    settings = Settings()
    golden = load_golden_set()
    model = RerankerModel()
    async with (
        golden_store(
            settings.qdrant_url,
            prefix="recall-cloud",
            chunks=gate_chunks(),
            api_key=settings.qdrant_api_key,
        ) as store,
        in_process_reranker(model) as reranker,
    ):
        gate = await measure(RetrievalPipeline(store, reranker), golden)
    _report("1. the gate, on a throwaway collection on the cloud cluster", gate, golden)

    law_only = golden.model_copy(
        update={"queries": tuple(q for q in golden.queries if q.tenant_id == PUBLIC_TENANT_ID)}
    )
    # Read only: no `ensure_ready`, which could add a payload index.
    deployed = QdrantRetrievalStore.from_settings(
        settings, embedder=CachedEmbedder(EmbeddingCache.load()), ledger=FakeLedgerStore()
    )
    async with in_process_reranker(model) as reranker:
        whole = await measure(RetrievalPipeline(deployed, reranker), law_only)
    _report(
        f"2. the {len(law_only.queries)} law-only queries on the deployed "
        f"{settings.qdrant_collection!r} collection (the whole compilation)",
        whole,
        golden,
    )


if __name__ == "__main__":
    asyncio.run(main())
