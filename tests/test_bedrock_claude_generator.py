"""`BedrockClaudeGenerator` (#322, ADR-0056): Claude Sonnet 5 on Bedrock's
Australian inference profile, with no AWS call. Two kinds of double:

- a fake messages client returning real `anthropic.types.Message` objects, for
  what the adapter does with a response (cache accounting, cost, the span);
- the SDK's own `AsyncAnthropicBedrock` over an in-memory transport
  (`tests/bedrock_support.py`), for what goes on the wire and for the
  exceptions the SDK itself raises, so the error mapping is proved on the
  real thing.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import Message, MessageParam, TextBlock, TextBlockParam, Usage
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.aws_credentials import AwsCredentials, CredentialsUnavailable
from compliance_rag.adapters.bedrock_claude_generator import (
    PRICE_TABLE,
    BedrockClaudeGenerator,
    CredentialedMessages,
    price_usd,
    unavailability_of,
)
from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    TenantTextGate,
    assert_question_allowed,
    assert_upload_allowed,
    is_offshore,
)
from compliance_rag.adapters.interfaces import GenerationUnavailable, Unavailability
from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.settings import Settings
from tests.bedrock_support import (
    PROFILE,
    REGION,
    SESSION_TOKEN,
    FakeCredentials,
    answered,
    bedrock_generator,
    refused,
)
from tests.contracts.test_interfaces import assert_generator_contract

PREFIX = (
    "<tenant_policies boundary=x>\n[x clause acme/p:v1:1]\nPolicy.\n[x end]\n</tenant_policies>"
)
PROVIDER_WORDS = ("ThrottlingException", "Too many requests", "arn:aws", "AKIA", "SECRETWORD")


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


def _generator(messages: FakeMessages) -> BedrockClaudeGenerator:
    return BedrockClaudeGenerator(messages, inference_profile=PROFILE, region=REGION)


async def _generate(messages: FakeMessages, prefix: str | None = PREFIX) -> Any:  # Any: Generation
    return await _generator(messages).generate(
        system="sys", cached_prefix=prefix, user="q", max_tokens=64
    )


# ---- the Generator contract -------------------------------------------------------------


async def test_the_generator_contract_holds_against_the_bedrock_adapter() -> None:
    generation = await assert_generator_contract(_generator(FakeMessages(cache_read=5000)))
    assert generation.cached_prefix_hit and generation.model == PROFILE
    cold = await assert_generator_contract(_generator(FakeMessages()))
    assert not cold.cached_prefix_hit


# ---- the cached prefix -------------------------------------------------------------------


async def test_the_policy_prefix_is_sent_as_one_checkpointed_block() -> None:
    messages = FakeMessages()
    await _generate(messages)
    system = messages.calls[0]["system"]
    assert system == [
        {"type": "text", "text": "sys"},
        {"type": "text", "text": PREFIX, "cache_control": {"type": "ephemeral"}},
    ]
    assert messages.calls[0]["model"] == PROFILE


async def test_without_policies_nothing_is_marked_for_caching() -> None:
    messages = FakeMessages()
    await _generate(messages, prefix=None)
    assert messages.calls[0]["system"] == [{"type": "text", "text": "sys"}]


async def test_a_cache_hit_and_a_write_are_read_from_the_response_not_assumed() -> None:
    hit = await _generate(FakeMessages(cache_read=5000))
    miss = await _generate(FakeMessages(cache_write=5000))
    assert hit.cached_prefix_hit and hit.cache_read_input_tokens == 5000
    assert not miss.cached_prefix_hit and miss.cache_creation_input_tokens == 5000
    assert hit.input_tokens == miss.input_tokens == 5120


async def test_the_cache_checkpoint_goes_over_the_wire_once_and_the_request_is_signed() -> None:
    """Through the SDK's own client: the AU profile is the model id in the path,
    the request goes to Sydney's runtime endpoint, exactly one block carries
    `cache_control`, and it is signed with the session token (SigV4 for the
    `bedrock` service in ap-southeast-2) — not a bearer key."""
    requests: list[httpx.Request] = []
    generator = bedrock_generator(lambda _: answered(cache_write=5000), requests)
    generation = await generator.generate(
        system="sys", cached_prefix=PREFIX, user="q", max_tokens=64
    )
    (request,) = requests
    assert request.url.host == f"bedrock-runtime.{REGION}.amazonaws.com"
    assert request.url.path == f"/model/{PROFILE}/invoke"
    body = json.loads(request.content)
    assert [b.get("cache_control") for b in body["system"]] == [None, {"type": "ephemeral"}]
    assert body["system"][1]["text"] == PREFIX
    assert request.headers["x-amz-security-token"] == SESSION_TOKEN
    assert f"/{REGION}/bedrock/aws4_request" in request.headers["authorization"]
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256")
    assert generation.cache_creation_input_tokens == 5000


# ---- cost --------------------------------------------------------------------------------


async def test_cost_comes_from_the_bedrock_au_price_table() -> None:
    hit = await _generate(FakeMessages(cache_read=5000))
    # Sonnet 5, Bedrock AU: $2.20 in, $0.22 cache read, $11.00 out per 1M.
    expected = (120 * 2.20 + 5000 * 0.22 + 40 * 11.00) / 1_000_000
    assert abs(hit.cost_usd - expected) < 1e-12
    miss = await _generate(FakeMessages(cache_write=5000))
    expected_miss = (120 * 2.20 + 5000 * 2.75 + 40 * 11.00) / 1_000_000  # $2.75 cache write
    assert abs(miss.cost_usd - expected_miss) < 1e-12
    assert miss.cost_usd == price_usd(uncached_input=120, cache_write=5000, cache_read=0, output=40)


def test_the_price_table_is_named_for_bedrock_au() -> None:
    assert PRICE_TABLE == "claude-sonnet-5-bedrock-au-geo-2026-11-10"


# ---- the span ----------------------------------------------------------------------------


async def test_the_span_carries_the_vertex_attributes_plus_region_and_profile(
    span_exporter: InMemorySpanExporter,
) -> None:
    await _generate(FakeMessages(cache_read=5000))
    (generate_span,) = [s for s in span_exporter.get_finished_spans() if s.name.startswith("gen")]
    attributes = dict(generate_span.attributes or {})
    assert attributes["generator.region"] == "ap-southeast-2"
    assert attributes["generator.inference_profile"] == PROFILE
    assert attributes["generator.backend"] == "bedrock"
    assert attributes["generator.model"] == PROFILE
    assert attributes["generator.price_table"] == PRICE_TABLE
    assert attributes["generator.cached_prefix_hit"] is True
    assert attributes["generator.cached_prefix_sent"] is True
    assert attributes["generator.cache_read_input_tokens"] == 5000
    assert attributes["generator.cache_creation_input_tokens"] == 0
    assert attributes["generator.input_tokens"] == 5120
    assert attributes["generator.output_tokens"] == 40
    assert attributes["generator.stop_reason"] == "end_turn"
    assert attributes["generator.prefix_not_cached"] is False
    cost = attributes["generator.cost_usd"]
    assert isinstance(cost, float) and cost > 0
    rendered = " ".join(str(v) for v in attributes.values())
    assert "Policy." not in rendered and "sys" not in rendered.split()


async def test_a_prefix_below_the_checkpoint_minimum_is_flagged(
    span_exporter: InMemorySpanExporter,
) -> None:
    """Under 1,024 tokens Bedrock neither writes nor reads the checkpoint, and
    says nothing. The span must."""
    generation = await _generate(FakeMessages())
    assert not generation.cached_prefix_hit
    (generate_span,) = [s for s in span_exporter.get_finished_spans() if s.name.startswith("gen")]
    assert (generate_span.attributes or {})["generator.prefix_not_cached"] is True


# ---- error mapping, on the SDK's own exceptions ------------------------------------------


QUOTA_CASES = {
    "throttling": refused(429, "Too many requests, please wait", "ThrottlingException"),
    "model not ready": refused(429, "Model is getting ready", "ModelNotReadyException"),
    "service quota": refused(
        400, "Too many tokens per minute: SECRETWORD", "ServiceQuotaExceededException"
    ),
}
ACCESS_CASES = {
    "access denied": refused(
        403,
        "You don't have access to the model with the specified model ID.",
        "AccessDeniedException",
    ),
    "access denied, no header": refused(
        403, "User: arn:aws:sts::1:assumed-role/x is not authorized"
    ),
    "payment instrument": refused(
        403,
        "Model access is denied due to INVALID_PAYMENT_INSTRUMENT: a valid payment instrument "
        "must be provided",
        "AccessDeniedException",
    ),
    "unrecognised credentials": refused(
        401, "bad token AKIAEXAMPLE", "UnrecognizedClientException"
    ),
    "first-time-use form": refused(
        404,
        "Model use case details have not been submitted for this account. Fill out the "
        "Anthropic use case details form before using the model.",
        "ResourceNotFoundException",
    ),
    "model not enabled": refused(
        400, "The requested model is not enabled for this account", "ValidationException"
    ),
}
INTERNAL_CASES = {
    "server error": refused(500, "Internal failure SECRETWORD", "InternalServerException"),
    "bad request": refused(400, "prompt is too long: 300000 tokens", "ValidationException"),
    "no such model": refused(
        404, "The provided model identifier is invalid.", "ResourceNotFoundException"
    ),
}


@pytest.mark.parametrize("name", list(QUOTA_CASES))
async def test_throttling_and_quota_become_generation_unavailable_quota(
    name: str, span_exporter: InMemorySpanExporter
) -> None:
    await _assert_unavailable(QUOTA_CASES[name], Unavailability.QUOTA, span_exporter)


@pytest.mark.parametrize("name", list(ACCESS_CASES))
async def test_access_denied_and_model_not_enabled_become_generation_unavailable_access(
    name: str, span_exporter: InMemorySpanExporter
) -> None:
    await _assert_unavailable(ACCESS_CASES[name], Unavailability.ACCESS, span_exporter)


async def _assert_unavailable(
    response: httpx.Response, reason: Unavailability, span_exporter: InMemorySpanExporter
) -> None:
    generator = bedrock_generator(lambda _: response)
    with pytest.raises(GenerationUnavailable) as raised:
        await generator.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64)
    assert raised.value.reason is reason
    assert isinstance(raised.value.__cause__, anthropic.APIStatusError)  # kept for the log
    text = str(raised.value)
    assert not any(word in text for word in PROVIDER_WORDS) and "Error code" not in text
    (generate_span,) = [s for s in span_exporter.get_finished_spans() if s.name.startswith("gen")]
    assert (generate_span.attributes or {})["generator.unavailable"] == reason.value
    rendered = " ".join(str(v) for v in (generate_span.attributes or {}).values())
    assert "SECRETWORD" not in rendered and "arn:aws" not in rendered


@pytest.mark.parametrize("name", list(INTERNAL_CASES))
async def test_anything_else_is_not_swallowed_as_unavailable(name: str) -> None:
    """Left as the SDK raised it, so the API reports it as `internal`."""
    generator = bedrock_generator(lambda _: INTERNAL_CASES[name])
    with pytest.raises(anthropic.APIStatusError) as raised:
        await generator.generate(system="sys", cached_prefix=None, user="q", max_tokens=64)
    assert not isinstance(raised.value, GenerationUnavailable)
    assert unavailability_of(raised.value) is None


async def test_credentials_the_provider_refuses_are_access_and_a_network_fault_is_not(
    span_exporter: InMemorySpanExporter,
) -> None:
    class Refusing:
        async def credentials(self) -> AwsCredentials:
            raise CredentialsUnavailable("STS refused the web-identity exchange", refused=True)

    class Unreachable:
        async def credentials(self) -> AwsCredentials:
            raise CredentialsUnavailable("STS could not be reached", refused=False)

    def generator(provider: Refusing | Unreachable) -> BedrockClaudeGenerator:
        return BedrockClaudeGenerator(
            CredentialedMessages(provider, region=REGION), inference_profile=PROFILE, region=REGION
        )

    with pytest.raises(GenerationUnavailable) as raised:
        await generator(Refusing()).generate(system="s", cached_prefix=None, user="q", max_tokens=8)
    assert raised.value.reason is Unavailability.ACCESS
    (generate_span,) = [s for s in span_exporter.get_finished_spans() if s.name.startswith("gen")]
    assert (generate_span.attributes or {})["generator.credentials_error"] == (
        "STS refused the web-identity exchange"
    )
    with pytest.raises(CredentialsUnavailable):
        await generator(Unreachable()).generate(
            system="s", cached_prefix=None, user="q", max_tokens=8
        )


# ---- credentials reach the client -------------------------------------------------------


async def test_a_client_is_built_per_set_of_credentials_and_not_per_request() -> None:
    credentials = FakeCredentials("one")
    requests: list[httpx.Request] = []
    generator = bedrock_generator(lambda _: answered(), requests, credentials=credentials)
    for _ in range(3):
        await generator.generate(system="s", cached_prefix=None, user="q", max_tokens=8)
    assert credentials.asks == 3  # asked every time: the provider decides when to renew
    keys = {r.headers["authorization"].split("Credential=")[1].split("/")[0] for r in requests}
    assert keys == {"fake-key-one"}
    credentials.rotate("two")  # the hourly STS renewal
    await generator.generate(system="s", cached_prefix=None, user="q", max_tokens=8)
    assert requests[-1].headers["authorization"].split("Credential=")[1].startswith("fake-key-two")


# ---- residency: Australian, and nothing else can be built --------------------------------


@pytest.mark.parametrize(
    "profile",
    [
        "global.anthropic.claude-sonnet-5",
        "us.anthropic.claude-sonnet-5",
        "apac.anthropic.claude-sonnet-5",
        "eu.anthropic.claude-sonnet-5",
        "anthropic.claude-sonnet-5",
        "AU.anthropic.claude-sonnet-5",
        " au.anthropic.claude-sonnet-5",
    ],
)
def test_only_the_australian_inference_profile_can_be_built(profile: str) -> None:
    with pytest.raises(GenerationResidencyError, match="profile"):
        BedrockClaudeGenerator(FakeMessages(), inference_profile=profile, region=REGION)
    with pytest.raises(ValueError, match="bedrock_inference_profile"):
        _settings(bedrock_inference_profile=profile)


@pytest.mark.parametrize(
    "region", ["us-east-1", "ap-southeast-4", "us-west-2", "ap-southeast-1", ""]
)
def test_only_sydney_can_be_the_source_region(region: str) -> None:
    with pytest.raises(GenerationResidencyError, match="Bedrock region"):
        BedrockClaudeGenerator(FakeMessages(), inference_profile=PROFILE, region=region)
    with pytest.raises(ValueError, match="bedrock_region"):
        _settings(bedrock_region=region)


def test_a_generator_with_no_profile_is_refused() -> None:
    with pytest.raises(ValueError, match="BEDROCK_INFERENCE_PROFILE"):
        BedrockClaudeGenerator(FakeMessages(), inference_profile="", region=REGION)


def _settings(**overrides: Any) -> Settings:  # Any: settings field values
    return Settings(_env_file=None, aws_profile="local", **overrides)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg


def test_the_bedrock_au_backend_is_the_default_and_is_not_offshore() -> None:
    settings = _settings()
    assert settings.generation_backend == "bedrock"
    assert settings.bedrock_region == "ap-southeast-2"
    assert settings.bedrock_inference_profile == "au.anthropic.claude-sonnet-5"
    assert settings.generation_region == "ap-southeast-2"
    assert not is_offshore(settings.generation_region)


def test_the_offshore_gates_do_not_fire_for_the_australian_profile() -> None:
    """A non-tester's question and unregistered tenant text pass; so does a
    real tenant chunk at the door. (ADR-0023, ADR-0032 are about generation
    that leaves Australia; this does not.)"""
    region = _settings().generation_region
    assert_question_allowed(
        generation_region=region, asker="anyone@acme.example", testers=frozenset()
    )
    nothing = SyntheticRegistry.of((), source="nothing registered")
    TenantTextGate(region, nothing).check(
        "acme", (("acme/policy:v1:1", "Real policy text."),), what="clause"
    )
    with pytest.raises(GenerationResidencyError):  # the same text, offshore, is refused
        TenantTextGate("us", nothing).check(
            "acme", (("acme/policy:v1:1", "Real policy text."),), what="clause"
        )
    real = Chunk(
        chunk_id="acme/p:v1:1",
        clause_id="acme/p:v1:1",
        text="real",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    assert_upload_allowed((real,), generation_region=region)


def test_the_vertex_us_path_keeps_its_gate_unchanged() -> None:
    settings = _settings(generation_backend="vertex", vertex_claude_region="us")
    assert settings.generation_region == "us" and is_offshore(settings.generation_region)
    with pytest.raises(GenerationResidencyError):
        assert_question_allowed(
            generation_region="us", asker="anyone@acme.example", testers=frozenset()
        )
    # and ap-southeast-2 is not a Vertex region
    with pytest.raises(ValueError, match="vertex_claude_region"):
        _settings(generation_backend="vertex", vertex_claude_region="ap-southeast-2")


def test_a_long_lived_bedrock_api_key_is_refused() -> None:
    """The SDK would pick it up from the environment by itself and let it
    replace the federated credentials without a word."""
    with pytest.raises(ValueError, match="long-lived"):
        _settings(aws_bearer_token_bedrock="bedrock-api-key-xyz")


def test_create_builds_from_settings_with_injected_credentials() -> None:
    generator = BedrockClaudeGenerator.create(_settings(), credentials=FakeCredentials())
    assert isinstance(generator, BedrockClaudeGenerator)
