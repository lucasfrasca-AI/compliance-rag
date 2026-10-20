"""User scope is private to its owner (`CONTEXT.md`: Scope, ADR-0036, #171).

`assert_user_scope_is_private` is the cross-member counterpart of the
cross-tenant leakage checks in `test_interfaces.py`, and one definition of it
runs here against `FakeRetrievalStore` and in `tests/test_qdrant_retrieval_store.py`
against the real adapter on a real Qdrant server, so the two cannot drift.

The seed is two members of one tenant (alice, bob), two of another (carol, and
a second alice: the same uid string in a different tenant, which a tenant-only
or owner-only filter would each get wrong), and a tenant policy everyone in
the tenant may read. For each viewer, and for each way a chunk can reach a
caller, exactly the viewer's own private chunks may come back: the dense
channel, the sparse channel, the fused list, the exact-id lookup, and the
router's two paths (an id query, and anything else). Every owner's own chunk
is the positive control: a viewer who reaches nothing proves nothing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.interfaces import Channel, LawScope, RetrievalAdmin, RetrievalStore
from compliance_rag.corpus.models import Chunk, ClauseRecord, DocumentClass, Scope
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import REGION
from tests.conftest import ALL_LAW, PUBLIC_AND_TENANT

TODAY = dt.date(2026, 1, 1)

ACME, OTHER = "acme", "other-corp"
ALICE, BOB, CAROL, MALLORY = "uid-alice", "uid-bob", "uid-carol", "uid-mallory"

EVERYTHING = (Scope.PUBLIC, Scope.TENANT, Scope.USER)

QUERY = "quokka"
"""Every seeded chunk says it, so the sparse channel can return any of them."""

NARROWED = LawScope(frozenset({"privacy-act-1988:TEST-FIXTURE-001:sch1/app11/1"}), "contract: one")
"""Track A cites one law clause; the tenant's own documents aren't law and
aren't scoped by it (ADR-0030), user-scoped ones included."""


def _note(chunk_id: str, tenant_id: str, user_id: str, who: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        clause_id=f"{tenant_id}/{who}-notes:v1:1/1",
        text=f"Quokka note from {who}: the backup keys stay offline.",
        scope=Scope.USER,
        tenant_id=tenant_id,
        user_id=user_id,
    )


ALICE_NOTE = _note("alice-note", ACME, ALICE, "alice")
BOB_NOTE = _note("bob-note", ACME, BOB, "bob")
CAROL_NOTE = _note("carol-note", OTHER, CAROL, "carol")
OTHER_ALICE_NOTE = _note("other-alice-note", OTHER, ALICE, "alice")
POLICY = Chunk(
    chunk_id="acme-policy",
    clause_id="acme/policy:v1:1/1",
    text="Quokka policy: backup keys are rotated every year.",
    scope=Scope.TENANT,
    tenant_id=ACME,
)
OTHER_POLICY = Chunk(
    chunk_id="other-policy",
    clause_id="other-corp/policy:v1:1/1",
    text="Quokka policy: backup keys are escrowed.",
    scope=Scope.TENANT,
    tenant_id=OTHER,
)

NOTES = (ALICE_NOTE, BOB_NOTE, CAROL_NOTE, OTHER_ALICE_NOTE)
USER_CHUNK_IDS = frozenset(c.chunk_id for c in NOTES)


def user_scope_chunks() -> tuple[Chunk, ...]:
    """The seed, for any backend's test."""
    return (*NOTES, POLICY, OTHER_POLICY)


@dataclass(frozen=True, slots=True)
class Viewer:
    name: str
    tenant_id: str
    user_id: str | None
    own: frozenset[str]
    """The private chunks this viewer, and only this viewer, may reach."""
    policy: str | None
    """The tenant policy any of the tenant's people may reach."""


VIEWERS = (
    Viewer("alice", ACME, ALICE, frozenset({"alice-note"}), "acme-policy"),
    Viewer("bob", ACME, BOB, frozenset({"bob-note"}), "acme-policy"),
    Viewer("a tenant member with no notes", ACME, MALLORY, frozenset(), "acme-policy"),
    Viewer("no member at all", ACME, None, frozenset(), "acme-policy"),
    Viewer("carol, of another tenant", OTHER, CAROL, frozenset({"carol-note"}), "other-policy"),
    Viewer(
        "alice's uid in another tenant",
        OTHER,
        ALICE,
        frozenset({"other-alice-note"}),
        "other-policy",
    ),
    Viewer("another tenant, no member", OTHER, None, frozenset(), "other-policy"),
    Viewer("the library's own tenant", "_public", ALICE, frozenset(), None),
)


async def reach(
    store: RetrievalStore, viewer: Viewer, *, law: LawScope, scopes: tuple[Scope, ...]
) -> dict[str, frozenset[str]]:
    """Every chunk the viewer reaches, per route: the two channels before
    fusion, the fused list, and an exact-id lookup of every chunk seeded."""
    found = await store.search(
        QUERY,
        law=law,
        tenant_id=viewer.tenant_id,
        scopes=scopes,
        as_of=TODAY,
        limit=20,
        user_id=viewer.user_id,
    )
    looked_up = [
        await store.lookup_clause(
            chunk.clause_id,
            law=law,
            tenant_id=viewer.tenant_id,
            scopes=scopes,
            as_of=TODAY,
            user_id=viewer.user_id,
        )
        for chunk in user_scope_chunks()
    ]
    return {
        Channel.DENSE.value: frozenset(c.chunk_id for c in found.per_channel[Channel.DENSE]),
        Channel.SPARSE.value: frozenset(c.chunk_id for c in found.per_channel[Channel.SPARSE]),
        "fused": frozenset(c.chunk_id for c in found.fused),
        Channel.KEYWORD.value: frozenset(h.chunk_id for h in looked_up if h is not None),
    }


async def route_reach(
    pipeline: RetrievalPipeline, viewer: Viewer, *, law: LawScope, scopes: tuple[Scope, ...]
) -> dict[str, frozenset[str]]:
    """The router's two paths: an id query, which is an exact lookup (a hit
    for a chunk that exists, a miss for one the viewer may not see), and a
    prose query, which runs the full pipeline."""
    by_id: set[str] = set()
    for chunk in user_scope_chunks():
        trace = await pipeline.route(
            chunk.clause_id,
            viewer.tenant_id,
            TODAY,
            law=law,
            scopes=scopes,
            user_id=viewer.user_id,
        )
        by_id |= {c.chunk_id for c in trace.fused}
    prose = await pipeline.route(
        QUERY, viewer.tenant_id, TODAY, law=law, scopes=scopes, user_id=viewer.user_id
    )
    return {
        "route (an id query)": frozenset(by_id),
        "route (anything else)": frozenset(c.chunk_id for c in prose.fused),
    }


def private_to(reached: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    """What of the private chunks each route reached."""
    return {route: found & USER_CHUNK_IDS for route, found in reached.items()}


async def assert_user_scope_is_private(store: RetrievalStore) -> None:
    """Against a store seeded with `user_scope_chunks()`."""
    pipeline = RetrievalPipeline(store, FakeReranker())
    for law in (ALL_LAW, NARROWED):
        for viewer in VIEWERS:
            routes = {
                **await reach(store, viewer, law=law, scopes=EVERYTHING),
                **await route_reach(pipeline, viewer, law=law, scopes=EVERYTHING),
            }
            # Exactly the viewer's own private chunks, through every route:
            # none of anyone else's (the leak), all of their own (the control).
            assert all(found == viewer.own for found in private_to(routes).values()), (
                f"{viewer.name} under {law.reason}",
                private_to(routes),
            )
            # The tenant's ordinary documents still reach each of its people
            # by every route, so the checks above are not passing because a
            # route returns nothing.
            if viewer.policy is not None:
                assert all(viewer.policy in found for found in routes.values()), (
                    f"{viewer.name} under {law.reason}",
                    routes,
                )

            # Asking for fewer scopes narrows, for the owner too: user scope
            # is opt-in, by every route and on the router's exact lookup.
            narrower = {
                **await reach(store, viewer, law=law, scopes=PUBLIC_AND_TENANT),
                **await route_reach(pipeline, viewer, law=law, scopes=PUBLIC_AND_TENANT),
            }
            assert all(not found for found in private_to(narrower).values()), (
                f"{viewer.name} asked for no user scope",
                private_to(narrower),
            )


async def test_fake_retrieval_store_keeps_user_scope_private() -> None:
    store = FakeRetrievalStore(user_scope_chunks(), ledger=FakeLedgerStore())
    await assert_user_scope_is_private(store)


async def assert_a_published_private_addition_is_private(
    store: RetrievalStore, admin: RetrievalAdmin
) -> None:
    """The write path carries the owner to the store (#171): alice's private
    addition, published through `CorpusLifecycle` like any other document
    (an external upload, never the policy corpus), is reached by alice by
    every route and by no one else. `store` and `admin` are one backend.
    So is her clause's canonical text, which publishing writes too (#233)."""
    ledger = FakeLedgerStore()
    clauses = FakeClauseStore()
    lifecycle = CorpusLifecycle(
        ledger=ledger,
        retrieval=admin,
        clauses=clauses,
        generation_region=REGION,  # in-region, where real customer data is allowed
        synthetic_registry=SyntheticRegistry(keys=set(), source="no synthetic content"),
    )
    note = ClauseRecord.build(
        instrument_id="acme/alice-notes",
        version_id="v1",
        clause_path="1/1",
        text=ALICE_NOTE.text,
        scope=Scope.USER,
        tenant_id=ACME,
        user_id=ALICE,
    )
    await lifecycle.publish_version(
        (note,),
        count_tokens=lambda text: len(text.split()),
        source_html="<p>alice's notes</p>",
        instrument_id="acme/alice-notes",
        tenant_id=ACME,
        version_id="v1",
        effective_from=dt.date(2025, 1, 1),
        supersedes=None,
        document_class=DocumentClass.EXTERNAL_UPLOAD,
    )
    # The store holds only the published chunk, whose id is its clause's.
    for viewer in VIEWERS:
        routes = await reach(store, viewer, law=ALL_LAW, scopes=EVERYTHING)
        # Her uid in another tenant is not her: the owner is a member of a tenant.
        is_alice = viewer.tenant_id == ACME and viewer.user_id == ALICE
        expected = frozenset({note.clause_id}) if is_alice else frozenset[str]()
        assert all(found == expected for found in routes.values()), (viewer.name, routes)
        held = await clauses.get_clause(
            note.clause_id, tenant_id=viewer.tenant_id, user_id=viewer.user_id
        )
        assert held == (note if is_alice else None), viewer.name


async def test_fake_stores_what_the_write_path_publishes_for_its_owner_alone() -> None:
    store = FakeRetrievalStore(ledger=FakeLedgerStore())
    await assert_a_published_private_addition_is_private(store, store)
