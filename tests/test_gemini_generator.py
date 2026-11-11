"""`GeminiGenerator` (#326, ADR-0057): Gemini 3.5 Flash on Vertex, with no
network. The SDK is the real `google-genai` over an in-memory transport
(`tests/gemini_support.py`), so what the adapter sends is read back from the
recorded request, what it receives is parsed by the SDK's own code, and the
errors are the ones the SDK itself raises."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.fakes import FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.gemini_generator import (
    IMPLICIT_CACHE_MIN_TOKENS,
    PRICE_TABLE,
    REPLY_SCHEMA,
    RETRY_ATTEMPTS,
    RETRY_HTTP_STATUS_CODES,
    RETRY_INITIAL_DELAY_S,
    RETRY_JITTER_S,
    RETRY_MAX_DELAY_S,
    THINKING_HEADROOM_TOKENS,
    GeminiGenerator,
    http_options,
    price_usd,
    unavailability_of,
)
from compliance_rag.adapters.generation_residency import GenerationResidencyError
from compliance_rag.adapters.interfaces import Generation, GenerationUnavailable, Unavailability
from compliance_rag.assessment.draft import (
    DraftAnswer,
    DraftCitation,
    MalformedGeneration,
    parse_draft,
    parse_repair,
)
from compliance_rag.assessment.pipeline import LIBRARY_QUESTION
from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION, REGION, Settings
from tests.contracts.test_interfaces import assert_generator_contract
from tests.gemini_support import (
    MODEL,
    PLACEHOLDER_TOKEN,
    QUOTA_BODY,
    SDK_WORDS,
    Handler,
    answered,
    body_of,
    gemini_generator,
    quota_exhausted_generator,
    refused,
)
from tests.test_prompt_injection import (
    AS_OF,
    MALFORMED_REPLIES,
    QUESTION,
    TENANT,
    pipeline_over,
)

PREFIX = (
    "<tenant_policies boundary=x>\n[x clause acme/p:v1:1]\nPolicy.\n[x end]\n</tenant_policies>"
)

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


def _corpus(*chunks: Chunk) -> FakeRetrievalStore:
    return FakeRetrievalStore(chunks, ledger=FakeLedgerStore())


def _ok_corpus() -> FakeRetrievalStore:
    return _corpus(LAW, SYNTHETIC)


async def _generate(
    response: httpx.Response,
    prefix: str | None = PREFIX,
    requests: list[httpx.Request] | None = None,
    max_tokens: int = 64,
) -> Generation:
    generator = gemini_generator(lambda _: response, _ok_corpus(), requests)
    return await generator.generate(
        system="sys", cached_prefix=prefix, user="q", max_tokens=max_tokens
    )


def _generate_span(exporter: InMemorySpanExporter) -> dict[str, Any]:  # Any: span attributes
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "generator.generate"]
    return dict(span.attributes or {})


# ---- the Generator contract -------------------------------------------------------------


async def test_the_generator_contract_holds_against_the_gemini_adapter() -> None:
    warm = gemini_generator(lambda _: answered(cached=5000), _ok_corpus())
    generation = await assert_generator_contract(warm)
    assert generation.cached_prefix_hit and generation.model == MODEL
    cold = await assert_generator_contract(gemini_generator(lambda _: answered(), _ok_corpus()))
    assert not cold.cached_prefix_hit


# ---- what goes on the wire ---------------------------------------------------------------


async def test_the_request_goes_to_the_us_multi_region_endpoint_for_the_configured_model() -> None:
    requests: list[httpx.Request] = []
    await _generate(answered(), requests=requests)
    (request,) = requests
    assert request.url.host == "aiplatform.us.rep.googleapis.com"
    assert request.url.path.endswith(
        f"/locations/us/publishers/google/models/{MODEL}:generateContent"
    )
    assert request.headers["authorization"] == f"Bearer {PLACEHOLDER_TOKEN}"


async def test_the_system_prompt_is_the_system_instruction_and_the_prefix_leads_the_user_turn() -> (
    None
):
    """The bytes that repeat across a tenant's requests come first — the system
    instruction, then the prefix — so Vertex's implicit cache can match them;
    the per-question text follows."""
    requests: list[httpx.Request] = []
    await _generate(answered(), requests=requests)
    body = body_of(requests[0])
    assert body["systemInstruction"]["parts"] == [{"text": "sys"}]
    assert body["contents"] == [{"role": "user", "parts": [{"text": PREFIX}, {"text": "q"}]}]


async def test_without_policies_the_user_turn_is_the_question_alone() -> None:
    requests: list[httpx.Request] = []
    await _generate(answered(), prefix=None, requests=requests)
    assert body_of(requests[0])["contents"] == [{"role": "user", "parts": [{"text": "q"}]}]


async def test_the_same_prefix_is_the_same_leading_bytes_whatever_the_question() -> None:
    """The point of the layout: two questions for one tenant differ only after
    the shared system instruction and prefix."""
    requests: list[httpx.Request] = []
    generator = gemini_generator(lambda _: answered(), _ok_corpus(), requests)
    for question in ("first question", "another, longer question"):
        await generator.generate(system="sys", cached_prefix=PREFIX, user=question, max_tokens=64)
    first, second = (body_of(r) for r in requests)
    assert first["systemInstruction"] == second["systemInstruction"]
    assert first["contents"][0]["parts"][0] == second["contents"][0]["parts"][0]
    assert first["contents"] != second["contents"]


async def test_the_reply_is_requested_as_json_with_a_schema() -> None:
    requests: list[httpx.Request] = []
    await _generate(answered(), requests=requests)
    config = body_of(requests[0])["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    schema = config["responseSchema"]
    shapes = schema["anyOf"]
    assert [sorted(s["required"]) for s in shapes] == [
        ["answer", "citations"],
        ["clause_id", "quote"],
    ]
    assert all(s["type"] == "OBJECT" for s in shapes)


async def test_thinking_is_held_low_and_the_reply_budget_is_not_spent_on_it() -> None:
    requests: list[httpx.Request] = []
    await _generate(answered(), requests=requests, max_tokens=1024)
    config = body_of(requests[0])["generationConfig"]
    assert config["thinkingConfig"] == {"thinkingLevel": "LOW"}
    assert config["maxOutputTokens"] == 1024 + THINKING_HEADROOM_TOKENS


async def test_no_tools_are_offered_and_nothing_secret_is_sent() -> None:
    requests: list[httpx.Request] = []
    await _generate(answered(), requests=requests)
    body = body_of(requests[0])
    assert "tools" not in body and "cachedContent" not in body
    assert PLACEHOLDER_TOKEN not in json.dumps(body)


# ---- the reply schema is the draft's ----------------------------------------------------


def test_the_reply_schema_names_exactly_the_fields_the_draft_models_require() -> None:
    """The schema helps the model and is never trusted (`draft.py` validates
    strictly), but it must describe the same shapes: if a draft model gains a
    field, this fails until the schema does."""
    answer, repair = REPLY_SCHEMA.any_of or []
    assert set(answer.properties or {}) == set(DraftAnswer.model_fields)
    assert set(answer.required or []) == set(DraftAnswer.model_fields)
    assert set(repair.properties or {}) == set(DraftCitation.model_fields)
    assert set(repair.required or []) == set(DraftCitation.model_fields)
    citation = (answer.properties or {})["citations"].items
    assert citation is not None and set(citation.properties or {}) == set(
        DraftCitation.model_fields
    )


def test_what_the_schema_describes_parses_and_what_it_does_not_is_still_refused() -> None:
    answer = '{"answer": "a", "citations": [{"clause_id": "acme/p:v1:1", "quote": "q"}]}'
    assert parse_draft(answer).citations[0].quote == "q"
    assert parse_repair('{"clause_id": "acme/p:v1:1", "quote": "q"}').quote == "q"
    # The schema is a request, not a guarantee: a reply that breaks it is refused whole.
    with pytest.raises(MalformedGeneration):
        parse_draft('{"clause_id": "acme/p:v1:1", "quote": "q"}')
    with pytest.raises(MalformedGeneration):
        parse_draft('{"answer": "a", "citations": [{"clause_id": "x y", "quote": "q"}]}')


# ---- the cached prefix: implicit caching, read from the response ------------------------


async def test_a_cache_hit_is_read_from_the_response_not_assumed() -> None:
    hit = await _generate(answered(prompt=5120, cached=5000))
    miss = await _generate(answered(prompt=5120))
    zero = await _generate(answered(prompt=5120, cached=0))
    assert hit.cached_prefix_hit and hit.cache_read_input_tokens == 5000
    assert not miss.cached_prefix_hit and miss.cache_read_input_tokens == 0
    assert not zero.cached_prefix_hit
    # input tokens are everything billed, the cached ones included
    assert hit.input_tokens == miss.input_tokens == 5120
    # implicit caching has no write
    assert hit.cache_creation_input_tokens == miss.cache_creation_input_tokens == 0


async def test_a_cached_count_above_the_prompt_is_clamped_not_trusted() -> None:
    generation = await _generate(answered(prompt=100, cached=5000))
    assert generation.cache_read_input_tokens == 100
    assert generation.cost_usd >= 0


async def test_cost_comes_from_the_real_token_counts_with_thinking_billed_as_output() -> None:
    hit = await _generate(answered(prompt=5120, cached=5000, candidates=40, thoughts=300))
    # `us` endpoint: $1.65 in, $0.165 cached in, $9.90 out per 1M; thinking is output.
    expected = (120 * 1.65 + 5000 * 0.165 + 340 * 9.90) / 1_000_000
    assert abs(hit.cost_usd - expected) < 1e-12
    assert hit.output_tokens == 340
    assert hit.cost_usd == price_usd(uncached_input=120, cached_input=5000, output=340)
    miss = await _generate(answered(prompt=5120, candidates=40, thoughts=300))
    assert miss.cost_usd > hit.cost_usd


async def test_missing_usage_metadata_is_zeros_not_a_crash() -> None:
    response = httpx.Response(
        200,
        json={"candidates": [{"content": {"role": "model", "parts": [{"text": "{}"}]}}]},
    )
    generation = await _generate(response)
    assert (generation.input_tokens, generation.output_tokens, generation.cost_usd) == (0, 0, 0)


async def test_the_span_carries_tokens_cache_and_cost_but_no_prompt_text(
    span_exporter: InMemorySpanExporter,
) -> None:
    await _generate(answered(prompt=5120, cached=5000, thoughts=10))
    attributes = _generate_span(span_exporter)
    assert attributes["generator.backend"] == "gemini"
    assert attributes["generator.model"] == MODEL
    assert attributes["generator.region"] == "us"
    assert attributes["generator.cache_mode"] == "implicit"
    assert attributes["generator.cached_prefix_hit"] is True
    assert attributes["generator.cache_read_input_tokens"] == 5000
    assert attributes["generator.cache_creation_input_tokens"] == 0
    assert attributes["generator.input_tokens"] == 5120
    assert attributes["generator.output_tokens"] == 50
    assert attributes["generator.thoughts_tokens"] == 10
    cost = attributes["generator.cost_usd"]
    assert isinstance(cost, float) and cost > 0
    assert attributes["generator.prefix_not_cached"] is False
    assert (
        attributes["generator.price_table"]
        == PRICE_TABLE
        == "gemini-3.5-flash-vertex-us-2026-11-11"
    )
    assert attributes["generator.stop_reason"] == "STOP"
    assert attributes["generator.implicit_cache_min_tokens"] == IMPLICIT_CACHE_MIN_TOKENS
    # Prompt text never reaches the span: neither the system text, the
    # cached policy prefix nor the user turn.
    rendered = " ".join(str(v) for v in attributes.values())
    assert "Policy." not in rendered and "sys" not in rendered.split()


async def test_a_prefix_that_was_sent_and_not_hit_is_flagged(
    span_exporter: InMemorySpanExporter,
) -> None:
    """The first request for a prefix, or one under the implicit minimum, is
    not cached and says nothing. The span must, or D-11 looks healthy while
    every request pays full price."""
    generation = await _generate(answered())
    assert not generation.cached_prefix_hit
    assert _generate_span(span_exporter)["generator.prefix_not_cached"] is True


async def test_no_prefix_sent_is_not_a_prefix_missed(span_exporter: InMemorySpanExporter) -> None:
    await _generate(answered(), prefix=None)
    attributes = _generate_span(span_exporter)
    assert attributes["generator.cached_prefix_sent"] is False
    assert attributes["generator.prefix_not_cached"] is False


# ---- the reply ---------------------------------------------------------------------------


async def test_the_reply_is_the_candidates_text_and_thinking_is_not_part_of_it() -> None:
    response = httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "I should quote the clause.", "thought": True},
                            {"text": '{"answer": "a", '},
                            {"text": '"citations": []}'},
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        },
    )
    generation = await _generate(response)
    assert generation.text == '{"answer": "a", "citations": []}'
    assert parse_draft(generation.text).answer == "a"


async def test_a_reply_cut_off_says_so_and_is_refused_by_the_draft_parser() -> None:
    generation = await _generate(answered('{"answer": "a", "citat', finish="MAX_TOKENS"))
    assert generation.stop_reason == "MAX_TOKENS"
    with pytest.raises(MalformedGeneration):
        parse_draft(generation.text)


async def test_a_blocked_prompt_is_an_empty_reply_the_draft_parser_refuses() -> None:
    blocked = httpx.Response(
        200,
        json={
            "promptFeedback": {"blockReason": "SAFETY"},
            "usageMetadata": {"promptTokenCount": 10},
        },
    )
    generation = await _generate(blocked)
    assert generation.text == "" and generation.stop_reason is None
    with pytest.raises(MalformedGeneration):
        parse_draft(generation.text)


# ---- errors, in our words ----------------------------------------------------------------


async def test_vertex_refusing_for_quota_is_generation_unavailable_in_our_words(
    span_exporter: InMemorySpanExporter,
) -> None:
    """The SDK's own client, answered 429 `RESOURCE_EXHAUSTED` as Vertex
    answers while the quota is zero (#251): the SDK raises `ClientError`, and
    the adapter raises `GenerationUnavailable(QUOTA)` with none of its words.
    The SDK's exception is kept as the cause, for the server's log."""
    requests: list[httpx.Request] = []
    generator = quota_exhausted_generator(_ok_corpus(), requests)
    with pytest.raises(GenerationUnavailable) as raised:
        await generator.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64)
    assert raised.value.reason is Unavailability.QUOTA
    cause = raised.value.__cause__
    assert isinstance(cause, genai_errors.ClientError) and cause.code == 429
    assert cause.details == QUOTA_BODY  # pyright: ignore[reportUnknownMemberType] — as the SDK parsed it
    assert not any(word in str(raised.value) for word in SDK_WORDS)
    assert len(requests) == RETRY_ATTEMPTS  # a 429 is retried before it is quota (#329)
    assert all(r.url.path.endswith(f"{MODEL}:generateContent") for r in requests)
    attributes = _generate_span(span_exporter)
    assert attributes["generator.unavailable"] == "quota"
    assert attributes["generator.provider_status"] == 429


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (refused(429, "x", "RESOURCE_EXHAUSTED"), Unavailability.QUOTA),
        (refused(429, "x", "SOMETHING_ELSE"), Unavailability.QUOTA),
        (refused(403, "Permission denied on resource", "PERMISSION_DENIED"), Unavailability.ACCESS),
        (refused(403, "x", "SOMETHING_ELSE"), Unavailability.ACCESS),
        (
            refused(401, "Request had invalid authentication", "UNAUTHENTICATED"),
            Unavailability.ACCESS,
        ),
    ],
    ids=["quota", "429-any-status", "permission-denied", "403-any-status", "unauthenticated"],
)
async def test_quota_and_access_refusals_are_generation_unavailable(
    response: httpx.Response, reason: Unavailability, span_exporter: InMemorySpanExporter
) -> None:
    generator = gemini_generator(lambda _: response, _ok_corpus())
    with pytest.raises(GenerationUnavailable) as raised:
        await generator.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64)
    assert raised.value.reason is reason
    assert isinstance(raised.value.__cause__, genai_errors.APIError)
    assert "Permission" not in str(raised.value) and "authentication" not in str(raised.value)
    assert _generate_span(span_exporter)["generator.unavailable"] == reason.value


@pytest.mark.parametrize(
    "response",
    [
        refused(500, "Internal error", "INTERNAL"),
        refused(400, "Request contains an invalid argument", "INVALID_ARGUMENT"),
        refused(404, "Publisher Model was not found", "NOT_FOUND"),
    ],
    ids=["500", "400", "404"],
)
async def test_anything_else_is_raised_as_it_is_and_is_not_marked_unavailable(
    response: httpx.Response, span_exporter: InMemorySpanExporter
) -> None:
    """Not quota, not access: the API reports it as `internal`, so it must not
    be dressed up as one of our two reasons."""
    generator = gemini_generator(lambda _: response, _ok_corpus())
    with pytest.raises(genai_errors.APIError) as raised:
        await generator.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64)
    assert not isinstance(raised.value, GenerationUnavailable)
    attributes = _generate_span(span_exporter)
    assert "generator.unavailable" not in attributes
    assert attributes["generator.provider_status"] == raised.value.code


def test_unavailability_of_reads_the_code_or_the_status() -> None:
    assert unavailability_of(genai_errors.ClientError(429, {})) is Unavailability.QUOTA
    assert (
        unavailability_of(
            genai_errors.ClientError(400, {"error": {"status": "RESOURCE_EXHAUSTED"}})
        )
        is Unavailability.QUOTA
    )
    assert unavailability_of(genai_errors.ClientError(403, {})) is Unavailability.ACCESS
    assert unavailability_of(genai_errors.ServerError(500, {})) is None


# ---- a 429 or 503 is retried before quota is reported (#329) -----------------------------


def _sequence(*responses: httpx.Response) -> Handler:
    """Answers the requests in order, the last answer repeating."""
    remaining = list(responses)
    return lambda _: remaining.pop(0) if len(remaining) > 1 else remaining[0]


async def test_a_429_answered_twice_then_a_200_succeeds_and_the_span_shows_the_retries(
    span_exporter: InMemorySpanExporter,
) -> None:
    requests: list[httpx.Request] = []
    generator = gemini_generator(
        _sequence(refused(429), refused(429), answered()), _ok_corpus(), requests
    )
    generation = await generator.generate(
        system="sys", cached_prefix=PREFIX, user="q", max_tokens=64
    )
    assert generation.text == '{"answer": "a", "citations": []}'
    assert len(requests) == 3
    assert len({r.content for r in requests}) == 1  # the same request, sent again
    attributes = _generate_span(span_exporter)
    assert attributes["generator.attempts"] == 3
    assert attributes["generator.retry_attempts"] == 2
    assert "generator.unavailable" not in attributes


async def test_a_request_that_succeeds_first_time_records_one_attempt_and_no_retries(
    span_exporter: InMemorySpanExporter,
) -> None:
    generator = gemini_generator(lambda _: answered(), _ok_corpus())
    await generator.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64)
    attributes = _generate_span(span_exporter)
    assert attributes["generator.attempts"] == 1
    assert attributes["generator.retry_attempts"] == 0


async def test_a_429_on_every_attempt_is_quota_after_exactly_the_configured_attempts(
    span_exporter: InMemorySpanExporter,
) -> None:
    requests: list[httpx.Request] = []
    generator = quota_exhausted_generator(_ok_corpus(), requests)
    with pytest.raises(GenerationUnavailable) as raised:
        await generator.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64)
    assert raised.value.reason is Unavailability.QUOTA
    assert len(requests) == RETRY_ATTEMPTS
    attributes = _generate_span(span_exporter)
    assert attributes["generator.attempts"] == RETRY_ATTEMPTS
    assert attributes["generator.retry_attempts"] == RETRY_ATTEMPTS - 1
    assert attributes["generator.unavailable"] == "quota"


async def test_a_503_is_retried_and_is_not_quota_when_it_never_clears(
    span_exporter: InMemorySpanExporter,
) -> None:
    requests: list[httpx.Request] = []
    generator = gemini_generator(
        lambda _: refused(503, "The service is unavailable", "UNAVAILABLE"),
        _ok_corpus(),
        requests,
    )
    with pytest.raises(genai_errors.ServerError) as raised:
        await generator.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64)
    assert raised.value.code == 503
    assert len(requests) == RETRY_ATTEMPTS
    attributes = _generate_span(span_exporter)
    assert attributes["generator.retry_attempts"] == RETRY_ATTEMPTS - 1
    assert "generator.unavailable" not in attributes


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (refused(400, "bad", "INVALID_ARGUMENT"), genai_errors.ClientError),
        (refused(401, "bad", "UNAUTHENTICATED"), GenerationUnavailable),
        (refused(403, "bad", "PERMISSION_DENIED"), GenerationUnavailable),
        (refused(404, "bad", "NOT_FOUND"), genai_errors.ClientError),
        (refused(500, "bad", "INTERNAL"), genai_errors.ServerError),
    ],
    ids=["400", "401", "403", "404", "500"],
)
async def test_nothing_but_a_429_or_a_503_is_retried(
    response: httpx.Response, outcome: type[Exception], span_exporter: InMemorySpanExporter
) -> None:
    requests: list[httpx.Request] = []
    generator = gemini_generator(lambda _: response, _ok_corpus(), requests)
    with pytest.raises(outcome):
        await generator.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64)
    assert len(requests) == 1
    attributes = _generate_span(span_exporter)
    assert attributes["generator.attempts"] == 1
    assert attributes["generator.retry_attempts"] == 0


async def test_concurrent_requests_count_their_own_attempts(
    span_exporter: InMemorySpanExporter,
) -> None:
    """The count is per request: a neighbour's retries are not this span's."""
    flaky = gemini_generator(_sequence(refused(429), answered()), _ok_corpus())
    steady = gemini_generator(lambda _: answered(), _ok_corpus())
    await asyncio.gather(
        flaky.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64),
        steady.generate(system="sys", cached_prefix=PREFIX, user="q", max_tokens=64),
    )
    counts = sorted(
        int((s.attributes or {})["generator.attempts"])  # pyright: ignore[reportArgumentType]
        for s in span_exporter.get_finished_spans()
        if s.name == "generator.generate"
    )
    assert counts == [1, 2]


def test_the_retry_settings_are_bounded_to_about_a_minute_in_total() -> None:
    retry = http_options().retry_options
    assert retry is not None
    assert retry.attempts == RETRY_ATTEMPTS
    assert retry.http_status_codes == list(RETRY_HTTP_STATUS_CODES) == [429, 503]
    assert (retry.initial_delay, retry.max_delay, retry.jitter) == (
        RETRY_INITIAL_DELAY_S,
        RETRY_MAX_DELAY_S,
        RETRY_JITTER_S,
    )
    # the SDK waits initial * base**n plus up to `jitter`, capped at `max_delay`, between attempts
    waits = [
        min(RETRY_INITIAL_DELAY_S * 2**n + RETRY_JITTER_S, RETRY_MAX_DELAY_S)
        for n in range(RETRY_ATTEMPTS - 1)
    ]
    assert 30 <= sum(waits) <= 60


def test_the_delays_are_overridable() -> None:
    retry = http_options(
        retry_initial_delay=0.001, retry_max_delay=0.002, retry_jitter=0.001
    ).retry_options
    assert retry is not None and retry.initial_delay == 0.001 and retry.attempts == RETRY_ATTEMPTS


async def test_create_builds_the_client_with_the_adapters_own_http_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _patch_client(monkeypatch)
    await GeminiGenerator.create(_settings(), corpus=_ok_corpus())
    options = built["http_options"]
    expected = http_options()
    assert options.retry_options == expected.retry_options
    assert options.timeout == expected.timeout
    assert options.async_client_args  # the attempt counter rides on the real client


# ---- residency (ADR-0023, ADR-0032): `us` is offshore, as it was for Claude --------------


def _settings(region: str = "us", model: str = MODEL) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
        gcp_project_id="p",
        gemini_model=model,
        gemini_region=region,
    )


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:  # Any: constructor kwargs
    built: dict[str, Any] = {}  # Any: constructor kwargs

    class Client:
        def __init__(self, **kwargs: Any) -> None:  # Any: constructor kwargs
            built.update(kwargs)

            class Aio:
                models = None

            self.aio = Aio()

    from google import genai

    monkeypatch.setattr(genai, "Client", Client)
    return built


async def test_the_us_endpoint_is_allowed_while_the_corpus_is_law_and_synthetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _patch_client(monkeypatch)
    await GeminiGenerator.create(_settings(), corpus=_ok_corpus())
    assert built["vertexai"] is True
    assert (built["project"], built["location"]) == ("p", "us")


async def test_the_us_endpoint_is_refused_once_any_real_customer_data_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0023's gate: one real (unmarked) tenant chunk closes offshore
    generation, and no client is ever built."""
    built = _patch_client(monkeypatch)
    with pytest.raises(GenerationResidencyError, match="real"):
        await GeminiGenerator.create(_settings(), corpus=_corpus(LAW, SYNTHETIC, REAL))
    assert built == {}


async def test_sydney_generation_is_allowed_with_real_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate is about offshore generation, not real data as such."""
    built = _patch_client(monkeypatch)
    await GeminiGenerator.create(_settings(REGION), corpus=_corpus(LAW, REAL))
    assert built["location"] == REGION


@pytest.mark.parametrize(
    "region",
    ["global", "US", " us", "asia-southeast1", "us-east5", "eu", "europe-west1", "us-central1"],
)
def test_the_global_endpoint_and_every_other_region_are_refused(region: str) -> None:
    with pytest.raises(ValueError, match="never `global`"):
        _settings(region)
    with pytest.raises(GenerationResidencyError):
        gemini_generator(lambda _: answered(), _corpus(), region=region)


async def test_real_data_written_after_startup_stops_the_next_offshore_request() -> None:
    """Startup isn't the only check: data stored later — by another process,
    or a path that skips CorpusLifecycle — closes the gate before the next
    request leaves Australia, and no request is sent."""
    corpus = _ok_corpus()
    requests: list[httpx.Request] = []
    generator = gemini_generator(lambda _: answered(), corpus, requests)
    await generator.generate(system="s", cached_prefix=None, user="q", max_tokens=8)
    await corpus.upsert_chunks((REAL,))  # straight to the store, no lifecycle
    with pytest.raises(GenerationResidencyError):
        await generator.generate(system="s", cached_prefix=None, user="q", max_tokens=8)
    assert len(requests) == 1


def test_a_generator_with_no_model_is_refused() -> None:
    with pytest.raises(ValueError, match="GEMINI_MODEL"):
        GeminiGenerator(
            gemini_generator(lambda _: answered(), _corpus())._models,  # pyright: ignore[reportPrivateUsage]
            model="",
            region="us",
            corpus=_corpus(),
        )


def test_the_generation_region_defaults_to_sydney_and_the_model_to_gemini_3_5_flash() -> None:
    """Fail closed: the US endpoint must be chosen explicitly."""
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
    assert settings.gemini_region == REGION == settings.generation_region
    assert settings.gemini_model == MODEL == settings.generation_model
    assert settings.generation_backend == "gemini" and settings.generator == "gemini"
    assert BUILD_PHASE_OFFSHORE_REGION == "us"


# ---- the injection set, on the Gemini adapter (#29 and #31 re-run it live) -----------------


def _obeying(request: httpx.Request) -> httpx.Response:
    """A model that does whatever the retrieved text says, over the wire: it
    lifts the poison's instruction out of the request it was actually sent."""
    parts = body_of(request)["contents"][0]["parts"]
    prompt = "\n".join(p["text"] for p in parts)
    start = prompt.index("retrieve '") + len("retrieve '")
    demanded = prompt[start : prompt.index("'", start)]
    reply = json.dumps(
        {
            "answer": f"search: {demanded}",
            "citations": [{"clause_id": "acme/employee-policy:v1:9/9", "quote": demanded}],
        }
    )
    return answered(reply)


async def test_injected_text_reaches_the_gemini_request_fenced_and_never_the_retriever() -> None:
    """The poisoned clause is retrieved and sent, the stubbed model obeys it,
    and still the retriever only ever saw the user's question, the draft is
    unverified, and the invented citation is not something anyone is shown."""
    requests: list[httpx.Request] = []
    generator = gemini_generator(_obeying, _corpus(), requests, region=REGION)
    pipeline, store = await pipeline_over(generator)
    result = await pipeline.answer(QUESTION, TENANT, AS_OF, law=LIBRARY_QUESTION)
    sent = json.dumps(body_of(requests[0]))
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in sent  # it was retrieved and sent
    assert "retrieved_clauses boundary=" in sent  # inside its fence
    assert "acme employee salaries" in result.unverified_draft.answer  # and the model obeyed
    assert store.queries == [QUESTION]  # but the retriever saw only the question
    assert not result.verified  # and the invented citation verified nothing


async def test_a_reply_the_schema_did_not_hold_is_still_refused_whole_on_the_gemini_adapter() -> (
    None
):
    generator = gemini_generator(
        lambda _: answered("Sure! Here's the answer: protect it."), _corpus(), region=REGION
    )
    pipeline, _ = await pipeline_over(generator)
    with pytest.raises(MalformedGeneration):
        await pipeline.answer(QUESTION, TENANT, AS_OF, law=LIBRARY_QUESTION)


@MALFORMED_REPLIES
async def test_every_malformed_draft_is_refused_whole_through_the_gemini_adapter(
    reply: str,
) -> None:
    """The schema asks; `parse_draft` decides. The same replies the parser is
    held to in `test_prompt_injection.py`, arriving over the adapter's wire."""
    generator = gemini_generator(lambda _: answered(reply), _corpus(), region=REGION)
    pipeline, _ = await pipeline_over(generator)
    with pytest.raises(MalformedGeneration):
        await pipeline.answer(QUESTION, TENANT, AS_OF, law=LIBRARY_QUESTION)
