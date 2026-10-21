"""`RetrievalStore` over Qdrant (issue #22, D-07, D-08).

Collection `clauses` (`Settings.qdrant_collection`): a named dense vector
(1536, cosine — `gemini-embedding-001`) plus a named sparse vector (BM25).
Qdrant scores each channel; `search` cuts each channel's list and fuses the
two with RRF itself (`adapters.fusion`, ADR-0046, amending D-07's "native
RRF"), because Qdrant's own order among equal scores decided which tied
candidates entered fusion and the fused scores they got (#177).

Payload indexes on `clause_id`, `tenant_id`, `scope`, `user_id` (keyword) and
`valid_from`/`valid_to` (integer epoch days — `corpus.models.epoch_days`;
`valid_to` is never null, `CONTEXT.md`: Bitemporal, so the range side of every
filter below is one unconditional `Range`, never a range-or-is-null pair).

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

The `Qdrant/bm25` values are BM25's term-frequency part only. The
inverse-document-frequency part is Qdrant's, applied at query time to a sparse
vector configured with `Modifier.IDF`, so the sparse vector is created with it
and an older collection is given it in place (`ensure_collection`, #179).
Chunks and questions are encoded differently (#190): a chunk's terms weigh their
frequency in it (`_encode_documents`), a question's each weigh 1
(`_encode_query`). Both are client-side, so the deployed collection needs no
change for it.

Scope and tenant enforcement lives here, not at the call site: `_build_filter`
is the *only* place a Qdrant `Filter` gets constructed, and every method that
talks to the client calls it unconditionally. There is no public method on
this class that accepts a caller-supplied filter or exposes the raw client —
a query that forgets tenant/scope/as_of is not a possible call, not merely a
discouraged one.

User scope is private to its owner (ADR-0036), enforced in the same place: a
user-scoped chunk carries its owner's `user_id`, and `_build_filter` admits it
only when the chunk's tenant is the asking tenant AND its `user_id` is the
asking member's. No member means no user-scoped chunk. The filter is the whole
rule for retrieval: no later stage of retrieval re-checks it. (The clause
store's read of canonical text applies the same owner rule independently, for
the case where a filter somewhere did not.)

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
import re
import uuid
from collections.abc import AsyncGenerator, Coroutine, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import cast

import httpx
from fastembed import SparseEmbedding, SparseTextEmbedding
from opentelemetry import trace
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.conversions.common_types import PointId
from qdrant_client.http.exceptions import UnexpectedResponse

from compliance_rag import telemetry
from compliance_rag.adapters.fusion import cut, reciprocal_rank_fusion, scored
from compliance_rag.adapters.interfaces import (
    NO_PREFIX,
    Candidate,
    Channel,
    Embedder,
    LawScope,
    LedgerStore,
    PolicyCorpus,
    RetrievalAdmin,
    RetrievalResult,
    RetrievalStore,
)
from compliance_rag.adapters.removal_authorization import RemovalAuthorization
from compliance_rag.adapters.revision_authorization import RevisionAuthorization
from compliance_rag.corpus.models import (
    Chunk,
    Incomplete,
    Scope,
    clause_ancestors,
    epoch_days,
    incomplete_json,
    incomplete_of_json,
    parse_stable_id,
)
from compliance_rag.settings import Settings

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
_VECTOR_NAMES = {Channel.DENSE: DENSE_VECTOR_NAME, Channel.SPARSE: SPARSE_VECTOR_NAME}
"""The named vector each ranked channel searches."""
_SPARSE_MODEL_NAME = "Qdrant/bm25"
_SPARSE_CACHE_DIR = ".fastembed_cache"

_PAYLOAD_KEYWORD_FIELDS = (
    "clause_id",
    "ancestors",
    "tenant_id",
    "scope",
    "user_id",
    "instrument_id",
    "version_id",
)
_PAYLOAD_BOOL_FIELDS = ("withdrawn", "synthetic")
_PAYLOAD_INTEGER_FIELDS = ("valid_from", "valid_to")

_SCROLL_PAGE = 256
"""Points per scroll page when listing a version's chunk ids."""

UPSERT_BATCH = 64
"""Points per upsert request. A 1536-dim point is roughly 20-30 KB as JSON, so a
whole compilation in one request (779 clauses, #144) would be ~20 MB, close to
Qdrant's default 32 MB request limit; 64 keeps each request under 2 MB."""


@lru_cache(maxsize=1)
def _sparse_encoder() -> SparseTextEmbedding:
    """One `fastembed` model instance per process — loading it is not free,
    matching `RerankerModel`'s own one-instance-per-process reasoning."""
    return SparseTextEmbedding(_SPARSE_MODEL_NAME, cache_dir=_SPARSE_CACHE_DIR)


def _sparse_vector(embedding: SparseEmbedding) -> models.SparseVector:
    return models.SparseVector(indices=embedding.indices.tolist(), values=embedding.values.tolist())


def _encode_documents(texts: Iterable[str]) -> list[models.SparseVector]:
    """Chunks, for storing: each term weighs its frequency in the chunk,
    softened by the chunk's length (BM25's term-frequency part, `.embed`)."""
    return [_sparse_vector(e) for e in _sparse_encoder().embed(list(texts))]


def _encode_query(query: str) -> models.SparseVector:
    """A question, for searching: each distinct term weighs 1 (`.query_embed`),
    which is how BM25 scores a query. A document's weights would make a word the
    question repeats count again, and the question's length shift every term's
    weight (#190). The terms are the ones `_encode_documents` stores chunks
    under: the two share a tokeniser, stemmer and stop list."""
    (embedding,) = _sparse_encoder().query_embed(query)
    return _sparse_vector(embedding)


def _sparse_vector_params() -> models.SparseVectorParams:
    """The sparse vector's config. `Qdrant/bm25` writes BM25's term-frequency
    part only; IDF is Qdrant's to apply, at query time, and only when the
    sparse vector says so (#179). Without it a word every clause uses weighs as
    much as one that picks a clause out."""
    return models.SparseVectorParams(modifier=models.Modifier.IDF)


def _sparse_modifier(params: models.CollectionParams) -> models.Modifier | None:
    sparse = (params.sparse_vectors or {}).get(SPARSE_VECTOR_NAME)
    return sparse.modifier if sparse is not None else None


async def _apply_sparse_idf(client: AsyncQdrantClient, collection_name: str) -> None:
    """Give an existing collection's sparse vector the IDF modifier, in place
    (#179): Qdrant changes it without touching the points, whose stored values
    are the term-frequency part either way, and keeps the vector's index
    settings. Read back afterwards, so a server that accepted the call and
    ignored it is an error here, not a quiet no-op."""
    with telemetry.span(
        "qdrant.apply_sparse_idf", **{"retrieval.collection": collection_name}
    ) as span:
        await client.update_collection(
            collection_name, sparse_vectors_config={SPARSE_VECTOR_NAME: _sparse_vector_params()}
        )
        info = await client.get_collection(collection_name)
        modifier = _sparse_modifier(info.config.params)
        span.set_attribute("retrieval.sparse_modifier", modifier.value if modifier else "none")
        if modifier != models.Modifier.IDF:
            raise ValueError(
                f"collection {collection_name}: asked the server to give the "
                f"{SPARSE_VECTOR_NAME!r} sparse vector the IDF modifier and it still has none — "
                "the collection needs re-creating"
            )


async def ensure_collection(
    client: AsyncQdrantClient, collection_name: str, *, dense_dims: int
) -> bool:
    """Idempotent bootstrap: create the collection with its dense+sparse
    vector config unless it already exists, then create any payload index
    it's missing; True if it created the collection. Safe to call on every
    process start (production) and once per test run (CI's throwaway
    self-hosted container) — never re-creates an existing collection, so it
    never discards real data, but a collection created before an index was
    added (`instrument_id`, ADR-0020) still gets it. So does one whose sparse
    vector predates the IDF modifier (#179): that is changed in place, with its
    points kept, once (a span, `qdrant.apply_sparse_idf`, names the
    collection). An existing collection of another shape (dense size or
    distance, or no sparse vector) is refused, not used. Points upserted before
    a field existed don't carry it and must be re-upserted; upsert is
    idempotent on the deterministic point id."""
    created = not await client.collection_exists(collection_name)
    if not created:
        info = await client.get_collection(collection_name)
        _check_shape(collection_name, info.config.params, dense_dims=dense_dims)
        if _sparse_modifier(info.config.params) != models.Modifier.IDF:
            await _apply_sparse_idf(client, collection_name)
        existing = set(info.payload_schema)
    else:
        await client.create_collection(
            collection_name,
            vectors_config={
                DENSE_VECTOR_NAME: models.VectorParams(
                    size=dense_dims, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={SPARSE_VECTOR_NAME: _sparse_vector_params()},
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
    return created


def _check_shape(name: str, params: models.CollectionParams, *, dense_dims: int) -> None:
    vectors = params.vectors if isinstance(params.vectors, dict) else {}
    dense = vectors.get(DENSE_VECTOR_NAME)
    if (
        dense is None
        or dense.size != dense_dims
        or dense.distance != models.Distance.COSINE
        or SPARSE_VECTOR_NAME not in (params.sparse_vectors or {})
    ):
        raise ValueError(
            f"collection {name} exists with another shape: expected a {dense_dims}-dim cosine "
            f"{DENSE_VECTOR_NAME!r} vector and a {SPARSE_VECTOR_NAME!r} sparse vector"
        )


_HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)
"""Open connections are reused: httpx's defaults, which `qdrant-client` gives
every host but `localhost` and `127.0.0.1`. For those two it turns keep-alive
off, so every request opens a TCP connection, and through Docker Desktop's
port proxy a connect could outlast httpx's 5 s while a suite ran beside it
(#205). With these limits a local server is reached the way Qdrant Cloud is."""


def _qdrant_client(url: str, api_key: str) -> AsyncQdrantClient:
    """Every client this adapter makes: one connection policy (`_HTTP_LIMITS`).
    An empty `url` or `api_key` is none."""
    return AsyncQdrantClient(url=url or None, api_key=api_key or None, limits=_HTTP_LIMITS)


async def _drop_and_close(client: AsyncQdrantClient, collection_name: str) -> None:
    try:
        await client.delete_collection(collection_name)
    finally:
        await client.close()


async def _finish_despite_cancellation(cleanup: Coroutine[object, object, None]) -> None:
    """Run `cleanup` to the end even if this task is cancelled while it runs,
    then let the cancellation through (#256). A Ctrl+C to a uvicorn server
    becomes a cancellation of the main task, delivered at its next await,
    which is the throwaway collection's drop."""
    task = asyncio.create_task(cleanup)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as cancellation:
            if task.done():
                raise
            cancelled = cancellation
    if cancelled is not None:
        raise cancelled


_EPHEMERAL_SUFFIX = 8  # hex digits of a uuid4


def _ephemeral_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:_EPHEMERAL_SUFFIX]}"


def _ephemeral_pattern(prefix: str) -> re.Pattern[str]:
    """Exactly the names `_ephemeral_name` gives `prefix`."""
    if not prefix:
        raise ValueError("a throwaway collection's prefix can't be empty")
    return re.compile(rf"{re.escape(prefix)}-[0-9a-f]{{{_EPHEMERAL_SUFFIX}}}")


def _client(settings: Settings) -> AsyncQdrantClient:
    return _qdrant_client(settings.qdrant_url, settings.qdrant_api_key.get_secret_value())


@dataclass(frozen=True, slots=True)
class CollectionStatus:
    """What `bootstrap_collection` found or made: nothing about where the
    cluster is or how to reach it."""

    name: str
    created: bool
    points: int
    payload_indexes: tuple[str, ...]
    sparse_modifier: str
    """The sparse vector's weighting as the cluster reports it, read back after
    the bootstrap ("idf", or "none"): the evidence #179's change took."""


async def bootstrap_collection(settings: Settings) -> CollectionStatus:
    """The configured cluster's corpus collection, created if it is missing
    (issue #139) by the same idempotent `ensure_collection` a service runs on
    start. The deployed cluster then has its collection, and the keep-alive
    (`qdrant-keepalive.yml`) something to write to, before anything is
    deployed. Never recreates or empties an existing collection, and refuses
    one of another shape. A key the cluster refuses (wrong, rotated, or
    read-only: creating needs write) is a `PermissionError` that says so."""
    if not settings.qdrant_url:
        # The client would otherwise default to localhost.
        raise ValueError("QDRANT_URL is not set")
    name = settings.qdrant_collection
    client = _client(settings)
    with telemetry.span("qdrant.bootstrap_collection", **{"retrieval.collection": name}) as span:
        try:
            created = await ensure_collection(
                client, name, dense_dims=settings.vertex_embedding_dims
            )
            info = await client.get_collection(name)
        except UnexpectedResponse as refused:
            if refused.status_code in (401, 403):
                raise PermissionError(
                    f"the cluster refused the API key (HTTP {refused.status_code}): wrong, "
                    "rotated, or read-only — creating a collection needs a key that can write"
                ) from None
            raise
        finally:
            await client.close()
        modifier = _sparse_modifier(info.config.params)
        status = CollectionStatus(
            name=name,
            created=created,
            points=info.points_count or 0,
            payload_indexes=tuple(sorted(info.payload_schema)),
            sparse_modifier=modifier.value if modifier is not None else "none",
        )
        span.set_attribute("retrieval.collection_created", status.created)
        span.set_attribute("retrieval.collection_points", status.points)
        span.set_attribute("retrieval.sparse_modifier", status.sparse_modifier)
    return status


def _chunk_point_id(chunk_id: str) -> str:
    """Qdrant point IDs must be an unsigned int or a UUID; `chunk_id`
    (`{instrument_id}:{version_id}:{clause_path}` plus a split suffix, D-02)
    is neither, so it travels as the payload's own `chunk_id` field and a
    deterministic UUID5 becomes the point ID — stable across re-upserts of
    the same chunk, so re-ingesting a version is an update, not a duplicate."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"compliance-rag-chunk:{chunk_id}"))


class QdrantRetrievalStore(RetrievalStore, RetrievalAdmin):
    """Track B: hybrid retrieval over chunks, filtered to Track A's IDs
    (D-03), fused with RRF (D-04) over channel lists Qdrant scores and the
    adapter cuts (ADR-0046)."""

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
        cls,
        url: str,
        *,
        embedder: Embedder,
        ledger: LedgerStore,
        prefix: str,
        api_key: SecretStr | None = None,
    ) -> AsyncGenerator[QdrantRetrievalStore]:
        """A ready store on a uniquely named throwaway collection, dropped on
        exit — for eval runs and tests against a disposable server (CI's
        service container), never the deployed cluster's collection. The drop
        names only the uuid-named collection this call created, never one that
        existed before. `api_key` is for a one-off run on the deployed cluster
        (#144), read through `Settings` by the operator script; tests and CI
        pass none. On that cluster the drop removes the run's own throwaway
        collection, not a cloud resource: the cluster and `clauses` are
        untouched."""
        key = api_key.get_secret_value() if api_key is not None else ""
        client = _qdrant_client(url, key)
        store = cls(
            client,
            collection_name=_ephemeral_name(prefix),
            embedder=embedder,
            ledger=ledger,
        )
        try:
            await store.ensure_ready()
            yield store
        finally:
            await _finish_despite_cancellation(_drop_and_close(client, store._collection_name))

    @staticmethod
    async def drop_leftover_ephemerals(url: str, *, prefix: str) -> list[str]:
        """Drop every collection on `url` named as `ephemeral` names one for
        `prefix` (`{prefix}-` and eight hex digits): what a killed run left
        behind (#256). Nothing else is touched. It takes no API key, so it
        reaches only a server that needs none, never the deployed cluster.
        A run still open on the same prefix loses its collection, so a caller
        sweeps only where it is the prefix's one user."""
        pattern = _ephemeral_pattern(prefix)
        client = _qdrant_client(url, "")
        try:
            names = [c.name for c in (await client.get_collections()).collections]
            leftovers = sorted(n for n in names if pattern.fullmatch(n))
            for name in leftovers:
                await client.delete_collection(name)
        finally:
            await client.close()
        return leftovers

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
        sparse_vectors = _encode_documents(c.text for c in chunk_list)
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
                    # The clauses this one is inside, so a citation of a
                    # subclause admits its paragraphs (#163, `LawScope.admits`).
                    "ancestors": list(clause_ancestors(chunk.clause_id)),
                    "instrument_id": parse_stable_id(chunk.clause_id)[0],
                    "version_id": parse_stable_id(chunk.clause_id)[1],
                    "withdrawn": False,
                    "synthetic": chunk.synthetic,
                    "text": chunk.text,
                    "scope": chunk.scope.value,
                    "tenant_id": chunk.tenant_id,
                    # The owner of a user-scoped chunk, null for any other
                    # scope: the only thing that keeps it private (ADR-0036).
                    "user_id": chunk.user_id,
                    "valid_from": epoch_days(chunk.valid_from),
                    "valid_to": epoch_days(chunk.valid_to),
                    # Its clause's incompleteness, `[]` when complete: what
                    # marks it incomplete when it enters a prompt (ADR-0047).
                    "incomplete": incomplete_json(chunk.incomplete),
                },
            )
            for chunk, dense, sparse in zip(chunk_list, dense_vectors, sparse_vectors, strict=True)
        ]
        # Embedded first, written after: a failed embedding writes nothing.
        for start in range(0, len(points), UPSERT_BATCH):
            await self._client.upsert(
                self._collection_name, points=points[start : start + UPSERT_BATCH]
            )

    async def _build_filter(
        self,
        *,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        law: LawScope,
        policy_corpus: PolicyCorpus,
        user_id: str | None,
        extra: list[models.Condition] | None = None,
    ) -> models.Filter:
        """The *only* place a `Filter` is built (module docstring). Mirrors
        `FakeRetrievalStore._visible()` exactly, as native Qdrant conditions:
        scope must be one of `scopes`, AND (the tenant owns the chunk, and if
        it is user-scoped the asking member owns it too, OR it is library law
        in an instrument the tenant subscribes to), AND the chunk is in force
        on `as_of`. Subscriptions are read here, from the ledger, never
        accepted from the caller (ADR-0020). A tenant chunk of a clause in
        `policy_corpus` is excluded (ADR-0033). A chunk that is user-scoped
        but carries no `user_id` matches nothing here: it is visible to no one
        (ADR-0036)."""
        subscribed = await self._ledger.subscribed_instruments(tenant_id)
        # What the tenant could see is part of why a clause didn't come back.
        trace.get_current_span().set_attribute(
            "retrieval.subscribed_instruments", sorted(subscribed)
        )
        # Whether a member asked, not who: user scope admits only their own.
        trace.get_current_span().set_attribute("retrieval.member_present", user_id is not None)
        visible_to_asker: list[models.Condition] = [
            # The tenant's documents of every scope but user, which is private
            # to a member and admitted by the branch below.
            models.Filter(
                must=[
                    models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))
                ],
                must_not=[
                    models.FieldCondition(
                        key="scope", match=models.MatchValue(value=Scope.USER.value)
                    )
                ],
            )
        ]
        if user_id is not None:
            visible_to_asker.append(
                models.Filter(
                    must=[
                        models.FieldCondition(
                            key="tenant_id", match=models.MatchValue(value=tenant_id)
                        ),
                        models.FieldCondition(
                            key="scope", match=models.MatchValue(value=Scope.USER.value)
                        ),
                        models.FieldCondition(
                            key="user_id", match=models.MatchValue(value=user_id)
                        ),
                    ]
                )
            )
        if subscribed:
            # An empty MatchAny is not "match nothing" in every Qdrant
            # version, so with no subscriptions the branch is omitted outright.
            visible_to_asker.append(
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
            models.Filter(should=visible_to_asker),
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
                # A cited clause, or a clause inside one (#163): its paragraphs
                # are its text. A point stored before `ancestors` existed
                # matches on its own id only, which is narrower, never wider.
                cited = sorted(law.clause_ids)
                own_or_law.append(
                    models.Filter(
                        must=[
                            models.FieldCondition(
                                key="scope", match=models.MatchValue(value=Scope.PUBLIC.value)
                            )
                        ],
                        should=[
                            models.FieldCondition(
                                key="clause_id", match=models.MatchAny(any=cited)
                            ),
                            models.FieldCondition(
                                key="ancestors", match=models.MatchAny(any=cited)
                            ),
                        ],
                    )
                )
            must.append(models.Filter(should=own_or_law))
        if extra:
            must.extend(extra)
        # ADR-0021: a withdrawn version is out of retrieval at every as_of.
        # must_not, so a point upserted before the field existed still passes.
        must_not: list[models.Condition] = [
            models.FieldCondition(key="withdrawn", match=models.MatchValue(value=True))
        ]
        trace.get_current_span().set_attribute("retrieval.policy_corpus", policy_corpus.described)
        if policy_corpus.clause_ids:
            # ADR-0033: the prefix's clauses are already in front of the model.
            # Tenant scope only, so a law id here can never narrow the law; an
            # empty MatchAny isn't "match nothing" everywhere, so none is sent.
            must_not.append(
                models.Filter(
                    must=[
                        models.FieldCondition(
                            key="scope", match=models.MatchValue(value=Scope.TENANT.value)
                        ),
                        models.FieldCondition(
                            key="clause_id",
                            match=models.MatchAny(any=sorted(policy_corpus.clause_ids)),
                        ),
                    ]
                )
            )
        return models.Filter(must=must, must_not=must_not)

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

    async def holds_real_customer_data(self) -> bool:
        # must_not synthetic=true, not must synthetic=false: a point written
        # before the field existed has no `synthetic` key and counts as real.
        # Anything but public law is customer data: a tenant's documents and
        # one person's private additions alike (#166).
        result = await self._client.count(
            self._collection_name,
            count_filter=models.Filter(
                must_not=[
                    models.FieldCondition(
                        key="scope", match=models.MatchValue(value=Scope.PUBLIC.value)
                    ),
                    models.FieldCondition(key="synthetic", match=models.MatchValue(value=True)),
                ],
            ),
            exact=True,
        )
        return result.count > 0

    async def version_chunk_ids(
        self, *, instrument_id: str, tenant_id: str, version_id: str
    ) -> frozenset[str]:
        target = self._target(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        ).filter
        ids: set[str] = set()
        offset: PointId | None = None
        while True:
            points, offset = await self._client.scroll(
                self._collection_name,
                scroll_filter=target,
                with_payload=["chunk_id"],
                with_vectors=False,
                limit=_SCROLL_PAGE,
                offset=offset,
            )
            ids.update(str(p.payload["chunk_id"]) for p in points if p.payload)
            if offset is None:
                return frozenset(ids)

    async def stored_chunk_texts(
        self, *, instrument_id: str, tenant_id: str, version_id: str
    ) -> dict[str, str]:
        target = self._target(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        ).filter
        texts: dict[str, str] = {}
        offset: PointId | None = None
        with telemetry.span(
            "qdrant.stored_chunk_texts",
            **{"retrieval.version": f"{instrument_id}:{version_id}", "retrieval.tenant": tenant_id},
        ) as span:
            while True:
                points, offset = await self._client.scroll(
                    self._collection_name,
                    scroll_filter=target,
                    with_payload=["chunk_id", "text"],
                    with_vectors=False,
                    limit=_SCROLL_PAGE,
                    offset=offset,
                )
                texts.update(
                    {
                        str(p.payload["chunk_id"]): str(p.payload["text"])
                        for p in points
                        if p.payload
                    }
                )
                if offset is None:
                    span.set_attribute("retrieval.stored_chunks", len(texts))
                    return texts

    async def stored_chunk_incomplete(
        self, *, instrument_id: str, tenant_id: str, version_id: str
    ) -> dict[str, Incomplete]:
        target = self._target(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        ).filter
        held: dict[str, Incomplete] = {}
        offset: PointId | None = None
        with telemetry.span(
            "qdrant.stored_chunk_incomplete",
            **{"retrieval.version": f"{instrument_id}:{version_id}", "retrieval.tenant": tenant_id},
        ) as span:
            while True:
                points, offset = await self._client.scroll(
                    self._collection_name,
                    scroll_filter=target,
                    with_payload=["chunk_id", "incomplete"],
                    with_vectors=False,
                    limit=_SCROLL_PAGE,
                    offset=offset,
                )
                held.update(
                    {
                        str(p.payload["chunk_id"]): incomplete_of_json(p.payload.get("incomplete"))
                        for p in points
                        if p.payload
                    }
                )
                if offset is None:
                    span.set_attribute(
                        "retrieval.incomplete_chunks", sorted(c for c, i in held.items() if i)
                    )
                    return held

    async def revise_chunks(
        self, authorization: RevisionAuthorization, chunks: tuple[Chunk, ...]
    ) -> None:
        """Read each point's stored payload, then write the point again under
        its own id with that payload, the new text and incompleteness, and
        vectors embedded from the new text: one upsert per point, so a point
        never holds new text beside old vectors. A chunk whose text is the
        stored text only takes its incompleteness, by `set_payload`, with no
        embedding: its vectors are already its text's (ADR-0047). Every
        check, and the embedding, before the first write (ADR-0042)."""
        if not chunks:
            return
        with telemetry.span(
            "qdrant.revise_chunks",
            **{
                "retrieval.revision_id": authorization.record.revision_id,
                "retrieval.revised_chunks": [c.chunk_id for c in chunks],
            },
        ) as span:
            for chunk in chunks:
                authorization.check_form(chunk.clause_id, chunk.text, chunk.incomplete)
            stored = await self._client.retrieve(
                self._collection_name,
                ids=[_chunk_point_id(c.chunk_id) for c in chunks],
                with_payload=True,
                with_vectors=False,
            )
            payloads: dict[str, dict[str, object]] = {
                str(p.payload["chunk_id"]): dict(p.payload) for p in stored if p.payload
            }
            for chunk in chunks:
                payload = payloads.get(chunk.chunk_id)
                if payload is None or (payload.get("clause_id"), payload.get("tenant_id")) != (
                    chunk.clause_id,
                    authorization.tenant_id,
                ):
                    raise PermissionError(
                        f"{chunk.chunk_id} isn't stored as this version's library law: a "
                        "correction rewords a stored chunk, never adds one"
                    )
            retexted = tuple(c for c in chunks if payloads[c.chunk_id].get("text") != c.text)
            restated = tuple(c for c in chunks if c not in retexted)
            span.set_attribute("retrieval.reembedded_chunks", [c.chunk_id for c in retexted])
            points: list[models.PointStruct] = []
            if retexted:
                dense_vectors = await self._embedder.embed(tuple(c.text for c in retexted))
                sparse_vectors = _encode_documents(c.text for c in retexted)
                points = [
                    models.PointStruct(
                        id=_chunk_point_id(chunk.chunk_id),
                        vector={DENSE_VECTOR_NAME: list(dense), SPARSE_VECTOR_NAME: sparse},
                        payload={
                            **payloads[chunk.chunk_id],
                            "text": chunk.text,
                            "incomplete": incomplete_json(chunk.incomplete),
                        },
                    )
                    for chunk, dense, sparse in zip(
                        retexted, dense_vectors, sparse_vectors, strict=True
                    )
                ]
            for start in range(0, len(points), UPSERT_BATCH):
                await self._client.upsert(
                    self._collection_name, points=points[start : start + UPSERT_BATCH]
                )
            for chunk in restated:
                await self._client.set_payload(
                    self._collection_name,
                    payload={"incomplete": incomplete_json(chunk.incomplete)},
                    points=[_chunk_point_id(chunk.chunk_id)],
                )

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
            incomplete=incomplete_of_json(data.get("incomplete")),
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
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> RetrievalResult:
        query_filter = await self._build_filter(
            tenant_id=tenant_id,
            scopes=scopes,
            as_of=as_of,
            law=law,
            policy_corpus=policy_corpus,
            user_id=user_id,
        )
        (dense_vector,) = await self._embedder.embed((query,))
        queries: dict[Channel, list[float] | models.SparseVector] = {
            Channel.DENSE: list(dense_vector),
            Channel.SPARSE: _encode_query(query),
        }

        # Qdrant scores each channel; the order among equal scores, which
        # decides who enters a channel's list and at which rank, and so the
        # fused scores, is ours (`fusion`, #177, ADR-0046).
        with telemetry.span(
            "qdrant.search",
            query=query,
            **{"retrieval.collection": self._collection_name, "retrieval.limit": limit},
        ) as current:
            read = await asyncio.gather(
                *(
                    self._channel(channel, vector, query_filter=query_filter, limit=limit)
                    for channel, vector in queries.items()
                )
            )
            per_channel: dict[Channel, tuple[Candidate, ...]] = {}
            # Both channels are logged before fusion (CLAUDE.md), with how
            # deep each was read to settle a tie at its cut.
            for channel, (candidates, depth) in zip(queries, read, strict=True):
                per_channel[channel] = candidates
                current.set_attribute(f"retrieval.channel.{channel.value}", scored(candidates))
                current.set_attribute(f"retrieval.channel_depth.{channel.value}", depth)
            fused = reciprocal_rank_fusion(per_channel, limit)
            current.set_attribute("retrieval.fused", scored(fused))
        return RetrievalResult(fused=fused, per_channel=per_channel)

    async def _channel(
        self,
        channel: Channel,
        query: list[float] | models.SparseVector,
        *,
        query_filter: models.Filter,
        limit: int,
    ) -> tuple[tuple[Candidate, ...], int]:
        """One channel's first `limit` candidates in `(-score, chunk_id)` order
        (`fusion.cut`), and how many Qdrant was asked for to get them.

        Qdrant returns equal scores in an order of its own, and at a cut keeps
        whichever of them it likes. So the channel is read past the cut, one
        more than `limit` and then twice as deep each time, until the last
        candidate read scores below the one at the cut, or the channel runs
        out. Every candidate tied with the one at the cut is then in hand, and
        the cut keeps the lowest `chunk_id`s. One extra point's payload when
        nothing ties at the cut, which is nearly always."""
        if limit < 1:
            return (), 0
        depth = limit + 1
        while True:
            hits = (
                await self._client.query_points(
                    self._collection_name,
                    query=query,
                    using=_VECTOR_NAMES[channel],
                    query_filter=query_filter,
                    limit=depth,
                    with_payload=True,
                )
            ).points
            if len(hits) < depth or hits[-1].score < hits[limit - 1].score:
                break
            depth *= 2
        candidates = (
            self._payload_to_candidate(p.payload, score=p.score, channel=channel) for p in hits
        )
        return cut(candidates, limit), depth

    async def lookup_clause(
        self,
        clause_id: str,
        *,
        law: LawScope,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> Candidate | None:
        scroll_filter = await self._build_filter(
            tenant_id=tenant_id,
            scopes=scopes,
            as_of=as_of,
            law=law,
            policy_corpus=policy_corpus,
            user_id=user_id,
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

    async def lookup_lineage(
        self,
        clause_ids: tuple[str, ...],
        *,
        law: LawScope,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> tuple[Candidate, ...]:
        """One filtered scroll (ADR-0045), through the same `_build_filter` as
        every other read: a clause whose `clause_id` is an ancestor of one of
        `clause_ids`, or whose `ancestors` hold one of them. A point stored
        before `ancestors` existed is found inside nothing: narrower, never
        wider."""
        given = sorted(set(clause_ids))
        containing = sorted({a for clause_id in given for a in clause_ancestors(clause_id)})
        # An empty MatchAny isn't "match nothing" in every Qdrant version, so
        # an empty side is left out, and with none there is nothing to read.
        related: list[models.Condition] = []
        if containing:
            related.append(
                models.FieldCondition(key="clause_id", match=models.MatchAny(any=containing))
            )
        if given:
            related.append(models.FieldCondition(key="ancestors", match=models.MatchAny(any=given)))
        if not related:
            return ()
        scroll_filter = await self._build_filter(
            tenant_id=tenant_id,
            scopes=scopes,
            as_of=as_of,
            law=law,
            policy_corpus=policy_corpus,
            user_id=user_id,
            extra=[models.Filter(should=related)],
        )
        found: list[Candidate] = []
        offset: PointId | None = None
        while True:
            records, offset = await self._client.scroll(
                self._collection_name,
                scroll_filter=scroll_filter,
                limit=_SCROLL_PAGE,
                offset=offset,
                with_payload=True,
            )
            found += [
                self._payload_to_candidate(r.payload, score=1.0, channel=Channel.KEYWORD)
                for r in records
            ]
            if offset is None:
                return tuple(sorted(found, key=lambda c: (c.clause_id, c.chunk_id)))
