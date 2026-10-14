"""`QdrantRetrievalStore` against a real Qdrant server (issue #22).

Where the cross-tenant/cross-scope leakage test actually runs, stated
plainly: a **self-hosted, ephemeral, CI-only** Qdrant container
(`qdrant/qdrant`, `.github/workflows/ci.yml`'s `ci` job — same pattern as
the Postgres service already there), not the real Qdrant Cloud free-tier
cluster. No `QDRANT_URL`/`QDRANT_API_KEY` secrets, no throwaway collection
on the shared dev cluster to tear down, no cloud account touched at all —
just the real `qdrant-client` wire protocol against a real Qdrant server,
which is what actually exercises the adapter's filter-building and payload
indexes. `FakeEmbedder` (hash-bucket, deterministic, free) stands in for a
real `Embedder` here — this test is about the retrieval store's own
behaviour, not the embedding call, so no Vertex credentials or cost enter
into it either.

One thing this setup genuinely does NOT test: Qdrant Cloud's server-side
"cloud inference" sparse encoding. `QdrantRetrievalStore` doesn't call it —
see its own module docstring for why — so there is nothing to exercise
here or anywhere else in CI; the code path this test proves is real.

Self-skips when `QDRANT_URL` isn't set or isn't reachable, same pattern
`tests/test_ledger_schema.py` uses for a missing `DATABASE_URL`.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest

from compliance_rag.adapters.fakes import FakeEmbedder, FakeLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import (
    QdrantRetrievalStore,
    _chunk_point_id,  # pyright: ignore[reportPrivateUsage] — addresses one point directly
)
from compliance_rag.corpus.models import Chunk, Scope
from tests.conftest import ALL_LAW, qdrant_reachable

TODAY = dt.date(2026, 1, 1)
LATER = dt.date(2027, 6, 1)

APP11 = "privacy-act-1988:TEST-FIXTURE-001:sch1/app11/1"
APP12 = "privacy-act-1988:TEST-FIXTURE-001:sch1/app12/1"

_QDRANT_URL = os.environ.get("QDRANT_URL", "")


pytestmark = pytest.mark.skipif(
    not qdrant_reachable(_QDRANT_URL),
    reason="QDRANT_URL not set or not reachable — this test needs a real Qdrant server "
    "(CI's self-hosted service container, or `docker run -p 6333:6333 qdrant/qdrant`, "
    "with QDRANT_URL=http://localhost:6333 locally)",
)


@pytest.fixture
def ledger() -> FakeLedgerStore:
    """Subscriptions are the ledger's; this test is about the retrieval
    store's enforcement of them, so the in-memory ledger stands in (there is
    no Postgres `LedgerStore` adapter yet — the subscription DDL itself is
    proven against real Postgres in `tests/test_ledger_schema.py`)."""
    return FakeLedgerStore()


@pytest.fixture
async def store(ledger: FakeLedgerStore) -> QdrantRetrievalStore:
    """A fresh, uniquely-named collection per test run — never reused, so
    tests can't see each other's data and there is nothing to reset between
    runs."""
    from qdrant_client import AsyncQdrantClient

    client = AsyncQdrantClient(url=_QDRANT_URL)
    collection_name = f"clauses-contract-test-{uuid.uuid4().hex[:8]}"
    embedder = FakeEmbedder(dimensions=8)
    retrieval_store = QdrantRetrievalStore(
        client, collection_name=collection_name, embedder=embedder, ledger=ledger
    )
    await retrieval_store.ensure_ready()
    await retrieval_store.upsert_chunks(
        (
            Chunk(
                chunk_id="c1",
                clause_id=APP11,
                text="An APP entity must take reasonable steps to protect the information.",
            ),
            Chunk(
                chunk_id="c2",
                clause_id=APP12,
                text="An APP entity must give the individual access to the information.",
            ),
            Chunk(
                chunk_id="c3",
                clause_id="acme-policy:v3:sec2/1",
                text="Acme retains customer records for seven years to protect the information.",
                scope=Scope.TENANT,
                tenant_id="acme",
            ),
        )
    )
    return retrieval_store


async def test_qdrant_satisfies_the_revise_chunks_contract(store: QdrantRetrievalStore) -> None:
    """A parse correction rewrites a stored point's text and vectors under its
    own id, and nothing else (ADR-0042)."""
    from tests.test_corpus_revision import assert_revise_chunks_contract

    await assert_revise_chunks_contract(store)


async def test_qdrant_retrieval_store_satisfies_the_shared_contract(
    store: QdrantRetrievalStore, ledger: FakeLedgerStore
) -> None:
    from tests.contracts.test_interfaces import assert_retrieval_store_contract

    await assert_retrieval_store_contract(store, ledger)


async def test_qdrant_orders_equal_scores_by_chunk_id() -> None:
    """#160 on the real adapter. Two chunks the channels rank in mirror image
    get exactly equal RRF scores, and Qdrant returns them in either order from
    one call to the next, and keeps either at a cut that falls between them:
    before the fix this failed. The expected order is asserted, not just that
    two calls agree."""
    from tests.contracts.test_interfaces import (
        ORDER_VECTORS,
        ScriptedEmbedder,
        assert_retrieval_order_contract,
        order_chunks,
    )

    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL,
        embedder=ScriptedEmbedder(ORDER_VECTORS),
        ledger=FakeLedgerStore(),
        prefix="order",
    ) as store:
        await store.upsert_chunks(order_chunks())
        await assert_retrieval_order_contract(store)


async def test_qdrant_satisfies_the_corpus_lifecycle_contract(ledger: FakeLedgerStore) -> None:
    """ADR-0021 against the real adapter. "Stored" is read straight off the
    collection through the raw client, ignoring every filter — so "withdrawn
    but retained" and "purged" are about Qdrant's actual points, not about
    what the adapter chooses to show."""
    from qdrant_client import AsyncQdrantClient

    from compliance_rag.adapters.fakes import FakeClauseStore
    from tests.contracts.test_corpus_lifecycle import assert_corpus_lifecycle_contract

    client = AsyncQdrantClient(url=_QDRANT_URL)
    name = f"clauses-lifecycle-{uuid.uuid4().hex[:8]}"
    store = QdrantRetrievalStore(
        client, collection_name=name, embedder=FakeEmbedder(dimensions=8), ledger=ledger
    )
    await store.ensure_ready()

    async def stored_chunk_ids() -> frozenset[str]:
        points, _ = await client.scroll(name, limit=1000, with_payload=True)
        return frozenset(str((p.payload or {})["chunk_id"]) for p in points)

    async def seed(tenant_id: str, clause_id: str) -> None:
        ledger.seed_finding(tenant_id, clause_id)

    await assert_corpus_lifecycle_contract(
        store, store, ledger, FakeClauseStore(), stored_chunk_ids, seed
    )


async def test_instrument_id_index_is_added_to_a_collection_created_before_it(
    ledger: FakeLedgerStore,
) -> None:
    """A collection that predates the `instrument_id` index (ADR-0020) gets
    it on the next `ensure_ready`, without being re-created or emptied."""
    from qdrant_client import AsyncQdrantClient, models

    client = AsyncQdrantClient(url=_QDRANT_URL)
    name = f"clauses-legacy-{uuid.uuid4().hex[:8]}"
    await client.create_collection(
        name,
        vectors_config={"dense": models.VectorParams(size=8, distance=models.Distance.COSINE)},
        sparse_vectors_config={"sparse": models.SparseVectorParams()},
    )
    await client.upsert(
        name,
        points=[models.PointStruct(id=1, vector={"dense": [0.1] * 8}, payload={"keep": True})],
    )
    store = QdrantRetrievalStore(
        client, collection_name=name, embedder=FakeEmbedder(dimensions=8), ledger=ledger
    )
    await store.ensure_ready()
    info = await client.get_collection(name)
    # ADR-0020, ADR-0035, ADR-0036
    assert {"instrument_id", "ancestors", "user_id"} <= set(info.payload_schema)
    assert info.points_count == 1


async def test_bitemporal_filter_is_one_unconditional_range_both_sides(
    store: QdrantRetrievalStore,
) -> None:
    """CONTEXT.md: Bitemporal — `valid_to` carries `NO_END_DATE`, never
    null, so a chunk with an explicit expiry is excluded once `as_of` is
    past it and visible before, with no is-null branch anywhere in the
    filter (this session's fix, issue #22)."""
    await store.upsert_chunks(
        (
            Chunk(
                chunk_id="c4",
                clause_id="privacy-act-1988:TEST-FIXTURE-001:sch1/app13/1",
                text="A repealed clause about direct marketing opt-outs.",
                valid_from=dt.date(2020, 1, 1),
                valid_to=dt.date(2024, 1, 1),
            ),
        )
    )
    before_expiry = await store.search(
        "direct marketing opt-outs",
        law=ALL_LAW,
        tenant_id="_public",
        scopes=(Scope.PUBLIC,),
        as_of=dt.date(2022, 1, 1),
        limit=10,
    )
    after_expiry = await store.search(
        "direct marketing opt-outs",
        law=ALL_LAW,
        tenant_id="_public",
        scopes=(Scope.PUBLIC,),
        as_of=dt.date(2026, 1, 1),
        limit=10,
    )
    assert any(c.chunk_id == "c4" for c in before_expiry.fused)
    assert all(c.chunk_id != "c4" for c in after_expiry.fused)


async def test_the_residency_gate_query_on_real_qdrant() -> None:
    """ADR-0023's gate asks the store whether it holds real tenant data. On
    real Qdrant: law and synthetic tenant chunks don't count, an unmarked
    tenant chunk does, and so does a point stored before the `synthetic`
    field existed — the filter is must_not synthetic=true, fail closed."""
    from qdrant_client import AsyncQdrantClient

    def tenant(chunk_id: str, *, synthetic: bool) -> Chunk:
        return Chunk(
            chunk_id=chunk_id,
            clause_id=f"acme/p:v1:{chunk_id}",
            text="Acme keeps records.",
            scope=Scope.TENANT,
            tenant_id="acme",
            synthetic=synthetic,
        )

    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="gate"
    ) as store:
        await store.upsert_chunks(
            (Chunk(chunk_id="law", clause_id=APP11, text="law"), tenant("s1", synthetic=True))
        )
        assert not await store.holds_real_customer_data()

        # A legacy point: written synthetic, then the field removed as if it
        # predated the flag. It must count as real.
        await store.upsert_chunks((tenant("s2", synthetic=True),))
        client = AsyncQdrantClient(url=_QDRANT_URL)
        collection = store._collection_name  # pyright: ignore[reportPrivateUsage]
        await client.delete_payload(collection, keys=["synthetic"], points=[_chunk_point_id("s2")])
        await client.close()
        assert await store.holds_real_customer_data()

    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="gate"
    ) as store:
        await store.upsert_chunks((tenant("r1", synthetic=False),))
        assert await store.holds_real_customer_data()

    # One person's private addition is customer data too (#166).
    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="gate"
    ) as store:
        mine = tenant("u1", synthetic=False).model_copy(
            update={"scope": Scope.USER, "user_id": "alice"}
        )
        await store.upsert_chunks((mine,))
        assert await store.holds_real_customer_data()


async def test_a_version_larger_than_one_upsert_request_is_stored_whole_and_listed() -> None:
    """#144: a compilation is written in batches of `UPSERT_BATCH`, and
    `version_chunk_ids` lists exactly one version's chunks, across scroll
    pages, so a load can tell finished from partial from a different set."""
    from compliance_rag.adapters.qdrant_retrieval_store import UPSERT_BATCH

    many = 2 * UPSERT_BATCH + 3
    ids = [f"privacy-act-1988:C9:s{n}/1" for n in range(many)]
    version = tuple(Chunk(chunk_id=i, clause_id=i, text=f"section {i}") for i in ids)
    other = Chunk(chunk_id=APP11, clause_id=APP11, text="another version of the same act")
    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="ids"
    ) as store:
        await store.upsert_chunks((*version, other))

        async def stored(version_id: str) -> frozenset[str]:
            return await store.version_chunk_ids(
                instrument_id="privacy-act-1988", tenant_id="_public", version_id=version_id
            )

        assert await stored("C9") == frozenset(ids)
        assert await stored("TEST-FIXTURE-001") == {APP11}
        assert await stored("C10") == frozenset()
        assert await store.version_exists(
            instrument_id="privacy-act-1988", tenant_id="_public", version_id="C9"
        )


# ---- user scope is private to its owner (ADR-0036, #171) --------------------------


async def test_qdrant_keeps_user_scope_private_to_its_owner(ledger: FakeLedgerStore) -> None:
    """The cross-member counterpart of the cross-tenant leakage test, on the
    real adapter: alice's private chunk reaches alice and no one else — not bob
    of her tenant, not another tenant's member, not a request with no member,
    not alice's own uid in another tenant — through the dense channel, the
    sparse channel, the fused list, the exact-id lookup and the router's two
    paths, each with alice as the positive control."""
    from tests.contracts.test_user_scope import assert_user_scope_is_private, user_scope_chunks

    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=ledger, prefix="user-scope"
    ) as store:
        await store.upsert_chunks(user_scope_chunks())
        await assert_user_scope_is_private(store)


async def test_the_owner_is_stored_on_a_user_scoped_point_and_on_no_other() -> None:
    from qdrant_client import AsyncQdrantClient

    from tests.contracts.test_user_scope import ALICE, ALICE_NOTE, POLICY

    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="owner"
    ) as store:
        await store.upsert_chunks((ALICE_NOTE, POLICY))
        client = AsyncQdrantClient(url=_QDRANT_URL)
        points, _ = await client.scroll(
            store._collection_name,  # pyright: ignore[reportPrivateUsage]
            limit=10,
            with_payload=True,
        )
        await client.close()
    owners = {str((p.payload or {})["chunk_id"]): (p.payload or {})["user_id"] for p in points}
    assert owners == {ALICE_NOTE.chunk_id: ALICE, POLICY.chunk_id: None}


async def test_a_user_scoped_point_with_no_owner_in_its_payload_is_visible_to_no_one() -> None:
    """A point written before `user_id` existed (or by hand) with scope `user`
    names no owner, so it fails closed: no member, not even the one who wrote
    it, reaches it by any route. The same point with its owner is reached, so
    the check is not passing because nothing is stored."""
    from qdrant_client import AsyncQdrantClient

    from tests.contracts.test_user_scope import ALICE, ALICE_NOTE, VIEWERS, reach

    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="legacy"
    ) as store:
        await store.upsert_chunks((ALICE_NOTE,))
        owner = next(v for v in VIEWERS if v.user_id == ALICE and v.tenant_id == "acme")
        every = (Scope.PUBLIC, Scope.TENANT, Scope.USER)
        with_owner = await reach(store, owner, law=ALL_LAW, scopes=every)
        assert all(found == {ALICE_NOTE.chunk_id} for found in with_owner.values())

        client = AsyncQdrantClient(url=_QDRANT_URL)
        collection = store._collection_name  # pyright: ignore[reportPrivateUsage]
        await client.delete_payload(
            collection, keys=["user_id"], points=[_chunk_point_id(ALICE_NOTE.chunk_id)]
        )
        await client.close()
        for viewer in VIEWERS:
            without_owner = await reach(store, viewer, law=ALL_LAW, scopes=every)
            assert all(not found for found in without_owner.values()), (viewer.name, without_owner)


async def test_qdrant_stores_what_the_write_path_publishes_for_its_owner_alone() -> None:
    """`CorpusLifecycle` carries `user_id` through to the payload: a private
    addition published like any other document is alice's by every route on
    the real adapter, and no one else's."""
    from tests.contracts.test_user_scope import assert_a_published_private_addition_is_private

    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="publish"
    ) as store:
        await assert_a_published_private_addition_is_private(store, store)


async def test_the_user_id_index_is_added_to_a_collection_that_lacks_only_it() -> None:
    """A deployed collection already has every other index (ADR-0020,
    ADR-0035): `ensure_ready` adds just `user_id`, without touching the data."""
    from qdrant_client import AsyncQdrantClient

    from tests.contracts.test_user_scope import ALICE_NOTE

    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="index"
    ) as store:
        await store.upsert_chunks((ALICE_NOTE,))
        client = AsyncQdrantClient(url=_QDRANT_URL)
        collection = store._collection_name  # pyright: ignore[reportPrivateUsage]
        await client.delete_payload_index(collection, "user_id")
        assert "user_id" not in (await client.get_collection(collection)).payload_schema
        await store.ensure_ready()
        info = await client.get_collection(collection)
        await client.close()
    assert "user_id" in info.payload_schema and info.points_count == 1
