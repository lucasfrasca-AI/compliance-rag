"""`Generator` over Claude on Vertex AI (issue #29), in `asia-southeast1` during
the build phase (ADR-0023): Claude is not served from `australia-southeast1`.
`create` refuses to build a client offshore once the corpus holds real tenant
data (`adapters/generation_residency.py`).

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
from compliance_rag.adapters.generation_residency import (
    assert_generation_allowed,
    is_offshore,
    validate_generation_region,
)
from compliance_rag.adapters.interfaces import Generation, Generator, RetrievalAdmin
from compliance_rag.settings import Settings

# `claude-sonnet-4-6` per-token prices, USD: Google's list rates times the 10%
# premium regional endpoints carry over the global one — this repo only ever
# uses a regional endpoint. Checked against the Vertex AI pricing page's
# regional table on 2026-10-09: input $3.30, output $16.50, 5-minute cache
# write $4.13, cache hit $0.33 per 1M tokens. Constants: re-check when the
# model or the page changes.
_INPUT_USD = 3.00 / 1_000_000
_OUTPUT_USD = 15.00 / 1_000_000
_CACHE_WRITE_USD = 3.75 / 1_000_000  # 5-minute ephemeral cache write: 1.25x input
_CACHE_READ_USD = 0.30 / 1_000_000  # cache hit: 0.1x input
_REGIONAL_PREMIUM = 1.10
PRICE_TABLE = "claude-sonnet-4-6-vertex-regional-2026-10-09"
"""Names the rates above on every span, so a cost figure carries its own
provenance (checked against Google's published page, not yet an invoice)."""


def price_usd(*, uncached_input: int, cache_write: int, cache_read: int, output: int) -> float:
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


class VertexClaudeGenerator(Generator):
    def __init__(
        self, messages: _MessagesLike, *, model: str, region: str, corpus: RetrievalAdmin
    ) -> None:
        """`corpus` is required, not optional: offshore, the gate is asked
        again before every request (`_assert_still_allowed`), so no way of
        constructing the generator — and no data written after it started,
        by any process or path — gets real tenant text sent offshore."""
        if not model:
            raise ValueError("no Claude model configured (VERTEX_CLAUDE_MODEL)")
        self._messages = messages
        self._model = model
        self._region = validate_generation_region(region)
        self._corpus = corpus

    async def _assert_still_allowed(self) -> None:
        if is_offshore(self._region):
            assert_generation_allowed(
                generation_region=self._region,
                holds_real_tenant_data=await self._corpus.holds_real_tenant_data(),
            )

    @classmethod
    async def create(cls, settings: Settings, *, corpus: RetrievalAdmin) -> VertexClaudeGenerator:
        """The one place a real Vertex client is built — after the residency
        gate (ADR-0023). The region is re-validated here as well as in
        `Settings` (`global` and anything but Sydney/Singapore refused), and
        an offshore region is refused outright while `corpus` holds any real
        tenant data. Async because the gate asks the store."""
        region = validate_generation_region(settings.vertex_claude_region)
        if is_offshore(region):
            assert_generation_allowed(
                generation_region=region,
                holds_real_tenant_data=await corpus.holds_real_tenant_data(),
            )
        from anthropic.lib.vertex import AsyncAnthropicVertex  # only the real path needs it

        client = AsyncAnthropicVertex(region=region, project_id=settings.gcp_project_id)
        return cls(
            client.messages, model=settings.vertex_claude_model, region=region, corpus=corpus
        )

    async def generate(
        self,
        *,
        system: str,
        cached_prefix: str | None,
        user: str,
        max_tokens: int,
    ) -> Generation:
        await self._assert_still_allowed()
        blocks: list[TextBlockParam] = [{"type": "text", "text": system}]
        if cached_prefix:
            blocks.append(
                {"type": "text", "text": cached_prefix, "cache_control": {"type": "ephemeral"}}
            )
        with telemetry.span(
            "generator.generate",
            **{
                "generator.model": self._model,
                "generator.region": self._region,
                "generator.cached_prefix_sent": bool(cached_prefix),
            },
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
            cost = price_usd(
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
            current.set_attribute("generator.price_table", PRICE_TABLE)
            # A prefix below the model's minimum cacheable length is neither
            # written nor read — silently. Flag it, or D-11 would look healthy
            # on the trace while every request paid full price for the prefix.
            current.set_attribute(
                "generator.prefix_not_cached",
                bool(cached_prefix) and not (cache_read or cache_write),
            )
            current.set_attribute("generator.stop_reason", message.stop_reason or "")
            return generation
