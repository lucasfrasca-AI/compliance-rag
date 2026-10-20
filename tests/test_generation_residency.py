"""The build-phase residency gate (ADR-0023). On ingest: while generation runs
offshore, a real tenant upload fails loudly at the door instead of being sent
outside Australia on its first question. Both ways, through the real write path
(`CorpusLifecycle`). Per request (ADR-0032): the question and the policy prefix,
which never pass through the stores the startup gate asks."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    TenantTextGate,
    assert_question_allowed,
    assert_upload_allowed,
    is_real_customer_data,
    validate_generation_region,
)
from compliance_rag.assessment.pipeline import LIBRARY_QUESTION, AssessmentPipeline
from compliance_rag.assessment.prompt import SourceClause
from compliance_rag.corpus.models import (
    Chunk,
    ClauseRecord,
    DocumentClass,
    Scope,
    parse_stable_id,
)
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.golden_corpus import golden_chunks, golden_clauses
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier

POLICY = "acme/retention-policy"


def _chunk(*, synthetic: bool, scope: Scope = Scope.TENANT) -> Chunk:
    return Chunk(
        chunk_id=f"{POLICY}:v1:1/1",
        clause_id=f"{POLICY}:v1:1/1",
        text="Accounts are kept seven years.",
        scope=scope,
        tenant_id="acme",
        user_id="alice" if scope is Scope.USER else None,
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
    """Publish the clause `chunk` was made from: the lifecycle takes clauses
    and derives their chunks (#233), so this chunk's own flag never reaches it."""
    instrument_id, version_id, clause_path = parse_stable_id(chunk.clause_id)
    clause = ClauseRecord.build(
        instrument_id=instrument_id,
        version_id=version_id,
        clause_path=clause_path,
        text=chunk.text,
        scope=chunk.scope,
        tenant_id=chunk.tenant_id,
        user_id=chunk.user_id,
    )
    await lifecycle.publish_version(
        (clause,),
        count_tokens=lambda text: len(text.split()),
        source_html="<p>retention policy</p>",
        instrument_id=POLICY,
        tenant_id="acme",
        version_id="v1",
        effective_from=dt.date(2026, 1, 1),
        supersedes=None,
        # One person's private additions can't be a policy: every member reads
        # the policy corpus in the cached prefix (ADR-0036).
        document_class=(
            DocumentClass.EXTERNAL_UPLOAD
            if chunk.scope is Scope.USER
            else DocumentClass.INTERNAL_POLICY
        ),
        title="The document's title",
    )


def _real_chunk() -> Chunk:
    return _chunk(synthetic=False).model_copy(update={"text": "Acme keeps payroll for 7 years."})


async def test_the_first_real_policy_upload_fails_while_generation_is_offshore() -> None:
    lifecycle, store = _lifecycle("us")
    with pytest.raises(GenerationResidencyError, match="of real customer data"):
        await _publish(lifecycle, _real_chunk())
    assert not await store.holds_real_customer_data()  # refused before any write
    assert store.stored_chunk_ids == frozenset()


async def test_a_registered_synthetic_policy_is_accepted_while_generation_is_offshore() -> None:
    lifecycle, store = _lifecycle("us")
    await _publish(lifecycle, _chunk(synthetic=False))  # synthetic because registered
    assert store.stored_chunk_ids == frozenset({f"{POLICY}:v1:1/1"})
    assert not await store.holds_real_customer_data()


async def test_a_real_policy_is_accepted_once_generation_is_in_sydney() -> None:
    lifecycle, store = _lifecycle("australia-southeast1")
    await _publish(lifecycle, _real_chunk())
    assert await store.holds_real_customer_data()


async def test_a_users_private_chunk_counts_as_real_too() -> None:
    """`user` scope is one person's private additions: customer data as much
    as a tenant's policy (#166). Unregistered, it closes the gate in the store
    and is refused at the door while generation is offshore."""
    mine = _chunk(synthetic=False, scope=Scope.USER).model_copy(
        update={"text": "My notes on Acme's payroll."}
    )
    assert is_real_customer_data(mine)
    store = FakeRetrievalStore((mine,), ledger=FakeLedgerStore())
    assert await store.holds_real_customer_data()
    with pytest.raises(GenerationResidencyError, match="of real customer data"):
        assert_upload_allowed((mine,), generation_region="us")
    # Through the write path, which derives the flag from the registry first.
    lifecycle, lifecycle_store = _lifecycle("us")
    with pytest.raises(GenerationResidencyError, match="of real customer data"):
        await _publish(lifecycle, mine)
    assert lifecycle_store.stored_chunk_ids == frozenset()
    record = ClauseRecord(
        instrument_id="acme/my-notes",
        version_id="v1",
        clause_path="1/1",
        text="My notes on Acme's payroll.",
        canonical_text="My notes on Acme's payroll.",
        scope=Scope.USER,
        tenant_id="acme",
        user_id="alice",
    )
    assert REGISTERED.real_clauses((record,)) == (record.clause_id,)


async def test_a_registered_user_scoped_chunk_is_accepted_offshore() -> None:
    """The positive control: the registry keys on the tenant and the text,
    not the scope, so registered text is synthetic in either scope."""
    lifecycle, store = _lifecycle("us")
    await _publish(lifecycle, _chunk(synthetic=False, scope=Scope.USER))
    assert store.stored_chunk_ids == frozenset({f"{POLICY}:v1:1/1"})
    assert not await store.holds_real_customer_data()


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
        assert_question_allowed(generation_region="us", asker="lead@acme.example", testers=TESTERS)
    with pytest.raises(GenerationResidencyError, match="registered build-phase tester"):
        assert_question_allowed(generation_region="us", asker="anyone", testers=frozenset())


def test_offshore_a_registered_tester_is_allowed_whatever_the_case_of_their_email() -> None:
    assert_question_allowed(generation_region="us", asker="tester@acme.EXAMPLE", testers=TESTERS)


def test_in_sydney_any_question_is_allowed() -> None:
    assert_question_allowed(
        generation_region="australia-southeast1", asker="lead@acme.example", testers=frozenset()
    )


def test_offshore_tenant_text_the_registry_doesnt_vouch_for_is_refused() -> None:
    gate = TenantTextGate("us", REGISTERED)
    registered = (f"{POLICY}:v1:1/1", _chunk(synthetic=False).text)
    gate.check("acme", (registered,), what="clause(s) of the CAG prefix")
    with pytest.raises(GenerationResidencyError, match="1 clause\\(s\\) of the CAG prefix"):
        gate.check(
            "acme",
            (registered, (f"{POLICY}:v2:1/1", "Acme keeps payroll for 7 years.")),
            what="clause(s) of the CAG prefix",
        )
    # The key binds the tenant as well as the words (ADR-0026).
    with pytest.raises(GenerationResidencyError):
        gate.check("globex", (registered,), what="clause to repair")


def test_in_sydney_tenant_text_is_not_checked() -> None:
    TenantTextGate("australia-southeast1", REGISTERED).check(
        "acme", ((f"{POLICY}:v2:1/1", "Real."),), what="clause to repair"
    )


def test_a_tenant_text_gate_refuses_a_region_that_is_never_allowed() -> None:
    with pytest.raises(GenerationResidencyError):
        TenantTextGate("global", REGISTERED)


def test_the_golden_corpus_marks_every_tenant_chunk_synthetic() -> None:
    """The six Acme policies are invented; if this marking were dropped, the
    recall gate's corpus would close offshore generation for everyone."""
    tenant = [c for c in golden_chunks() if c.scope is Scope.TENANT]
    assert tenant and all(c.synthetic for c in tenant)


# ---- inside answer(): text read from the bucket (ADR-0032) --------------------


async def _offshore_pipeline(
    reply: str,
) -> tuple[AssessmentPipeline, FakeGenerator, FakeClauseStore]:
    from compliance_rag.eval.harness import golden_clause_store, golden_ledger

    ledger, clauses = await golden_ledger(), await golden_clause_store()
    generator = FakeGenerator(reply)
    pipeline = AssessmentPipeline(
        RetrievalPipeline(FakeRetrievalStore(golden_chunks(), ledger=ledger), FakeReranker()),
        generator,
        Verifier(clauses, ledger, CanonicalCitationVerifier()),
        ledger,
        tenant_text=TenantTextGate("us", SyntheticRegistry.load()),
    )
    return pipeline, generator, clauses


def _acme_clause() -> ClauseRecord:
    return next(c for c in golden_clauses() if c.instrument_id == "acme/privacy-policy")


async def test_offshore_answer_refuses_an_unregistered_prefix_before_retrieving() -> None:
    """Whoever calls `answer()`, the API or a script: the prefix is checked
    before anything is retrieved or generated."""
    pipeline, generator, _ = await _offshore_pipeline('{"answer": "-", "citations": []}')
    clause = _acme_clause()
    retrieved: list[RetrievalTrace] = []

    async def seen(trace: RetrievalTrace) -> None:
        retrieved.append(trace)

    with pytest.raises(GenerationResidencyError, match="CAG prefix"):
        await pipeline.answer(
            "What does Acme do with personal information?",
            "acme",
            dt.date(2026, 10, 12),
            law=LIBRARY_QUESTION,
            policies=(SourceClause(clause.clause_id, "Acme keeps payroll for 7 years."),),
            on_retrieved=seen,
        )
    assert retrieved == [] and generator.calls == []


async def test_offshore_a_clause_to_repair_the_registry_doesnt_vouch_for_is_never_sent() -> None:
    """A repair sends a clause's canonical text, read from the bucket, which
    the generator's store gate never asks. The retrieved chunk is registered;
    the bucket's text for the same clause isn't, so it is real data."""
    clause = _acme_clause()
    reply = json.dumps(
        {"answer": "-", "citations": [{"clause_id": clause.clause_id, "quote": "not in it"}]}
    )
    pipeline, generator, clauses = await _offshore_pipeline(reply)
    real = "Acme keeps the payroll of every employee for seven years."
    await clauses.put_clause(clause.model_copy(update={"canonical_text": real}))
    question = next(c.text for c in golden_chunks() if c.clause_id == clause.clause_id)

    with pytest.raises(GenerationResidencyError, match="clause to repair"):
        await pipeline.answer(question, "acme", dt.date(2026, 10, 12), law=LIBRARY_QUESTION)
    assert len(generator.calls) == 1  # the answer; never the repair
    assert all(real not in str(call) for call in generator.calls)
