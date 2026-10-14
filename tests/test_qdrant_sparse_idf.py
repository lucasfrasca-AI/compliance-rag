"""The BM25 channel weighs a rare term above a common one (issue #179), on a
real Qdrant server.

`fastembed`'s `Qdrant/bm25` stores BM25's term-frequency part only; Qdrant
applies the inverse-document-frequency part at query time, and only to a sparse
vector created with `Modifier.IDF`. Without it the channel ranked a short clause
sharing common words with the question above the one clause holding the word
that mattered. These tests build exactly that pair (one chunk with a rare term,
many with common ones) and prove, on the real adapter and a real server:

- a new collection is created with the modifier and ranks the rare term first;
- a collection created before it (as the deployed `clauses` was) is changed in
  place, once, with its points and its index settings kept, and its sparse order
  changes with no re-upsert;
- a server that accepted the change and ignored it is an error, not a no-op.

Self-skips without a reachable `QDRANT_URL`, like `test_qdrant_retrieval_store.py`.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from qdrant_client import AsyncQdrantClient, models

from compliance_rag.adapters.fakes import FakeEmbedder, FakeLedgerStore
from compliance_rag.adapters.interfaces import Channel
from compliance_rag.adapters.qdrant_retrieval_store import (
    SPARSE_VECTOR_NAME,
    QdrantRetrievalStore,
    ensure_collection,
)
from compliance_rag.corpus.models import Chunk, Scope
from tests.conftest import ALL_LAW, qdrant_reachable

_QDRANT_URL = os.environ.get("QDRANT_URL", "")

pytestmark = pytest.mark.skipif(
    not qdrant_reachable(_QDRANT_URL),
    reason="QDRANT_URL not set or not reachable — this test needs a real Qdrant server",
)

QUERY = "entity information xylophone"
RARE_TEXT = "entity xylophone register maintained by quarterly custodians of the archive"
COMMON_TEXTS = (
    "entity information personal",
    "entity information holds",
    "entity personal records",
    "information personal data",
    "entity information data",
    "entity personal holds",
    "information holds records",
    "entity records data",
)
"""Three-word clauses made of words most of the corpus uses, three of each."""

_INDEX_MARKER = 77
"""A sparse index setting no one picks by default, to see it survive the change."""


def _chunk(chunk_id: str, number: int, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        clause_id=f"privacy-act-1988:TEST-FIXTURE-001:sch1/app{number}/1",
        text=text,
    )


def pair_chunks() -> tuple[Chunk, ...]:
    """One chunk holding the rare term, and many holding only common ones."""
    common = tuple(
        _chunk(f"common-{n}", n, COMMON_TEXTS[n % len(COMMON_TEXTS)]) for n in range(1, 25)
    )
    return (*common, _chunk("rare", 99, RARE_TEXT))


async def _sparse_ranking(store: QdrantRetrievalStore) -> list[str]:
    """The sparse channel's own list for `QUERY`, best first, every point."""
    result = await store.search(
        QUERY,
        law=ALL_LAW,
        tenant_id="_public",
        scopes=(Scope.PUBLIC,),
        as_of=dt.date(2026, 1, 1),
        limit=30,
    )
    return [c.chunk_id for c in result.per_channel[Channel.SPARSE]]


async def _modifier(client: AsyncQdrantClient, name: str) -> models.Modifier | None:
    sparse = (await client.get_collection(name)).config.params.sparse_vectors
    assert sparse is not None
    return sparse[SPARSE_VECTOR_NAME].modifier


@pytest.fixture
async def client() -> AsyncIterator[AsyncQdrantClient]:
    qdrant = AsyncQdrantClient(url=_QDRANT_URL)
    try:
        yield qdrant
    finally:
        await qdrant.close()


@pytest.fixture
async def legacy(client: AsyncQdrantClient) -> AsyncIterator[QdrantRetrievalStore]:
    """A store on a collection as it was created before #179: the sparse vector
    has no modifier, and an index setting of its own."""
    name = f"sparse-legacy-{uuid.uuid4().hex[:8]}"
    await client.create_collection(
        name,
        vectors_config={"dense": models.VectorParams(size=8, distance=models.Distance.COSINE)},
        sparse_vectors_config={
            SPARSE_VECTOR_NAME: models.SparseVectorParams(
                index=models.SparseIndexParams(full_scan_threshold=_INDEX_MARKER)
            )
        },
    )
    try:
        yield QdrantRetrievalStore(
            client,
            collection_name=name,
            embedder=FakeEmbedder(dimensions=8),
            ledger=FakeLedgerStore(),
        )
    finally:
        await client.delete_collection(name)


def _name(store: QdrantRetrievalStore) -> str:
    return store._collection_name  # pyright: ignore[reportPrivateUsage]


async def test_a_new_collection_is_created_with_the_idf_modifier(
    client: AsyncQdrantClient,
) -> None:
    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="idf-new"
    ) as store:
        assert await _modifier(client, _name(store)) == models.Modifier.IDF


async def test_a_new_collection_ranks_the_rare_term_above_the_common_ones() -> None:
    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="idf-new"
    ) as store:
        await store.upsert_chunks(pair_chunks())
        ranking = await _sparse_ranking(store)
    assert ranking[0] == "rare", ranking


async def test_a_collection_without_idf_is_changed_in_place_and_keeps_what_it_holds(
    client: AsyncQdrantClient, legacy: QdrantRetrievalStore
) -> None:
    await legacy.upsert_chunks(pair_chunks())
    name = _name(legacy)
    assert await _modifier(client, name) is None

    created = await ensure_collection(client, name, dense_dims=8)

    assert not created  # the same collection, not a new one
    info = await client.get_collection(name)
    assert info.points_count == len(pair_chunks())
    sparse = (info.config.params.sparse_vectors or {})[SPARSE_VECTOR_NAME]
    assert sparse.modifier == models.Modifier.IDF
    # Only the modifier changed: the vector's own index settings are still its.
    assert sparse.index is not None and sparse.index.full_scan_threshold == _INDEX_MARKER


async def test_idf_changes_the_sparse_order_of_points_already_stored(
    legacy: QdrantRetrievalStore,
) -> None:
    """The constructed pair: the rare-term chunk is not first while the common
    words outweigh it, and is first once the collection has the modifier —
    with the points as they were, no re-upsert between the two lists."""
    await legacy.upsert_chunks(pair_chunks())
    before = await _sparse_ranking(legacy)

    await legacy.ensure_ready()
    after = await _sparse_ranking(legacy)

    assert before[0] != "rare", before
    assert after[0] == "rare", after
    assert sorted(before) == sorted(after)  # the same points, ordered differently


async def test_the_change_is_made_once_and_logged_by_collection_name(
    client: AsyncQdrantClient,
    legacy: QdrantRetrievalStore,
    monkeypatch: pytest.MonkeyPatch,
    span_exporter: InMemorySpanExporter,
) -> None:
    updated: list[str] = []
    update = client.update_collection

    async def counting(collection_name: str, **kwargs: Any) -> bool:  # Any: forwarded untouched
        updated.append(collection_name)
        return await update(collection_name, **kwargs)

    monkeypatch.setattr(client, "update_collection", counting)

    await legacy.ensure_ready()
    await legacy.ensure_ready()  # the next process start finds nothing to do

    assert updated == [_name(legacy)]
    (applied,) = [
        s for s in span_exporter.get_finished_spans() if s.name == "qdrant.apply_sparse_idf"
    ]
    assert (applied.attributes or {})["retrieval.collection"] == _name(legacy)


async def test_a_new_collection_needs_no_change(
    client: AsyncQdrantClient,
    monkeypatch: pytest.MonkeyPatch,
    span_exporter: InMemorySpanExporter,
) -> None:
    async def refused(collection_name: str, **kwargs: Any) -> bool:  # Any: never reached
        raise AssertionError(f"update_collection({collection_name!r}) on a collection with IDF")

    monkeypatch.setattr(client, "update_collection", refused)
    name = f"idf-new-{uuid.uuid4().hex[:8]}"
    try:
        assert await ensure_collection(client, name, dense_dims=8)  # created, with it
        assert not await ensure_collection(client, name, dense_dims=8)  # nothing to change
    finally:
        await client.delete_collection(name)
    assert not [
        s for s in span_exporter.get_finished_spans() if s.name == "qdrant.apply_sparse_idf"
    ]


async def test_a_server_that_ignores_the_modifier_is_an_error_naming_the_collection(
    client: AsyncQdrantClient, legacy: QdrantRetrievalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ignoring(collection_name: str, **kwargs: Any) -> bool:  # Any: never forwarded
        return True

    monkeypatch.setattr(client, "update_collection", ignoring)
    with pytest.raises(ValueError, match=rf"{_name(legacy)}.*re-creating"):
        await legacy.ensure_ready()
