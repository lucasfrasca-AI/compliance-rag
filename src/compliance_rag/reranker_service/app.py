"""FastAPI service: `POST /rerank` (issue #14). Ships in Docker with plain
`onnxruntime` — no `torch`, no `optimum`. `RerankerClient`
(`adapters/reranker_client.py`) is the `Reranker`-interface client that
calls this over HTTP; nothing else in the codebase talks to it directly.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from compliance_rag.corpus.chunker import RERANKER_MAX_TOKENS
from compliance_rag.reranker_service.model import RerankerModel


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    top_k: int


class ScoredDocumentOut(BaseModel):
    index: int
    score: float


class RerankResponse(BaseModel):
    scores: list[ScoredDocumentOut]


def create_app(model: RerankerModel | None = None) -> FastAPI:
    """Factory rather than a module-level `app = FastAPI()` with a
    module-level model: tests build an app around an already-loaded model
    (or one pointed at a test fixture path) without needing the real
    vendored artifact to exist on disk just to import this module."""
    app = FastAPI(title="compliance-rag reranker")
    reranker_model = model or RerankerModel()

    @app.post("/rerank", response_model=RerankResponse)
    def rerank(request: RerankRequest) -> RerankResponse:
        if not request.documents:
            raise HTTPException(status_code=400, detail="documents must not be empty")

        longest_tokens = max(
            reranker_model.token_count(request.query, document) for document in request.documents
        )
        if longest_tokens > RERANKER_MAX_TOKENS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"query plus longest document is {longest_tokens} tokens, over the "
                    f"model's {RERANKER_MAX_TOKENS}-token window"
                ),
            )

        scores = reranker_model.score(request.query, tuple(request.documents))
        ranked = sorted(enumerate(scores), key=lambda pair: pair[1], reverse=True)
        top = ranked[: request.top_k]
        return RerankResponse(
            scores=[ScoredDocumentOut(index=index, score=score) for index, score in top]
        )

    return app
