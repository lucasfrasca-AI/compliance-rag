"""Vertex's quota error as the SDK itself raises it (#251).

A real `AsyncAnthropicVertex` over an in-memory transport that answers every
request as Vertex does while a model's quota is zero: HTTP 429, status
`RESOURCE_EXHAUSTED`. The SDK reads that response and raises
`anthropic.RateLimitError` through its own `_make_status_error`, so what
reaches `VertexClaudeGenerator` is the exception a live call would raise, built
the way a live call builds it. No network, no credentials (the access token is
a placeholder, so no ADC lookup runs), nothing billed, Vertex never called.
"""

from __future__ import annotations

import httpx
from anthropic.lib.vertex import AsyncAnthropicVertex

from compliance_rag.adapters.interfaces import RetrievalAdmin
from compliance_rag.adapters.vertex_claude_generator import VertexClaudeGenerator
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION

QUOTA_MESSAGE = (
    "Quota exceeded for aiplatform.googleapis.com/"
    "us_multi_region_online_prediction_requests_per_base_model with base model: "
    "anthropic-claude-sonnet-5. Please submit a quota increase request. "
    "https://cloud.google.com/vertex-ai/docs/generative-ai/quotas-genai."
)
QUOTA_BODY = {"error": {"code": 429, "message": QUOTA_MESSAGE, "status": "RESOURCE_EXHAUSTED"}}
"""Google's error envelope for a 429 on a Vertex model endpoint."""
SDK_WORDS = ("RESOURCE_EXHAUSTED", "Quota exceeded", "RateLimitError", "Error code: 429")
"""What the SDK's exception says, and its type's name: none of it may reach a client."""
PLACEHOLDER_TOKEN = "ya29.not-a-real-token"


def quota_exhausted_client(requests: list[httpx.Request] | None = None) -> AsyncAnthropicVertex:
    """The SDK's client, answered 429 `RESOURCE_EXHAUSTED` for every request.
    `max_retries=0`: the SDK retries a 429 twice by default and raises the
    same type when it gives up, so the test skips the back-off, not the path."""
    seen = requests if requests is not None else []

    def vertex(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(429, json=QUOTA_BODY)

    return AsyncAnthropicVertex(
        region=BUILD_PHASE_OFFSHORE_REGION,
        project_id="test-project",
        access_token=PLACEHOLDER_TOKEN,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(vertex)),
    )


def quota_exhausted_generator(
    corpus: RetrievalAdmin, requests: list[httpx.Request] | None = None
) -> VertexClaudeGenerator:
    """`VertexClaudeGenerator` as `create` builds it, over that client."""
    return VertexClaudeGenerator(
        quota_exhausted_client(requests).messages,
        model="claude-sonnet-5",
        region=BUILD_PHASE_OFFSHORE_REGION,
        corpus=corpus,
    )
