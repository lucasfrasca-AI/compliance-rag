"""`Reranker` over the local reranker service (issue #14), reached only by
HTTP (`httpx`) — nothing here imports `onnxruntime`, `torch` or the service
module directly, matching `CLAUDE.md`'s "No ... calls outside adapters/"
discipline extended to this repo's one other real backend besides Cloud
Storage and Vertex.
"""

from __future__ import annotations

import httpx

from compliance_rag.adapters.interfaces import Reranker, ScoredDocument


class RerankerWindowExceeded(ValueError):
    """The service rejected a request because query + longest document
    overflowed the model's window (issue #14: "rejects ... rather than
    letting the model truncate silently") — raised here rather than left
    as an opaque HTTP 422 the caller has to know to interpret."""


class RerankerClient(Reranker):
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def rerank(
        self, query: str, documents: tuple[str, ...], *, top_k: int
    ) -> tuple[ScoredDocument, ...]:
        response = await self._client.post(
            "/rerank",
            json={"query": query, "documents": list(documents), "top_k": top_k},
        )
        if response.status_code == 422:
            raise RerankerWindowExceeded(response.json()["detail"])
        response.raise_for_status()
        body = response.json()
        return tuple(
            ScoredDocument(index=item["index"], score=item["score"]) for item in body["scores"]
        )
