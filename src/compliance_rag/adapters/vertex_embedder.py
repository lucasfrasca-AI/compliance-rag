"""`Embedder` over Vertex AI's `gemini-embedding-001` (issue #13).

Two real, verified constraints from the Vertex SDK shape this module, not
assumptions carried in from documentation:

- **One text per call.** `TextEmbeddingModel.get_embeddings`/`_async`'s own
  signature takes a list, and Vertex's docs describe "up to 250 inputs" —
  true for older embedding models, not for `gemini-embedding-001`. Multiple
  independent projects (langchain.js #8490, mem0 #6189) hit the same bug
  trusting that figure. `embed()` here issues one request per text.
- **`auto_truncate` defaults to `True`.** Reading
  `TextEmbeddingModel._prepare_text_embedding_request`'s own source: it
  passes `autoTruncate` straight through as a request parameter with no
  client-side validation — the server decides what happens. Explicitly
  passing `auto_truncate=False` is what turns "silently truncate" into
  "refuse the request," which is what issue #13's acceptance criterion
  ("fails loudly rather than silently truncating") actually needs.

`_EmbeddingModelLike` names only the one method this module calls, for the
same reason `gcs_clause_store.py`'s `_BucketLike` exists: `google-cloud-
aiplatform` ships no inline type stubs, so typing against the real
`TextEmbeddingModel` propagates `Unknown` through every call site under
pyright strict, and a real client can't be swapped for a lightweight fake in
tests without a shared, minimally-typed shape either way.
"""

from __future__ import annotations

from typing import Protocol, cast

from google.api_core.exceptions import GoogleAPICallError
from vertexai.language_models import TextEmbedding, TextEmbeddingInput, TextEmbeddingModel

from compliance_rag import telemetry
from compliance_rag.adapters.interfaces import Embedder
from compliance_rag.settings import Settings

_TASK_TYPE = "RETRIEVAL_DOCUMENT"
"""Vertex's task-type hint tunes the embedding for asymmetric search, with
different optimal values for corpus text (`RETRIEVAL_DOCUMENT`) and a
search query (`RETRIEVAL_QUERY`). The existing `Embedder` interface takes
no such hint — `embed(texts) -> vectors` alone, unchanged by this issue's
own acceptance criterion ("no call sites change") — so every call through
this adapter is tagged as if it were corpus text. Query-time embedding
through this same method is measurably suboptimal until the interface (or a
second adapter method) can express the distinction; flagged here rather
than silently decided, since `adapters/interfaces.py` is locked.
"""

_PRICE_PER_TOKEN_USD = 0.15 / 1_000_000
"""$0.15 per 1M input tokens, gemini-embedding-001 has no output-token cost
(published rate, 2026-09-27). Re-verify against the Vertex AI pricing
console before trusting this for anything beyond SERVICES.md's own <$1/mo
Phase 1 estimate — pricing pages change without this constant being told."""


class _EmbeddingModelLike(Protocol):
    async def get_embeddings_async(
        self,
        texts: list[TextEmbeddingInput],
        *,
        auto_truncate: bool,
        output_dimensionality: int | None,
    ) -> list[TextEmbedding]: ...


class VertexEmbedder(Embedder):
    """Dense vectors from `gemini-embedding-001` (`SERVICES.md`: Vertex AI
    Embeddings, 1536 dims)."""

    def __init__(self, model: _EmbeddingModelLike, *, dimensions: int) -> None:
        self._model = model
        self._dimensions = dimensions

    @classmethod
    def from_settings(cls, settings: Settings) -> VertexEmbedder:
        """The one place this module constructs a real Vertex client — never
        exercised in tests, which inject `_EmbeddingModelLike` fakes
        instead (`tests/test_vertex_embedder.py`)."""
        import vertexai  # local import: only the real path needs it

        vertexai.init(project=settings.gcp_project_id, location=settings.gcp_region)
        # TextEmbeddingModel.get_embeddings_async accepts list[str | TextEmbeddingInput]
        # (invariant List, so not structurally a _EmbeddingModelLike by pyright's rules)
        # but every call this module makes only ever passes list[TextEmbeddingInput] —
        # exactly what the protocol promises callers.
        model = cast(
            "_EmbeddingModelLike",
            TextEmbeddingModel.from_pretrained(settings.vertex_embedding_model),
        )
        return cls(model, dimensions=settings.vertex_embedding_dims)

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        with telemetry.span(
            "vertex_embedder.embed",
            **{
                "embedder.model": Settings.model_fields["vertex_embedding_model"].default,
                "embedder.batch_size": len(texts),
            },
        ) as current_span:
            vectors: list[tuple[float, ...]] = []
            total_tokens = 0
            for index, text in enumerate(texts):
                try:
                    embeddings = await self._model.get_embeddings_async(
                        [TextEmbeddingInput(text=text, task_type=_TASK_TYPE)],
                        auto_truncate=False,
                        output_dimensionality=self._dimensions,
                    )
                except GoogleAPICallError as exc:
                    raise ValueError(f"text {index}: rejected by the embedder — {exc}") from exc
                embedding = embeddings[0]
                vectors.append(tuple(embedding.values))
                if embedding.statistics is not None:
                    total_tokens += embedding.statistics.token_count

            current_span.set_attribute("embedder.total_tokens", total_tokens)
            current_span.set_attribute(
                "embedder.total_cost_usd", total_tokens * _PRICE_PER_TOKEN_USD
            )
            return tuple(vectors)
