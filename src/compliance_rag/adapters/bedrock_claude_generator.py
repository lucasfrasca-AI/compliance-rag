"""`Generator` over Claude Sonnet 5 on Amazon Bedrock's Australian geographic
inference profile (#322, ADR-0056): `au.anthropic.claude-sonnet-5`, called from
`ap-southeast-2` (Sydney). The profile's only destination Regions are Sydney and
Melbourne, so the model processes every request in Australia, and the offshore
gates of ADR-0023 and ADR-0032 do not fire: `validate_bedrock_target` refuses
any other profile prefix or source Region, so nothing offshore can be built.

It mirrors `VertexClaudeGenerator` and speaks the same Messages API through the
`anthropic` SDK's Bedrock client:

- **The tenant policy corpus is an explicit cache checkpoint** (D-11): the last
  system block carries `cache_control`, so every request for the same tenant
  re-reads it at the cached rate. Bedrock's minimum is 1,024 tokens per
  checkpoint and the default 5-minute TTL is enough. Whether the cache was hit
  comes from the response's own usage counts, never assumed, and a prefix below
  the minimum is flagged on the span (`generator.prefix_not_cached`).
- **Cost** is computed from the Bedrock AU price table below and named on the
  span.
- **The span** carries what the Vertex adapter's does, with
  `generator.region=ap-southeast-2`, `generator.inference_profile` and
  `generator.backend=bedrock`. Prompts are opaque strings here: no prompt or
  completion text goes onto it.
- **Credentials** are short-lived and come from a `CredentialsProvider`
  (`adapters/aws_credentials.py`): tests inject a fake. The SDK's Bedrock client
  takes its credentials at construction, so a client is rebuilt whenever the
  provider hands back different ones (on Cloud Run, hourly), over one shared
  HTTP connection pool.

Error mapping (nothing the provider said ever leaves this module; the SDK's
exception is the `__cause__`, for the server's log):

- throttling, a service quota (HTTP 429 `ThrottlingException`, or
  `ServiceQuotaExceededException`) is `GenerationUnavailable(QUOTA)`;
- access denied, the model not enabled for the account, the first-time-use form
  or the Marketplace subscription not completed, or credentials AWS refuses, is
  `GenerationUnavailable(ACCESS)`: shown to the member as unavailable, not as
  an internal error;
- anything else is raised as it is, and the API reports it as `internal`.

`_MessagesLike` names the one call this module makes, so tests drive it with a
fake that returns real `anthropic.types.Message` objects.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Protocol, cast

import anthropic
import httpx
from anthropic.types import Message, MessageParam, TextBlock, TextBlockParam

from compliance_rag import telemetry
from compliance_rag.adapters.aws_credentials import (
    AwsCredentials,
    CredentialsProvider,
    CredentialsUnavailable,
    credentials_for,
)
from compliance_rag.adapters.generation_residency import validate_bedrock_target
from compliance_rag.adapters.interfaces import (
    Generation,
    GenerationUnavailable,
    Generator,
    Unavailability,
)
from compliance_rag.settings import Settings

# `claude-sonnet-5` per-token prices, USD, on Bedrock's Australian geographic
# inference profile: input $2.20, output $11.00, 5-minute cache write $2.75,
# cache read $0.22 per 1M tokens (ADR-0056). Constants: re-check when the model
# or the profile changes, and against the first invoice.
_INPUT_USD = 2.20 / 1_000_000
_OUTPUT_USD = 11.00 / 1_000_000
_CACHE_WRITE_USD = 2.75 / 1_000_000  # 5-minute cache checkpoint write
_CACHE_READ_USD = 0.22 / 1_000_000  # cache hit
PRICE_TABLE = "claude-sonnet-5-bedrock-au-geo-2026-11-10"
"""Names the rates above on every span, so a cost figure carries its own
provenance."""


def price_usd(*, uncached_input: int, cache_write: int, cache_read: int, output: int) -> float:
    return (
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


_ACCESS_PHRASES = (
    "use case",  # "Model use case details have not been submitted for this account"
    "model access",
    "access to the model",
    "marketplace",
    "payment instrument",
    "not enabled",
    "marked by provider as legacy",
)
"""What Bedrock's own message says when the account, not the load, is the
problem and it answers with a status other than 403. Matched against the
provider's text, which is never kept or repeated."""
_ERROR_TYPE = re.compile(r"[A-Za-z]{1,64}")


def _error_type(error: anthropic.APIStatusError) -> str:
    """Bedrock's exception name, from the `x-amzn-ErrorType` header
    (`ThrottlingException:http://internal.amazon.com/coral/…`), if it is a
    plain name."""
    raw = error.response.headers.get("x-amzn-errortype", "").split(":", 1)[0].strip()
    return raw if _ERROR_TYPE.fullmatch(raw) else ""


def _says_access(error: anthropic.APIStatusError) -> bool:
    body = error.body
    message = cast("Mapping[str, object]", body).get("message") if isinstance(body, dict) else None
    text = str(message).lower() if message is not None else ""
    return any(phrase in text for phrase in _ACCESS_PHRASES)


def unavailability_of(error: anthropic.APIStatusError) -> Unavailability | None:
    """Which of our reasons a Bedrock refusal is, or None when it is something
    else (and so an internal error)."""
    kind = _error_type(error)
    if isinstance(error, anthropic.RateLimitError) or kind == "ServiceQuotaExceededException":
        return Unavailability.QUOTA
    if isinstance(error, anthropic.PermissionDeniedError | anthropic.AuthenticationError):
        return Unavailability.ACCESS
    if kind == "AccessDeniedException":
        return Unavailability.ACCESS
    if isinstance(error, anthropic.NotFoundError | anthropic.BadRequestError) and _says_access(
        error
    ):
        return Unavailability.ACCESS
    return None


class CredentialedMessages:
    """`messages.create` signed with whatever the provider holds now. A client
    is built per distinct set of credentials, over one shared connection pool,
    because the SDK's Bedrock client takes its credentials at construction."""

    def __init__(
        self,
        credentials: CredentialsProvider,
        *,
        region: str,
        http_client: httpx.AsyncClient | None = None,
        client_for: Callable[[AwsCredentials], _MessagesLike] | None = None,
        max_retries: int = 2,  # the SDK's own default
    ) -> None:
        self._credentials = credentials
        self._region = region
        self._max_retries = max_retries
        self._http = http_client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0))
        self._client_for = client_for or self._sdk_client
        self._built: tuple[AwsCredentials, _MessagesLike] | None = None

    def _sdk_client(self, credentials: AwsCredentials) -> _MessagesLike:
        from anthropic.lib.bedrock import AsyncAnthropicBedrock  # only the real path needs it

        return AsyncAnthropicBedrock(
            aws_access_key=credentials.access_key_id,
            aws_secret_key=credentials.secret_access_key,
            aws_session_token=credentials.session_token,
            aws_region=self._region,
            max_retries=self._max_retries,
            http_client=self._http,
        ).messages

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: list[TextBlockParam],
        messages: list[MessageParam],
    ) -> Message:
        credentials = await self._credentials.credentials()
        built = self._built
        if built is None or built[0] != credentials:
            built = (credentials, self._client_for(credentials))
            self._built = built
        return await built[1].create(
            model=model, max_tokens=max_tokens, system=system, messages=messages
        )


class BedrockClaudeGenerator(Generator):
    def __init__(
        self,
        messages: _MessagesLike,
        *,
        inference_profile: str,
        region: str,
    ) -> None:
        if not inference_profile:
            raise ValueError("no inference profile configured (BEDROCK_INFERENCE_PROFILE)")
        self._messages = messages
        self._profile = inference_profile
        self._region = validate_bedrock_target(region=region, inference_profile=inference_profile)

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        credentials: CredentialsProvider | None = None,
    ) -> BedrockClaudeGenerator:
        """The one place a real Bedrock client is built. The profile and Region
        are re-validated here as well as in `Settings` (only `au.` called from
        `ap-southeast-2`). `credentials` is injectable; by default `Settings`
        names the source (`AWS_ROLE_ARN` or `AWS_PROFILE`) and a missing one is
        refused. No residency check is asked of the store: nothing here leaves
        Australia."""
        validate_bedrock_target(
            region=settings.bedrock_region, inference_profile=settings.bedrock_inference_profile
        )
        provider = credentials or credentials_for(settings)
        return cls(
            CredentialedMessages(provider, region=settings.bedrock_region),
            inference_profile=settings.bedrock_inference_profile,
            region=settings.bedrock_region,
        )

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
            **{
                "generator.backend": "bedrock",
                "generator.model": self._profile,
                "generator.inference_profile": self._profile,
                "generator.region": self._region,
                "generator.cached_prefix_sent": bool(cached_prefix),
            },
        ) as current:
            try:
                message = await self._messages.create(
                    model=self._profile,
                    max_tokens=max_tokens,
                    system=blocks,
                    messages=[{"role": "user", "content": user}],
                )
            except CredentialsUnavailable as unusable:
                if not unusable.refused:
                    raise
                current.set_attribute("generator.unavailable", Unavailability.ACCESS.value)
                # Our own words, naming the STS error code and nothing else, so
                # a trust policy that doesn't match can be told from an SSO
                # session that has lapsed.
                current.set_attribute("generator.credentials_error", str(unusable))
                raise GenerationUnavailable(Unavailability.ACCESS) from unusable
            except anthropic.APIStatusError as refused:
                # Bedrock's refusal, raised by the SDK after its own retries.
                # Named in our words; the SDK's text stays on the cause, for the
                # server's log. The AWS exception *name* (no message) goes on the
                # span so an operator can tell the cases apart.
                reason = unavailability_of(refused)
                current.set_attribute("generator.provider_status", refused.status_code)
                current.set_attribute("generator.provider_error_type", _error_type(refused))
                if reason is None:
                    raise
                current.set_attribute("generator.unavailable", reason.value)
                raise GenerationUnavailable(reason) from refused
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
                model=self._profile,
            )
            current.set_attribute("generator.input_tokens", generation.input_tokens)
            current.set_attribute("generator.output_tokens", generation.output_tokens)
            current.set_attribute("generator.cache_read_input_tokens", cache_read)
            current.set_attribute("generator.cache_creation_input_tokens", cache_write)
            current.set_attribute("generator.cached_prefix_hit", generation.cached_prefix_hit)
            current.set_attribute("generator.cost_usd", cost)
            current.set_attribute("generator.price_table", PRICE_TABLE)
            # A prefix below the checkpoint minimum (1,024 tokens) is neither
            # written nor read — silently. Flag it, or D-11 would look healthy
            # on the trace while every request paid full price for the prefix.
            current.set_attribute(
                "generator.prefix_not_cached",
                bool(cached_prefix) and not (cache_read or cache_write),
            )
            current.set_attribute("generator.stop_reason", message.stop_reason or "")
            return generation
