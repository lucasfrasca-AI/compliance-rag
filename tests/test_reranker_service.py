"""The reranker service and its client (issue #14) — the real converted
ONNX model, the real vendored tokenizer, the real FastAPI app, exercised
in-process via `httpx.ASGITransport` (no Docker container, no network
port, but the same request/response path Docker would run).

Self-skips if `models/reranker-onnx/model.onnx` doesn't exist yet — the
same pattern `tests/test_ledger_schema.py` already uses for `DATABASE_URL`
(`CLAUDE.md`: confirm this actually ran, don't just tolerate a skip).
Build it with:

    uv sync --group export && uv run python scripts/export_reranker_onnx.py
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from compliance_rag.adapters.reranker_client import RerankerClient, RerankerWindowExceeded
from compliance_rag.reranker_service.app import create_app
from compliance_rag.reranker_service.model import DEFAULT_ONNX_MODEL_PATH, RerankerModel
from tests.contracts.test_interfaces import assert_reranker_contract

pytestmark = pytest.mark.skipif(
    not DEFAULT_ONNX_MODEL_PATH.exists(),
    reason=(
        f"no converted ONNX model at {DEFAULT_ONNX_MODEL_PATH} — run "
        "`uv sync --group export && uv run python scripts/export_reranker_onnx.py` first"
    ),
)

QUERY = "What steps must an entity take to protect personal information from misuse?"
RELEVANT_DOCUMENT = (
    "An APP entity must take such steps as are reasonable in the circumstances to "
    "protect the information from misuse, interference and loss."
)
IRRELEVANT_DOCUMENT = "The weather in Canberra in autumn is mild, with occasional frost overnight."


@pytest.fixture(scope="module")
def model() -> RerankerModel:
    return RerankerModel()


@pytest.fixture
async def client(model: RerankerModel) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(model)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# --------------------------------------------------------------------------
# The FastAPI app directly
# --------------------------------------------------------------------------


async def test_rerank_ranks_the_relevant_document_first(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/rerank",
        json={
            "query": QUERY,
            "documents": [IRRELEVANT_DOCUMENT, RELEVANT_DOCUMENT],
            "top_k": 2,
        },
    )
    assert response.status_code == 200
    scores = response.json()["scores"]
    assert len(scores) == 2
    assert scores[0]["index"] == 1  # RELEVANT_DOCUMENT, ranked first
    assert scores[0]["score"] > scores[1]["score"]


async def test_top_k_truncates_the_result(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/rerank",
        json={
            "query": QUERY,
            "documents": [IRRELEVANT_DOCUMENT, RELEVANT_DOCUMENT],
            "top_k": 1,
        },
    )
    assert len(response.json()["scores"]) == 1


async def test_empty_documents_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/rerank", json={"query": QUERY, "documents": [], "top_k": 1})
    assert response.status_code == 400


async def test_an_over_long_request_is_rejected_not_silently_truncated(
    client: httpx.AsyncClient,
) -> None:
    """Issue #14's own acceptance: 'Rejects a request whose query plus
    longest document exceeds the model window, rather than letting the
    model truncate silently.'"""
    huge_document = "reasonable steps to protect personal information " * 1000
    response = await client.post(
        "/rerank",
        json={"query": QUERY, "documents": [huge_document], "top_k": 1},
    )
    assert response.status_code == 422
    assert "token" in response.json()["detail"]


# --------------------------------------------------------------------------
# RerankerClient — the Reranker-interface adapter, against the real service
# --------------------------------------------------------------------------


async def test_reranker_client_satisfies_the_shared_reranker_contract(
    client: httpx.AsyncClient,
) -> None:
    """The exact same contract `FakeReranker` proves in
    `tests/contracts/test_interfaces.py`, run here against the real
    service — not a second, independently-written test that could
    silently diverge from it (issue #14: "contract test passes against
    both the fake and the local service")."""
    await assert_reranker_contract(RerankerClient(client))


async def test_reranker_client_ranks_the_relevant_document_first(
    client: httpx.AsyncClient,
) -> None:
    reranker = RerankerClient(client)
    scored = await reranker.rerank(QUERY, (IRRELEVANT_DOCUMENT, RELEVANT_DOCUMENT), top_k=2)
    assert len(scored) == 2
    assert scored[0].index == 1
    assert scored[0].score > scored[1].score


async def test_reranker_client_truncates_to_top_k(client: httpx.AsyncClient) -> None:
    reranker = RerankerClient(client)
    scored = await reranker.rerank(QUERY, (IRRELEVANT_DOCUMENT, RELEVANT_DOCUMENT), top_k=1)
    assert len(scored) == 1


async def test_reranker_client_raises_on_window_overflow(client: httpx.AsyncClient) -> None:
    reranker = RerankerClient(client)
    huge_document = "reasonable steps to protect personal information " * 1000
    with pytest.raises(RerankerWindowExceeded, match="token"):
        await reranker.rerank(QUERY, (huge_document,), top_k=1)
