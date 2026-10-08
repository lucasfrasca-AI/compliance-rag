"""`VertexClaudeGenerator` (issue #29), driven by a fake messages client that
returns real `anthropic.types.Message` objects — no network, no billing."""

from __future__ import annotations

from typing import Any

import pytest
from anthropic.types import Message, MessageParam, TextBlock, TextBlockParam, Usage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.vertex_claude_generator import (
    GeneratorResidencyError,
    VertexClaudeGenerator,
    price_usd,
)
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
    generator = VertexClaudeGenerator(messages, model="claude-test")
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
    expected = 1.10 * (120 * 3.00 + 5000 * 0.30 + 40 * 15.00) / 1_000_000
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
    assert str(attributes["generator.price_table"]).endswith("-unverified")
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
        VertexClaudeGenerator(FakeMessages(), model="")


def test_the_real_client_is_built_for_sydney_only(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, Any] = {}  # Any: constructor kwargs

    class Client:
        def __init__(self, **kwargs: Any) -> None:  # Any: constructor kwargs
            built.update(kwargs)
            self.messages = FakeMessages()

    import anthropic.lib.vertex

    monkeypatch.setattr(anthropic.lib.vertex, "AsyncAnthropicVertex", Client)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
        gcp_project_id="p",
        vertex_claude_model="claude-test",
    )
    VertexClaudeGenerator.from_settings(settings)
    assert built == {"region": "australia-southeast1", "project_id": "p"}


def test_a_non_sydney_region_is_refused_even_past_settings_validation() -> None:
    """`Settings` already refuses another region; this is the second lock,
    for a Settings built without validation."""
    settings = Settings.model_construct(gcp_region="global", vertex_claude_model="claude-test")
    with pytest.raises(GeneratorResidencyError):
        VertexClaudeGenerator.from_settings(settings)
