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
    FakeGraphStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.interfaces import (
    Channel,
    CitationVerifier,
    ClauseStore,
    Embedder,
    Generator,
    GraphStore,
    LedgerStore,
    Obligation,
    Profile,
    Reranker,
    RetrievalStore,
)
from compliance_rag.corpus.models import (
    CANONICALISATION_VERSION,
    Chunk,
    ClauseRecord,
    Scope,
    canonical_sha256,
)

TODAY = dt.date(2026, 1, 1)
LATER = dt.date(2027, 6, 1)

APP11 = "privacy-act-1988:TEST-FIXTURE-001:sch1/app11/1"
APP12 = "privacy-act-1988:TEST-FIXTURE-001:sch1/app12/1"


@pytest.fixture
def graph() -> FakeGraphStore:
    return FakeGraphStore(
        obligations={
            "OB-APP11": Obligation(
                obligation_id="OB-APP11",
                clause_id=APP11,
                instrument_id="privacy-act-1988",
                summary="Take reasonable steps to protect personal information.",
            ),
            "OB-APP12": Obligation(
                obligation_id="OB-APP12",
                clause_id=APP12,
                instrument_id="privacy-act-1988",
                summary="Give an individual access to their personal information.",
            ),
            "OB-ISO-A8": Obligation(
                obligation_id="OB-ISO-A8",
                clause_id="iso-27001:2022:annexa/8/3",
                instrument_id="iso-27001",
                summary="Information access restriction.",
            ),
        },
        applicability={"AU": ("OB-APP11", "OB-APP12"), "health": ("OB-APP11",)},
        crosswalk_edges={"OB-APP11": ("OB-ISO-A8",)},
        # Tranche-style commencement: OB-APP12 only binds from 2027.
        valid_from={"OB-APP12": dt.date(2027, 1, 1)},
    )


@pytest.fixture
def chunks() -> tuple[Chunk, ...]:
    return (
        Chunk(
            chunk_id="c1",
            clause_id=APP11,
            obligation_id="OB-APP11",
            text="An APP entity must take such steps as are reasonable to protect the information.",
        ),
        Chunk(
            chunk_id="c2",
            clause_id=APP12,
            obligation_id="OB-APP12",
            text="An APP entity must give the individual access to the information.",
        ),
        Chunk(
            chunk_id="c3",
            clause_id="acme-policy:v3:sec2/1",
            obligation_id=None,
            text="Acme retains customer records for seven years to protect the information.",
            scope=Scope.TENANT,
            tenant_id="acme",
        ),
    )


# --------------------------------------------------------------------------
# GraphStore
# --------------------------------------------------------------------------


async def test_graph_store_is_a_graph_store(graph: FakeGraphStore) -> None:
    assert isinstance(graph, GraphStore)


async def test_traversal_requires_a_depth_cap(graph: FakeGraphStore) -> None:
    """D-05: an uncapped traversal is a test failure."""
    profile = Profile(tenant_id="acme", jurisdictions=("AU",))
    with pytest.raises(ValueError, match="max_depth"):
        await graph.applicable_obligations(profile, as_of=TODAY, max_depth=0)


async def test_as_of_changes_the_obligation_set(graph: FakeGraphStore) -> None:
    """D-05: the same question on two dates gives different, correct answers."""
    profile = Profile(tenant_id="acme", jurisdictions=("AU",))
    now = await graph.applicable_obligations(profile, as_of=TODAY, max_depth=3)
    later = await graph.applicable_obligations(profile, as_of=LATER, max_depth=3)
    assert [o.obligation_id for o in now] == ["OB-APP11"]
    assert [o.obligation_id for o in later] == ["OB-APP11", "OB-APP12"]


async def test_applicable_obligations_are_untruncated(graph: FakeGraphStore) -> None:
    """D-03: Track A returns the full set; there is no top-k to pass."""
    profile = Profile(tenant_id="acme", jurisdictions=("AU",), sectors=("health",))
    found = await graph.applicable_obligations(profile, as_of=LATER, max_depth=3)
    assert {o.obligation_id for o in found} == {"OB-APP11", "OB-APP12"}


async def test_crosswalks_respect_the_depth_cap(graph: FakeGraphStore) -> None:
    hops = await graph.crosswalks("OB-APP11", as_of=TODAY, max_depth=1)
    assert [o.obligation_id for o in hops] == ["OB-ISO-A8"]
    with pytest.raises(ValueError, match="max_depth"):
        await graph.crosswalks("OB-APP11", as_of=TODAY, max_depth=0)


# --------------------------------------------------------------------------
# RetrievalStore
# --------------------------------------------------------------------------


async def assert_retrieval_store_contract(store: RetrievalStore) -> None:
    """The `RetrievalStore` contract itself, against a store already seeded
    with the `chunks` fixture's three records (D-08). Shared by
    `FakeRetrievalStore` here and by `QdrantRetrievalStore` against a real,
    self-hosted, CI-only Qdrant server (`tests/test_qdrant_retrieval_store.py`,
    issue #22) — one definition of the contract, not two independently
    written tests that could silently diverge, same reasoning as
    `assert_reranker_contract` below.
    """
    assert isinstance(store, RetrievalStore)

    # D-03: filtered to Track A's obligation IDs, whatever the text similarity.
    result = await store.search(
        "steps to protect the information",
        obligation_ids=("OB-APP11",),
        tenant_id="acme",
        scopes=(Scope.PUBLIC, Scope.TENANT),
        as_of=TODAY,
        limit=10,
    )
    assert {c.chunk_id for c in result.fused} == {"c1"}

    # Both channel candidate lists are logged before fusion (CLAUDE.md).
    result = await store.search(
        "access to the information",
        obligation_ids=(),
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
        obligation_ids=(),
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
        obligation_ids=(),
        tenant_id="acme",
        scopes=(Scope.PUBLIC,),
        as_of=TODAY,
        limit=10,
    )
    assert all(c.chunk_id != "c3" for c in result.fused)

    # D-04 / D-12: the router's only decision, and it returns the keyword channel.
    hit = await store.lookup_clause(APP11, tenant_id="acme", as_of=TODAY)
    assert hit is not None
    assert hit.channel is Channel.KEYWORD
    assert hit.clause_id == APP11

    # Cross-tenant lookup: acme can never look up beta's clause by ID either,
    # not just by ranked search.
    hit = await store.lookup_clause("acme-policy:v3:sec2/1", tenant_id="other-corp", as_of=TODAY)
    assert hit is None


async def test_retrieval_store_is_a_retrieval_store(chunks: tuple[Chunk, ...]) -> None:
    assert isinstance(FakeRetrievalStore(chunks), RetrievalStore)


async def test_fake_retrieval_store_satisfies_the_shared_contract(
    chunks: tuple[Chunk, ...],
) -> None:
    await assert_retrieval_store_contract(FakeRetrievalStore(chunks))


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
    """PHASES.md: assessment records corpus_version, mapping_version, as_of, trace_id."""
    ledger = FakeLedgerStore()
    await ledger.record_assessment(
        tenant_id="acme",
        assessment_id="a1",
        corpus_version="corpus-2026.09",
        mapping_version="mappings-2026.09",
        as_of=TODAY,
        trace_id="0" * 32,
    )
    row = await ledger.get_assessment("acme", "a1")
    assert row is not None
    assert row["corpus_version"] == "corpus-2026.09"
    assert row["mapping_version"] == "mappings-2026.09"
    assert row["as_of"] == TODAY
    assert row["trace_id"] == "0" * 32
    assert await ledger.get_assessment("other-corp", "a1") is None


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
