"""The recall gate's measurement on the deployed Qdrant cluster (#144, #180), so
its numbers come at least once from the store that will serve answers, not only
from a container.

Two measurements, both on the gate's corpus (the whole compilation's 779
selected clauses, ADR-0043), so every difference below is cloud versus
container on the same corpus:

1. **The gate itself.** The gate corpus on a throwaway collection on the cloud
   cluster (uniquely named, dropped on exit, never `clauses`), fused and
   reranked. Fused is compared query by query with the container baseline
   (`corpus/golden/recall_baseline.json`); reranked with the floors.
2. **The deployed collection.** The golden set's law-only queries (tenant
   `_public`) against `clauses` itself, read only — the Acme queries need
   policies that aren't in it, and shouldn't be. Fused is compared query by
   query with the container baseline, reranked with measurement 1's reranked
   for the same query (same reranker, same machine).

Until #178 part 2 republishes `clauses`, it holds the compilation's *previous*
parse, where #178's s6(1) definitions and #186's connectors read differently,
so a small difference in measurement 2 is expected; after it, measurement 2
should match the container exactly. A difference then is a finding to explain,
not something to re-baseline away.

Query vectors come from the checked-in cache, so nothing calls Vertex; the
reranker is the int8 cross-encoder, in-process. Reads QDRANT_URL /
QDRANT_API_KEY through `Settings` and prints neither. Never run in CI: CI
holds no Qdrant Cloud credentials (`tests/test_keepalive_workflows.py`).

    uv run python -m scripts.measure_recall_on_cloud
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.corpus.models import PUBLIC_TENANT_ID
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden import load_golden_set
from compliance_rag.eval.golden_corpus import gate_chunks
from compliance_rag.eval.harness import golden_store, in_process_reranker
from compliance_rag.eval.recall import (
    FUSED,
    RERANKED,
    QueryRecall,
    by_cell,
    describe_cell,
    load_baseline,
    measure,
)
from compliance_rag.reranker_service.model import RerankerModel
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings


def _report(
    title: str,
    results: tuple[QueryRecall, ...],
    *,
    fused_reference: Mapping[str, float],
    reranked_reference: Mapping[str, float] | None,
) -> None:
    """Each cell, then every query whose recall differs from its reference, in
    either direction: on the same corpus, cloud and container should agree.
    Both sides are computed by the same arithmetic, so they compare exactly."""
    container = by_cell(
        (
            QueryRecall(
                r.query_id, r.category, r.document_class, {FUSED: fused_reference[r.query_id]}
            )
            for r in results
        ),
        FUSED,
    )
    print(f"\n{title}: recall@10 (fused, beside the container's fused, then reranked)")
    for cell, now in sorted(by_cell(results, FUSED).items()):
        print(f"  {describe_cell(cell, now)}; container {container[cell]:.3f}")
    for cell, now in sorted(by_cell(results, RERANKED).items()):
        print(f"  {describe_cell(cell, now)}")
    for r in results:
        if r.by_list[FUSED] != fused_reference[r.query_id]:
            print(
                f"  fused differs from the container: {r.query_id} "
                f"{fused_reference[r.query_id]:.3f} -> {r.by_list[FUSED]:.3f}"
            )
        if reranked_reference is not None and (
            r.by_list[RERANKED] != reranked_reference[r.query_id]
        ):
            print(
                f"  reranked differs from measurement 1: {r.query_id} "
                f"{reranked_reference[r.query_id]:.3f} -> {r.by_list[RERANKED]:.3f}"
            )
        if r.by_list[RERANKED] < 1.0:
            print(f"  reranked miss {r.query_id} ({r.category.value}): {r.by_list}")


async def main() -> None:
    settings = Settings()
    golden = load_golden_set()
    container = {query_id: lists[FUSED] for query_id, lists in load_baseline().items()}
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
    _report(
        "1. the gate, on a throwaway collection on the cloud cluster",
        gate,
        fused_reference=container,
        reranked_reference=None,
    )

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
        f"{settings.qdrant_collection!r} collection",
        whole,
        fused_reference=container,
        reranked_reference={r.query_id: r.by_list[RERANKED] for r in gate},
    )


if __name__ == "__main__":
    asyncio.run(main())
