"""Builds the `httpx.AsyncClient` that `RerankerClient`
(`adapters/reranker_client.py`) is constructed with, and asserts the
configured reranker endpoint's residency — both at this composition layer,
not inside `RerankerClient` itself (issue #23). `RerankerClient`'s own code
is unchanged between the local-Docker path (#14) and the deployed Cloud Run
path (#23): the ticket's "no code change from #14" meant the adapter's
interface, not "no new code anywhere" — this module is that new code.

The deployed service is private (`--no-allow-unauthenticated`, `run.invoker`
restricted to `compliance-rag-runtime`, not `allUsers`), so a real Cloud Run
endpoint needs a Google-signed ID token attached to every request; a local
Docker endpoint does not.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Generator
from typing import cast

import httpx

REGION = "australia-southeast1"
_LOCAL_HOSTS = ("localhost", "127.0.0.1")

# ID tokens minted by this flow are valid ~1 hour; refresh a little early
# rather than racing expiry mid-request.
_TOKEN_REUSE_WINDOW = dt.timedelta(minutes=50)


class RerankerResidencyError(ValueError):
    """A configured reranker endpoint outside Sydney is refused at startup
    (D-15) — the query text and candidate chunks it receives are customer
    policy text, not synthetic data."""


def assert_reranker_residency(reranker_url: str) -> None:
    if not reranker_url:
        raise RerankerResidencyError("reranker_url is not configured")
    host = httpx.URL(reranker_url).host
    if host in _LOCAL_HOSTS:
        return
    if REGION not in host:
        raise RerankerResidencyError(f"reranker_url host {host!r} is not in {REGION} (D-15)")


def default_fetch_id_token(audience: str) -> str:
    """The real path: mint a Google-signed ID token via ADC. Kept as a
    plain function, injected as a default, so tests can supply a fake
    without needing real GCP credentials or network access."""
    import google.auth.transport.requests
    import google.oauth2.id_token

    # google-auth's own `fetch_id_token` is loosely typed (returns Any under
    # pyright strict despite shipping py.typed), same friction as
    # gcs_clause_store.py's `_BucketLike` comment describes for
    # google-cloud-storage; ADC always returns a real token or raises, so
    # `None` here would mean the library changed underneath us, not a case
    # to handle quietly.
    token = cast(
        "str | None",
        google.oauth2.id_token.fetch_id_token(  # pyright: ignore[reportUnknownMemberType]
            google.auth.transport.requests.Request(), audience
        ),
    )
    assert token is not None
    return token


class GoogleIDTokenAuth(httpx.Auth):
    """Attaches `Authorization: Bearer <ID token>` to every request, minting
    a fresh token only once per reuse window rather than on every call —
    the reranker is called once per RAG query, and re-minting on every one
    would be a needless round trip to the metadata server or IAM Credentials
    API on the hot path."""

    def __init__(self, audience: str, fetch_id_token: Callable[[str], str]) -> None:
        self._audience = audience
        self._fetch_id_token = fetch_id_token
        self._token: str | None = None
        self._minted_at: dt.datetime | None = None

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token_for_this_request()}"
        yield request

    def _token_for_this_request(self) -> str:
        now = dt.datetime.now(dt.UTC)
        stale = (
            self._token is None
            or self._minted_at is None
            or now - self._minted_at >= _TOKEN_REUSE_WINDOW
        )
        if stale:
            self._token = self._fetch_id_token(self._audience)
            self._minted_at = now
        assert self._token is not None
        return self._token


def build_reranker_http_client(
    reranker_url: str,
    *,
    timeout_seconds: float,
    fetch_id_token: Callable[[str], str] = default_fetch_id_token,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """`timeout_seconds` has no default on purpose: httpx's own 5 s is far
    below a real 50-candidate rerank (15-37 s on Cloud Run, BUILD_LOG
    2026-10-06), and silently inheriting it timed out every real call.
    `transport` is exposed only so tests can swap in `httpx.MockTransport`
    without touching the client's private attributes; production callers
    never pass it."""
    assert_reranker_residency(reranker_url)
    timeout = httpx.Timeout(timeout_seconds, connect=10.0)
    host = httpx.URL(reranker_url).host
    if host in _LOCAL_HOSTS:
        return httpx.AsyncClient(base_url=reranker_url, transport=transport, timeout=timeout)
    return httpx.AsyncClient(
        base_url=reranker_url,
        auth=GoogleIDTokenAuth(reranker_url, fetch_id_token),
        transport=transport,
        timeout=timeout,
    )
