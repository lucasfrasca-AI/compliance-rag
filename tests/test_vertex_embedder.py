"""`VertexEmbedder` (issue #13): the `Embedder` seam backed by Vertex AI's
`gemini-embedding-001`.

Against a tiny stand-in for `vertexai.language_models.TextEmbeddingModel` —
the same two methods (`get_embeddings_async`) the adapter actually calls —
so these tests exercise the adapter's own batching, truncation-handling and
telemetry without a real (billable) API call. `VertexEmbedder.from_settings`,
the one place that actually constructs a real Vertex client, is deliberately
never exercised here.

Two real, verified findings from the Vertex SDK shape this adapter, not
assumptions:

- `gemini-embedding-001` accepts exactly one text per `predict` call — the
  SDK's own `get_embeddings`/`get_embeddings_async` signature takes a list,
  but multiple independent projects (langchain.js #8490, mem0 #6189) hit the
  same bug trusting the documented "up to 250 inputs" figure, which turns
  out to be true for older embedding models but not this one. `embed()`
  loops one text per call rather than batching them into one request.
- `get_embeddings`/`get_embeddings_async` default to `auto_truncate=True`
  (silent truncation) — `_prepare_text_embedding_request`'s source shows
  `autoTruncate` is passed straight through as a request parameter, so
  passing `auto_truncate=False` moves "reject an over-long input" from a
  silent server-side truncation to a request the server itself refuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from google.api_core.exceptions import InvalidArgument
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from vertexai.language_models import TextEmbedding, TextEmbeddingInput

from compliance_rag.adapters.interfaces import Embedder
from compliance_rag.adapters.vertex_embedder import VertexEmbedder

DIMENSIONS = 1536
# Deliberately not "gemini-embedding-001" (Settings' own default) — proves
# the span reports what this instance was actually constructed with, not
# Settings.vertex_embedding_model's class-level default.
MODEL_NAME = "test-fixture-embedding-model"


@dataclass
class _FakeVertexModel:
    """Stands in for `TextEmbeddingModel`: one call per text, real
    signature, real return shape — no network."""

    calls: list[list[TextEmbeddingInput]] = field(default_factory=list[list[TextEmbeddingInput]])
    kwargs_seen: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    raise_on_call: int | None = None  # 0-indexed call number to fail, if any

    async def get_embeddings_async(
        self,
        texts: list[TextEmbeddingInput],
        *,
        auto_truncate: bool,
        output_dimensionality: int | None,
    ) -> list[TextEmbedding]:
        call_index = len(self.calls)
        self.calls.append(texts)
        self.kwargs_seen.append(
            {"auto_truncate": auto_truncate, "output_dimensionality": output_dimensionality}
        )
        if call_index == self.raise_on_call:
            raise InvalidArgument("input is longer than the maximum length")
        assert len(texts) == 1, "gemini-embedding-001 accepts exactly one text per call"
        token_count = max(1, len(texts[0].text.split()))
        return [
            TextEmbedding(
                values=[0.1] * (output_dimensionality or DIMENSIONS),
                statistics=_Statistics(token_count=token_count, truncated=False),
            )
        ]


def _Statistics(*, token_count: int, truncated: bool):
    # TextEmbeddingStatistics isn't exported from vertexai.language_models'
    # public surface (confirmed: absent from dir(vertexai.language_models)),
    # even though TextEmbedding.statistics's own type annotation names it —
    # the private submodule is the only path to constructing one.
    from vertexai.language_models._language_models import TextEmbeddingStatistics

    return TextEmbeddingStatistics(token_count=token_count, truncated=truncated)


def test_vertex_embedder_is_an_embedder() -> None:
    embedder = VertexEmbedder(_FakeVertexModel(), model_name=MODEL_NAME, dimensions=DIMENSIONS)
    assert isinstance(embedder, Embedder)


def test_dimensions_property_reports_configured_value() -> None:
    embedder = VertexEmbedder(_FakeVertexModel(), model_name=MODEL_NAME, dimensions=DIMENSIONS)
    assert embedder.dimensions == DIMENSIONS


async def test_embed_returns_one_vector_per_text_in_order() -> None:
    model = _FakeVertexModel()
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    vectors = await embedder.embed(("first text", "second text"))
    assert len(vectors) == 2
    assert all(len(v) == DIMENSIONS for v in vectors)


async def test_embed_calls_the_model_once_per_text_not_once_for_the_whole_batch() -> None:
    """The real, verified constraint: gemini-embedding-001 takes one input
    per call. A single call carrying the whole batch would either error or
    silently embed only the first text against the model's real API."""
    model = _FakeVertexModel()
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    await embedder.embed(("a", "b", "c"))
    assert len(model.calls) == 3
    assert all(len(call) == 1 for call in model.calls)


async def test_embed_disables_auto_truncate_on_every_call() -> None:
    model = _FakeVertexModel()
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    await embedder.embed(("text",))
    assert model.kwargs_seen[0]["auto_truncate"] is False


async def test_embed_passes_the_configured_output_dimensionality() -> None:
    model = _FakeVertexModel()
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    await embedder.embed(("text",))
    assert model.kwargs_seen[0]["output_dimensionality"] == DIMENSIONS


async def test_an_over_long_input_fails_loudly_naming_its_position_not_silently_truncated() -> None:
    """Issue #13's own acceptance: 'fails loudly rather than silently
    truncating an over-long input.' `auto_truncate=False` makes the real
    API refuse rather than truncate (see module docstring); this proves
    the adapter surfaces that refusal as a clear error naming which input
    in the batch it was, not a bare re-raise of the SDK's own exception."""
    model = _FakeVertexModel(raise_on_call=1)
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    with pytest.raises(ValueError, match=r"text 1\b"):
        await embedder.embed(("short", "way too long" * 10_000, "short again"))


async def test_embed_emits_a_span_with_token_count_and_cost(
    span_exporter: InMemorySpanExporter,
) -> None:
    model = _FakeVertexModel()
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    await embedder.embed(("one two three", "four five"))  # 3 + 2 words -> 3 + 2 tokens (stub)

    spans = [s for s in span_exporter.get_finished_spans() if s.name == "vertex_embedder.embed"]
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs.get("embedder.model") == MODEL_NAME
    assert attrs.get("embedder.batch_size") == 2
    assert attrs.get("embedder.total_tokens") == 5
    cost = attrs.get("embedder.total_cost_usd")
    assert isinstance(cost, float)
    assert cost > 0
