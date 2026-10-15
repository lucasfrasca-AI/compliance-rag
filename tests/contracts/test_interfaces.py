"""Contract test per interface, run against the in-memory fakes (D-08).

Each test asserts the promise the interface makes, not the fake's mechanics:
traversal is capped and `as_of`-scoped (D-05), Track B never escapes Track A's
filter (D-03), a bad citation fails the verifier (D-02).
"""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from compliance_rag.adapters.fakes import (
    FakeCitationVerifier,
    FakeClauseStore,
    FakeEmbedder,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.fusion import RRF_K
from compliance_rag.adapters.interfaces import (
    AssessmentRecord,
    Candidate,
    Channel,
    CitationVerifier,
    ClauseStore,
    Embedder,
    Generator,
    LawScope,
    LedgerStore,
    Profile,
    Reranker,
    RetrievalAdmin,
    RetrievalResult,
    RetrievalStore,
    SourceKind,
    SourceVersion,
)
from compliance_rag.corpus.models import (
    CANONICALISATION_VERSION,
    Chunk,
    ClauseRecord,
    Scope,
    canonical_sha256,
)
from tests.conftest import ALL_LAW, PUBLIC_AND_TENANT

TODAY = dt.date(2026, 1, 1)
LATER = dt.date(2027, 6, 1)

LIBRARY_INSTRUMENT = "privacy-act-1988"
APP11 = f"{LIBRARY_INSTRUMENT}:TEST-FIXTURE-001:sch1/app11/1"
APP12 = f"{LIBRARY_INSTRUMENT}:TEST-FIXTURE-001:sch1/app12/1"
LIBRARY_CHUNK_IDS = frozenset({"c1", "c2"})  # the `chunks` fixture's public-scope chunks


@pytest.fixture
def chunks() -> tuple[Chunk, ...]:
    return contract_chunks()


def contract_chunks() -> tuple[Chunk, ...]:
    """The retrieval contract's seed records — a plain function as well as
    the `chunks` fixture, so other backends' tests can seed the same set."""
    return (
        Chunk(
            chunk_id="c1",
            clause_id=APP11,
            text="An APP entity must take such steps as are reasonable to protect the information.",
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


# --------------------------------------------------------------------------
# GraphStore: its contract runs on the fake and on Neo4j in
# tests/contracts/test_graph_store.py
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# RetrievalStore
# --------------------------------------------------------------------------


async def assert_retrieval_store_contract(store: RetrievalStore, ledger: LedgerStore) -> None:
    """The `RetrievalStore` contract itself, against a store already seeded
    with the `chunks` fixture's three records and reading subscriptions from
    `ledger` (D-08). Shared by `FakeRetrievalStore` here and by
    `QdrantRetrievalStore` against a real, self-hosted, CI-only Qdrant server
    (`tests/test_qdrant_retrieval_store.py`, issue #22) — one definition of
    the contract, not two independently written tests that could silently
    diverge, same reasoning as `assert_reranker_contract` below.
    """
    assert isinstance(store, RetrievalStore)
    await ledger.subscribe(
        tenant_id="acme", instrument_id=LIBRARY_INSTRUMENT, subscribed_by="contract-test"
    )

    # D-03: library law only from Track A's clauses, whatever the text similarity.
    result = await store.search(
        "steps to protect the information",
        law=LawScope(frozenset({APP11}), "contract: Track A cites APP 11.1 only"),
        tenant_id="acme",
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
    )
    # APP 12.1 (c2) matches too but is outside Track A's set; the tenant's
    # own policy (c3) isn't law and isn't Track A's to scope (ADR-0030).
    assert {c.chunk_id for c in result.fused} == {"c1", "c3"}

    # Both channel candidate lists are logged before fusion (CLAUDE.md).
    result = await store.search(
        "access to the information",
        law=ALL_LAW,
        tenant_id="acme",
        scopes=(Scope.PUBLIC,),
        as_of=LATER,
        limit=10,
    )
    assert set(result.per_channel) == {Channel.DENSE, Channel.SPARSE}
    assert result.fused

    # D-14 / D-09: another tenant never sees a tenant-scoped chunk, even
    # when they ask for tenant-scope results at all (cross-tenant leakage).
    result = await store.search(
        "retains customer records",
        law=ALL_LAW,
        tenant_id="other-corp",
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
    )
    assert all(c.chunk_id != "c3" for c in result.fused)

    # Scope leakage: a public-only request never sees a tenant-scoped chunk,
    # even for the tenant that owns it.
    result = await store.search(
        "retains customer records",
        law=ALL_LAW,
        tenant_id="acme",
        scopes=(Scope.PUBLIC,),
        as_of=TODAY,
        limit=10,
    )
    assert all(c.chunk_id != "c3" for c in result.fused)

    # D-04 / D-12: the router's only decision, and it returns the keyword channel.
    hit = await store.lookup_clause(
        APP11, law=ALL_LAW, tenant_id="acme", scopes=PUBLIC_AND_TENANT, as_of=TODAY
    )
    assert hit is not None
    assert hit.channel is Channel.KEYWORD
    assert hit.clause_id == APP11

    # Cross-tenant lookup: acme can never look up beta's clause by ID either,
    # not just by ranked search.
    hit = await store.lookup_clause(
        "acme-policy:v3:sec2/1",
        law=ALL_LAW,
        tenant_id="other-corp",
        scopes=PUBLIC_AND_TENANT,
        as_of=TODAY,
    )
    assert hit is None

    # ADR-0020. Positive control first: with the same probe, a subscribed
    # tenant gets every library chunk in every channel — so the "unreachable"
    # assertions below can't pass just because a channel never returns them.
    reached = await _library_reach(store, tenant_id="acme")
    assert all(found == LIBRARY_CHUNK_IDS for found in reached.values()), reached

    # A tenant that never subscribed can't reach them through any channel.
    await assert_library_unreachable(store, tenant_id="other-corp")

    # `_public` owns every library chunk: it sees the library with no rows.
    result = await store.search(
        "steps to protect the information",
        law=ALL_LAW,
        tenant_id="_public",
        scopes=(Scope.PUBLIC,),
        as_of=TODAY,
        limit=10,
    )
    assert "c1" in {c.chunk_id for c in result.fused}

    # Unsubscribe removes the row and nothing else: acme loses the library
    # through every channel but keeps its own documents...
    await ledger.unsubscribe(tenant_id="acme", instrument_id=LIBRARY_INSTRUMENT)
    await assert_library_unreachable(store, tenant_id="acme")
    own = await store.search(
        "retains customer records",
        law=ALL_LAW,
        tenant_id="acme",
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
    )
    assert "c3" in {c.chunk_id for c in own.fused}
    # ...and another tenant's view of the shared library is untouched.
    still_public = await store.lookup_clause(
        APP11, law=ALL_LAW, tenant_id="_public", scopes=PUBLIC_AND_TENANT, as_of=TODAY
    )
    assert still_public is not None

    # Resubscribing is another row, with nothing re-upserted: the same
    # stored points become visible again.
    await ledger.subscribe(
        tenant_id="acme", instrument_id=LIBRARY_INSTRUMENT, subscribed_by="contract-test"
    )
    hit = await store.lookup_clause(
        APP11, law=ALL_LAW, tenant_id="acme", scopes=PUBLIC_AND_TENANT, as_of=TODAY
    )
    assert hit is not None
    assert hit.clause_id == APP11


async def _library_reach(store: RetrievalStore, *, tenant_id: str) -> dict[str, frozenset[str]]:
    """Which of the `chunks` fixture's library chunks reach `tenant_id` by
    each of the four routes a chunk can take to a caller: the dense and
    sparse channels, the fused list, and an exact-ID lookup."""
    result = await store.search(
        "An APP entity must take steps to protect and give access to the information",
        law=ALL_LAW,
        tenant_id=tenant_id,
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=LATER,
        limit=10,
    )

    def library(candidates: tuple[Candidate, ...]) -> frozenset[str]:
        return frozenset(c.chunk_id for c in candidates) & LIBRARY_CHUNK_IDS

    looked_up = [
        await store.lookup_clause(
            clause_id, law=ALL_LAW, tenant_id=tenant_id, scopes=PUBLIC_AND_TENANT, as_of=LATER
        )
        for clause_id in (APP11, APP12)
    ]
    return {
        Channel.DENSE.value: library(result.per_channel.get(Channel.DENSE, ())),
        Channel.SPARSE.value: library(result.per_channel.get(Channel.SPARSE, ())),
        "fused": library(result.fused),
        Channel.KEYWORD.value: frozenset(h.chunk_id for h in looked_up if h is not None),
    }


async def assert_library_unreachable(store: RetrievalStore, *, tenant_id: str) -> None:
    reached = await _library_reach(store, tenant_id=tenant_id)
    assert all(not found for found in reached.values()), reached


LINEAGE_VERSION = f"{LIBRARY_INSTRUMENT}:LINEAGE-V1"
LINEAGE_IN_FORCE = dt.date(2025, 7, 1)
LINEAGE = {
    "sch1/app11/1": "the entity must take such steps to protect the information:",
    "sch1/app11/1/a": "from misuse, interference and loss; and",
    "sch1/app11/1/a/i": "a subparagraph, for depth",
    "sch1/app11/1/b": "from unauthorised access, modification or disclosure.",
    "sch1/app11/10": "a tenth subclause: shares a prefix, not a path",
}


def lineage_chunks() -> tuple[Chunk, ...]:
    """Seed for `assert_lookup_lineage_contract`: a chapeau, its paragraphs and
    a subparagraph; a clause whose path shares a prefix with the chapeau's and
    is not inside it; the chapeau's own path in another version; and a
    tenant's nested clauses."""
    library = tuple(
        Chunk(
            chunk_id=f"{LINEAGE_VERSION}:{p}",
            clause_id=f"{LINEAGE_VERSION}:{p}",
            text=t,
            valid_from=LINEAGE_IN_FORCE,
        )
        for p, t in LINEAGE.items()
    )
    other_version = Chunk(
        chunk_id=f"{LIBRARY_INSTRUMENT}:LINEAGE-V2:sch1/app11/1/a",
        clause_id=f"{LIBRARY_INSTRUMENT}:LINEAGE-V2:sch1/app11/1/a",
        text="the same paragraph in a later compilation",
    )
    tenant = tuple(
        Chunk(
            chunk_id=f"acme-policy:v3:{p}",
            clause_id=f"acme-policy:v3:{p}",
            text=f"Acme's own clause {p}",
            scope=Scope.TENANT,
            tenant_id="acme",
        )
        for p in ("sec5", "sec5/1")
    )
    return (*library, other_version, *tenant)


async def assert_lookup_lineage_contract(store: RetrievalStore, admin: RetrievalAdmin) -> None:
    """`RetrievalStore.lookup_lineage` (ADR-0045): the clauses containing one
    of the given clauses or inside one, by path segment, within its own
    instrument version, through every filter a lookup applies. Shared by the
    fake here and `QdrantRetrievalStore` (`tests/test_qdrant_retrieval_store.py`)."""
    await admin.upsert_chunks(lineage_chunks())

    def at(path: str) -> str:
        return f"{LINEAGE_VERSION}:{path}"

    async def lineage(*clause_ids: str, law: LawScope = ALL_LAW, **kw: object) -> list[str]:
        options: dict[str, object] = {
            "tenant_id": "_public",
            "scopes": (Scope.PUBLIC,),
            "as_of": TODAY,
        } | kw
        found = await store.lookup_lineage(clause_ids, law=law, **options)  # pyright: ignore[reportArgumentType]
        assert all(c.channel is Channel.KEYWORD for c in found)
        return [c.clause_id for c in found]

    # Inside: every depth, in clause_id order; never a prefix that isn't a path.
    assert await lineage(at("sch1/app11/1")) == [
        at("sch1/app11/1/a"),
        at("sch1/app11/1/a/i"),
        at("sch1/app11/1/b"),
    ]
    # Containing and inside at once; a sibling is neither.
    assert await lineage(at("sch1/app11/1/a")) == [at("sch1/app11/1"), at("sch1/app11/1/a/i")]
    # A given id comes back only as the lineage of another.
    assert await lineage(at("sch1/app11/1"), at("sch1/app11/1/b")) == [
        at("sch1/app11/1"),
        at("sch1/app11/1/a"),
        at("sch1/app11/1/a/i"),
        at("sch1/app11/1/b"),
    ]
    assert await lineage() == []
    # Every filter a lookup applies: the law scope (D-03), the asker's scopes
    # and tenant (D-14, ADR-0020), and validity.
    cited_b = LawScope(frozenset({at("sch1/app11/1/b")}), "contract: Track A cites (b) only")
    assert await lineage(at("sch1/app11/1"), law=cited_b) == [at("sch1/app11/1/b")]
    assert await lineage(at("sch1/app11/1"), tenant_id="other-corp") == []
    assert await lineage(at("sch1/app11/1"), as_of=LINEAGE_IN_FORCE - dt.timedelta(days=1)) == []
    assert (
        await lineage("acme-policy:v3:sec5", tenant_id="other-corp", scopes=PUBLIC_AND_TENANT) == []
    )
    assert await lineage("acme-policy:v3:sec5", tenant_id="acme", scopes=PUBLIC_AND_TENANT) == [
        "acme-policy:v3:sec5/1"
    ]


async def test_fake_retrieval_store_satisfies_the_lookup_lineage_contract() -> None:
    store = FakeRetrievalStore(ledger=FakeLedgerStore())
    await assert_lookup_lineage_contract(store, store)


async def test_retrieval_store_is_a_retrieval_store(chunks: tuple[Chunk, ...]) -> None:
    assert isinstance(FakeRetrievalStore(chunks, ledger=FakeLedgerStore()), RetrievalStore)


async def test_fake_retrieval_store_satisfies_the_shared_contract(
    chunks: tuple[Chunk, ...],
) -> None:
    ledger = FakeLedgerStore()
    await assert_retrieval_store_contract(FakeRetrievalStore(chunks, ledger=ledger), ledger)


# --------------------------------------------------------------------------
# RetrievalStore: the order of `fused` (#160)
# --------------------------------------------------------------------------

# The three chunks of the ordering scenario, named by how the channels rank
# them. `SPARSE_LED` and `DENSE_LED` are mirror images: the dense channel ranks
# `DENSE_LED` 2nd and `SPARSE_LED` 3rd, the sparse channel the other way round,
# so RRF gives them exactly equal fused scores. `chunk_id` order puts
# `SPARSE_LED` first.
BEST_IN_BOTH = "k-best-in-both"
SPARSE_LED = "l-sparse-led"
DENSE_LED = "m-dense-led"

ORDER_QUERY = "zeta eta"
ORDER_VECTORS: dict[str, tuple[float, ...]] = {
    ORDER_QUERY: (1.0, 0.0, 0.0, 0.0),  # also BEST_IN_BOTH's text: cosine 1.0
    "zeta alpha": (0.98, 0.2, 0.0, 0.0),  # DENSE_LED: cosine 0.98
    "zeta eta beta": (0.8, 0.6, 0.0, 0.0),  # SPARSE_LED: cosine 0.8
}
"""The dense vector of the query and of each chunk's text. Sparse (BM25) needs
no script: `BEST_IN_BOTH`'s text is the query itself, `SPARSE_LED`'s adds a
word to it, and `DENSE_LED`'s shares only one of its two terms."""


def order_chunks() -> tuple[Chunk, ...]:
    """Stored in the opposite order to `chunk_id`, so insertion order can't
    stand in for the order under test."""
    return (
        Chunk(chunk_id=DENSE_LED, clause_id=APP11, text="zeta alpha"),
        Chunk(chunk_id=SPARSE_LED, clause_id=APP12, text="zeta eta beta"),
        Chunk(
            chunk_id=BEST_IN_BOTH,
            clause_id=f"{LIBRARY_INSTRUMENT}:TEST-FIXTURE-001:sch1/app13/1",
            text=ORDER_QUERY,
        ),
    )


class ScriptedEmbedder(Embedder):
    """Dense vectors from a table, so a test decides which chunk the dense
    channel ranks first. A text that isn't in the table is an error."""

    def __init__(self, vectors: dict[str, tuple[float, ...]]) -> None:
        self._vectors = vectors

    @property
    def dimensions(self) -> int:
        return len(next(iter(self._vectors.values())))

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vectors[text] for text in texts)


class SplitChannelFake(FakeRetrievalStore):
    """The fake with channels that disagree, as real dense and sparse channels
    do: cosine over `ORDER_VECTORS` for dense, and for sparse the share of the
    text's tokens that are query tokens (shorter text first, like BM25). The
    stock fake's two channels both use token overlap, so they never rank
    alike-but-differently and can't produce a fused tie."""

    def _score(self, channel: Channel, query: str, text: str) -> float:
        if channel is Channel.DENSE:
            q, t = ORDER_VECTORS[query], ORDER_VECTORS[text]
            norm = (sum(x * x for x in q) * sum(x * x for x in t)) ** 0.5
            return sum(x * y for x, y in zip(q, t, strict=True)) / norm
        tokens = text.split()
        return sum(1 for token in tokens if token in query.split()) / len(tokens)


async def assert_retrieval_order_contract(store: RetrievalStore) -> None:
    """`fused` is `(-score, chunk_id)` order, cut to `limit` after that order
    is applied, and the same search returns the same list every time (#160).

    Against a store seeded with `order_chunks()` and, for dense vectors,
    `ORDER_VECTORS`. Asserts the order itself, not that two calls agree: real
    Qdrant breaks an RRF tie differently from call to call, so a test that
    only compares two calls can pass by luck."""

    async def search(limit: int) -> RetrievalResult:
        return await store.search(
            ORDER_QUERY,
            law=ALL_LAW,
            tenant_id="_public",
            scopes=(Scope.PUBLIC,),
            as_of=TODAY,
            limit=limit,
        )

    def ids(candidates: tuple[Candidate, ...]) -> list[str]:
        return [c.chunk_id for c in candidates]

    full = await search(10)

    # The scenario is what it claims to be, or this test proves nothing: the
    # channels disagree about the two mirror-image chunks, and RRF ties them.
    assert ids(full.per_channel[Channel.DENSE]) == [BEST_IN_BOTH, DENSE_LED, SPARSE_LED]
    assert ids(full.per_channel[Channel.SPARSE]) == [BEST_IN_BOTH, SPARSE_LED, DENSE_LED]
    scores = {c.chunk_id: c.score for c in full.fused}
    assert scores[SPARSE_LED] == scores[DENSE_LED] < scores[BEST_IN_BOTH]
    # "Highest first" holds for each channel too.
    for candidates in full.per_channel.values():
        assert [c.score for c in candidates] == sorted((c.score for c in candidates), reverse=True)

    # The defined order, spelled out: the best, then the tie by `chunk_id`.
    assert ids(full.fused) == [BEST_IN_BOTH, SPARSE_LED, DENSE_LED]
    assert list(full.fused) == sorted(full.fused, key=lambda c: (-c.score, c.chunk_id))

    # A tie at the cut: with limit 2 the cut falls between the two tied
    # chunks, and the one that stays is the lower `chunk_id`.
    cut = await search(2)
    assert ids(cut.fused) == [BEST_IN_BOTH, SPARSE_LED]

    # And every time: 20 repeats, so a store that breaks the tie at random
    # can't match the defined order by luck.
    for _ in range(20):
        assert await search(10) == full
        assert ids((await search(2)).fused) == [BEST_IN_BOTH, SPARSE_LED]
        assert ids((await search(1)).fused) == [BEST_IN_BOTH]


async def test_fake_retrieval_store_orders_equal_scores_by_chunk_id() -> None:
    await assert_retrieval_order_contract(
        SplitChannelFake(order_chunks(), ledger=FakeLedgerStore())
    )


# --------------------------------------------------------------------------
# RetrievalStore: a tie at a channel's own cut (#177)
# --------------------------------------------------------------------------

TIED = tuple(f"t-tied-{n}" for n in range(1, 6))
"""Five chunks with one text, so every channel scores them exactly alike, below
`BEST_IN_BOTH`: in `chunk_id` order here."""
TIED_TEXT = "zeta eta beta"  # in `ORDER_VECTORS`: cosine 0.8 to the query


def tie_chunks() -> tuple[Chunk, ...]:
    """`BEST_IN_BOTH` and the five `TIED`, stored in the opposite order to
    `chunk_id`, so insertion order can't stand in for the order under test."""
    tied = tuple(
        Chunk(
            chunk_id=chunk_id,
            clause_id=f"{LIBRARY_INSTRUMENT}:TEST-FIXTURE-001:sch1/app{n}/1",
            text=TIED_TEXT,
        )
        for n, chunk_id in enumerate(TIED, start=1)
    )
    best = Chunk(
        chunk_id=BEST_IN_BOTH,
        clause_id=f"{LIBRARY_INSTRUMENT}:TEST-FIXTURE-001:sch1/app13/1",
        text=ORDER_QUERY,
    )
    return (*reversed(tied), best)


async def assert_channel_cut_contract(store: RetrievalStore) -> None:
    """Which of a channel's tied candidates it hands to fusion, and at which
    places, is `chunk_id` order, whichever store answers (#177, ADR-0046). So
    the fused set and the fused scores are too.

    Against a store seeded with `tie_chunks()` and, for dense vectors,
    `ORDER_VECTORS`. With `limit` 3 a channel's cut falls inside the five-way
    tie, after its second member: the two it keeps are the two lowest
    `chunk_id`s, and they take the 2nd and 3rd places in that order."""

    async def search(limit: int) -> RetrievalResult:
        return await store.search(
            ORDER_QUERY,
            law=ALL_LAW,
            tenant_id="_public",
            scopes=(Scope.PUBLIC,),
            as_of=TODAY,
            limit=limit,
        )

    def ids(candidates: tuple[Candidate, ...]) -> list[str]:
        return [c.chunk_id for c in candidates]

    full = await search(10)
    for channel in (Channel.DENSE, Channel.SPARSE):
        candidates = full.per_channel[channel]
        # The scenario is what it claims to be, or this test proves nothing:
        # in each channel the five tie exactly, below the best.
        best, *tied = (c.score for c in candidates)
        assert len(tied) == len(TIED) and set(tied) == {tied[0]} and best > tied[0], channel
        # Tied candidates take their places in `chunk_id` order.
        assert ids(candidates) == [BEST_IN_BOTH, *TIED], channel

    # A fused score comes from places alone, so the five tied chunks' places,
    # `chunk_id` order in both channels, give them five different fused scores.
    assert ids(full.fused) == [BEST_IN_BOTH, *TIED]
    assert [c.score for c in full.fused] == [2 / (RRF_K + place) for place in range(6)]

    # The cut inside the tie: each channel keeps the two lowest `chunk_id`s,
    # and fusion has those two only, at the scores those places give.
    straddled = await search(3)
    for channel in (Channel.DENSE, Channel.SPARSE):
        assert ids(straddled.per_channel[channel]) == [BEST_IN_BOTH, *TIED[:2]], channel
    assert ids(straddled.fused) == [BEST_IN_BOTH, *TIED[:2]]
    assert [c.score for c in straddled.fused] == [2 / (RRF_K + place) for place in range(3)]

    # And every time, so a store that keeps a tie at random can't match by luck.
    for _ in range(10):
        assert await search(3) == straddled
        assert await search(10) == full


async def test_fake_retrieval_store_breaks_a_tie_at_a_channels_cut_by_chunk_id() -> None:
    await assert_channel_cut_contract(SplitChannelFake(tie_chunks(), ledger=FakeLedgerStore()))


# --------------------------------------------------------------------------
# LedgerStore
# --------------------------------------------------------------------------


async def test_ledger_store_is_a_ledger_store() -> None:
    assert isinstance(FakeLedgerStore(), LedgerStore)


async def test_ledger_round_trips_a_profile() -> None:
    ledger = FakeLedgerStore()
    profile = Profile(tenant_id="acme", jurisdictions=("AU",), sectors=("health",))
    await ledger.put_profile(profile)
    assert await ledger.get_profile("acme") == profile
    assert await ledger.get_profile("other-corp") is None


async def test_assessment_records_versions_and_trace_id() -> None:
    """PHASES.md: assessment records corpus_version, mapping_version, as_of,
    trace_id — each version as the tag and the commit it pointed at (#153)."""
    ledger = FakeLedgerStore()
    await ledger.put_profile(Profile(tenant_id="acme"))
    corpus = SourceVersion(SourceKind.CORPUS, sha="a" * 40, tag="corpus-2026.09")
    mapping = SourceVersion(SourceKind.MAPPING, sha="b" * 40, tag="mappings-2026.09")
    await ledger.record_coverage_assessment(
        AssessmentRecord(
            tenant_id="acme",
            assessment_id="a1",
            user_id="lead@acme.example",
            corpus=corpus,
            mapping=mapping,
            as_of=TODAY,
            trace_id="0" * 32,
        ),
        (),
    )
    record = await ledger.get_assessment("acme", "a1")
    assert record is not None
    assert (record.corpus, record.mapping) == (corpus, mapping)
    assert (record.corpus.tag, record.corpus.sha) == ("corpus-2026.09", "a" * 40)
    assert record.as_of == TODAY
    assert record.trace_id == "0" * 32
    assert record.user_id == "lead@acme.example"
    assert await ledger.get_assessment("other-corp", "a1") is None


async def test_subscriptions_are_per_tenant_and_idempotent() -> None:
    ledger = FakeLedgerStore()
    await ledger.subscribe(tenant_id="acme", instrument_id="privacy-act-1988", subscribed_by="u1")
    await ledger.subscribe(tenant_id="acme", instrument_id="privacy-act-1988", subscribed_by="u2")
    assert await ledger.subscribed_instruments("acme") == frozenset({"privacy-act-1988"})
    assert await ledger.subscribed_instruments("other-corp") == frozenset()


async def test_unsubscribe_removes_only_that_row() -> None:
    ledger = FakeLedgerStore()
    for tenant in ("acme", "other-corp"):
        await ledger.subscribe(
            tenant_id=tenant, instrument_id="privacy-act-1988", subscribed_by="u"
        )
    await ledger.subscribe(tenant_id="acme", instrument_id="iso-27001", subscribed_by="u")
    await ledger.unsubscribe(tenant_id="acme", instrument_id="privacy-act-1988")
    assert await ledger.subscribed_instruments("acme") == frozenset({"iso-27001"})
    assert await ledger.subscribed_instruments("other-corp") == frozenset({"privacy-act-1988"})
    await ledger.unsubscribe(tenant_id="acme", instrument_id="never-subscribed")  # no-op


@pytest.mark.parametrize(
    ("tenant_id", "instrument_id"),
    [("acme", "acme/privacy-policy"), ("acme", "beta/privacy-policy"), ("_public", "gdpr")],
    ids=["own-document", "other-tenants-document", "reserved-tenant"],
)
async def test_only_a_real_tenant_can_subscribe_and_only_to_library_law(
    tenant_id: str, instrument_id: str
) -> None:
    with pytest.raises(ValueError):
        await FakeLedgerStore().subscribe(
            tenant_id=tenant_id, instrument_id=instrument_id, subscribed_by="u"
        )


# --------------------------------------------------------------------------
# Reranker, Embedder, Generator
# --------------------------------------------------------------------------


async def assert_reranker_contract(reranker: Reranker) -> None:
    """The `Reranker` contract itself — top_k truncation, descending score
    order, valid indices — shared by every implementation regardless of
    how it actually scores. `RerankerClient` against the real local
    service (issue #14) calls this same function, not a second,
    independently-written test, so the two can't silently diverge
    (`tests/test_reranker_service.py`)."""
    assert isinstance(reranker, Reranker)
    documents = ("protect the information now", "unrelated text", "the information")
    scored = await reranker.rerank("protect the information", documents, top_k=2)
    assert len(scored) == 2
    assert scored[0].score >= scored[1].score
    assert all(0 <= s.index < len(documents) for s in scored)


async def test_reranker_is_a_reranker_and_truncates_to_top_k() -> None:
    await assert_reranker_contract(FakeReranker())


async def test_embedder_returns_configured_dimensions() -> None:
    embedder = FakeEmbedder(dimensions=1536)
    assert isinstance(embedder, Embedder)
    vectors = await embedder.embed(("hello world", "another"))
    assert len(vectors) == 2
    assert all(len(v) == 1536 for v in vectors)


async def test_generator_reports_cached_prefix_hit() -> None:
    """D-11: the cached prefix is the tenant policy corpus, and its hit is traced."""
    generator = FakeGenerator()
    assert isinstance(generator, Generator)
    warm = await generator.generate(
        system="s", cached_prefix="tenant policies", user="q", max_tokens=100
    )
    cold = await generator.generate(system="s", cached_prefix=None, user="q", max_tokens=100)
    assert warm.cached_prefix_hit is True
    assert cold.cached_prefix_hit is False


# --------------------------------------------------------------------------
# CitationVerifier
# --------------------------------------------------------------------------


@pytest.fixture
def clause() -> ClauseRecord:
    return ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="TEST-FIXTURE-001",
        clause_path="sch1/app11/1",
        text="An APP entity must take such steps as are reasonable in the circumstances "
        "to protect the information from misuse, interference and loss.",
    )


async def test_verifier_is_a_verifier() -> None:
    assert isinstance(FakeCitationVerifier(), CitationVerifier)


async def test_good_citation_passes(clause: ClauseRecord) -> None:
    verifier = FakeCitationVerifier()
    assert await verifier.verify(
        clause.clause_id,
        "take such steps as are reasonable in the circumstances",
        clauses=(clause,),
        canonicalisation_version=CANONICALISATION_VERSION,
    )


async def test_bad_citation_blocks(clause: ClauseRecord) -> None:
    """D-02: the deliberately bad citation PHASES.md requires us to prove blocks."""
    verifier = FakeCitationVerifier()
    assert not await verifier.verify(
        clause.clause_id,
        "must encrypt all personal information at rest using AES-256",
        clauses=(clause,),
        canonicalisation_version=CANONICALISATION_VERSION,
    )


async def test_citation_matching_is_canonical_not_literal(clause: ClauseRecord) -> None:
    """D-02: curly quotes and collapsed whitespace still match."""
    verifier = FakeCitationVerifier()
    assert await verifier.verify(
        clause.clause_id,
        "  protect   the information from misuse,\ninterference and loss.  ",
        clauses=(clause,),
        canonicalisation_version=CANONICALISATION_VERSION,
    )


async def test_citation_against_unknown_clause_blocks(clause: ClauseRecord) -> None:
    verifier = FakeCitationVerifier()
    assert not await verifier.verify(
        "privacy-act-1988:TEST-FIXTURE-001:sch1/app99/9",
        "take such steps as are reasonable",
        clauses=(clause,),
        canonicalisation_version=CANONICALISATION_VERSION,
    )


async def test_stale_citation_is_not_silently_reverified_under_a_new_version(
    clause: ClauseRecord,
) -> None:
    """A citation recorded under an older canonicalisation version must fail
    loudly against a clause recorded under a newer one — never silently
    compared as if the same rules produced both (`CONTEXT.md`:
    Canonicalisation version, issue #2). The quote below is the same one
    `test_good_citation_passes` proves matches textually, so the raise here
    can only be the version check, nothing else.
    """
    verifier = FakeCitationVerifier()
    with pytest.raises(ValueError, match="canonicalis"):
        await verifier.verify(
            clause.clause_id,
            "take such steps as are reasonable in the circumstances",
            clauses=(clause,),
            canonicalisation_version=CANONICALISATION_VERSION + 1,
        )


# --------------------------------------------------------------------------
# Reserved public tenant (CONTEXT.md: Tenant id, Scope)
# --------------------------------------------------------------------------


def test_public_clause_carries_the_reserved_tenant() -> None:
    from compliance_rag.corpus.models import PUBLIC_TENANT_ID

    record = ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="TEST-FIXTURE-001",
        clause_path="sch1/app11/1",
        text="An APP entity must take such steps as are reasonable.",
    )
    assert record.tenant_id == PUBLIC_TENANT_ID


def test_public_scope_rejects_a_real_tenant() -> None:
    with pytest.raises(ValueError, match="_public"):
        ClauseRecord.build(
            instrument_id="privacy-act-1988",
            version_id="TEST-FIXTURE-001",
            clause_path="sch1/app11/1",
            text="Text.",
            tenant_id="acme",
        )


def test_reserved_tenant_is_rejected_for_a_policy() -> None:
    """`_public` is reserved; a tenant-scoped clause may never claim it."""
    from compliance_rag.corpus.models import PUBLIC_TENANT_ID

    # NB: not "acme/hr-policy" — that would fail on the mismatched prefix
    # (tenant "acme" vs. tenant_id "_public"), not the reserved-tenant check
    # this test is about (see issue #4's tests for the prefix behaviour).
    with pytest.raises(ValueError, match="reserved"):
        ClauseRecord.build(
            instrument_id="acme-hr-policy",
            version_id="v3",
            clause_path="sec2/1",
            text="Text.",
            scope=Scope.TENANT,
            tenant_id=PUBLIC_TENANT_ID,
        )


# --------------------------------------------------------------------------
# Tenant id validation (CONTEXT.md: Tenant id, issue #3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_tenant_id", ["acme:corp", "acme/corp", ""])
def test_clause_record_rejects_unsafe_tenant_ids(bad_tenant_id: str) -> None:
    """':' and '/' would make a stable ID ambiguous to parse (CONTEXT.md: Clause)."""
    with pytest.raises(ValueError):
        ClauseRecord.build(
            instrument_id="acme-hr-policy",
            version_id="v3",
            clause_path="sec2/1",
            text="Text.",
            scope=Scope.TENANT,
            tenant_id=bad_tenant_id,
        )


@pytest.mark.parametrize("bad_tenant_id", ["acme:corp", "acme/corp", ""])
def test_chunk_rejects_unsafe_tenant_ids(bad_tenant_id: str) -> None:
    with pytest.raises(ValueError):
        Chunk(
            chunk_id="c1",
            clause_id="acme-hr-policy:v3:sec2/1",
            text="Text.",
            scope=Scope.TENANT,
            tenant_id=bad_tenant_id,
        )


@pytest.mark.parametrize("bad_tenant_id", ["acme:corp", "acme/corp", "", "_public"])
def test_profile_rejects_every_invalid_tenant_id(bad_tenant_id: str) -> None:
    """Profile is a tenant's structured facts (CONTEXT.md) — where a tenant
    enters the system. `_public` is rejected here even though it is a valid
    `tenant_id` value on public-scope records: a Profile is never public law.
    """
    with pytest.raises(ValueError):
        Profile(tenant_id=bad_tenant_id)


def test_profile_accepts_a_real_tenant_id() -> None:
    assert Profile(tenant_id="acme").tenant_id == "acme"


# --------------------------------------------------------------------------
# Tenant-prefixed instrument ids (CONTEXT.md: Instrument id, issue #4)
# --------------------------------------------------------------------------


def test_clause_record_accepts_a_tenant_prefixed_instrument_id() -> None:
    record = ClauseRecord.build(
        instrument_id="acme/hr-policy",
        version_id="v3",
        clause_path="sec2/1",
        text="Text.",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    assert record.clause_id == "acme/hr-policy:v3:sec2/1"


def test_clause_record_rejects_a_mismatched_tenant_prefix() -> None:
    """`beta`'s policy may not be filed under `acme`'s instrument id."""
    with pytest.raises(ValueError, match="does not match"):
        ClauseRecord.build(
            instrument_id="beta/hr-policy",
            version_id="v3",
            clause_path="sec2/1",
            text="Text.",
            scope=Scope.TENANT,
            tenant_id="acme",
        )


def test_public_clause_rejects_a_tenant_prefixed_instrument_id() -> None:
    """Public instruments stay unprefixed (CONTEXT.md: Instrument id).

    A default-scope build's `tenant_id` is `PUBLIC_TENANT_ID`, which can never
    equal a prefix's tenant (that prefix itself must pass `validate_tenant_id`,
    which rejects `PUBLIC_TENANT_ID` — see `test_instrument_id.py`) — so this
    goes through the *mismatch* path, not a separate "public" rule.
    """
    with pytest.raises(ValueError, match="does not match"):
        ClauseRecord.build(
            instrument_id="acme/hr-policy",
            version_id="v3",
            clause_path="sec2/1",
            text="Text.",
            scope=Scope.PUBLIC,
        )


# --------------------------------------------------------------------------
# ClauseStore (PHASES.md: Cloud Storage layout, issue #15)
# --------------------------------------------------------------------------


def test_clause_store_is_a_clause_store() -> None:
    assert isinstance(FakeClauseStore(), ClauseStore)


async def test_put_then_get_clause_round_trips() -> None:
    store = FakeClauseStore()
    written = ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="TEST-FIXTURE-001",
        clause_path="sch1/app11/1",
        text="An APP entity must take such steps as are reasonable.",
    )
    await store.put_clause(written)
    read = await store.get_clause(written.clause_id, tenant_id=written.tenant_id)
    assert read == written


async def test_get_unknown_clause_returns_none() -> None:
    from compliance_rag.corpus.models import PUBLIC_TENANT_ID

    store = FakeClauseStore()
    assert await store.get_clause(APP11, tenant_id=PUBLIC_TENANT_ID) is None


async def test_manifest_lists_every_clause_path_with_its_canonical_hash() -> None:
    """PHASES.md: one manifest.json per instrument version, listing every
    clause path with its canonical_sha256 — the verifier's single
    deterministic GET, Phase 4's diff surface."""
    from compliance_rag.corpus.models import PUBLIC_TENANT_ID

    store = FakeClauseStore()
    first = ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="TEST-FIXTURE-001",
        clause_path="sch1/app11/1",
        text="An APP entity must take such steps as are reasonable.",
    )
    second = ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="TEST-FIXTURE-001",
        clause_path="sch1/app11/2",
        text="A different clause entirely.",
    )
    await store.put_clause(first)
    await store.put_clause(second)

    manifest = await store.get_manifest(
        "privacy-act-1988", "TEST-FIXTURE-001", tenant_id=PUBLIC_TENANT_ID
    )

    assert manifest == {
        "sch1/app11/1": canonical_sha256(first.canonical_text),
        "sch1/app11/2": canonical_sha256(second.canonical_text),
    }


async def test_manifest_excludes_a_different_instrument_version() -> None:
    from compliance_rag.corpus.models import PUBLIC_TENANT_ID

    store = FakeClauseStore()
    in_scope = ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="TEST-FIXTURE-001",
        clause_path="sch1/app11/1",
        text="Text.",
    )
    other_version = ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="TEST-FIXTURE-002",
        clause_path="sch1/app11/1",
        text="A later compilation of the same clause.",
    )
    await store.put_clause(in_scope)
    await store.put_clause(other_version)

    manifest = await store.get_manifest(
        "privacy-act-1988", "TEST-FIXTURE-001", tenant_id=PUBLIC_TENANT_ID
    )

    assert manifest == {"sch1/app11/1": canonical_sha256(in_scope.canonical_text)}


async def test_manifest_excludes_a_different_tenant() -> None:
    """A well-formed corpus can't actually produce this collision — issue #4
    requires a tenant-owned instrument id to be prefixed by its tenant, so two
    tenants can never share one unprefixed `instrument_id` like this test
    does on purpose. It exists as defense in depth: `get_manifest`'s
    `tenant_id` must scope correctly even if something upstream failed to
    keep that invariant, not only when the corpus is already well-formed.
    """
    store = FakeClauseStore()
    acme_clause = ClauseRecord.build(
        instrument_id="acme-hr-policy",
        version_id="v1",
        clause_path="sec1",
        text="Text.",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    beta_clause = ClauseRecord.build(
        instrument_id="acme-hr-policy",
        version_id="v1",
        clause_path="sec1",
        text="A different tenant's clause under the same instrument id string.",
        scope=Scope.TENANT,
        tenant_id="beta",
    )
    await store.put_clause(acme_clause)
    await store.put_clause(beta_clause)

    manifest = await store.get_manifest("acme-hr-policy", "v1", tenant_id="acme")

    assert manifest == {"sec1": canonical_sha256(acme_clause.canonical_text)}


async def test_manifest_metadata_round_trips_and_is_absent_before_first_write() -> None:
    """`put_manifest` records `canonicalisation_version` and
    `source_html`'s own sha256 once at the manifest level (issue #19),
    alongside the per-clause paths `get_manifest` already returns."""
    from compliance_rag.corpus.models import PUBLIC_TENANT_ID

    store = FakeClauseStore()
    assert (
        await store.get_manifest_metadata(
            "privacy-act-1988", "TEST-FIXTURE-001", tenant_id=PUBLIC_TENANT_ID
        )
        is None
    )
    assert (
        await store.get_source_html(
            "privacy-act-1988", "TEST-FIXTURE-001", tenant_id=PUBLIC_TENANT_ID
        )
        is None
    )

    html = '<p id="clause-sch1-app11-1">An APP entity must take reasonable steps.</p>'
    await store.put_manifest(
        "privacy-act-1988",
        "TEST-FIXTURE-001",
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html=html,
    )

    metadata = await store.get_manifest_metadata(
        "privacy-act-1988", "TEST-FIXTURE-001", tenant_id=PUBLIC_TENANT_ID
    )
    assert metadata is not None
    assert metadata.canonicalisation_version == CANONICALISATION_VERSION
    assert metadata.source_html_sha256 == hashlib.sha256(html.encode("utf-8")).hexdigest()
    assert (
        await store.get_source_html(
            "privacy-act-1988", "TEST-FIXTURE-001", tenant_id=PUBLIC_TENANT_ID
        )
        == html
    )


async def test_get_clause_is_scoped_to_its_tenant() -> None:
    """Same defense-in-depth reasoning as `test_manifest_excludes_a_different_tenant`:
    two tenants sharing one unprefixed `instrument_id` shouldn't occur in a
    well-formed corpus (issue #4), but `get_clause` must not let one tenant
    read another's clause if it somehow does.
    """
    store = FakeClauseStore()
    acme_clause = ClauseRecord.build(
        instrument_id="acme-hr-policy",
        version_id="v1",
        clause_path="sec1",
        text="Text.",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    beta_clause = ClauseRecord.build(
        instrument_id="acme-hr-policy",
        version_id="v1",
        clause_path="sec1",
        text="A different tenant's clause under the same instrument id string.",
        scope=Scope.TENANT,
        tenant_id="beta",
    )
    await store.put_clause(acme_clause)
    await store.put_clause(beta_clause)

    assert await store.get_clause(acme_clause.clause_id, tenant_id="acme") == acme_clause
    assert await store.get_clause(beta_clause.clause_id, tenant_id="beta") == beta_clause
