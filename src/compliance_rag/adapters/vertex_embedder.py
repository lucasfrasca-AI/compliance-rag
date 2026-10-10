"""`Embedder` over Vertex AI's `gemini-embedding-001` (issue #13), through the
supported `google-genai` SDK on Vertex (issue #73).

The `vertexai.language_models` surface this adapter first used
(`TextEmbeddingModel`) was deprecated on 2025-06-24 with removal announced for
2026-06-24 (https://cloud.google.com/vertex-ai/generative-ai/docs/deprecations/genai-vertexai-sdk).
`google-genai`'s `Client(vertexai=True, location=...)` calls the same Vertex
endpoint in the same region with the same model, so the vectors are the same
model's — `scripts/check_embedding_drift.py` measures that rather than
assuming it.

Two real, verified constraints shape this module:

- **One text per call.** `embed_content` takes a list of contents, but
  `gemini-embedding-001` on Vertex accepts one input per request — multiple
  independent projects (langchain.js #8490, mem0 #6189) hit the same bug
  trusting the "up to 250 inputs" figure, which is true for older embedding
  models only. `embed()` here issues one request per text.
- **`auto_truncate` defaults to `True`** on Vertex. Passing
  `auto_truncate=False` is what turns "silently truncate" into "refuse the
  request", which issue #13's acceptance criterion ("fails loudly rather than
  silently truncating") needs. `statistics.truncated` is also checked: if the
  server ever truncates anyway, the call fails rather than returning a vector
  for text it never read.

`_EmbedContentLike` names only the one method this module calls
(`client.aio.models.embed_content`), so tests swap in a fake with the same
shape and no network.
"""

from __future__ import annotations

from typing import Protocol, cast

from google.genai import types
from google.genai.errors import APIError

from compliance_rag import telemetry
from compliance_rag.adapters.interfaces import Embedder
from compliance_rag.settings import Settings

TASK_TYPE = "RETRIEVAL_DOCUMENT"
"""Vertex's task-type hint tunes the embedding for asymmetric search, with
different optimal values for corpus text (`RETRIEVAL_DOCUMENT`) and a
search query (`RETRIEVAL_QUERY`). The existing `Embedder` interface takes
no such hint — `embed(texts) -> vectors` alone — so every call through this
adapter is tagged as if it were corpus text. Query-time embedding through this
same method is measurably suboptimal until the interface (or a second adapter
method) can express the distinction; flagged here rather than silently
decided, since `adapters/interfaces.py` is locked.
"""

_PRICE_PER_TOKEN_USD = 0.15 / 1_000_000
"""$0.15 per 1M input tokens, gemini-embedding-001 has no output-token cost
(published rate, 2026-09-27). Re-verify against the Vertex AI pricing
console before trusting this for anything beyond SERVICES.md's own <$1/mo
Phase 1 estimate — pricing pages change without this constant being told."""


class _EmbedContentLike(Protocol):
    async def embed_content(
        self, *, model: str, contents: list[str], config: types.EmbedContentConfig
    ) -> types.EmbedContentResponse: ...


class VertexEmbedder(Embedder):
    """Dense vectors from `gemini-embedding-001` (`SERVICES.md`: Vertex AI
    Embeddings, 1536 dims)."""

    def __init__(self, models: _EmbedContentLike, *, model_name: str, dimensions: int) -> None:
        self._models = models
        self._model_name = model_name
        self._dimensions = dimensions

    @classmethod
    def from_settings(cls, settings: Settings) -> VertexEmbedder:
        """The one place this module constructs a real Vertex client — never
        exercised in tests, which inject `_EmbedContentLike` fakes instead
        (`tests/test_vertex_embedder.py`). The location is the AU region
        (`gcp_region`), never `global`."""
        from google import genai  # local import: only the real path needs it

        client = genai.Client(
            vertexai=True, project=settings.gcp_project_id, location=settings.gcp_region
        )
        # `embed_content` accepts a broad content union whose list members are
        # invariant, so it isn't structurally an `_EmbedContentLike` by
        # pyright's rules — but this module only ever passes `list[str]`.
        return cls(
            cast("_EmbedContentLike", client.aio.models),
            model_name=settings.vertex_embedding_model,
            dimensions=settings.vertex_embedding_dims,
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        with telemetry.span(
            "vertex_embedder.embed",
            **{
                "embedder.model": self._model_name,
                "embedder.batch_size": len(texts),
            },
        ) as current_span:
            config = types.EmbedContentConfig(
                task_type=TASK_TYPE,
                output_dimensionality=self._dimensions,
                auto_truncate=False,
            )
            vectors: list[tuple[float, ...]] = []
            total_tokens = 0
            for index, text in enumerate(texts):
                try:
                    response = await self._models.embed_content(
                        model=self._model_name, contents=[text], config=config
                    )
                except APIError as exc:
                    raise ValueError(f"text {index}: rejected by the embedder — {exc}") from exc
                embeddings = response.embeddings or []
                if len(embeddings) != 1 or not embeddings[0].values:
                    raise ValueError(f"text {index}: expected one embedding, got {len(embeddings)}")
                embedding = embeddings[0]
                statistics = embedding.statistics
                if statistics is not None and statistics.truncated:
                    raise ValueError(f"text {index}: truncated by the embedder")
                if len(embedding.values or ()) != self._dimensions:
                    raise ValueError(
                        f"text {index}: {len(embedding.values or ())} dimensions, "
                        f"expected {self._dimensions}"
                    )
                vectors.append(tuple(embedding.values or ()))
                if statistics is not None and statistics.token_count is not None:
                    total_tokens += int(statistics.token_count)

            current_span.set_attribute("embedder.total_tokens", total_tokens)
            current_span.set_attribute(
                "embedder.total_cost_usd", total_tokens * _PRICE_PER_TOKEN_USD
            )
            return tuple(vectors)
