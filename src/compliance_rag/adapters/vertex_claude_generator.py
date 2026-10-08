"""`Generator` over Claude on Vertex AI, in `australia-southeast1` (issue #29).

The tenant policy corpus rides as a cached prompt prefix (D-11): it is the
last system block and carries `cache_control`, so every request for the same
tenant re-reads it at the cached rate instead of paying for it again. Whether
the cache was hit comes from the response's own usage counts, never assumed.

This module sees prompts only as opaque strings. Delimiting ingested text and
validating the structured output are the caller's job (`assessment/prompt.py`,
`assessment/draft.py`), so no prompt or completion text goes onto the span —
only the model, token counts, cache use and cost.

`_MessagesLike` names the one call this module makes, so tests drive it with
a fake that returns real `anthropic.types.Message` objects.
"""

from __future__ import annotations

from typing import Protocol

from anthropic.types import Message, MessageParam, TextBlock, TextBlockParam

from compliance_rag import telemetry
from compliance_rag.adapters.interfaces import Generation, Generator
from compliance_rag.settings import REGION, Settings

# Sonnet-class list prices per token, USD (2026-10-08). Vertex's regional
# endpoints are priced at a premium over its global endpoint; this repo only
# ever uses the regional one (D-07). Re-verify against the Vertex AI pricing
# page before trusting a projection built on these — they are constants, and
# pricing pages change without telling them.
_INPUT_USD = 3.00 / 1_000_000
_OUTPUT_USD = 15.00 / 1_000_000
_CACHE_WRITE_USD = 3.75 / 1_000_000  # 5-minute ephemeral cache write: 1.25x input
_CACHE_READ_USD = 0.30 / 1_000_000  # cache hit: 0.1x input
_REGIONAL_PREMIUM = 1.10


def cost_usd(*, uncached_input: int, cache_write: int, cache_read: int, output: int) -> float:
    return _REGIONAL_PREMIUM * (
        uncached_input * _INPUT_USD
        + cache_write * _CACHE_WRITE_USD
        + cache_read * _CACHE_READ_USD
        + output * _OUTPUT_USD
    )


class _MessagesLike(Protocol):
    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: list[TextBlockParam],
        messages: list[MessageParam],
    ) -> Message: ...


class GeneratorResidencyError(ValueError):
    """Claude on Vertex outside `australia-southeast1` (D-07)."""


class VertexClaudeGenerator(Generator):
    def __init__(self, messages: _MessagesLike, *, model: str) -> None:
        if not model:
            raise ValueError("no Claude model configured (VERTEX_CLAUDE_MODEL)")
        self._messages = messages
        self._model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> VertexClaudeGenerator:
        """The one place a real Vertex client is built. The region is
        asserted here as well as in `Settings`: a `global` or US endpoint
        would move prompts — tenant policy text — out of Australia."""
        if settings.gcp_region != REGION:
            raise GeneratorResidencyError(f"Claude on Vertex must run in {REGION}")
        from anthropic.lib.vertex import AsyncAnthropicVertex  # only the real path needs it

        client = AsyncAnthropicVertex(region=REGION, project_id=settings.gcp_project_id)
        return cls(client.messages, model=settings.vertex_claude_model)

    async def generate(
        self,
        *,
        system: str,
        cached_prefix: str | None,
        user: str,
        max_tokens: int,
    ) -> Generation:
        blocks: list[TextBlockParam] = [{"type": "text", "text": system}]
        if cached_prefix:
            blocks.append(
                {"type": "text", "text": cached_prefix, "cache_control": {"type": "ephemeral"}}
            )
        with telemetry.span(
            "generator.generate",
            **{"generator.model": self._model, "generator.cached_prefix_sent": bool(cached_prefix)},
        ) as current:
            message = await self._messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=blocks,
                messages=[{"role": "user", "content": user}],
            )
            usage = message.usage
            cache_read = usage.cache_read_input_tokens or 0
            cache_write = usage.cache_creation_input_tokens or 0
            cost = cost_usd(
                uncached_input=usage.input_tokens,
                cache_write=cache_write,
                cache_read=cache_read,
                output=usage.output_tokens,
            )
            generation = Generation(
                text="".join(b.text for b in message.content if isinstance(b, TextBlock)),
                input_tokens=usage.input_tokens + cache_write + cache_read,
                output_tokens=usage.output_tokens,
                cached_prefix_hit=cache_read > 0,
                stop_reason=message.stop_reason,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_write,
                cost_usd=cost,
            )
            current.set_attribute("generator.input_tokens", generation.input_tokens)
            current.set_attribute("generator.output_tokens", generation.output_tokens)
            current.set_attribute("generator.cache_read_input_tokens", cache_read)
            current.set_attribute("generator.cache_creation_input_tokens", cache_write)
            current.set_attribute("generator.cached_prefix_hit", generation.cached_prefix_hit)
            current.set_attribute("generator.cost_usd", cost)
            current.set_attribute("generator.stop_reason", message.stop_reason or "")
            return generation
