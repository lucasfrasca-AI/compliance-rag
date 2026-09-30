"""`RetrievalStore` over Qdrant (issue #22, D-07, D-08).

Collection `clauses` (`Settings.qdrant_collection`): a named dense vector
(1536, cosine — `gemini-embedding-001`) plus a named sparse vector (BM25),
fused with Qdrant's own native RRF (D-07: "hybrid, native RRF") rather than
a hand-rolled client-side fusion. Payload indexes on `clause_id`,
`obligation_id`, `tenant_id`, `scope` (keyword) and `valid_from`/`valid_to`
(integer epoch days — `corpus.models.epoch_days`; `valid_to` is never null,
`CONTEXT.md`: Bitemporal, so the range side of every filter below is one
unconditional `Range`, never a range-or-is-null pair).

Sparse vectors are always produced client-side via `fastembed`'s `Qdrant/bm25`
model, not Qdrant Cloud's server-side inference: the sparse VECTOR this
produces is identical either way (Qdrant just stores indices+values), but
implementing genuine server-side inference correctly needs verifying an
exact SDK call shape against live Qdrant Cloud docs, which wasn't available
while building this. SERVICES.md's own fallback row ("Client-side sparse via
fastembed — free, no new service, no approval needed") already pre-approves
exactly this path, so this ships the fallback as the only path for now
rather than half-wire an unverified one — a decision surfaced here and in
the PR, not silently made. `Qdrant/bm25` is a ~10MB, model-free (pure
hashing-trick BM25, no neural network) artifact fetched from Hugging Face on
first use; CI caches `.fastembed_cache/` across runs the same way it caches
the reranker artifact, rather than a network call inside a gate that must
be deterministic (issue #21's own standard).

Scope and tenant enforcement lives here, not at the call site: `_build_filter`
is the *only* place a Qdrant `Filter` gets constructed, and every method that
talks to the client calls it unconditionally. There is no public method on
this class that accepts a caller-supplied filter or exposes the raw client —
a query that forgets tenant/scope/as_of is not a possible call, not merely a
discouraged one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterable
from functools import lru_cache
from typing import cast

from fastembed import SparseTextEmbedding
from qdrant_client import AsyncQdrantClient, models

from compliance_rag.adapters.interfaces import (
    Candidate,
    Channel,
    Embedder,
    RetrievalResult,
    RetrievalStore,
)
from compliance_rag.corpus.models import Chunk, Scope, epoch_days
from compliance_rag.settings import Settings

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
_SPARSE_MODEL_NAME = "Qdrant/bm25"
_SPARSE_CACHE_DIR = ".fastembed_cache"

_PAYLOAD_KEYWORD_FIELDS = ("clause_id", "obligation_id", "tenant_id", "scope")
_PAYLOAD_INTEGER_FIELDS = ("valid_from", "valid_to")


@lru_cache(maxsize=1)
def _sparse_encoder() -> SparseTextEmbedding:
    """One `fastembed` model instance per process — loading it is not free,
    matching `RerankerModel`'s own one-instance-per-process reasoning."""
    return SparseTextEmbedding(_SPARSE_MODEL_NAME, cache_dir=_SPARSE_CACHE_DIR)


def _encode_sparse(texts: Iterable[str]) -> list[models.SparseVector]:
    embeddings = _sparse_encoder().embed(list(texts))
    return [
        models.SparseVector(indices=e.indices.tolist(), values=e.values.tolist())
        for e in embeddings
    ]


async def ensure_collection(
    client: AsyncQdrantClient, collection_name: str, *, dense_dims: int
) -> None:
    """Idempotent bootstrap: create the collection with its dense+sparse
    vector config and every payload index #22 asks for, unless it already
    exists. Safe to call on every process start (production) and once per
    test run (CI's throwaway self-hosted container) — never re-creates an
    existing collection, so it never discards real data."""
    if await client.collection_exists(collection_name):
        return
    await client.create_collection(
        collection_name,
        vectors_config={
            DENSE_VECTOR_NAME: models.VectorParams(size=dense_dims, distance=models.Distance.COSINE)
        },
        sparse_vectors_config={SPARSE_VECTOR_NAME: models.SparseVectorParams()},
    )
    for field in _PAYLOAD_KEYWORD_FIELDS:
        await client.create_payload_index(
            collection_name, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD
        )
    for field in _PAYLOAD_INTEGER_FIELDS:
        await client.create_payload_index(
            collection_name, field_name=field, field_schema=models.PayloadSchemaType.INTEGER
        )


def _chunk_point_id(chunk_id: str) -> str:
    """Qdrant point IDs must be an unsigned int or a UUID; `chunk_id`
    (`{instrument_id}:{version_id}:{clause_path}` plus a split suffix, D-02)
    is neither, so it travels as the payload's own `chunk_id` field and a
    deterministic UUID5 becomes the point ID — stable across re-upserts of
    the same chunk, so re-ingesting a version is an update, not a duplicate."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"compliance-rag-chunk:{chunk_id}"))


class QdrantRetrievalStore(RetrievalStore):
    """Track B: hybrid retrieval over chunks, filtered to Track A's IDs
    (D-03), fused with Qdrant's native RRF (D-04, D-07)."""

    def __init__(
        self, client: AsyncQdrantClient, *, collection_name: str, embedder: Embedder
    ) -> None:
        self._client = client
        self._collection_name = collection_name
        self._embedder = embedder

    @classmethod
    def from_settings(cls, settings: Settings, *, embedder: Embedder) -> QdrantRetrievalStore:
        client = AsyncQdrantClient(
            url=settings.qdrant_url or None,
            api_key=settings.qdrant_api_key.get_secret_value() or None,
        )
        return cls(client, collection_name=settings.qdrant_collection, embedder=embedder)

    async def ensure_ready(self) -> None:
        await ensure_collection(
            self._client, self._collection_name, dense_dims=self._embedder.dimensions
        )

    async def upsert_chunks(self, chunks: Iterable[Chunk]) -> None:
        """Embeds and upserts chunks (#22's own "chunks embedded and upserted
        into Qdrant"). Not part of `RetrievalStore` itself — the interface's
        fakes are seeded in-memory with no network call at all, so a write
        path has no shared shape to abstract over."""
        chunk_list = list(chunks)
        if not chunk_list:
            return
        dense_vectors = await self._embedder.embed(tuple(c.text for c in chunk_list))
        sparse_vectors = _encode_sparse(c.text for c in chunk_list)
        points = [
            models.PointStruct(
                id=_chunk_point_id(chunk.chunk_id),
                vector={
                    DENSE_VECTOR_NAME: list(dense),
                    SPARSE_VECTOR_NAME: sparse,
                },
                payload={
                    "chunk_id": chunk.chunk_id,
                    "clause_id": chunk.clause_id,
                    "obligation_id": chunk.obligation_id,
                    "text": chunk.text,
                    "scope": chunk.scope.value,
                    "tenant_id": chunk.tenant_id,
                    "valid_from": epoch_days(chunk.valid_from),
                    "valid_to": epoch_days(chunk.valid_to),
                },
            )
            for chunk, dense, sparse in zip(chunk_list, dense_vectors, sparse_vectors, strict=True)
        ]
        await self._client.upsert(self._collection_name, points=points)

    def _build_filter(
        self,
        *,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        obligation_ids: tuple[str, ...],
        extra: list[models.Condition] | None = None,
    ) -> models.Filter:
        """The *only* place a `Filter` is built (module docstring). Mirrors
        `FakeRetrievalStore._visible()` exactly, as native Qdrant conditions:
        scope must be one of `scopes`, AND (scope is public OR tenant_id
        matches), AND the chunk is in force on `as_of`."""
        must: list[models.Condition] = [
            models.FieldCondition(
                key="scope", match=models.MatchAny(any=[s.value for s in scopes])
            ),
            models.Filter(
                should=[
                    models.FieldCondition(
                        key="scope", match=models.MatchValue(value=Scope.PUBLIC.value)
                    ),
                    models.FieldCondition(
                        key="tenant_id", match=models.MatchValue(value=tenant_id)
                    ),
                ]
            ),
            models.FieldCondition(key="valid_from", range=models.Range(lte=epoch_days(as_of))),
            models.FieldCondition(key="valid_to", range=models.Range(gt=epoch_days(as_of))),
        ]
        if obligation_ids:
            # D-03: Track B only ever returns chunks under Track A's obligations.
            must.append(
                models.FieldCondition(
                    key="obligation_id", match=models.MatchAny(any=list(obligation_ids))
                )
            )
        if extra:
            must.extend(extra)
        return models.Filter(must=must)

    @staticmethod
    def _payload_to_candidate(
        payload: dict[str, object] | None, *, score: float, channel: Channel | None
    ) -> Candidate:
        data = payload or {}
        return Candidate(
            chunk_id=cast("str", data["chunk_id"]),
            clause_id=cast("str", data["clause_id"]),
            text=cast("str", data["text"]),
            score=score,
            channel=channel,
            obligation_id=cast("str | None", data.get("obligation_id")),
        )

    async def search(
        self,
        query: str,
        *,
        obligation_ids: tuple[str, ...],
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        limit: int,
    ) -> RetrievalResult:
        query_filter = self._build_filter(
            tenant_id=tenant_id, scopes=scopes, as_of=as_of, obligation_ids=obligation_ids
        )
        (dense_vector,) = await self._embedder.embed((query,))
        (sparse_vector,) = _encode_sparse([query])

        # Both channels run and are returned before fusion (CLAUDE.md code
        # standards), so each is its own real query against the collection,
        # not reconstructed from the fused call's own (unexposed) internals.
        dense_hits, sparse_hits, fused_hits = await asyncio.gather(
            self._client.query_points(
                self._collection_name,
                query=list(dense_vector),
                using=DENSE_VECTOR_NAME,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            ),
            self._client.query_points(
                self._collection_name,
                query=sparse_vector,
                using=SPARSE_VECTOR_NAME,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            ),
            self._client.query_points(
                self._collection_name,
                prefetch=[
                    models.Prefetch(
                        query=list(dense_vector),
                        using=DENSE_VECTOR_NAME,
                        filter=query_filter,
                        limit=limit,
                    ),
                    models.Prefetch(
                        query=sparse_vector,
                        using=SPARSE_VECTOR_NAME,
                        filter=query_filter,
                        limit=limit,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
                with_payload=True,
            ),
        )

        return RetrievalResult(
            fused=tuple(
                self._payload_to_candidate(p.payload, score=p.score, channel=None)
                for p in fused_hits.points
            ),
            per_channel={
                Channel.DENSE: tuple(
                    self._payload_to_candidate(p.payload, score=p.score, channel=Channel.DENSE)
                    for p in dense_hits.points
                ),
                Channel.SPARSE: tuple(
                    self._payload_to_candidate(p.payload, score=p.score, channel=Channel.SPARSE)
                    for p in sparse_hits.points
                ),
            },
        )

    async def lookup_clause(
        self,
        clause_id: str,
        *,
        tenant_id: str,
        as_of: dt.date,
    ) -> Candidate | None:
        scroll_filter = self._build_filter(
            tenant_id=tenant_id,
            scopes=(Scope.PUBLIC, Scope.TENANT, Scope.USER),
            as_of=as_of,
            obligation_ids=(),
            extra=[
                models.FieldCondition(key="clause_id", match=models.MatchValue(value=clause_id))
            ],
        )
        records, _next_offset = await self._client.scroll(
            self._collection_name, scroll_filter=scroll_filter, limit=1, with_payload=True
        )
        if not records:
            return None
        return self._payload_to_candidate(records[0].payload, score=1.0, channel=Channel.KEYWORD)
