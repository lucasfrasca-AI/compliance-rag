"""User scope is private to its owner, beyond the retrieval store (ADR-0036, #171).

The retrieval store's own leakage contract is `tests/contracts/test_user_scope.py`
(fake) and `tests/test_qdrant_retrieval_store.py` (real Qdrant). This file is
the rest of the read-path audit: the models' owner rule, the write path, the
clause store's reads, the cached policy prefix, the verifier's read of
canonical text, and `answer()` end to end.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import ClauseStore
from compliance_rag.assessment.pipeline import LIBRARY_QUESTION, AssessmentPipeline
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.corpus.chunker import chunk_clause
from compliance_rag.corpus.models import Chunk, ClauseRecord, DocumentClass, Scope
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import REGION
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Outcome, Verifier
from tests.test_gcs_clause_store import _FakeBucket  # pyright: ignore[reportPrivateUsage]

TODAY = dt.date(2026, 10, 1)
ALICE, BOB = "uid-alice", "uid-bob"
NOTES = "acme/alice-notes"
ALICE_TEXT = "Quokka note: alice keeps the backup keys offline in a drawer."
BOB_TEXT = "Quokka note: bob keeps the backup keys in a safe."


def _private(owner: str | None, text: str = ALICE_TEXT) -> ClauseRecord:
    return ClauseRecord.build(
        instrument_id=NOTES,
        version_id="v1",
        clause_path="1/1",
        text=text,
        scope=Scope.USER,
        tenant_id="acme",
        user_id=owner,
    )


def _chunk_of(record: ClauseRecord) -> Chunk:
    return chunk_clause(record, count_tokens=lambda text: len(text.split()))


# ---- the owner rule on the records ------------------------------------------------


@pytest.mark.parametrize("owner", [None, "", "   "], ids=["none", "empty", "blank"])
def test_a_user_scoped_chunk_names_its_owner(owner: str | None) -> None:
    with pytest.raises(ValidationError, match="needs a user_id"):
        Chunk(
            chunk_id="c",
            clause_id=f"{NOTES}:v1:1/1",
            text="t",
            scope=Scope.USER,
            tenant_id="acme",
            user_id=owner,
        )


@pytest.mark.parametrize("owner", [None, "", "   "], ids=["none", "empty", "blank"])
def test_a_user_scoped_clause_names_its_owner(owner: str | None) -> None:
    with pytest.raises(ValueError, match="needs a user_id"):
        _private(owner)


@pytest.mark.parametrize("scope", [Scope.PUBLIC, Scope.TENANT])
def test_only_user_scope_has_an_owner(scope: Scope) -> None:
    tenant = "_public" if scope is Scope.PUBLIC else "acme"
    with pytest.raises(ValidationError, match="belongs to user scope only"):
        Chunk(
            chunk_id="c",
            clause_id="privacy-act-1988:v1:1",
            text="t",
            scope=scope,
            tenant_id=tenant,
            user_id=ALICE,
        )
    with pytest.raises(ValueError, match="belongs to user scope only"):
        ClauseRecord.build(
            instrument_id="privacy-act-1988" if scope is Scope.PUBLIC else "acme/policy",
            version_id="v1",
            clause_path="1",
            text="t",
            scope=scope,
            tenant_id=tenant,
            user_id=ALICE,
        )


def test_a_private_addition_belongs_to_a_tenant() -> None:
    """`tenant_id` always names an owner, never nobody (`CONTEXT.md`: Scope)."""
    with pytest.raises(ValidationError, match="reserved for public law"):
        Chunk(
            chunk_id="c",
            clause_id=f"{NOTES}:v1:1/1",
            text="t",
            scope=Scope.USER,
            tenant_id="_public",
            user_id=ALICE,
        )


def test_a_clause_becomes_a_chunk_with_its_owner() -> None:
    chunk = _chunk_of(_private(ALICE))
    assert (chunk.scope, chunk.tenant_id, chunk.user_id) == (Scope.USER, "acme", ALICE)
    tenant = ClauseRecord.build(
        instrument_id="acme/policy",
        version_id="v1",
        clause_path="1/1",
        text="t",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    assert _chunk_of(tenant).user_id is None


# ---- the write path ---------------------------------------------------------------


def _lifecycle() -> tuple[CorpusLifecycle, FakeRetrievalStore, FakeLedgerStore, FakeClauseStore]:
    ledger, clauses = FakeLedgerStore(), FakeClauseStore()
    store = FakeRetrievalStore(ledger=ledger)
    lifecycle = CorpusLifecycle(
        ledger=ledger,
        retrieval=store,
        clauses=clauses,
        generation_region=REGION,
        synthetic_registry=SyntheticRegistry(keys=set(), source="no synthetic content"),
    )
    return lifecycle, store, ledger, clauses


async def _publish(
    lifecycle: CorpusLifecycle,
    *chunks: Chunk,
    document_class: DocumentClass = DocumentClass.EXTERNAL_UPLOAD,
) -> None:
    await lifecycle.publish_version(
        chunks,
        instrument_id=NOTES,
        tenant_id="acme",
        version_id="v1",
        effective_from=dt.date(2025, 1, 1),
        supersedes=None,
        document_class=document_class,
    )


async def test_a_private_chunk_with_no_owner_is_refused_before_any_store_is_touched() -> None:
    """The models refuse to build one, but `model_copy` skips validation, so
    the write path checks again where a store is about to hold the chunk."""
    lifecycle, store, ledger, _ = _lifecycle()
    ownerless = _chunk_of(_private(ALICE)).model_copy(update={"user_id": None})
    with pytest.raises(ValueError, match="needs a user_id"):
        await _publish(lifecycle, ownerless)
    assert store.stored_chunk_ids == frozenset()
    assert await ledger.document_class(tenant_id="acme", instrument_id=NOTES) is None


async def test_an_owner_on_a_chunk_of_another_scope_is_refused_too() -> None:
    lifecycle, store, _, _ = _lifecycle()
    stray = _chunk_of(_private(ALICE)).model_copy(update={"scope": Scope.TENANT})
    with pytest.raises(ValueError, match="belongs to user scope only"):
        await _publish(lifecycle, stray)
    assert store.stored_chunk_ids == frozenset()


async def test_a_version_is_one_document_with_one_owner() -> None:
    """A version is retired, withdrawn and purged as a unit, by tenant and
    instrument: one spanning two people's private additions would let either
    one's removal reach the other's."""
    lifecycle, store, _, _ = _lifecycle()
    bobs = _chunk_of(_private(BOB, BOB_TEXT).model_copy(update={"clause_path": "2/1"}))
    with pytest.raises(ValueError, match="more than one scope or owner"):
        await _publish(lifecycle, _chunk_of(_private(ALICE)), bobs)
    assert store.stored_chunk_ids == frozenset()


async def test_an_internal_policy_cannot_hold_private_text() -> None:
    """The policy corpus is the cached prefix every member's answer reads
    (D-11): a private addition published as a policy would be shown to all."""
    lifecycle, store, _, _ = _lifecycle()
    with pytest.raises(ValueError, match="can't hold user-scoped"):
        await _publish(
            lifecycle,
            _chunk_of(_private(ALICE)),
            document_class=DocumentClass.INTERNAL_POLICY,
        )
    assert store.stored_chunk_ids == frozenset()


async def test_replacing_a_policy_in_place_cannot_make_it_private_text() -> None:
    lifecycle, store, _, _ = _lifecycle()
    policy = ClauseRecord.build(
        instrument_id=NOTES,
        version_id="v1",
        clause_path="1/1",
        text="Acme keeps backups.",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    await _publish(lifecycle, _chunk_of(policy), document_class=DocumentClass.INTERNAL_POLICY)
    private = _private(ALICE)
    with pytest.raises(ValueError, match="can't hold user-scoped"):
        await lifecycle.replace_in_place(
            (_chunk_of(private),),
            (private,),
            tenant_id="acme",
            instrument_id=NOTES,
            version_id="v1",
            source_html="<p/>",
            actor="alice",
            reason="typo",
            acknowledged_findings_citing=0,
        )
    assert store.stored_chunk_ids == frozenset({policy.clause_id})


# ---- the clause store's reads -----------------------------------------------------


def _clause_stores() -> list[ClauseStore]:
    return [FakeClauseStore(), GCSClauseStore(_FakeBucket())]


@pytest.mark.parametrize("store", _clause_stores(), ids=["fake", "gcs"])
async def test_a_clause_store_gives_a_private_clause_to_its_owner_alone(
    store: ClauseStore,
) -> None:
    mine = _private(ALICE)
    shared = ClauseRecord.build(
        instrument_id="acme/policy",
        version_id="v1",
        clause_path="1/1",
        text="Acme keeps backups.",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    await store.put_clause(mine)
    await store.put_clause(shared)
    assert await store.get_clause(mine.clause_id, tenant_id="acme", user_id=ALICE) == mine
    for stranger in (BOB, None):
        assert await store.get_clause(mine.clause_id, tenant_id="acme", user_id=stranger) is None
    # Another tenant is no one's way in either, even as the same uid.
    assert await store.get_clause(mine.clause_id, tenant_id="other-corp", user_id=ALICE) is None
    # The tenant's ordinary clauses are given to everyone it admits.
    for member in (ALICE, BOB, None):
        assert await store.get_clause(shared.clause_id, tenant_id="acme", user_id=member) == shared


async def test_a_private_clause_naming_no_owner_is_given_to_no_one() -> None:
    """Fails closed, as retrieval does for a point with no `user_id`: no
    member, and no one at all, is its owner. (`model_copy` skips the model's
    own check, which is how such a record can exist. The Cloud Storage adapter
    can't hold one: it validates what it reads, so it raises rather than
    return it.)"""
    ownerless = _private(ALICE).model_copy(update={"user_id": None})
    store = FakeClauseStore()
    await store.put_clause(ownerless)
    for member in (ALICE, BOB, None):
        assert not ownerless.visible_to(member)
        assert await store.get_clause(ownerless.clause_id, tenant_id="acme", user_id=member) is None


# ---- the cached policy prefix -----------------------------------------------------


async def test_a_private_clause_listed_in_a_policy_never_reaches_the_prefix() -> None:
    """Confirms the prefix is tenant scope only: `tenant_policies` reads as no
    member, so a private clause that got into a policy's manifest is not
    returned. It fails loudly instead of leaving a gap or leaking."""
    ledger, clauses = FakeLedgerStore(), FakeClauseStore()
    await ledger.record_version(
        tenant_id="acme",
        instrument_id=NOTES,
        version_id="v1",
        valid_from=dt.date(2025, 1, 1),
        supersedes=None,
        document_class=DocumentClass.INTERNAL_POLICY,
    )
    await clauses.put_clause(_private(ALICE))
    with pytest.raises(LookupError) as raised:
        await tenant_policies(ledger, clauses, tenant_id="acme", as_of=TODAY)
    assert ALICE_TEXT not in str(raised.value)


# ---- answer(): the retriever, the generator's context, and the verifier ------------


class _Leaky(FakeRetrievalStore):
    """Retrieval that has forgotten the owner: it hands any member any private
    chunk of the tenant — what the verifier's own read must still refuse."""

    def _visible(  # pyright: ignore[reportIncompatibleMethodOverride] — deliberately leaky
        self,
        chunk: Chunk,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        subscribed: frozenset[str],
        user_id: str | None,
    ) -> bool:
        return chunk.scope in scopes and chunk.tenant_id == tenant_id


class _Scripted(FakeGenerator):
    def __init__(self, reply: object) -> None:
        super().__init__(json.dumps(reply))


async def _pipeline(
    generator: FakeGenerator, *, leaky: bool = False
) -> tuple[AssessmentPipeline, FakeRetrievalStore]:
    ledger, clauses = FakeLedgerStore(), FakeClauseStore()
    await ledger.record_version(
        tenant_id="acme",
        instrument_id=NOTES,
        version_id="v1",
        valid_from=dt.date(2025, 1, 1),
        supersedes=None,
        document_class=DocumentClass.EXTERNAL_UPLOAD,
    )
    mine = _private(ALICE)
    await clauses.put_clause(mine)
    seeded = (_chunk_of(mine),)
    store = (_Leaky if leaky else FakeRetrievalStore)(seeded, ledger=ledger)
    pipeline = AssessmentPipeline(
        RetrievalPipeline(store, FakeReranker()),
        generator,
        Verifier(clauses, ledger, CanonicalCitationVerifier()),
        ledger,
        tenant_text=TenantTextGate(REGION, SyntheticRegistry.load()),
    )
    return pipeline, store


QUESTION = "Where are the backup keys kept, according to the quokka notes?"


async def test_bobs_question_never_puts_alices_private_chunk_in_the_generators_context() -> None:
    """Both the free-text question and the exact-id one, which the router sends
    to the keyword channel. Alice asking is the positive control in each."""
    mine = _private(ALICE)
    for question in (QUESTION, mine.clause_id):
        generator = FakeGenerator()
        pipeline, _ = await _pipeline(generator)

        await pipeline.answer(question, "acme", TODAY, law=LIBRARY_QUESTION, user_id=ALICE)
        assert mine.canonical_text in str(generator.calls[-1]["user"]), question

        for asker in (BOB, None):
            await pipeline.answer(question, "acme", TODAY, law=LIBRARY_QUESTION, user_id=asker)
            sent = json.dumps(generator.calls[-1])
            # (The exact-id question is the clause id, so look for it as a
            # retrieved clause's marker, not anywhere in the prompt.)
            assert mine.canonical_text not in sent, (question, asker)
            assert f" clause {mine.clause_id}]" not in sent, (question, asker)


async def test_the_verifier_refuses_a_leaked_private_clause_to_a_stranger() -> None:
    """Mechanism 3, like the version check: bob's answer was handed alice's
    chunk by a store that forgot the owner and cites it verbatim. The clause is
    never read for him, so the citation blocks and her text is not shown;
    alice's identical answer verifies (the positive control)."""
    mine = _private(ALICE)
    reply = {
        "answer": "In a drawer.",
        "citations": [{"clause_id": mine.clause_id, "quote": "offline in a drawer"}],
    }

    pipeline, _ = await _pipeline(_Scripted(reply), leaky=True)
    bobs = await pipeline.answer(QUESTION, "acme", TODAY, law=LIBRARY_QUESTION, user_id=BOB)
    # It leaked: the chunk was in what the model was shown and could be cited...
    assert mine.clause_id in {c.clause_id for c in bobs.retrieval.fused}
    # ...and still the verifier refused to read it for him.
    assert not bobs.verified and bobs.citations == ()
    assert [c.outcome for c in bobs.blocked] == [Outcome.CLAUSE_MISSING]
    assert all(c.clause is None for c in bobs.blocked)

    pipeline, _ = await _pipeline(_Scripted(reply), leaky=True)
    alices = await pipeline.answer(QUESTION, "acme", TODAY, law=LIBRARY_QUESTION, user_id=ALICE)
    assert alices.verified
    [shown] = alices.citations
    assert shown.clause is not None and shown.clause.canonical_text == mine.canonical_text


async def test_the_traces_say_whether_a_member_asked_never_who(
    span_exporter: InMemorySpanExporter,
) -> None:
    """CLAUDE.md: no identifiers in traces. The retrieval span records that a
    member asked; the uid appears in no span, on either router path."""
    pipeline, _ = await _pipeline(FakeGenerator())
    mine = _private(ALICE)
    for question in (QUESTION, mine.clause_id):
        span_exporter.clear()
        await pipeline.answer(question, "acme", TODAY, law=LIBRARY_QUESTION, user_id=ALICE)
        spans = span_exporter.get_finished_spans()
        assert any(s.attributes and s.attributes.get("retrieval.member_present") for s in spans)
        assert ALICE not in json.dumps([dict(s.attributes or {}) for s in spans], default=str)
        span_exporter.clear()
        await pipeline.answer(question, "acme", TODAY, law=LIBRARY_QUESTION)
        assert any(
            s.attributes and s.attributes.get("retrieval.member_present") is False
            for s in span_exporter.get_finished_spans()
        )


async def test_a_member_cites_their_own_private_addition_through_answer() -> None:
    reply = {
        "answer": "In a drawer.",
        "citations": [{"clause_id": _private(ALICE).clause_id, "quote": "offline in a drawer"}],
    }
    pipeline, _ = await _pipeline(_Scripted(reply))
    answer = await pipeline.answer(QUESTION, "acme", TODAY, law=LIBRARY_QUESTION, user_id=ALICE)
    assert answer.verified

    # No member, no private text: the same question asked with no one signed in.
    pipeline, _ = await _pipeline(_Scripted(reply))
    anonymous = await pipeline.answer(QUESTION, "acme", TODAY, law=LIBRARY_QUESTION)
    assert not anonymous.verified
    assert [c.outcome for c in anonymous.blocked] == [Outcome.NOT_IN_CONTEXT]
