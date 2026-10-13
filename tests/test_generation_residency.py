"""The build-phase residency gate (ADR-0023). On ingest: while generation runs
offshore, a real tenant upload fails loudly at the door instead of being sent
outside Australia on its first question. Both ways, through the real write path
(`CorpusLifecycle`). Per request (ADR-0032): the question and the policy prefix,
which never pass through the stores the startup gate asks."""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_request_allowed,
    validate_generation_region,
)
from compliance_rag.corpus.models import Chunk, DocumentClass, Scope
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry

POLICY = "acme/retention-policy"


def _chunk(*, synthetic: bool, scope: Scope = Scope.TENANT) -> Chunk:
    return Chunk(
        chunk_id=f"{POLICY}:v1:1/1",
        clause_id=f"{POLICY}:v1:1/1",
        text="Accounts are kept seven years.",
        scope=scope,
        tenant_id="acme",
        synthetic=synthetic,
    )


REGISTERED = SyntheticRegistry.of((_chunk(synthetic=False),), source="this test's fixture")
"""The operator has registered this one invented policy as synthetic (#78)."""


def _lifecycle(region: str) -> tuple[CorpusLifecycle, FakeRetrievalStore]:
    ledger = FakeLedgerStore()
    store = FakeRetrievalStore(ledger=ledger)
    return (
        CorpusLifecycle(
            ledger=ledger,
            retrieval=store,
            clauses=FakeClauseStore(),
            generation_region=region,
            synthetic_registry=REGISTERED,
        ),
        store,
    )


async def _publish(lifecycle: CorpusLifecycle, chunk: Chunk) -> None:
    await lifecycle.publish_version(
        (chunk,),
        instrument_id=POLICY,
        tenant_id="acme",
        version_id="v1",
        effective_from=dt.date(2026, 1, 1),
        supersedes=None,
        document_class=DocumentClass.INTERNAL_POLICY,
    )


def _real_chunk() -> Chunk:
    return _chunk(synthetic=False).model_copy(update={"text": "Acme keeps payroll for 7 years."})


async def test_the_first_real_policy_upload_fails_while_generation_is_offshore() -> None:
    lifecycle, store = _lifecycle("us")
    with pytest.raises(GenerationResidencyError, match="real tenant chunk"):
        await _publish(lifecycle, _real_chunk())
    assert not await store.holds_real_tenant_data()  # refused before any write
    assert store.stored_chunk_ids == frozenset()


async def test_a_registered_synthetic_policy_is_accepted_while_generation_is_offshore() -> None:
    lifecycle, store = _lifecycle("us")
    await _publish(lifecycle, _chunk(synthetic=False))  # synthetic because registered
    assert store.stored_chunk_ids == frozenset({f"{POLICY}:v1:1/1"})
    assert not await store.holds_real_tenant_data()


async def test_a_real_policy_is_accepted_once_generation_is_in_sydney() -> None:
    lifecycle, store = _lifecycle("australia-southeast1")
    await _publish(lifecycle, _real_chunk())
    assert await store.holds_real_tenant_data()


def test_an_unmarked_chunk_counts_as_real() -> None:
    """Fail-closed default: forgetting the flag closes the gate."""
    assert Chunk(chunk_id="c", clause_id="acme/p:v1:1", text="t").synthetic is False


@pytest.mark.parametrize(
    "region", ["global", "US", " us", "asia-southeast1", "us-central1", "eu", "asia-northeast1", ""]
)
def test_no_other_generation_region_is_accepted(region: str) -> None:
    with pytest.raises(GenerationResidencyError):
        validate_generation_region(region)
    with pytest.raises(GenerationResidencyError):
        _lifecycle(region)


TESTERS = frozenset({"Tester@Acme.example"})


def test_offshore_a_question_from_a_member_who_isnt_a_registered_tester_is_refused() -> None:
    """A member can type anything, real customer data included, so a question
    counts as real unless the operator registered its asker (ADR-0032)."""
    with pytest.raises(GenerationResidencyError, match="registered build-phase tester"):
        assert_request_allowed(
            generation_region="us", member_email="lead@acme.example", testers=TESTERS
        )
    with pytest.raises(GenerationResidencyError, match="registered build-phase tester"):
        assert_request_allowed(generation_region="us", member_email="anyone", testers=frozenset())


def test_offshore_a_registered_tester_is_allowed_whatever_the_case_of_their_email() -> None:
    assert_request_allowed(
        generation_region="us", member_email="tester@acme.EXAMPLE", testers=TESTERS
    )


def test_offshore_a_prefix_clause_the_registry_doesnt_vouch_for_is_refused_even_for_a_tester() -> (
    None
):
    with pytest.raises(GenerationResidencyError, match="1 policy clause"):
        assert_request_allowed(
            generation_region="us",
            member_email="tester@acme.example",
            testers=TESTERS,
            real_prefix_clause_ids=(f"{POLICY}:v2:1/1",),
        )


def test_in_sydney_neither_the_asker_nor_the_prefix_is_checked() -> None:
    assert_request_allowed(
        generation_region="australia-southeast1",
        member_email="lead@acme.example",
        testers=frozenset(),
        real_prefix_clause_ids=(f"{POLICY}:v2:1/1",),
    )


def test_the_registry_vouches_for_a_clause_text_under_its_own_tenant_only() -> None:
    text = _chunk(synthetic=False).text
    assert REGISTERED.vouches_for_text("acme", text)
    assert not REGISTERED.vouches_for_text("globex", text)
    assert not REGISTERED.vouches_for_text("acme", text + " ")


def test_the_golden_corpus_marks_every_tenant_chunk_synthetic() -> None:
    """The six Acme policies are invented; if this marking were dropped, the
    recall gate's corpus would close offshore generation for everyone."""
    from compliance_rag.eval.golden_corpus import golden_chunks

    tenant = [c for c in golden_chunks() if c.scope is Scope.TENANT]
    assert tenant and all(c.synthetic for c in tenant)
