"""Publishing a policy writes what its citations verify against (#233, ADR-0048).

`CorpusLifecycle.publish_version` takes a version's clauses, writes their
canonical text and the version's manifest to the clause store, derives and
upserts the chunks, then records the version in the ledger. The verifier and
the cached policy prefix read the clause store, so a policy published this
way — what an upload API will call — can be cited, and its citations verify.

Run on `FakeClauseStore` and on the real `GCSClauseStore` over the versioned
in-memory bucket its own tests use (`tests/test_gcs_clause_store.py`): never a
real bucket. Every one of the six synthetic Acme policies is published, with
generation offshore and the operator's real registry, as in the build phase.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path

import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.generation_residency import GenerationResidencyError, TenantTextGate
from compliance_rag.adapters.interfaces import ClauseStore
from compliance_rag.assessment.draft import DraftCitation
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import Chunk, ClauseRecord, DocumentClass, Scope, stable_id
from compliance_rag.corpus.policy_parser import parse_policy
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.golden_corpus import FIXTURES, POLICY_INSTRUMENT_IDS
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Outcome, Verifier
from tests.test_gcs_clause_store import _FakeBucket  # pyright: ignore[reportPrivateUsage]

TENANT = "acme"
VERSION = "v1"
EFFECTIVE = dt.date(2026, 1, 1)
AS_OF = dt.date(2026, 10, 1)
POLICIES = FIXTURES / "synthetic-policies"
PRIVACY = "acme/privacy-policy"


def _markdown(filename: str) -> str:
    return (POLICIES / filename).read_text(encoding="utf-8")


def _parsed(filename: str, *, pdf: Path | None = None) -> tuple[ClauseRecord, ...]:
    return parse_policy(
        _markdown(filename),
        instrument_id=POLICY_INSTRUMENT_IDS[filename],
        version_id=VERSION,
        tenant_id=TENANT,
        pdf_bytes=None if pdf is None else pdf.read_bytes(),
    )


EVERY_POLICY = tuple(POLICY_INSTRUMENT_IDS)
"""All six fixtures, not a representative one (`CLAUDE.md`: a parser or
extractor is tested against every available real fixture)."""
PRIVACY_FILE = next(f for f, i in POLICY_INSTRUMENT_IDS.items() if i == PRIVACY)


class _Backend:
    """One clause store under test, with what a test needs to see of it."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.bucket = _FakeBucket()
        self.clauses: ClauseStore = (
            GCSClauseStore(self.bucket) if kind == "gcs" else FakeClauseStore()
        )


BACKENDS = ("fake", "gcs")


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> _Backend:
    kind: str = request.param
    return _Backend(kind)


class _FailingRetrieval(FakeRetrievalStore):
    """Retrieval that dies part-way through its next upsert: after
    `keep` chunks, or before any."""

    def __init__(self, *, ledger: FakeLedgerStore) -> None:
        super().__init__(ledger=ledger)
        self.fail_after: int | None = None

    async def upsert_chunks(self, chunks: tuple[Chunk, ...]) -> None:
        if self.fail_after is None:
            return await super().upsert_chunks(chunks)
        keep, self.fail_after = self.fail_after, None
        await super().upsert_chunks(chunks[:keep])
        raise RuntimeError("qdrant unavailable")


class _Stores:
    def __init__(self, clauses: ClauseStore) -> None:
        self.ledger = FakeLedgerStore()
        self.retrieval = _FailingRetrieval(ledger=self.ledger)
        self.clauses = clauses
        self.registry = SyntheticRegistry.load()
        self.lifecycle = CorpusLifecycle(
            ledger=self.ledger,
            retrieval=self.retrieval,
            clauses=clauses,
            generation_region=BUILD_PHASE_OFFSHORE_REGION,
            synthetic_registry=self.registry,
        )
        self.verifier = Verifier(clauses, self.ledger, CanonicalCitationVerifier())

    async def publish(self, clauses: tuple[ClauseRecord, ...], *, source: str) -> None:
        await self.lifecycle.publish_version(
            clauses,
            count_tokens=count_reranker_tokens,
            source_html=source,
            instrument_id=clauses[0].instrument_id,
            tenant_id=TENANT,
            version_id=VERSION,
            effective_from=EFFECTIVE,
            supersedes=None,
            document_class=DocumentClass.INTERNAL_POLICY,
        )

    async def complete(self, clauses: tuple[ClauseRecord, ...], *, source: str) -> int:
        return await self.lifecycle.complete_version(
            clauses,
            count_tokens=count_reranker_tokens,
            source_html=source,
            instrument_id=clauses[0].instrument_id,
            tenant_id=TENANT,
            version_id=VERSION,
            effective_from=EFFECTIVE,
            document_class=DocumentClass.INTERNAL_POLICY,
        )

    async def record(self, instrument_id: str) -> None:
        await self.lifecycle.record_published_version(
            instrument_id=instrument_id,
            tenant_id=TENANT,
            version_id=VERSION,
            effective_from=EFFECTIVE,
            supersedes=None,
            document_class=DocumentClass.INTERNAL_POLICY,
        )

    async def check(self, clause_id: str, quote: str) -> Outcome:
        checked = await self.verifier.check(
            DraftCitation(clause_id=clause_id, quote=quote),
            context=frozenset({clause_id}),
            as_of=AS_OF,
        )
        return checked.outcome


def _wrong(text: str) -> str:
    """The clause's own words with one changed: a quote that isn't in it."""
    words = text.split()
    words[len(words) // 2] = "never" if words[len(words) // 2] != "never" else "always"
    return " ".join(words)


# ---- end to end -------------------------------------------------------------


async def test_every_published_policy_is_cited_and_verified(backend: _Backend) -> None:
    """The acceptance criterion: published through the lifecycle and nothing
    else, each of the six policies is in the cached prefix, offshore
    generation may read it (it is registered synthetic), and every one of its
    clauses verifies a correct quote and blocks a wrong one."""
    stores = _Stores(backend.clauses)
    published: list[ClauseRecord] = []
    for filename in EVERY_POLICY:
        clauses = _parsed(filename)
        assert clauses, filename
        await stores.publish(clauses, source=_markdown(filename))
        published += clauses

    prefix = await tenant_policies(stores.ledger, stores.clauses, tenant_id=TENANT, as_of=AS_OF)
    assert {(c.clause_id, c.text) for c in prefix} == {
        (c.clause_id, c.canonical_text) for c in published
    }
    TenantTextGate(BUILD_PHASE_OFFSHORE_REGION, stores.registry).check(
        TENANT, ((c.clause_id, c.text) for c in prefix), what="clause(s) of the CAG prefix"
    )
    for clause in published:
        text = clause.canonical_text
        assert await stores.check(clause.clause_id, text) is Outcome.VERIFIED, clause.clause_id
        middle = " ".join(text.split()[1:-1]) or text
        assert await stores.check(clause.clause_id, middle) is Outcome.VERIFIED, clause.clause_id
        assert await stores.check(clause.clause_id, _wrong(text)) is Outcome.QUOTE_MISMATCH, (
            clause.clause_id
        )
    unparsed = stable_id(PRIVACY, VERSION, "99/9")
    assert await stores.check(unparsed, "anything") is Outcome.CLAUSE_MISSING


async def test_a_policy_with_pdf_positions_keeps_them_in_the_clause_store(
    backend: _Backend,
) -> None:
    """Click-to-clause for an uploaded PDF reads page and offsets off the
    stored clause (`CONTEXT.md`: Position map): published, they are stored
    as parsed, and the text is the Markdown's either way."""
    stores = _Stores(backend.clauses)
    clauses = _parsed(PRIVACY_FILE, pdf=POLICIES / "acme-privacy-policy.pdf")
    assert all(c.page is not None for c in clauses)
    await stores.publish(clauses, source=_markdown(PRIVACY_FILE))
    for clause in clauses:
        assert await stores.clauses.get_clause(clause.clause_id, tenant_id=TENANT) == clause
    assert await stores.clauses.get_source_html(PRIVACY, VERSION, tenant_id=TENANT) == _markdown(
        PRIVACY_FILE
    )


async def test_a_real_policy_offshore_reaches_neither_store(backend: _Backend) -> None:
    """The residency gate stands in front of the clause store too (ADR-0023):
    unregistered text is refused before anything is written."""
    stores = _Stores(backend.clauses)
    real = tuple(
        c.model_copy(update={"text": t, "canonical_text": t})
        for c in _parsed(PRIVACY_FILE)
        for t in (f"{c.canonical_text} Acme also sells this data.",)
    )
    with pytest.raises(GenerationResidencyError, match="of real customer data"):
        await stores.publish(real, source=_markdown(PRIVACY_FILE))
    assert not await stores.clauses.get_manifest(PRIVACY, VERSION, tenant_id=TENANT)
    assert stores.retrieval.stored_chunk_ids == frozenset()
    if backend.kind == "gcs":
        assert backend.bucket.objects == {}


# ---- a publish that stops part-way -------------------------------------------


async def test_a_publish_that_died_before_retrieval_is_finished_by_running_it_again(
    backend: _Backend,
) -> None:
    stores = _Stores(backend.clauses)
    clauses, source = _parsed(PRIVACY_FILE), _markdown(PRIVACY_FILE)
    stores.retrieval.fail_after = 0
    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        await stores.publish(clauses, source=source)
    # The clause store is complete; nothing else is written, and until the
    # ledger records the version its citations block and the prefix skips it.
    assert await stores.clauses.get_manifest_metadata(PRIVACY, VERSION, tenant_id=TENANT)
    assert stores.retrieval.stored_chunk_ids == frozenset()
    first = clauses[0]
    assert await stores.check(first.clause_id, first.canonical_text) is Outcome.VERSION_UNKNOWN
    assert not await tenant_policies(stores.ledger, stores.clauses, tenant_id=TENANT, as_of=AS_OF)
    generations = len(backend.bucket.noncurrent)

    await stores.publish(clauses, source=source)
    assert stores.retrieval.stored_chunk_ids == {c.clause_id for c in clauses}
    assert await stores.check(first.clause_id, first.canonical_text) is Outcome.VERIFIED
    # Nothing in the bucket was written again: no new noncurrent generation.
    assert len(backend.bucket.noncurrent) == generations


async def test_a_publish_that_died_in_retrieval_is_finished_by_complete_version(
    backend: _Backend,
) -> None:
    """Some chunks stored: publishing again is refused, as re-publishing is,
    and `complete_version` then `record_published_version` finish it.
    Finishing twice writes nothing the second time."""
    stores = _Stores(backend.clauses)
    clauses, source = _parsed(PRIVACY_FILE), _markdown(PRIVACY_FILE)
    stores.retrieval.fail_after = 3
    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        await stores.publish(clauses, source=source)
    assert len(stores.retrieval.stored_chunk_ids) == 3
    with pytest.raises(ValueError, match="already published"):
        await stores.publish(clauses, source=source)

    assert await stores.complete(clauses, source=source) == len(clauses) - 3
    assert await stores.complete(clauses, source=source) == 0
    await stores.record(PRIVACY)
    assert stores.retrieval.stored_chunk_ids == {c.clause_id for c in clauses}
    for clause in clauses:
        assert await stores.check(clause.clause_id, clause.canonical_text) is Outcome.VERIFIED


async def test_a_publish_that_died_inside_the_clause_store_is_finished_by_running_it_again(
    backend: _Backend,
) -> None:
    """The Cloud Storage adapter lists every path on the manifest before it
    writes the clause objects (an intent record): a crash between leaves
    paths with no object, which a re-run writes."""
    stores = _Stores(backend.clauses)
    clauses, source = _parsed(PRIVACY_FILE), _markdown(PRIVACY_FILE)
    # What the dead run left: two clause objects written, and in the bucket
    # every path already listed.
    if backend.kind == "gcs":
        await stores.clauses.put_clauses(clauses)
        for clause in clauses[2:]:
            del backend.bucket.objects[f"{PRIVACY}/{VERSION}/clauses/{clause.clause_path}.json"]
        assert set(await stores.clauses.get_manifest(PRIVACY, VERSION, tenant_id=TENANT)) == {
            c.clause_path for c in clauses
        }
    else:
        await stores.clauses.put_clauses(clauses[:2])
    assert await stores.clauses.get_manifest_metadata(PRIVACY, VERSION, tenant_id=TENANT) is None
    await stores.publish(clauses, source=source)
    for clause in clauses:
        assert await stores.clauses.get_clause(clause.clause_id, tenant_id=TENANT) == clause
        assert await stores.check(clause.clause_id, clause.canonical_text) is Outcome.VERIFIED


Change = Callable[[tuple[ClauseRecord, ...]], tuple[ClauseRecord, ...]]


def _reworded(cs: tuple[ClauseRecord, ...]) -> tuple[ClauseRecord, ...]:
    """Another registered clause's words in the first clause, so the residency
    gate passes it and the clause store's own check is what refuses it."""
    words = {"text": cs[1].text, "canonical_text": cs[1].canonical_text}
    return (cs[0].model_copy(update=words), *cs[1:])


def _dropped(cs: tuple[ClauseRecord, ...]) -> tuple[ClauseRecord, ...]:
    return cs[1:]


def _reheaded(cs: tuple[ClauseRecord, ...]) -> tuple[ClauseRecord, ...]:
    return (cs[0].model_copy(update={"heading": "Another heading"}), *cs[1:])


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (_reworded, "different text"),
        (_dropped, "not among those given"),
        (_reheaded, "differently from the clause given"),
    ],
    ids=["a clause reworded", "a clause dropped", "a clause otherwise changed"],
)
async def test_after_a_failure_a_different_document_under_the_version_is_refused(
    backend: _Backend,
    change: Change,
    refusal: str,
) -> None:
    """Finishing writes only what is missing, so a re-run that brings another
    document under the same version_id is refused before any write: that is a
    new version, or a replace in place."""
    stores = _Stores(backend.clauses)
    clauses, source = _parsed(PRIVACY_FILE), _markdown(PRIVACY_FILE)
    stores.retrieval.fail_after = 0
    with pytest.raises(RuntimeError):
        await stores.publish(clauses, source=source)
    before = dict(backend.bucket.objects)
    with pytest.raises(ValueError, match=refusal):
        await stores.publish(change(clauses), source=source)
    with pytest.raises(ValueError, match="different source"):
        await stores.publish(clauses, source=source + "\nedited")
    assert stores.retrieval.stored_chunk_ids == frozenset()
    assert backend.bucket.objects == before
    for clause in clauses:
        assert await stores.clauses.get_clause(clause.clause_id, tenant_id=TENANT) == clause


async def test_a_version_whose_clause_text_was_never_written_is_not_recorded(
    backend: _Backend,
) -> None:
    """Chunks upserted with no clause text (as `publish_version` did before
    #233): the ledger record would make citations that can't verify, so it is
    refused until `complete_version` writes the text."""
    stores = _Stores(backend.clauses)
    clauses, source = _parsed(PRIVACY_FILE), _markdown(PRIVACY_FILE)
    chunks = stores.registry.mark(chunk_clauses(clauses, count_tokens=count_reranker_tokens))
    await stores.retrieval.upsert_chunks(chunks)
    with pytest.raises(ValueError, match="doesn't hold it complete"):
        await stores.record(PRIVACY)
    assert await stores.complete(clauses, source=source) == 0
    await stores.record(PRIVACY)
    first = clauses[0]
    assert await stores.check(first.clause_id, first.canonical_text) is Outcome.VERIFIED


async def test_a_users_private_addition_is_stored_for_and_verified_for_its_owner_alone(
    backend: _Backend,
) -> None:
    """User scope (ADR-0036): the clause text published carries its owner,
    and the verifier reads it for her answer and for no one else's."""
    ledger = FakeLedgerStore()
    lifecycle = CorpusLifecycle(
        ledger=ledger,
        retrieval=FakeRetrievalStore(ledger=ledger),
        clauses=backend.clauses,
        generation_region="australia-southeast1",  # real data is allowed in Sydney
        synthetic_registry=SyntheticRegistry.load(),
    )
    note = ClauseRecord.build(
        instrument_id="acme/alice-notes",
        version_id=VERSION,
        clause_path="1/1",
        text="My notes: the backup keys stay offline.",
        scope=Scope.USER,
        tenant_id=TENANT,
        user_id="uid-alice",
    )
    await lifecycle.publish_version(
        (note,),
        count_tokens=count_reranker_tokens,
        source_html="My notes: the backup keys stay offline.",
        instrument_id=note.instrument_id,
        tenant_id=TENANT,
        version_id=VERSION,
        effective_from=EFFECTIVE,
        supersedes=None,
        document_class=DocumentClass.EXTERNAL_UPLOAD,
    )
    verifier = Verifier(backend.clauses, ledger, CanonicalCitationVerifier())
    citation = DraftCitation(clause_id=note.clause_id, quote="the backup keys stay offline")
    for member, outcome in (
        ("uid-alice", Outcome.VERIFIED),
        ("uid-bob", Outcome.CLAUSE_MISSING),
        (None, Outcome.CLAUSE_MISSING),
    ):
        checked = await verifier.check(
            citation, context=frozenset({note.clause_id}), as_of=AS_OF, user_id=member
        )
        assert checked.outcome is outcome, member


async def test_a_private_clause_is_never_written_onto_a_path_its_owner_cant_read(
    backend: _Backend,
) -> None:
    """Owner-only reads (ADR-0036) can't tell an owner's own unwritten object
    from another member's private clause on the same path, so the lifecycle
    refuses rather than write over what might be someone else's."""
    words = "My notes: the backup keys stay offline."

    def note(owner: str) -> ClauseRecord:
        return ClauseRecord.build(
            instrument_id="acme/shared-notes",
            version_id=VERSION,
            clause_path="1/1",
            text=words,
            scope=Scope.USER,
            tenant_id=TENANT,
            user_id=owner,
        )

    bobs = note("uid-bob")
    await backend.clauses.put_clauses((bobs,))
    ledger = FakeLedgerStore()
    lifecycle = CorpusLifecycle(
        ledger=ledger,
        retrieval=FakeRetrievalStore(ledger=ledger),
        clauses=backend.clauses,
        generation_region="australia-southeast1",
        synthetic_registry=SyntheticRegistry.load(),
    )
    with pytest.raises(ValueError, match="its owner can't read it"):
        await lifecycle.publish_version(
            (note("uid-alice"),),
            count_tokens=count_reranker_tokens,
            source_html=words,
            instrument_id="acme/shared-notes",
            tenant_id=TENANT,
            version_id=VERSION,
            effective_from=EFFECTIVE,
            supersedes=None,
            document_class=DocumentClass.EXTERNAL_UPLOAD,
        )
    assert (
        await backend.clauses.get_clause(bobs.clause_id, tenant_id=TENANT, user_id="uid-bob")
        == bobs
    )
