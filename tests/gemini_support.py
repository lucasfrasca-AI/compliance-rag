"""Gemini on Vertex as the SDK itself speaks it, with no network (#251, #326).

A real `google.genai.Client` (`vertexai=True`, location `us`) over an in-memory
httpx transport. The SDK builds the request, reads the response and raises its
own `ClientError`/`ServerError` through its own code, so what reaches
`GeminiGenerator` is what a live call would deliver, built the way a live call
builds it, and what the adapter puts on the wire can be read back from the
recorded request. Credentials are a placeholder access token, so no ADC lookup
runs; nothing is billed and Vertex is never called.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import httpx
from google import genai
from google.oauth2.credentials import Credentials

from compliance_rag.adapters.gemini_generator import GeminiGenerator, http_options
from compliance_rag.adapters.interfaces import RetrievalAdmin
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION

MODEL = "gemini-3.5-flash"
QUOTA_MESSAGE = (
    "Quota exceeded for aiplatform.googleapis.com/"
    "us_multi_region_online_prediction_requests_per_base_model with base model: "
    "gemini-3.5-flash. Please submit a quota increase request. "
    "https://cloud.google.com/vertex-ai/docs/generative-ai/quotas-genai."
)
QUOTA_BODY = {"error": {"code": 429, "message": QUOTA_MESSAGE, "status": "RESOURCE_EXHAUSTED"}}
"""Google's error envelope for a 429 on a Vertex model endpoint."""
SDK_WORDS = ("RESOURCE_EXHAUSTED", "Quota exceeded", "ClientError", "429 RESOURCE")
"""What the SDK's exception says, and its type's name: none of it may reach a client."""
PLACEHOLDER_TOKEN = "ya29.not-a-real-token"
RETRY_TEST_DELAY_S = 0.001
"""The wait between retries in tests: a millisecond, so a request retried to
exhaustion doesn't slow the suite."""

Handler = Callable[[httpx.Request], httpx.Response]


def answered(
    text: str = '{"answer": "a", "citations": []}',
    *,
    prompt: int = 120,
    cached: int | None = None,
    candidates: int = 40,
    thoughts: int | None = None,
    finish: str = "STOP",
) -> httpx.Response:
    """A 200 as Vertex sends it: `usageMetadata` in the API's own camelCase
    field names, which the SDK parses into `usage_metadata`."""
    usage: dict[str, int] = {"promptTokenCount": prompt, "candidatesTokenCount": candidates}
    if cached is not None:
        usage["cachedContentTokenCount"] = cached
    if thoughts is not None:
        usage["thoughtsTokenCount"] = thoughts
    usage["totalTokenCount"] = prompt + candidates + (thoughts or 0)
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}
            ],
            "usageMetadata": usage,
            "modelVersion": MODEL,
        },
    )


def refused(
    code: int, message: str = QUOTA_MESSAGE, status: str = "RESOURCE_EXHAUSTED"
) -> httpx.Response:
    return httpx.Response(
        code, json={"error": {"code": code, "message": message, "status": status}}
    )


def gemini_client(handler: Handler, requests: list[httpx.Request] | None = None) -> genai.Client:
    """The SDK's client over `handler`, every request recorded in `requests`."""
    seen = requests if requests is not None else []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return genai.Client(
        vertexai=True,
        project="test-project",
        location=BUILD_PHASE_OFFSHORE_REGION,
        credentials=Credentials(token=PLACEHOLDER_TOKEN),
        # The adapter's own timeout, retry policy and attempt counter, with
        # the waits made near-zero (never 0: the SDK reads that as unset).
        http_options=http_options(
            retry_initial_delay=RETRY_TEST_DELAY_S,
            retry_max_delay=RETRY_TEST_DELAY_S,
            retry_jitter=RETRY_TEST_DELAY_S,
            httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(recording)),
        ),
    )


def gemini_generator(
    handler: Handler,
    corpus: RetrievalAdmin,
    requests: list[httpx.Request] | None = None,
    *,
    region: str = BUILD_PHASE_OFFSHORE_REGION,
) -> GeminiGenerator:
    """`GeminiGenerator` as `create` builds it, over that client."""
    client = gemini_client(handler, requests)
    return GeminiGenerator(
        client.aio.models,  # pyright: ignore[reportArgumentType] — a broader contents union
        model=MODEL,
        region=region,
        corpus=corpus,
        sdk_client=client,
    )


def quota_exhausted_generator(
    corpus: RetrievalAdmin,
    requests: list[httpx.Request] | None = None,
    *,
    message: str = QUOTA_MESSAGE,
) -> GeminiGenerator:
    """A generator whose every request is answered 429 `RESOURCE_EXHAUSTED`."""
    return gemini_generator(lambda _: refused(429, message), corpus, requests)


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def _camelled(value: Any) -> Any:  # Any: JSON
    if isinstance(value, dict):
        return {_camel(k): _camelled(v) for k, v in cast("dict[str, Any]", value).items()}
    if isinstance(value, list):
        return [_camelled(v) for v in cast("list[Any]", value)]
    return value


def body_of(request: httpx.Request) -> dict[str, Any]:  # Any: JSON
    """The JSON the SDK sent, as Vertex received it, with every key in
    lowerCamelCase. The installed SDK writes some of them (`any_of`,
    `min_length`, `thinking_level`) in the proto's own snake_case, which
    Vertex's proto3 JSON parser accepts as it does lowerCamelCase; the test
    compares names, not spellings."""
    return _camelled(json.loads(request.content))
