"""The shared setup for measuring retrieval on the golden set (#28, #32): the
golden corpus loaded into a throwaway Qdrant collection with real cached
vectors, and the reranker service run in-process behind the real client.

One place, so the recall gates and the benchmark scripts can't drift apart.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from pydantic import SecretStr

from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.reranker_client import RerankerClient
from compliance_rag.adapters.reranker_http_client import build_reranker_http_client
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    ClauseRecord,
    DocumentClass,
    Scope,
    clause_anchor_id,
)
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden_corpus import (
    LAW_FIXTURE,
    LAW_INSTRUMENT_ID,
    LAW_VERSION_ID,
    golden_chunks,
    golden_clauses,
)
from compliance_rag.reranker_service.app import create_app
from compliance_rag.reranker_service.model import RerankerModel


@asynccontextmanager
async def golden_store(
    qdrant_url: str, *, prefix: str, api_key: SecretStr | None = None
) -> AsyncGenerator[QdrantRetrievalStore]:
    """The golden corpus on a throwaway collection, with the real
    `gemini-embedding-001` snapshot as its embedder. `api_key` only for the
    one-off run on the deployed cluster (#144); the gates pass none."""
    async with QdrantRetrievalStore.ephemeral(
        qdrant_url,
        embedder=CachedEmbedder(EmbeddingCache.load()),
        ledger=FakeLedgerStore(),
        prefix=prefix,
        api_key=api_key,
    ) as store:
        await store.upsert_chunks(golden_chunks())
        yield store


@asynccontextmanager
async def in_process_reranker(model: RerankerModel) -> AsyncGenerator[RerankerClient]:
    """The reranker service's own FastAPI app behind the real
    `RerankerClient`, over in-process ASGI: the production code path, no
    network."""
    http = build_reranker_http_client(
        "http://localhost",
        timeout_seconds=600,
        transport=httpx.ASGITransport(app=create_app(model)),
    )
    try:
        yield RerankerClient(http)
    finally:
        await http.aclose()


GOLDEN_VALID_FROM = dt.date(2026, 1, 1)
"""When the golden fixtures' versions are recorded as coming into force — before
every golden query's `as_of` (2026-10-01). A fixture date, not a legal one."""


async def golden_clause_store() -> FakeClauseStore:
    """The golden corpus's canonical text, as `GCSClauseStore` would serve it."""
    store = FakeClauseStore()
    for clause in golden_clauses():
        await store.put_clause(clause)
    return store


async def golden_ledger() -> FakeLedgerStore:
    """A ledger with every golden instrument version recorded in force, so the
    verifier's ledger check (ADR-0021 mechanism 3) has a record to consult."""
    ledger = FakeLedgerStore()
    versions = {(c.tenant_id, c.instrument_id, c.version_id) for c in golden_clauses()}
    for tenant_id, instrument_id, version_id in sorted(versions):
        await ledger.record_version(
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            valid_from=GOLDEN_VALID_FROM,
            supersedes=None,
            document_class=DocumentClass.LIBRARY_LAW
            if tenant_id == PUBLIC_TENANT_ID
            else DocumentClass.INTERNAL_POLICY,
        )
    return ledger


def law_source_html() -> str:
    """The law fixture's HTML with the parser's synthesized clause anchors
    written in — what `GCSClauseStore` stores as `source.html`."""
    return parse_compilation(
        LAW_FIXTURE.read_text(encoding="utf-8"),
        instrument_id=LAW_INSTRUMENT_ID,
        version_id=LAW_VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        scope=Scope.PUBLIC,
    ).annotated_html


def clause_link(clause: ClauseRecord, source_html_path: Path) -> str | None:
    """Where a citation opens: the stored source HTML at the clause's
    synthesized anchor (ADR-0019). Law only; a policy's position map is PDF
    offsets, not an anchor — None."""
    if clause.tenant_id != PUBLIC_TENANT_ID:
        return None
    return f"{source_html_path.resolve().as_uri()}#{clause_anchor_id(clause.clause_path)}"
