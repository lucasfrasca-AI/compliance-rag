"""`Generator` over Gemini 3.5 Flash on Vertex AI (#326, ADR-0057):
`gemini-3.5-flash` at the US multi-region endpoint during the build phase
(ADR-0023), through the `google-genai` SDK the embedder already uses. It is
the default generator, because Claude on Vertex has not been approved on this
project; `VertexClaudeGenerator` stays, dormant, behind
`GENERATION_BACKEND=claude`.

`create` refuses to build a client offshore once the corpus holds real tenant
data (`adapters/generation_residency.py`), and `generate` asks again before
every offshore request. `us` is offshore, exactly as it is for Claude: the
gates of ADR-0023 and ADR-0032 are unchanged.

**How the prompt is laid out.** The system prompt is the request's system
instruction. The tenant policy corpus (the cached prefix, D-11) is the first
part of the one user turn, and the per-question text is the part after it, so
every request for the same tenant starts with the same bytes: the system
instruction, then the prefix. That is what Vertex's implicit cache keys on.

**Caching is implicit, not explicit** (decided 2026-11-11 from Google's
context-caching overview, https://docs.cloud.google.com/vertex-ai/generative-ai/
docs/context-cache/context-cache-overview):

- implicit caching is on by default for every project, needs nothing from the
  caller, carries a 90% discount on cached input tokens and **no storage
  charge**, and reports a hit as `usage_metadata.cached_content_token_count`;
- explicit `cachedContents` would add state to manage (a cache per tenant, its
  TTL, its invalidation when a policy changes) and a per-token-hour storage
  charge, to buy a guaranteed hit. Nothing here needs the guarantee: the
  hit is reported, and a miss costs the uncached rate, not a failure. It
  stays the fallback if implicit caching proves unreliable on the live check
  (#29, #31).

It is best-effort, which is why a hit is read from the response, never
assumed: the first request for a prefix is always a miss, a prefix under the
minimum never hits, and Google gives no guarantee beyond that. A prefix that
was sent and not hit says so on the span (`generator.prefix_not_cached`), so
D-11 can't look healthy on a trace while every request paid full price.
`cache_creation_input_tokens` is always 0: implicit caching has no write.

**Structured output.** The reply is requested as JSON (`response_mime_type`)
with a response schema, `REPLY_SCHEMA`: either an answer (`answer`,
`citations`) or a repair (`clause_id`, `quote`), the two shapes
`assessment/draft.py` parses. The schema helps the model and is never trusted:
`parse_draft` and `parse_repair` still validate strictly afterwards, and
`tests/test_gemini_generator.py` holds the schema to those models.

**Thinking.** Gemini 3.5 Flash thinks before it answers, and thinking tokens
count against `max_output_tokens` and are billed as output. They are held to
`thinking_level=LOW`, and `max_output_tokens` is the caller's `max_tokens`
plus `THINKING_HEADROOM_TOKENS`, so the caller's budget for the reply isn't
spent on thought. `output_tokens` is the reply plus the thinking, because that
is what is billed.

This module sees prompts only as opaque strings. Delimiting ingested text and
validating the structured output are the caller's job (`assessment/prompt.py`,
`assessment/draft.py`), so no prompt or completion text goes onto the span:
only the model, token counts, cache use and cost.

**Errors** leave here in our words (the SDK's exception is the `__cause__`, for
the server's log, and its text never reaches a member):

- HTTP 429 or `RESOURCE_EXHAUSTED` is `GenerationUnavailable(QUOTA)`, but only
  after the SDK's own retry (`http_options`, #329) has sent the request
  `RETRY_ATTEMPTS` times with exponential backoff: on Standard PayGo a 429 is
  Google's "try again later", not a fixed quota (ADR-0057). 503 is retried the
  same way and, if it never clears, raised as it is. The attempts made are
  `generator.attempts` and `generator.retry_attempts` on the span;
- HTTP 401/403 or `PERMISSION_DENIED`/`UNAUTHENTICATED` is
  `GenerationUnavailable(ACCESS)`;
- anything else is raised as it is, and the API reports it as `internal`.

`_ModelsLike` names the one call this module makes, so tests drive it with a
fake that returns real `google.genai.types.GenerateContentResponse` objects.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Protocol, cast

import httpx
from google.genai import types
from google.genai.errors import APIError

from compliance_rag import telemetry
from compliance_rag.adapters.generation_residency import (
    assert_generation_allowed,
    is_offshore,
    validate_generation_region,
)
from compliance_rag.adapters.interfaces import (
    Generation,
    GenerationUnavailable,
    Generator,
    RetrievalAdmin,
    Unavailability,
)
from compliance_rag.settings import Settings

# `gemini-3.5-flash` per-token prices, USD, at the `us` (non-global) endpoint:
# input $1.65, cached input $0.165 (a 90% discount), output $9.90 (thinking
# tokens included) per 1M tokens. Source: ADR-0057's research section
# (2026-11-10); a parallel docs PR re-verifies them against Google's pricing
# page, and if it finds different numbers these constants are updated.
_INPUT_USD = 1.65 / 1_000_000
_CACHED_INPUT_USD = 0.165 / 1_000_000
_OUTPUT_USD = 9.90 / 1_000_000
PRICE_TABLE = "gemini-3.5-flash-vertex-us-2026-11-11"
"""Names the rates above on every span, so a cost figure carries its own
provenance (Google's published rates as ADR-0057 recorded them, not yet an
invoice)."""

IMPLICIT_CACHE_MIN_TOKENS = 4_096
"""Google's cache minimum for Gemini 3.5 Flash, implicit and explicit alike
(the Gemini 3 row of the context-caching limits table; 6,144 applies to later
Flash models, not this one). Recorded on the span
beside the prompt's own size so an operator can tell "below the minimum" from
"above it and still missed"."""

THINKING_HEADROOM_TOKENS = 2_048
"""Added to the caller's `max_tokens` because thinking counts against
`max_output_tokens`: with `thinking_level=LOW` it is a few hundred tokens, and
a reply cut off by the model's own thinking would be unparseable."""

_THINKING_LEVEL = types.ThinkingLevel.LOW
_TIMEOUT_MS = 300_000

RETRY_HTTP_STATUS_CODES = (429, 503)
"""Retried, because Google's guidance for both on pay-as-you-go is to back off
and try again: a 429 on Standard PayGo "doesn't indicate that you've hit a
fixed quota" (ADR-0057). Never 400, 401, 403 or 404 (the request or the
access is wrong, and sending it again won't change that), nor the SDK's other
defaults (408, 500, 502, 504)."""
RETRY_ATTEMPTS = 5
"""Including the first request: four retries."""
RETRY_INITIAL_DELAY_S = 2.0
RETRY_MAX_DELAY_S = 20.0
RETRY_JITTER_S = 1.0
"""The SDK waits `initial * 2**n + uniform(0, jitter)` seconds before retry
n+1, capped at the max: 2, 4, 8 and 16 s plus jitter, so a member waits at most
about 34 s in total on top of the requests themselves before `quota` is shown
(#329). The SDK reads a `0` for any of these as unset and uses its own
default, so an override is a small positive number, never 0."""

_ATTEMPTS: ContextVar[list[int] | None] = ContextVar("gemini_attempts", default=None)


async def _count_attempt(request: httpx.Request) -> None:
    """httpx `request` hook: one more HTTP request made for the `generate` call
    in progress. The SDK's own retry loop gives no count back, so the transport
    is where an attempt is seen; `ContextVar` keeps concurrent calls apart."""
    del request
    counter = _ATTEMPTS.get()
    if counter is not None:
        counter[0] += 1


def http_options(
    *,
    retry_initial_delay: float = RETRY_INITIAL_DELAY_S,
    retry_max_delay: float = RETRY_MAX_DELAY_S,
    retry_jitter: float = RETRY_JITTER_S,
    httpx_async_client: httpx.AsyncClient | None = None,
) -> types.HttpOptions:
    """The one place the client's timeout and retry policy are set: `create`
    builds the real client from it and the tests build theirs from it, so both
    run the same attempts and status codes. Only the delays and the HTTP client
    can be overridden, so a test runs with near-zero waits over a stub
    transport (`httpx_async_client`), which gets the attempt counter too."""
    retry = types.HttpRetryOptions(
        attempts=RETRY_ATTEMPTS,
        initial_delay=retry_initial_delay,
        max_delay=retry_max_delay,
        jitter=retry_jitter,
        http_status_codes=list(RETRY_HTTP_STATUS_CODES),
    )
    if httpx_async_client is None:
        return types.HttpOptions(
            timeout=_TIMEOUT_MS,
            retry_options=retry,
            async_client_args={"event_hooks": {"request": [_count_attempt]}},
        )
    hooks = httpx_async_client.event_hooks
    httpx_async_client.event_hooks = {**hooks, "request": [*hooks["request"], _count_attempt]}
    return types.HttpOptions(
        timeout=_TIMEOUT_MS, retry_options=retry, httpx_async_client=httpx_async_client
    )


def price_usd(*, uncached_input: int, cached_input: int, output: int) -> float:
    return uncached_input * _INPUT_USD + cached_input * _CACHED_INPUT_USD + output * _OUTPUT_USD


def _object(properties: dict[str, types.Schema], *order: str) -> types.Schema:
    return types.Schema(
        type=types.Type.OBJECT,
        properties=properties,
        required=list(order),
        property_ordering=list(order),
    )


_CITATION = {
    "clause_id": types.Schema(type=types.Type.STRING),
    "quote": types.Schema(type=types.Type.STRING, min_length=1),
}

REPLY_SCHEMA = types.Schema(
    any_of=[
        # `DraftAnswer`: what the model replies to a question
        _object(
            {
                "answer": types.Schema(type=types.Type.STRING, min_length=1),
                "citations": types.Schema(
                    type=types.Type.ARRAY, items=_object(_CITATION, "clause_id", "quote")
                ),
            },
            "answer",
            "citations",
        ),
        # `DraftCitation`: what the model replies when asked to repair one
        _object(_CITATION, "clause_id", "quote"),
    ]
)
"""The two reply shapes of `assessment/draft.py`. One schema for both because
the `Generator` interface carries no schema: the prompt says which is wanted."""


class _ModelsLike(Protocol):
    async def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse: ...


def unavailability_of(error: APIError) -> Unavailability | None:
    """Which of our reasons a Vertex refusal is, or None when it is something
    else (and so an internal error)."""
    if error.code == 429 or error.status == "RESOURCE_EXHAUSTED":
        return Unavailability.QUOTA
    if error.code in (401, 403) or error.status in ("PERMISSION_DENIED", "UNAUTHENTICATED"):
        return Unavailability.ACCESS
    return None


def _reply_text(response: types.GenerateContentResponse) -> str:
    """The reply's own text: every non-thought text part of the first
    candidate. Empty when the model returned nothing (a blocked prompt, a
    stop before any text), which `parse_draft` then refuses."""
    candidates = response.candidates or []
    if not candidates or candidates[0].content is None:
        return ""
    return "".join(
        part.text for part in candidates[0].content.parts or [] if part.text and not part.thought
    )


class GeminiGenerator(Generator):
    def __init__(
        self,
        models: _ModelsLike,
        *,
        model: str,
        region: str,
        corpus: RetrievalAdmin,
        sdk_client: object = None,
    ) -> None:
        """`corpus` is required, not optional: offshore, the gate is asked
        again before every request (`_assert_still_allowed`), so no way of
        constructing the generator — and no data written after it started,
        by any process or path — gets real tenant text sent offshore.
        `sdk_client` is the client `models` belongs to, held for as long as the
        generator lives: `google-genai`'s `Client` closes its HTTP clients when
        it is garbage-collected, and `client.aio.models` alone doesn't keep it
        alive (#144)."""
        if not model:
            raise ValueError("no Gemini model configured (GEMINI_MODEL)")
        self._models = models
        self._sdk_client = sdk_client
        self._model = model
        self._region = validate_generation_region(region)
        self._corpus = corpus

    async def _assert_still_allowed(self) -> None:
        if is_offshore(self._region):
            assert_generation_allowed(
                generation_region=self._region,
                holds_real_customer_data=await self._corpus.holds_real_customer_data(),
            )

    @classmethod
    async def create(cls, settings: Settings, *, corpus: RetrievalAdmin) -> GeminiGenerator:
        """The one place a real Vertex client is built for generation — after
        the residency gate (ADR-0023). The region is re-validated here as well
        as in `Settings` (`global` and anything but Sydney or the US
        multi-region refused), and an offshore region is refused outright while
        `corpus` holds any real tenant data. Async because the gate asks the
        store. Application Default Credentials, as the embedder's client: no
        key exists anywhere."""
        generation = settings.generation
        if generation.backend != "gemini":
            raise ValueError(f"GeminiGenerator built for GENERATION_BACKEND={generation.backend}")
        region = validate_generation_region(generation.region)
        if is_offshore(region):
            assert_generation_allowed(
                generation_region=region,
                holds_real_customer_data=await corpus.holds_real_customer_data(),
            )
        from google import genai  # only the real path needs it

        client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=region,
            http_options=http_options(),
        )
        # `generate_content` accepts a broad contents union whose list members
        # are invariant, so it isn't structurally a `_ModelsLike` by pyright's
        # rules — but this module only ever passes `list[types.Content]`.
        return cls(
            cast("_ModelsLike", client.aio.models),
            model=generation.model,
            region=region,
            corpus=corpus,
            sdk_client=client,
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
        # The prefix first, then the question: the bytes that repeat across a
        # tenant's requests lead, so the implicit cache can match them.
        parts = [types.Part(text=cached_prefix)] if cached_prefix else []
        parts.append(types.Part(text=user))
        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens + THINKING_HEADROOM_TOKENS,
            response_mime_type="application/json",
            response_schema=REPLY_SCHEMA,
            thinking_config=types.ThinkingConfig(thinking_level=_THINKING_LEVEL),
            # No tools are offered, so there is nothing to call automatically.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        with telemetry.span(
            "generator.generate",
            **{
                "generator.backend": "gemini",
                "generator.model": self._model,
                "generator.region": self._region,
                "generator.cached_prefix_sent": bool(cached_prefix),
                "generator.cache_mode": "implicit",
            },
        ) as current:
            attempts = [0]
            token = _ATTEMPTS.set(attempts)
            try:
                response = await self._models.generate_content(
                    model=self._model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=config,
                )
            except APIError as refused:
                # Vertex's refusal, raised by the SDK. Named in our words; the
                # SDK's text stays on the cause, for the server's log (#251).
                reason = unavailability_of(refused)
                current.set_attribute("generator.provider_status", refused.code)
                if reason is None:
                    raise
                current.set_attribute("generator.unavailable", reason.value)
                raise GenerationUnavailable(reason) from refused
            finally:
                _ATTEMPTS.reset(token)
                # Requests actually sent, the SDK's retries of a 429 or 503 included.
                current.set_attribute("generator.attempts", attempts[0])
                current.set_attribute("generator.retry_attempts", max(attempts[0] - 1, 0))
            usage = response.usage_metadata or types.GenerateContentResponseUsageMetadata()
            prompt_tokens = usage.prompt_token_count or 0
            cached = min(usage.cached_content_token_count or 0, prompt_tokens)
            # Thinking is billed as output, and `candidates_token_count` leaves it out.
            output = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
            cost = price_usd(
                uncached_input=prompt_tokens - cached, cached_input=cached, output=output
            )
            candidates = response.candidates or []
            finish = candidates[0].finish_reason if candidates else None
            stop_reason = finish.value if finish is not None else None
            generation = Generation(
                text=_reply_text(response),
                input_tokens=prompt_tokens,
                output_tokens=output,
                cached_prefix_hit=cached > 0,
                stop_reason=stop_reason,
                cache_read_input_tokens=cached,
                cache_creation_input_tokens=0,
                cost_usd=cost,
                model=self._model,
            )
            current.set_attribute("generator.input_tokens", generation.input_tokens)
            current.set_attribute("generator.output_tokens", generation.output_tokens)
            current.set_attribute("generator.thoughts_tokens", usage.thoughts_token_count or 0)
            current.set_attribute("generator.cache_read_input_tokens", cached)
            current.set_attribute("generator.cache_creation_input_tokens", 0)
            current.set_attribute("generator.cached_prefix_hit", generation.cached_prefix_hit)
            current.set_attribute("generator.cost_usd", cost)
            current.set_attribute("generator.price_table", PRICE_TABLE)
            # A prefix that was sent and not hit: the first request for it, a
            # prompt under the implicit minimum, or a miss on a long one. The
            # prompt's own size is beside it, to tell which.
            current.set_attribute(
                "generator.prefix_not_cached", bool(cached_prefix) and cached == 0
            )
            current.set_attribute("generator.implicit_cache_min_tokens", IMPLICIT_CACHE_MIN_TOKENS)
            current.set_attribute("generator.stop_reason", stop_reason or "")
            return generation
