"""Bedrock as the SDK's own client sees it, with no network and no AWS call
(#322).

`bedrock_generator` is `BedrockClaudeGenerator` over a real
`AsyncAnthropicBedrock` whose HTTP transport is in memory: the SDK builds the
request, signs it (locally, with the fake credentials below) and parses
whatever `handler` answers, so the exception that reaches the adapter is the
one a live call would raise, built the way a live call builds it. The
credentials are fakes handed in through the provider seam; nothing reads the
environment or a profile.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable

import httpx
from anthropic.types import Message, TextBlock, Usage

from compliance_rag.adapters.aws_credentials import AwsCredentials
from compliance_rag.adapters.bedrock_claude_generator import (
    BedrockClaudeGenerator,
    CredentialedMessages,
)

PROFILE = "au.anthropic.claude-sonnet-5"
REGION = "ap-southeast-2"
SESSION_TOKEN = "fake-session-token-for-tests"


class FakeCredentials:
    """A `CredentialsProvider` that hands out fixed fake credentials and counts
    the asks. `rotate` makes the next ask return different ones."""

    def __init__(self, label: str = "one") -> None:
        self.asks = 0
        self._label = label

    def rotate(self, label: str) -> None:
        self._label = label

    async def credentials(self) -> AwsCredentials:
        self.asks += 1
        return AwsCredentials(
            f"fake-key-{self._label}",
            f"fake-secret-{self._label}",
            SESSION_TOKEN,
            dt.datetime(2030, 1, 1, tzinfo=dt.UTC),
        )


def answered(
    text: str = '{"answer": "a", "citations": []}', *, cache_read: int = 0, cache_write: int = 0
) -> httpx.Response:
    """Bedrock's 200 for an Anthropic `invoke`: the Messages body."""
    message = Message(
        id="msg_1",
        type="message",
        role="assistant",
        model=PROFILE,
        content=[TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        usage=Usage(
            input_tokens=120,
            output_tokens=40,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )
    return httpx.Response(200, json=json.loads(message.model_dump_json()))


def refused(status: int, message: str, error_type: str = "") -> httpx.Response:
    """Bedrock's error envelope: `{"message": …}`, the exception's name in the
    `x-amzn-ErrorType` header."""
    headers = {"x-amzn-ErrorType": f"{error_type}:http://internal.amazon.com/coral/"}
    return httpx.Response(status, json={"message": message}, headers=headers if error_type else {})


def bedrock_generator(
    handler: Callable[[httpx.Request], httpx.Response],
    requests: list[httpx.Request] | None = None,
    *,
    credentials: FakeCredentials | None = None,
) -> BedrockClaudeGenerator:
    seen = requests if requests is not None else []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    messages = CredentialedMessages(
        credentials or FakeCredentials(), region=REGION, http_client=http, max_retries=0
    )
    return BedrockClaudeGenerator(messages, inference_profile=PROFILE, region=REGION)
