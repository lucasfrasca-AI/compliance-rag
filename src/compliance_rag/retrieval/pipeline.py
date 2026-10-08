"""`retrieve()`: Track B end to end, from query to reranked candidates (issue #16).

Track A (`GraphStore`, obligation filtering) is a later ticket's concern —
this tracer bullet proves the retrieve -> rerank -> generate -> verify shape
end to end (Loop B, `CONTEXT.md`), not the full loop. `obligation_ids` is
always passed empty to `RetrievalStore.search`, which is D-03's "no filter"
case, not a bypass of it.

`RetrievalPipeline.from_settings` is the real composition (issue #25): the
Qdrant store, the Vertex embedder and the reranker client, with no LLM
anywhere in the path.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from compliance_rag.adapters.interfaces import (
    Candidate,
    Channel,
    LedgerStore,
    Reranker,
    RetrievalStore,
    ScoredDocument,
)
from compliance_rag.corpus.models import Scope
from compliance_rag.retrieval.router import is_clause_id_query
from compliance_rag.settings import Settings
from compliance_rag.telemetry import span


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Every stage of `retrieve()`, kept separate rather than collapsed into a
    top-k list — so a later swap of one fake for a real adapter (the reranker
    service, Qdrant) is attributable to that adapter, not the composition
    (issue #16).
    """

    per_channel: dict[Channel, tuple[Candidate, ...]]
    fused: tuple[Candidate, ...]
    reranked: tuple[ScoredDocument, ...]
    """The reranker's own output — `ScoredDocument.index` indexes into
    `fused`, exactly as `Reranker.rerank()` promises."""

    def ranked_candidates(self) -> tuple[Candidate, ...]:
        """`fused`, reordered (and trimmed) by rerank score — what generation
        should actually see, rather than the pre-rerank fused order."""
        return tuple(self.fused[doc.index] for doc in self.reranked)

    def top_candidate(self) -> Candidate | None:
        """The single best candidate after reranking, or `None` if nothing
        was retrieved."""
        ranked = self.ranked_candidates()
        return ranked[0] if ranked else None


def _scored(candidates: tuple[Candidate, ...]) -> list[str]:
    return [f"{c.chunk_id}:{c.score:.4f}" for c in candidates]


class RetrievalPipeline:
    """Composes exactly one `RetrievalStore` and one `Reranker` (D-08)."""

    def __init__(self, retrieval_store: RetrievalStore, reranker: Reranker) -> None:
        self.retrieval_store = retrieval_store
        self.reranker = reranker

    @classmethod
    def from_settings(cls, settings: Settings, *, ledger: LedgerStore) -> RetrievalPipeline:
        """The real adapters behind the same `retrieve()` the fakes proved
        (issue #25). The reranker endpoint's residency is asserted when its
        client is built (`build_reranker_http_client`, D-15)."""
        # Imported here, not at module level: these pull in qdrant-client,
        # fastembed and the Vertex SDK, which nothing that only composes
        # fakes (every unit test) should pay for.
        from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
        from compliance_rag.adapters.reranker_client import RerankerClient
        from compliance_rag.adapters.reranker_http_client import build_reranker_http_client
        from compliance_rag.adapters.vertex_embedder import VertexEmbedder

        # Residency first: a reranker endpoint outside Sydney is refused
        # before any cloud client is constructed (D-15).
        reranker = RerankerClient(
            build_reranker_http_client(
                settings.reranker_url, timeout_seconds=settings.reranker_timeout_seconds
            )
        )
        store = QdrantRetrievalStore.from_settings(
            settings, embedder=VertexEmbedder.from_settings(settings), ledger=ledger
        )
        return cls(store, reranker)

    async def retrieve(
        self,
        query: str,
        tenant_id: str,
        as_of: dt.date,
        *,
        scopes: tuple[Scope, ...] = (Scope.PUBLIC, Scope.TENANT),
        limit: int = 50,
        top_k: int = 10,
    ) -> RetrievalTrace:
        """Both channels, fused, then reranked (`CONTEXT.md`: Fusion, Rerank)."""
        with span("retrieve", query=query, tenant_id=tenant_id, as_of=as_of.isoformat()) as current:
            result = await self.retrieval_store.search(
                query,
                obligation_ids=(),
                tenant_id=tenant_id,
                scopes=scopes,
                as_of=as_of,
                limit=limit,
            )
            # Both retrieval channels are logged before fusion (CLAUDE.md).
            for channel, candidates in result.per_channel.items():
                current.set_attribute(f"retrieval.channel.{channel.value}", _scored(candidates))
            current.set_attribute("retrieval.fused", _scored(result.fused))

            reranked = await self.reranker.rerank(
                query,
                tuple(c.text for c in result.fused),
                top_k=top_k,
            )
            current.set_attribute(
                "retrieval.rerank_scores", [f"{doc.index}:{doc.score:.4f}" for doc in reranked]
            )

            return RetrievalTrace(
                per_channel=result.per_channel,
                fused=result.fused,
                reranked=reranked,
            )

    async def route(
        self,
        query: str,
        tenant_id: str,
        as_of: dt.date,
        *,
        scopes: tuple[Scope, ...] = (Scope.PUBLIC, Scope.TENANT),
        limit: int = 50,
        top_k: int = 10,
    ) -> RetrievalTrace:
        """The router's fixed decision (`CONTEXT.md`: Router, D-04, D-12):
        a clause-ID query goes straight to `lookup_clause` on the keyword
        channel and never reaches the reranker; anything else runs the full
        `retrieve()` pipeline unchanged.
        """
        if not is_clause_id_query(query):
            return await self.retrieve(
                query, tenant_id, as_of, scopes=scopes, limit=limit, top_k=top_k
            )

        with span(
            "retrieve.router_hit", query=query, tenant_id=tenant_id, as_of=as_of.isoformat()
        ) as current:
            hit = await self.retrieval_store.lookup_clause(
                query.strip(), tenant_id=tenant_id, as_of=as_of
            )
            fused = (hit,) if hit is not None else ()
            current.set_attribute("retrieval.channel.keyword", _scored(fused))
            current.set_attribute("retrieval.fused", _scored(fused))
            reranked = tuple(ScoredDocument(index=i, score=c.score) for i, c in enumerate(fused))
            return RetrievalTrace(
                per_channel={Channel.KEYWORD: fused},
                fused=fused,
                reranked=reranked,
            )
