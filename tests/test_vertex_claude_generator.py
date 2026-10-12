"""`VertexClaudeGenerator` (issue #29), driven by a fake messages client that
returns real `anthropic.types.Message` objects — no network, no billing."""

from __future__ import annotations

from typing import Any

import pytest
from anthropic.types import Message, MessageParam, TextBlock, TextBlockParam, Usage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.fakes import FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.generation_residency import GenerationResidencyError
from compliance_rag.adapters.vertex_claude_generator import VertexClaudeGenerator, price_usd
from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.settings import Settings

PREFIX = (
    "<tenant_policies boundary=x>\n[x clause acme/p:v1:1]\nPolicy.\n[x end]\n</tenant_policies>"
)


class FakeMessages:
    def __init__(self, *, cache_read: int = 0, cache_write: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []  # Any: recorded request kwargs
        self._usage = Usage(
            input_tokens=120,
            output_tokens=40,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        )

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: list[TextBlockParam],
        messages: list[MessageParam],
    ) -> Message:
        self.calls.append(
            {"model": model, "max_tokens": max_tokens, "system": system, "messages": messages}
        )
        return Message(
            id="msg_1",
            type="message",
            role="assistant",
            model=model,
            content=[TextBlock(type="text", text='{"answer": "a", "citations": []}')],
            stop_reason="end_turn",
            usage=self._usage,
        )


async def _generate(messages: FakeMessages, prefix: str | None = PREFIX) -> Any:  # Any: Generation
    generator = VertexClaudeGenerator(
        messages, model="claude-test", region="us", corpus=_corpus(LAW, SYNTHETIC)
    )
    return await generator.generate(system="sys", cached_prefix=prefix, user="q", max_tokens=64)


async def test_the_policy_prefix_is_the_cached_system_block() -> None:
    messages = FakeMessages()
    await _generate(messages)
    system = messages.calls[0]["system"]
    assert system[0] == {"type": "text", "text": "sys"}
    assert system[1] == {"type": "text", "text": PREFIX, "cache_control": {"type": "ephemeral"}}


async def test_without_policies_nothing_is_marked_for_caching() -> None:
    messages = FakeMessages()
    await _generate(messages, prefix=None)
    assert messages.calls[0]["system"] == [{"type": "text", "text": "sys"}]


async def test_a_cache_hit_is_read_from_the_response_not_assumed() -> None:
    hit = await _generate(FakeMessages(cache_read=5000))
    miss = await _generate(FakeMessages(cache_write=5000))
    assert hit.cached_prefix_hit and hit.cache_read_input_tokens == 5000
    assert not miss.cached_prefix_hit and miss.cache_creation_input_tokens == 5000
    assert hit.input_tokens == miss.input_tokens == 5120


async def test_cost_comes_from_the_real_token_counts() -> None:
    hit = await _generate(FakeMessages(cache_read=5000))
    # Sonnet 5, US multi-region: $2.20 in, $0.22 cache hit, $11.00 out per 1M.
    expected = (120 * 2.20 + 5000 * 0.22 + 40 * 11.00) / 1_000_000
    assert abs(hit.cost_usd - expected) < 1e-12
    assert hit.cost_usd == price_usd(uncached_input=120, cache_write=0, cache_read=5000, output=40)
    miss = await _generate(FakeMessages(cache_write=5000))
    assert miss.cost_usd > hit.cost_usd


async def test_the_span_carries_tokens_cache_and_cost_but_no_prompt_text(
    span_exporter: InMemorySpanExporter,
) -> None:
    await _generate(FakeMessages(cache_read=5000))
    (generate_span,) = [s for s in span_exporter.get_finished_spans() if s.name.startswith("gen")]
    attributes = dict(generate_span.attributes or {})
    assert attributes["generator.cached_prefix_hit"] is True
    assert attributes["generator.cache_read_input_tokens"] == 5000
    assert attributes["generator.input_tokens"] == 5120
    cost = attributes["generator.cost_usd"]
    assert isinstance(cost, float) and cost > 0
    assert attributes["generator.prefix_not_cached"] is False
    assert str(attributes["generator.price_table"]).startswith(
        "claude-sonnet-5-vertex-us-multi-region"
    )
    assert attributes["generator.region"] == "us"
    # Prompt text never reaches the span: neither the system text, the
    # cached policy prefix nor the user turn.
    rendered = " ".join(str(v) for v in attributes.values())
    assert "Policy." not in rendered and "sys" not in rendered.split()


async def test_a_prefix_the_api_did_not_cache_is_flagged(
    span_exporter: InMemorySpanExporter,
) -> None:
    """Below the model's minimum cacheable length the API neither writes nor
    reads the cache, and says nothing. The span must."""
    generation = await _generate(FakeMessages())
    assert not generation.cached_prefix_hit
    (generate_span,) = [s for s in span_exporter.get_finished_spans() if s.name.startswith("gen")]
    assert (generate_span.attributes or {})["generator.prefix_not_cached"] is True


def test_a_generator_with_no_model_is_refused() -> None:
    with pytest.raises(ValueError, match="VERTEX_CLAUDE_MODEL"):
        VertexClaudeGenerator(FakeMessages(), model="", region="us", corpus=_corpus())


def _settings(region: str = "us") -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
        gcp_project_id="p",
        vertex_claude_model="claude-test",
        vertex_claude_region=region,
    )


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:  # Any: constructor kwargs
    built: dict[str, Any] = {}  # Any: constructor kwargs

    class Client:
        def __init__(self, **kwargs: Any) -> None:  # Any: constructor kwargs
            built.update(kwargs)
            self.messages = FakeMessages()

    import anthropic.lib.vertex

    monkeypatch.setattr(anthropic.lib.vertex, "AsyncAnthropicVertex", Client)
    return built


def _corpus(*chunks: Chunk) -> FakeRetrievalStore:
    return FakeRetrievalStore(chunks, ledger=FakeLedgerStore())


LAW = Chunk(chunk_id="law", clause_id="privacy-act-1988:C1:s6/1", text="law")
SYNTHETIC = Chunk(
    chunk_id="syn",
    clause_id="acme/privacy-policy:v1:1/1",
    text="invented",
    scope=Scope.TENANT,
    tenant_id="acme",
    synthetic=True,
)
REAL = SYNTHETIC.model_copy(update={"chunk_id": "real", "synthetic": False})


async def test_the_us_endpoint_is_allowed_while_the_corpus_is_law_and_synthetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _patch_client(monkeypatch)
    await VertexClaudeGenerator.create(_settings(), corpus=_corpus(LAW, SYNTHETIC))
    assert built == {"region": "us", "project_id": "p"}


async def test_the_us_endpoint_is_refused_once_any_real_tenant_data_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0023's gate: one real (unmarked) tenant chunk closes offshore
    generation, and no client is ever built."""
    built = _patch_client(monkeypatch)
    with pytest.raises(GenerationResidencyError, match="real"):
        await VertexClaudeGenerator.create(_settings(), corpus=_corpus(LAW, SYNTHETIC, REAL))
    assert built == {}


async def test_sydney_generation_is_allowed_with_real_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is about offshore generation, not real data as such."""
    built = _patch_client(monkeypatch)
    await VertexClaudeGenerator.create(_settings("australia-southeast1"), corpus=_corpus(LAW, REAL))
    assert built["region"] == "australia-southeast1"


@pytest.mark.parametrize(
    "region",
    ["global", "US", " us", "asia-southeast1", "us-east5", "eu", "europe-west1", "us-central1"],
)
def test_the_global_endpoint_and_every_other_region_are_refused(region: str) -> None:
    with pytest.raises(ValueError, match="never `global`"):
        _settings(region)
    with pytest.raises(GenerationResidencyError):
        VertexClaudeGenerator(FakeMessages(), model="claude-test", region=region, corpus=_corpus())


async def test_real_data_written_after_startup_stops_the_next_offshore_request() -> None:
    """Startup isn't the only check: data stored later — by another process,
    or a path that skips CorpusLifecycle — closes the gate before the next
    request leaves Australia, and no request is sent."""
    corpus = _corpus(LAW, SYNTHETIC)
    messages = FakeMessages()
    generator = VertexClaudeGenerator(messages, model="claude-test", region="us", corpus=corpus)
    await generator.generate(system="s", cached_prefix=None, user="q", max_tokens=8)
    await corpus.upsert_chunks((REAL,))  # straight to the store, no lifecycle
    with pytest.raises(GenerationResidencyError):
        await generator.generate(system="s", cached_prefix=None, user="q", max_tokens=8)
    assert len(messages.calls) == 1


def test_the_generation_region_defaults_to_sydney() -> None:
    """Fail closed: the US endpoint must be chosen explicitly."""
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
    assert settings.vertex_claude_region == "australia-southeast1"
