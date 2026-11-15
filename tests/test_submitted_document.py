"""A submitted document is stored as a versioned document of its own class,
and never chunked (#296, ADR-0053 U1, ADR-0059).

`CorpusLifecycle.submit_document` writes the document's passages and its source
to the clause store first (ADR-0048), then records the version in the ledger,
and never touches retrieval: every retrieval method here is spied on and none
is called. Run on `FakeClauseStore` and on the real `GCSClauseStore` over the
versioned in-memory bucket its own tests use, never a real bucket, with
generation offshore and the operator's real registry, as in the build phase.
Every fixture the segmenter has is submitted.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.generation_residency import GenerationResidencyError
from compliance_rag.adapters.interfaces import ClauseStore, RetrievalAdmin
from compliance_rag.adapters.removal_authorization import RemovalAuthorization
from compliance_rag.assessment.draft import DraftCitation
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.corpus.models import Chunk, ClauseRecord, DocumentClass, Scope, canonical_sha256
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.submitted_parser import SubmittedFormat, segment_submitted_document
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.golden_corpus import FIXTURES, submitted_fixture_files
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION, REGION
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Outcome, Verifier
from tests.test_gcs_clause_store import _FakeBucket  # pyright: ignore[reportPrivateUsage]

TENANT = "acme"
MEMBER = "uid-member-1"
OTHER_MEMBER = "uid-member-2"
SUBMITTED_ON = dt.date(2026, 11, 15)
SUBMITTED = FIXTURES / "synthetic-submitted"
PROPOSAL = SUBMITTED / "acme-customer-insights-proposal.md"
EVERY_FIXTURE = submitted_fixture_files()


def _format(path: Path) -> SubmittedFormat:
    return SubmittedFormat.MARKDOWN if path.suffix == ".md" else SubmittedFormat.TEXT


def _instrument(path: Path) -> str:
    return f"{TENANT}/{path.stem}"


def _passages(
    source: str,
    *,
    instrument_id: str = "acme/proposal",
    version_id: str = "v1",
    tenant_id: str = TENANT,
    user_id: str = MEMBER,
    fmt: SubmittedFormat = SubmittedFormat.MARKDOWN,
) -> tuple[ClauseRecord, ...]:
    return segment_submitted_document(
        source,
        document_format=fmt,
        instrument_id=instrument_id,
        version_id=version_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )


class _SpiedRetrieval(FakeRetrievalStore):
    """Retrieval that remembers every call made to it: a submitted document
    must make none."""

    def __init__(self, *, ledger: FakeLedgerStore) -> None:
        super().__init__(ledger=ledger)
        self.calls: list[str] = []

    async def upsert_chunks(self, chunks: tuple[Chunk, ...]) -> None:
        self.calls.append("upsert_chunks")
        await super().upsert_chunks(chunks)

    async def holds_real_customer_data(self) -> bool:
        self.calls.append("holds_real_customer_data")
        return await super().holds_real_customer_data()

    async def version_chunk_ids(
        self, *, instrument_id: str, tenant_id: str, version_id: str
    ) -> frozenset[str]:
        self.calls.append("version_chunk_ids")
        return await super().version_chunk_ids(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        )

    async def version_exists(self, *, instrument_id: str, tenant_id: str, version_id: str) -> bool:
        self.calls.append("version_exists")
        return await RetrievalAdmin.version_exists(
            self, instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        )

    async def retire_version(
        self, *, instrument_id: str, tenant_id: str, version_id: str, valid_to: dt.date
    ) -> None:
        self.calls.append("retire_version")
        await super().retire_version(
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=version_id,
            valid_to=valid_to,
        )

    async def withdraw_version(self, authorization: RemovalAuthorization) -> None:
        self.calls.append("withdraw_version")
        await super().withdraw_version(authorization)

    async def delete_chunks(self, authorization: RemovalAuthorization) -> None:
        self.calls.append("delete_chunks")
        await super().delete_chunks(authorization)

    async def stored_chunk_texts(
        self, *, instrument_id: str, tenant_id: str, version_id: str
    ) -> dict[str, str]:
        self.calls.append("stored_chunk_texts")
        return await super().stored_chunk_texts(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        )


class _Ledger(FakeLedgerStore):
    """A ledger whose next version record can fail, as a crash after the
    clause store was written would."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_record = False

    async def record_version(
        self,
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str,
        valid_from: dt.date,
        supersedes: str | None,
        document_class: DocumentClass,
        title: str,
    ) -> None:
        if self.fail_next_record:
            self.fail_next_record = False
            raise RuntimeError("ledger unavailable")
        await super().record_version(
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            valid_from=valid_from,
            supersedes=supersedes,
            document_class=document_class,
            title=title,
        )


class _World:
    def __init__(self, kind: str, *, region: str = BUILD_PHASE_OFFSHORE_REGION) -> None:
        self.kind = kind
        self.bucket = _FakeBucket()
        self.clauses: ClauseStore = (
            GCSClauseStore(self.bucket) if kind == "gcs" else FakeClauseStore()
        )
        self.ledger = _Ledger()
        self.retrieval = _SpiedRetrieval(ledger=self.ledger)
        self.lifecycle = CorpusLifecycle(
            ledger=self.ledger,
            retrieval=self.retrieval,
            clauses=self.clauses,
            generation_region=region,
            synthetic_registry=SyntheticRegistry.load(),
        )

    async def submit(
        self,
        source: str,
        *,
        instrument_id: str = "acme/proposal",
        version_id: str = "v1",
        tenant_id: str = TENANT,
        fmt: SubmittedFormat = SubmittedFormat.MARKDOWN,
        submitted_by: str = MEMBER,
        title: str = "Customer insights programme",
    ) -> tuple[ClauseRecord, ...]:
        """Submit `source`; what comes back is the passages it is stored as,
        cut by the segmenter on its own, to compare the store against."""
        await self.lifecycle.submit_document(
            source,
            document_format=fmt,
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=version_id,
            submitted_on=SUBMITTED_ON,
            submitted_by=submitted_by,
            title=title,
        )
        return _passages(
            source,
            instrument_id=instrument_id,
            version_id=version_id,
            tenant_id=tenant_id,
            user_id=submitted_by,
            fmt=fmt,
        )

    def assert_nothing_written(self) -> None:
        assert self.retrieval.calls == []
        assert self.retrieval.stored_chunk_ids == frozenset()
        assert not self.ledger._versions  # pyright: ignore[reportPrivateUsage]
        if isinstance(self.clauses, FakeClauseStore):
            assert not self.clauses._clauses  # pyright: ignore[reportPrivateUsage]
        else:
            assert self.bucket.objects == {}


@pytest.fixture(params=("fake", "gcs"))
def world(request: pytest.FixtureRequest) -> _World:
    kind: str = request.param
    return _World(kind)


@pytest.fixture(params=("fake", "gcs"))
def sydney(request: pytest.FixtureRequest) -> _World:
    """Generation in Australia, where no registry decides what may be stored:
    for the tests of something other than the residency gate, which use text of
    their own."""
    kind: str = request.param
    return _World(kind, region=REGION)


# ---- stored, versioned, never chunked ------------------------------------------


def test_the_fixtures_are_there() -> None:
    assert len(EVERY_FIXTURE) == 2  # the parametrised test below would pass on none


@pytest.mark.parametrize("path", EVERY_FIXTURE, ids=lambda p: p.name)
async def test_every_fixture_is_stored_as_a_submitted_document_and_never_chunked(
    world: _World, path: Path
) -> None:
    """The acceptance criteria: the retrieval store holds no chunk of the
    document, and was not so much as asked; its passages and source are in the
    clause store, its version in the ledger as its own class."""
    source = path.read_text(encoding="utf-8")
    instrument = _instrument(path)
    passages = await world.submit(source, instrument_id=instrument, fmt=_format(path))

    assert world.retrieval.calls == []
    assert world.retrieval.stored_chunk_ids == frozenset()
    assert not await world.retrieval.holds_real_customer_data()
    for passage in passages:
        stored = await world.clauses.get_clause(passage.clause_id, tenant_id=TENANT, user_id=MEMBER)
        assert stored == passage
    assert await world.clauses.get_source_html(instrument, "v1", tenant_id=TENANT) == source
    assert await world.clauses.get_manifest_metadata(instrument, "v1", tenant_id=TENANT)
    assert await world.ledger.document_class(tenant_id=TENANT, instrument_id=instrument) is (
        DocumentClass.SUBMITTED_DOCUMENT
    )
    assert await world.ledger.version_validity(
        tenant_id=TENANT, instrument_id=instrument, version_id="v1"
    ) == (SUBMITTED_ON, None)
    assert await world.ledger.version_titles(TENANT, [(instrument, "v1")]) == {
        (instrument, "v1"): "Customer insights programme"
    }


async def test_a_submitted_document_is_in_neither_the_policies_nor_the_environment(
    world: _World,
) -> None:
    await world.submit(PROPOSAL.read_text(encoding="utf-8"), instrument_id=_instrument(PROPOSAL))

    assert await world.ledger.policy_versions(TENANT, as_of=SUBMITTED_ON) == ()
    prefix = await tenant_policies(
        world.ledger, world.clauses, tenant_id=TENANT, as_of=SUBMITTED_ON
    )
    assert prefix == ()
    later = SUBMITTED_ON + dt.timedelta(days=400)
    assert await world.ledger.environment_titles(TENANT, as_of=SUBMITTED_ON) == {}
    assert await world.ledger.environment_titles(TENANT, as_of=later) == {}


async def test_the_submit_path_is_one_span_with_what_it_wrote(
    world: _World, span_exporter: InMemorySpanExporter
) -> None:
    passages = await world.submit(
        PROPOSAL.read_text(encoding="utf-8"), instrument_id=_instrument(PROPOSAL)
    )
    (span,) = (
        s
        for s in span_exporter.get_finished_spans()
        if s.name == "corpus_lifecycle.submit_document"
    )
    attributes = dict(span.attributes or {})
    assert attributes["lifecycle.version"] == f"{_instrument(PROPOSAL)}:v1"
    assert attributes["lifecycle.passages"] == len(passages)
    assert attributes["lifecycle.document_class"] == "submitted_document"
    assert attributes["lifecycle.retrieval_calls"] == 0


async def test_a_second_version_of_a_document_is_a_new_version_and_retires_nothing(
    sydney: _World,
) -> None:
    """An assessment keeps the document it judged (ADR-0034), so a resubmission
    leaves the first version as it was."""
    text = SubmittedFormat.TEXT
    one = await sydney.submit("The first draft.\n", version_id="v1", fmt=text)
    two = await sydney.submit("The second draft.\n", version_id="v2", fmt=text)
    for version in ("v1", "v2"):
        assert await sydney.ledger.version_validity(
            tenant_id=TENANT, instrument_id="acme/proposal", version_id=version
        ) == (SUBMITTED_ON, None)
    for passage in (one[0], two[0]):
        stored = await sydney.clauses.get_clause(
            passage.clause_id, tenant_id=TENANT, user_id=MEMBER
        )
        assert stored == passage


async def test_submitting_a_version_twice_is_refused_and_changes_nothing(sydney: _World) -> None:
    text = SubmittedFormat.TEXT
    first = await sydney.submit("Once.\n", fmt=text)
    with pytest.raises(ValueError, match="already submitted"):
        await sydney.submit("Twice.\n", fmt=text)
    stored = await sydney.clauses.get_clause(first[0].clause_id, tenant_id=TENANT, user_id=MEMBER)
    assert stored == first[0]
    assert (
        await sydney.clauses.get_source_html("acme/proposal", "v1", tenant_id=TENANT) == "Once.\n"
    )


async def test_a_submission_that_died_after_the_clause_store_is_finished_by_running_it_again(
    world: _World,
) -> None:
    """Clause text first (ADR-0048): the ledger's record is the commit point,
    nothing is citable before it, and a rerun writes only what is missing."""
    source = PROPOSAL.read_text(encoding="utf-8")
    instrument = _instrument(PROPOSAL)
    world.ledger.fail_next_record = True
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await world.submit(source, instrument_id=instrument)
    assert await world.clauses.get_manifest_metadata(instrument, "v1", tenant_id=TENANT)
    recorded = await world.ledger.version_validity(
        tenant_id=TENANT, instrument_id=instrument, version_id="v1"
    )
    assert recorded is None
    passages = _passages(source, instrument_id=instrument)
    assert await _verify(world, passages[0]) is Outcome.VERSION_UNKNOWN  # not citable yet

    await world.submit(source, instrument_id=instrument)
    assert await world.ledger.document_class(tenant_id=TENANT, instrument_id=instrument) is (
        DocumentClass.SUBMITTED_DOCUMENT
    )
    assert await _verify(world, passages[0]) is Outcome.VERIFIED
    assert world.retrieval.calls == []


async def _verify(world: _World, passage: ClauseRecord) -> Outcome:
    verifier = Verifier(world.clauses, world.ledger, CanonicalCitationVerifier())
    checked = await verifier.check(
        DraftCitation(clause_id=passage.clause_id, quote=passage.canonical_text),
        context=frozenset({passage.clause_id}),
        as_of=SUBMITTED_ON,
        user_id=MEMBER,
    )
    return checked.outcome


# ---- the residency gate ------------------------------------------------------


async def test_offshore_unregistered_text_is_refused_with_nothing_written(world: _World) -> None:
    source = "Acme will sell the customer list to a data broker in Singapore.\n"
    with pytest.raises(GenerationResidencyError, match="isn't registered synthetic"):
        await world.submit(source, fmt=SubmittedFormat.TEXT)
    world.assert_nothing_written()


async def test_one_unregistered_passage_among_registered_ones_refuses_the_whole_document(
    world: _World,
) -> None:
    source = PROPOSAL.read_text(encoding="utf-8") + "\nAnd we will also sell the list.\n"
    with pytest.raises(GenerationResidencyError, match="1 passage"):
        await world.submit(source, instrument_id=_instrument(PROPOSAL))
    world.assert_nothing_written()


async def test_registered_text_under_another_tenants_name_is_real(world: _World) -> None:
    """The registry vouches for a tenant's fixture, not for the words."""
    with pytest.raises(GenerationResidencyError):
        await world.submit(
            PROPOSAL.read_text(encoding="utf-8"), instrument_id="beta/proposal", tenant_id="beta"
        )
    world.assert_nothing_written()


async def test_in_sydney_any_text_may_be_submitted() -> None:
    """Generation in Australia needs no gate (ADR-0023): real text is allowed,
    and is still never chunked."""
    world = _World("fake", region=REGION)
    await world.submit(
        "Acme will sell the customer list to a data broker in Singapore.\n",
        fmt=SubmittedFormat.TEXT,
    )
    assert world.retrieval.calls == []
    assert world.retrieval.stored_chunk_ids == frozenset()
    assert await world.ledger.document_class(tenant_id=TENANT, instrument_id="acme/proposal")


# ---- visibility ----------------------------------------------------------------


async def test_a_passage_is_given_to_its_submitter_and_to_nobody_else(world: _World) -> None:
    """Private to the member who submitted it (ADR-0036): another member, and a
    caller naming nobody, are told there is no such clause."""
    passages = await world.submit(
        PROPOSAL.read_text(encoding="utf-8"), instrument_id=_instrument(PROPOSAL)
    )
    for passage in passages:
        cid = passage.clause_id
        assert (passage.scope, passage.user_id) == (Scope.USER, MEMBER)
        assert await world.clauses.get_clause(cid, tenant_id=TENANT, user_id=MEMBER) == passage
        assert await world.clauses.get_clause(cid, tenant_id=TENANT, user_id=OTHER_MEMBER) is None
        assert await world.clauses.get_clause(cid, tenant_id=TENANT) is None
        assert await world.clauses.get_clause(cid, tenant_id="beta", user_id=MEMBER) is None


@pytest.mark.parametrize("submitter", ["", "   "])
async def test_a_submission_names_its_submitter(world: _World, submitter: str) -> None:
    with pytest.raises(ValueError, match="user_id"):
        await world.submit("One paragraph.\n", fmt=SubmittedFormat.TEXT, submitted_by=submitter)
    world.assert_nothing_written()


# ---- the other guardrails -------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "\n  \n"])
async def test_a_document_with_no_words_is_refused(world: _World, blank: str) -> None:
    with pytest.raises(ValueError, match="no passage"):
        await world.submit(blank, fmt=SubmittedFormat.TEXT)
    world.assert_nothing_written()


async def test_a_title_that_is_not_a_title_is_refused_before_a_write(world: _World) -> None:
    for title in ("", "two\nlines"):
        with pytest.raises(ValueError, match="title"):
            await world.submit("One paragraph.\n", fmt=SubmittedFormat.TEXT, title=title)
    world.assert_nothing_written()


CHUNKING_PATHS = (
    "publish_version",
    "check_publishable",
    "complete_version",
    "record_published_version",
)


@pytest.mark.parametrize("path", CHUNKING_PATHS)
async def test_a_submitted_document_can_not_be_published_as_a_policy_or_chunked(
    world: _World, path: str
) -> None:
    """The class that is never chunked is refused by every path that chunks,
    each on its own, before anything is written."""
    passages = _passages("One paragraph.\n", fmt=SubmittedFormat.TEXT)
    common: dict[str, Any] = {  # Any: the keywords the chunking paths take alike
        "instrument_id": "acme/proposal",
        "tenant_id": TENANT,
        "version_id": "v1",
        "document_class": DocumentClass.SUBMITTED_DOCUMENT,
    }
    lifecycle = world.lifecycle
    calls = {
        "publish_version": lambda: lifecycle.publish_version(
            passages,
            count_tokens=count_reranker_tokens,
            source_html="One paragraph.\n",
            effective_from=SUBMITTED_ON,
            supersedes=None,
            title="T",
            **common,
        ),
        "check_publishable": lambda: lifecycle.check_publishable(
            passages, count_tokens=count_reranker_tokens, title="T", **common
        ),
        "complete_version": lambda: lifecycle.complete_version(
            passages,
            count_tokens=count_reranker_tokens,
            source_html="One paragraph.\n",
            effective_from=SUBMITTED_ON,
            **common,
        ),
        "record_published_version": lambda: lifecycle.record_published_version(
            effective_from=SUBMITTED_ON, supersedes=None, title="T", **common
        ),
    }
    assert set(calls) == set(CHUNKING_PATHS)
    with pytest.raises(ValueError, match="never chunked"):
        await calls[path]()
    world.assert_nothing_written()


async def test_replace_in_place_can_not_chunk_a_submitted_document(sydney: _World) -> None:
    (passage,) = await sydney.submit("One paragraph.\n", fmt=SubmittedFormat.TEXT)
    chunk = Chunk(
        chunk_id=passage.clause_id,
        clause_id=passage.clause_id,
        text=passage.text,
        scope=Scope.USER,
        tenant_id=TENANT,
        user_id=MEMBER,
    )
    other = passage.model_copy(update={"text": "Other.", "canonical_text": "Other."})
    with pytest.raises(ValueError, match="never chunked"):
        await sydney.lifecycle.replace_in_place(
            (chunk,),
            (other,),
            tenant_id=TENANT,
            instrument_id="acme/proposal",
            version_id="v1",
            source_html="Other.\n",
            actor="admin",
            reason="typo",
            acknowledged_findings_citing=0,
        )
    assert sydney.retrieval.stored_chunk_ids == frozenset()
    # The clause store is as it was: the passage, its manifest and its source.
    stored = await sydney.clauses.get_clause(passage.clause_id, tenant_id=TENANT, user_id=MEMBER)
    assert stored == passage
    assert await sydney.clauses.get_manifest("acme/proposal", "v1", tenant_id=TENANT) == {
        "p1": canonical_sha256(passage.canonical_text)
    }
    assert (
        await sydney.clauses.get_source_html("acme/proposal", "v1", tenant_id=TENANT)
        == "One paragraph.\n"
    )


async def test_an_instrument_of_another_class_is_not_submitted_to(sydney: _World) -> None:
    clause = ClauseRecord.build(
        instrument_id="acme/proposal",
        version_id="v0",
        clause_path="1/1",
        text="A policy sentence.",
        scope=Scope.TENANT,
        tenant_id=TENANT,
    )
    await sydney.lifecycle.publish_version(
        (clause,),
        count_tokens=count_reranker_tokens,
        source_html="Policy text.\n",
        instrument_id="acme/proposal",
        tenant_id=TENANT,
        version_id="v0",
        effective_from=SUBMITTED_ON,
        supersedes=None,
        document_class=DocumentClass.INTERNAL_POLICY,
        title="A policy",
    )
    with pytest.raises(ValueError, match="internal_policy, not submitted_document"):
        await sydney.submit("One paragraph.\n", fmt=SubmittedFormat.TEXT)


async def test_library_law_is_not_a_submission() -> None:
    world = _World("fake")
    with pytest.raises(ValueError, match="library law"):
        await world.submit("One paragraph.\n", fmt=SubmittedFormat.TEXT, tenant_id="_public")
    world.assert_nothing_written()


# ---- withdraw and purge are ADR-0021's -----------------------------------------


async def test_withdraw_and_purge_are_the_existing_operations(world: _World) -> None:
    instrument = _instrument(PROPOSAL)
    passages = await world.submit(PROPOSAL.read_text(encoding="utf-8"), instrument_id=instrument)

    await world.lifecycle.withdraw(
        tenant_id=TENANT, instrument_id=instrument, version_id="v1", actor="admin", reason="oops"
    )
    assert await world.ledger.is_withdrawn(
        tenant_id=TENANT, instrument_id=instrument, version_id="v1"
    )
    # Withdrawn keeps the text, as for a policy: prior assessments still read it.
    first = passages[0].clause_id
    assert await world.clauses.get_clause(first, tenant_id=TENANT, user_id=MEMBER)

    await world.lifecycle.purge(
        tenant_id=TENANT,
        instrument_id=instrument,
        version_id="v1",
        actor="admin",
        reason="member asked",
        acknowledged_findings_citing=0,
    )
    for passage in passages:
        gone = await world.clauses.get_clause(passage.clause_id, tenant_id=TENANT, user_id=MEMBER)
        assert gone is None
    assert await world.clauses.get_source_html(instrument, "v1", tenant_id=TENANT) is None
    assert await world.clauses.get_manifest(instrument, "v1", tenant_id=TENANT) == {}
    assert await world.ledger.purge_marker(
        tenant_id=TENANT, instrument_id=instrument, version_id="v1"
    ) == ("admin", "member asked")
    assert world.retrieval.stored_chunk_ids == frozenset()
