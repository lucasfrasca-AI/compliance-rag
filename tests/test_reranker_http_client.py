"""Residency assertion and ID-token auth for the deployed reranker (issue
#23) — the composition-layer code that keeps `RerankerClient` itself
unchanged between the local-Docker path (#14) and a real Cloud Run
endpoint. `fetch_id_token` is always injected as a fake here: no real GCP
credentials or network access are needed to prove this logic.
"""

from __future__ import annotations

import httpx
import pytest

from compliance_rag.adapters.reranker_http_client import (
    GoogleIDTokenAuth,
    RerankerResidencyError,
    assert_reranker_residency,
    build_reranker_http_client,
)

SYDNEY_URL = "https://reranker-abc123-ts.a.run.app"  # host has no region string on purpose
SYDNEY_HOST_URL = "https://reranker-abc123-australia-southeast1.a.run.app"
US_URL = "https://reranker-abc123-us-central1.a.run.app"


# --------------------------------------------------------------------------
# Residency assertion
# --------------------------------------------------------------------------


def test_empty_url_is_refused() -> None:
    with pytest.raises(RerankerResidencyError, match="not configured"):
        assert_reranker_residency("")


def test_localhost_is_always_allowed() -> None:
    assert_reranker_residency("http://localhost:8001")
    assert_reranker_residency("http://127.0.0.1:8001")


def test_a_sydney_host_is_allowed() -> None:
    assert_reranker_residency(SYDNEY_HOST_URL)


def test_an_endpoint_outside_sydney_is_refused() -> None:
    with pytest.raises(RerankerResidencyError, match="australia-southeast1"):
        assert_reranker_residency(US_URL)


# --------------------------------------------------------------------------
# GoogleIDTokenAuth — mint-once-and-reuse
# --------------------------------------------------------------------------


def test_auth_flow_attaches_the_minted_token_as_a_bearer_header() -> None:
    auth = GoogleIDTokenAuth(SYDNEY_HOST_URL, fetch_id_token=lambda aud: "tok-1")
    request = httpx.Request("POST", SYDNEY_HOST_URL + "/rerank")
    (authed_request,) = list(auth.auth_flow(request))
    assert authed_request.headers["Authorization"] == "Bearer tok-1"


def test_auth_flow_passes_the_url_as_the_audience() -> None:
    seen_audiences: list[str] = []
    auth = GoogleIDTokenAuth(
        SYDNEY_HOST_URL, fetch_id_token=lambda aud: seen_audiences.append(aud) or "tok"
    )
    list(auth.auth_flow(httpx.Request("POST", SYDNEY_HOST_URL + "/rerank")))
    assert seen_audiences == [SYDNEY_HOST_URL]


def test_token_is_reused_across_calls_not_re_minted_every_request() -> None:
    calls = 0

    def fetch(audience: str) -> str:
        nonlocal calls
        calls += 1
        return f"tok-{calls}"

    auth = GoogleIDTokenAuth(SYDNEY_HOST_URL, fetch_id_token=fetch)
    for _ in range(5):
        list(auth.auth_flow(httpx.Request("POST", SYDNEY_HOST_URL + "/rerank")))
    assert calls == 1


# --------------------------------------------------------------------------
# build_reranker_http_client — the composition-layer decision point
# --------------------------------------------------------------------------


def test_localhost_client_has_no_auth_attached() -> None:
    client = build_reranker_http_client("http://localhost:8001")
    assert client.auth is None


def test_cloud_run_client_has_id_token_auth_attached() -> None:
    client = build_reranker_http_client(SYDNEY_HOST_URL, fetch_id_token=lambda aud: "tok")
    assert isinstance(client.auth, GoogleIDTokenAuth)


async def test_cloud_run_client_sends_the_bearer_header_on_a_real_request() -> None:
    """End-to-end through httpx's own request pipeline (`MockTransport`),
    not just unit-testing `GoogleIDTokenAuth` in isolation — proves the
    client built by `build_reranker_http_client` actually attaches the
    header on the wire, not just that the auth class can produce one."""
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"scores": []})

    client = build_reranker_http_client(
        SYDNEY_HOST_URL,
        fetch_id_token=lambda aud: "tok-xyz",
        transport=httpx.MockTransport(handler),
    )
    await client.post("/rerank", json={"query": "q", "documents": ["d"], "top_k": 1})
    assert seen_headers["authorization"] == "Bearer tok-xyz"


def test_an_endpoint_outside_sydney_is_refused_before_any_client_is_built() -> None:
    with pytest.raises(RerankerResidencyError):
        build_reranker_http_client(US_URL, fetch_id_token=lambda aud: "tok")
