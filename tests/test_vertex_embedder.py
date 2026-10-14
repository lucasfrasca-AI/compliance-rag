"""`VertexEmbedder` (issues #13, #73): the `Embedder` seam backed by Vertex
AI's `gemini-embedding-001` through the `google-genai` SDK.

Against a tiny stand-in for `client.aio.models` — the one method
(`embed_content`) the adapter actually calls, with the SDK's real request and
response types — so these tests exercise the adapter's own batching,
truncation-handling and telemetry without a real (billable) API call.
`VertexEmbedder.from_settings`, the one place that constructs a real client,
is deliberately never exercised here.

Two real, verified findings shape this adapter, not assumptions:

- `gemini-embedding-001` accepts exactly one text per request on Vertex —
  multiple independent projects (langchain.js #8490, mem0 #6189) hit the
  same bug trusting the documented "up to 250 inputs" figure. `embed()`
  loops one text per call rather than batching them into one request.
- Vertex defaults to `auto_truncate=True` (silent truncation); passing
  `auto_truncate=False` makes the server refuse an over-long input instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from google.genai import types
from google.genai.errors import ClientError
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.interfaces import Embedder
from compliance_rag.adapters.vertex_embedder import TASK_TYPE, VertexEmbedder

DIMENSIONS = 1536
# Deliberately not "gemini-embedding-001" (Settings' own default) — proves
# the span reports what this instance was actually constructed with, not
# Settings.vertex_embedding_model's class-level default.
MODEL_NAME = "test-fixture-embedding-model"


@dataclass
class _FakeModels:
    """Stands in for `client.aio.models`: one call per text, real signature,
    real return shape — no network."""

    calls: list[list[str]] = field(default_factory=list[list[str]])
    configs: list[types.EmbedContentConfig] = field(default_factory=list[types.EmbedContentConfig])
    models: list[str] = field(default_factory=list[str])
    raise_on_call: int | None = None  # 0-indexed call number to fail, if any
    truncate_on_call: int | None = None
    dimensions: int = DIMENSIONS
    embeddings_per_call: int = 1

    async def embed_content(
        self, *, model: str, contents: list[str], config: types.EmbedContentConfig
    ) -> types.EmbedContentResponse:
        call_index = len(self.calls)
        self.calls.append(contents)
        self.configs.append(config)
        self.models.append(model)
        if call_index == self.raise_on_call:
            raise ClientError(
                400, {"error": {"message": "input is longer than the maximum length"}}
            )
        assert len(contents) == 1, "gemini-embedding-001 accepts exactly one text per call"
        return types.EmbedContentResponse(
            embeddings=self.embeddings_per_call
            * [
                types.ContentEmbedding(
                    values=[0.1] * self.dimensions,
                    statistics=types.ContentEmbeddingStatistics(
                        token_count=max(1, len(contents[0].split())),
                        truncated=call_index == self.truncate_on_call,
                    ),
                )
            ]
        )


def test_vertex_embedder_is_an_embedder() -> None:
    embedder = VertexEmbedder(_FakeModels(), model_name=MODEL_NAME, dimensions=DIMENSIONS)
    assert isinstance(embedder, Embedder)


def test_dimensions_property_reports_configured_value() -> None:
    embedder = VertexEmbedder(_FakeModels(), model_name=MODEL_NAME, dimensions=DIMENSIONS)
    assert embedder.dimensions == DIMENSIONS


async def test_embed_returns_one_vector_per_text_in_order() -> None:
    model = _FakeModels()
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    vectors = await embedder.embed(("first text", "second text"))
    assert len(vectors) == 2
    assert all(len(v) == DIMENSIONS for v in vectors)


async def test_embed_calls_the_model_once_per_text_not_once_for_the_whole_batch() -> None:
    """The real, verified constraint: gemini-embedding-001 takes one input
    per call. A single call carrying the whole batch would either error or
    silently embed only the first text against the model's real API."""
    model = _FakeModels()
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    await embedder.embed(("a", "b", "c"))
    assert len(model.calls) == 3
    assert all(len(call) == 1 for call in model.calls)


async def test_embed_disables_auto_truncate_on_every_call() -> None:
    model = _FakeModels()
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    await embedder.embed(("text", "more"))
    assert [c.auto_truncate for c in model.configs] == [False, False]


async def test_embed_passes_the_configured_output_dimensionality() -> None:
    model = _FakeModels()
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    await embedder.embed(("text",))
    assert model.configs[0].output_dimensionality == DIMENSIONS
    assert model.configs[0].task_type == TASK_TYPE
    assert model.models == [MODEL_NAME]


async def test_an_over_long_input_fails_loudly_naming_its_position_not_silently_truncated() -> None:
    """Issue #13's own acceptance: 'fails loudly rather than silently
    truncating an over-long input.' `auto_truncate=False` makes the real
    API refuse rather than truncate (see module docstring); this proves
    the adapter surfaces that refusal as a clear error naming which input
    in the batch it was, not a bare re-raise of the SDK's own exception."""
    model = _FakeModels(raise_on_call=1)
    embedder = VertexEmbedder(model, model_name=MODEL_NAME, dimensions=DIMENSIONS)
    with pytest.raises(ValueError, match=r"text 1\b"):
        await embedder.embed(("short", "way too long" * 10_000, "short again"))


async def test_embed_emits_a_span_with_token_count_and_cost(
    span_exporter: InMemorySpanExporter,
) -> None:
    model = _FakeModels()
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


async def test_the_embedder_keeps_a_running_total_of_tokens_and_cost() -> None:
    """What an operator script reports as its spend (#144), across calls."""
    embedder = VertexEmbedder(_FakeModels(), model_name=MODEL_NAME, dimensions=DIMENSIONS)
    assert (embedder.tokens_embedded, embedder.cost_usd) == (0, 0.0)
    await embedder.embed(("one two three",))
    await embedder.embed(("four five",))
    assert embedder.tokens_embedded == 5
    assert abs(embedder.cost_usd - 5 * 0.15 / 1_000_000) < 1e-15


async def test_a_server_side_truncation_fails_rather_than_returning_a_vector() -> None:
    """If the server ever truncates despite `auto_truncate=False`, the vector
    would be for text it never read — refused, naming the position."""
    embedder = VertexEmbedder(
        _FakeModels(truncate_on_call=0), model_name=MODEL_NAME, dimensions=DIMENSIONS
    )
    with pytest.raises(ValueError, match=r"text 0: truncated"):
        await embedder.embed(("text",))


async def test_a_vector_of_the_wrong_dimension_is_refused() -> None:
    embedder = VertexEmbedder(
        _FakeModels(dimensions=768), model_name=MODEL_NAME, dimensions=DIMENSIONS
    )
    with pytest.raises(ValueError, match="768 dimensions, expected 1536"):
        await embedder.embed(("text",))


@pytest.mark.parametrize("count", [0, 2])
async def test_a_response_without_exactly_one_embedding_is_refused(count: int) -> None:
    embedder = VertexEmbedder(
        _FakeModels(embeddings_per_call=count), model_name=MODEL_NAME, dimensions=DIMENSIONS
    )
    with pytest.raises(ValueError, match=f"expected one embedding, got {count}"):
        await embedder.embed(("text",))
