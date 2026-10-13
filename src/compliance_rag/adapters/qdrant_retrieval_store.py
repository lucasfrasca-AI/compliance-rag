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

Subscriptions (ADR-0020) are enforced the same way: `_build_filter` reads the
tenant's subscribed library instruments from the ledger itself on every call,
so a library chunk is visible only to a tenant subscribed to its
`instrument_id` (its own payload field — Qdrant can't filter on a substring of
`clause_id`), or to `_public`, which owns every library chunk. Nothing the
caller passes can widen that.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import cast

from fastembed import SparseTextEmbedding
from opentelemetry import trace
from qdrant_client import AsyncQdrantClient, models

from compliance_rag.adapters.interfaces import (
    Candidate,
    Channel,
    Embedder,
    LawScope,
    LedgerStore,
    RetrievalAdmin,
    RetrievalResult,
    RetrievalStore,
)
from compliance_rag.adapters.removal_authorization import RemovalAuthorization
from compliance_rag.corpus.models import Chunk, Scope, epoch_days, parse_stable_id
from compliance_rag.settings import Settings

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
_SPARSE_MODEL_NAME = "Qdrant/bm25"
_SPARSE_CACHE_DIR = ".fastembed_cache"

_PAYLOAD_KEYWORD_FIELDS = (
    "clause_id",
    "obligation_id",
    "tenant_id",
    "scope",
    "instrument_id",
    "version_id",
)
_PAYLOAD_BOOL_FIELDS = ("withdrawn", "synthetic")
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
    vector config unless it already exists, then create any payload index
    it's missing. Safe to call on every process start (production) and once
    per test run (CI's throwaway self-hosted container) — never re-creates
    an existing collection, so it never discards real data, but a collection
    created before an index was added (`instrument_id`, ADR-0020) still gets
    it. Points upserted before a field existed don't carry it and must be
    re-upserted; upsert is idempotent on the deterministic point id."""
    if await client.collection_exists(collection_name):
        info = await client.get_collection(collection_name)
        existing = set(info.payload_schema)
    else:
        await client.create_collection(
            collection_name,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=dense_dims, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={SPARSE_VECTOR_NAME: models.SparseVectorParams()},
        )
        existing = set[str]()
    for field in _PAYLOAD_KEYWORD_FIELDS:
        if field not in existing:
            await client.create_payload_index(
                collection_name, field_name=field, field_schema=models.PayloadSchemaType.KEYWORD
            )
    for field in _PAYLOAD_INTEGER_FIELDS:
        if field not in existing:
            await client.create_payload_index(
                collection_name, field_name=field, field_schema=models.PayloadSchemaType.INTEGER
            )
    for field in _PAYLOAD_BOOL_FIELDS:
        if field not in existing:
            await client.create_payload_index(
                collection_name, field_name=field, field_schema=models.PayloadSchemaType.BOOL
            )


def _client(settings: Settings) -> AsyncQdrantClient:
    return AsyncQdrantClient(
        url=settings.qdrant_url or None,
        api_key=settings.qdrant_api_key.get_secret_value() or None,
    )


@dataclass(frozen=True, slots=True)
class CollectionStatus:
    """What `bootstrap_collection` found or made: no URL, no key."""

    name: str
    created: bool
    points: int
    payload_indexes: tuple[str, ...]


async def bootstrap_collection(settings: Settings) -> CollectionStatus:
    """The configured cluster's corpus collection, created if it is missing
    (issue #139) by the same idempotent `ensure_collection` a service runs on
    start. The deployed cluster then has its collection, and the keep-alive
    (`qdrant-keepalive.yml`) something to write to, before anything is
    deployed. Never recreates or empties an existing collection."""
    if not settings.qdrant_url:
        # The client would otherwise default to localhost.
        raise ValueError("QDRANT_URL is not set")
    client = _client(settings)
    name = settings.qdrant_collection
    try:
        existed = await client.collection_exists(name)
        await ensure_collection(client, name, dense_dims=settings.vertex_embedding_dims)
        info = await client.get_collection(name)
    finally:
        await client.close()
    return CollectionStatus(
        name=name,
        created=not existed,
        points=info.points_count or 0,
        payload_indexes=tuple(sorted(info.payload_schema)),
    )


def _chunk_point_id(chunk_id: str) -> str:
    """Qdrant point IDs must be an unsigned int or a UUID; `chunk_id`
    (`{instrument_id}:{version_id}:{clause_path}` plus a split suffix, D-02)
    is neither, so it travels as the payload's own `chunk_id` field and a
    deterministic UUID5 becomes the point ID — stable across re-upserts of
    the same chunk, so re-ingesting a version is an update, not a duplicate."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"compliance-rag-chunk:{chunk_id}"))


class QdrantRetrievalStore(RetrievalStore, RetrievalAdmin):
    """Track B: hybrid retrieval over chunks, filtered to Track A's IDs
    (D-03), fused with Qdrant's native RRF (D-04, D-07)."""

    def __init__(
        self,
        client: AsyncQdrantClient,
        *,
        collection_name: str,
        embedder: Embedder,
        ledger: LedgerStore,
    ) -> None:
        self._client = client
        self._collection_name = collection_name
        self._embedder = embedder
        self._ledger = ledger

    @classmethod
    def from_settings(
        cls, settings: Settings, *, embedder: Embedder, ledger: LedgerStore
    ) -> QdrantRetrievalStore:
        return cls(
            _client(settings),
            collection_name=settings.qdrant_collection,
            embedder=embedder,
            ledger=ledger,
        )

    @classmethod
    @asynccontextmanager
    async def ephemeral(
        cls, url: str, *, embedder: Embedder, ledger: LedgerStore, prefix: str
    ) -> AsyncGenerator[QdrantRetrievalStore]:
        """A ready store on a uniquely named throwaway collection, dropped on
        exit — for eval runs and tests against a disposable server (CI's
        service container), never the deployed cluster's collection."""
        client = AsyncQdrantClient(url=url)
        store = cls(
            client,
            collection_name=f"{prefix}-{uuid.uuid4().hex[:8]}",
            embedder=embedder,
            ledger=ledger,
        )
        try:
            await store.ensure_ready()
            yield store
        finally:
            await client.delete_collection(store._collection_name)
            await client.close()

    async def ensure_ready(self) -> None:
        await ensure_collection(
            self._client, self._collection_name, dense_dims=self._embedder.dimensions
        )

    async def upsert_chunks(self, chunks: Iterable[Chunk]) -> None:
        """Embeds and upserts chunks (#22). `RetrievalAdmin`'s write path
        (ADR-0021): a new version is upserted beside the old, never over it —
        point ids derive from `chunk_id`, which carries the `version_id`."""
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
                    "instrument_id": parse_stable_id(chunk.clause_id)[0],
                    "version_id": parse_stable_id(chunk.clause_id)[1],
                    "withdrawn": False,
                    "synthetic": chunk.synthetic,
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

    async def _build_filter(
        self,
        *,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        law: LawScope,
        extra: list[models.Condition] | None = None,
    ) -> models.Filter:
        """The *only* place a `Filter` is built (module docstring). Mirrors
        `FakeRetrievalStore._visible()` exactly, as native Qdrant conditions:
        scope must be one of `scopes`, AND (the tenant owns the chunk OR it
        is library law in an instrument the tenant subscribes to), AND the
        chunk is in force on `as_of`. Subscriptions are read here, from the
        ledger, never accepted from the caller (ADR-0020)."""
        subscribed = await self._ledger.subscribed_instruments(tenant_id)
        # What the tenant could see is part of why a clause didn't come back.
        trace.get_current_span().set_attribute(
            "retrieval.subscribed_instruments", sorted(subscribed)
        )
        visible_to_tenant: list[models.Condition] = [
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))
        ]
        if subscribed:
            # An empty MatchAny is not "match nothing" in every Qdrant
            # version, so with no subscriptions the branch is omitted outright.
            visible_to_tenant.append(
                models.Filter(
                    must=[
                        models.FieldCondition(
                            key="scope", match=models.MatchValue(value=Scope.PUBLIC.value)
                        ),
                        models.FieldCondition(
                            key="instrument_id", match=models.MatchAny(any=sorted(subscribed))
                        ),
                    ]
                )
            )
        must: list[models.Condition] = [
            models.FieldCondition(
                key="scope", match=models.MatchAny(any=[s.value for s in scopes])
            ),
            models.Filter(should=visible_to_tenant),
            models.FieldCondition(key="valid_from", range=models.Range(lte=epoch_days(as_of))),
            models.FieldCondition(key="valid_to", range=models.Range(gt=epoch_days(as_of))),
        ]
        trace.get_current_span().set_attribute("retrieval.law_scope", law.described)
        if law.clause_ids is not None:
            # D-03: library law only from the clauses Track A cites; the
            # tenant's own documents aren't law and Track A doesn't scope them.
            # An empty MatchAny isn't "match nothing" everywhere, so with no
            # clauses the law branch is omitted outright.
            own_or_law: list[models.Condition] = [
                models.FieldCondition(
                    key="scope",
                    match=models.MatchAny(any=[Scope.TENANT.value, Scope.USER.value]),
                )
            ]
            if law.clause_ids:
                own_or_law.append(
                    models.Filter(
                        must=[
                            models.FieldCondition(
                                key="scope", match=models.MatchValue(value=Scope.PUBLIC.value)
                            ),
                            models.FieldCondition(
                                key="clause_id", match=models.MatchAny(any=sorted(law.clause_ids))
                            ),
                        ]
                    )
                )
            must.append(models.Filter(should=own_or_law))
        if extra:
            must.extend(extra)
        # ADR-0021: a withdrawn version is out of retrieval at every as_of.
        # must_not, so a point upserted before the field existed still passes.
        withdrawn = models.FieldCondition(key="withdrawn", match=models.MatchValue(value=True))
        return models.Filter(must=must, must_not=[withdrawn])

    @staticmethod
    def _target(
        *, instrument_id: str, tenant_id: str, version_id: str | None
    ) -> models.FilterSelector:
        must: list[models.Condition] = [
            models.FieldCondition(
                key="instrument_id", match=models.MatchValue(value=instrument_id)
            ),
            models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
        ]
        if version_id is not None:
            must.append(
                models.FieldCondition(key="version_id", match=models.MatchValue(value=version_id))
            )
        return models.FilterSelector(filter=models.Filter(must=must))

    @classmethod
    def _authorized_target(cls, authorization: RemovalAuthorization) -> models.FilterSelector:
        return cls._target(
            instrument_id=authorization.instrument_id,
            tenant_id=authorization.tenant_id,
            version_id=authorization.version_id,
        )

    async def retire_version(
        self, *, instrument_id: str, tenant_id: str, version_id: str, valid_to: dt.date
    ) -> None:
        await self._client.set_payload(
            self._collection_name,
            payload={"valid_to": epoch_days(valid_to)},
            points=self._target(
                instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
            ),
        )

    async def holds_real_tenant_data(self) -> bool:
        # must_not synthetic=true, not must synthetic=false: a point written
        # before the field existed has no `synthetic` key and counts as real.
        result = await self._client.count(
            self._collection_name,
            count_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="scope", match=models.MatchValue(value=Scope.TENANT.value)
                    )
                ],
                must_not=[
                    models.FieldCondition(key="synthetic", match=models.MatchValue(value=True))
                ],
            ),
            exact=True,
        )
        return result.count > 0

    async def version_exists(self, *, instrument_id: str, tenant_id: str, version_id: str) -> bool:
        result = await self._client.count(
            self._collection_name,
            count_filter=self._target(
                instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
            ).filter,
            exact=True,
        )
        return result.count > 0

    async def withdraw_version(self, authorization: RemovalAuthorization) -> None:
        await self._client.set_payload(
            self._collection_name,
            payload={"withdrawn": True},
            points=self._authorized_target(authorization),
        )

    async def delete_chunks(self, authorization: RemovalAuthorization) -> None:
        await self._client.delete(
            self._collection_name, points_selector=self._authorized_target(authorization)
        )

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
        law: LawScope,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        limit: int,
    ) -> RetrievalResult:
        query_filter = await self._build_filter(
            tenant_id=tenant_id, scopes=scopes, as_of=as_of, law=law
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
        law: LawScope,
        tenant_id: str,
        as_of: dt.date,
    ) -> Candidate | None:
        scroll_filter = await self._build_filter(
            tenant_id=tenant_id,
            scopes=(Scope.PUBLIC, Scope.TENANT, Scope.USER),
            as_of=as_of,
            law=law,
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
