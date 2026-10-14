"""A corpus revision (#178 part 2, ADR-0042): the plan, the exact
acknowledgement, the correction's refusals, the record, and each store's half
of the write, on the fakes and on `GCSClauseStore` over the versioned
in-memory bucket. The orchestrator across a real Neo4j and a real Qdrant is
`tests/test_corpus_revision_stores.py`.

The fixture is the real situation, small: the constructs fixture (cut
verbatim from the pinned compilation) parsed by today's parser is the
corrected text, and the same clauses as the parser before #187 stored them
are the deployed text (`DEPLOYED`, read off that parser's output). Over the
fixture's `s6/` and `sch1/app7/` the two differ on exactly the seven clauses
the deployed C2026C00227 differs on (#178's five, #186's two)."""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.interfaces import ClauseStore
from compliance_rag.adapters.revision_authorization import (
    RevisionAuthorization,
    _issue,  # pyright: ignore[reportPrivateUsage] — the stores' half of the contract
)
from compliance_rag.corpus.models import (
    CANONICALISATION_VERSION,
    PUBLIC_TENANT_ID,
    ClauseRecord,
    Scope,
    canonical_sha256,
    canonicalise,
    sha256_hex,
)
from compliance_rag.corpus.parser import ParsedCompilation, parse_compilation
from compliance_rag.corpus.revision import (
    RevisedClause,
    RevisionAcknowledgement,
    RevisionDrift,
    RevisionMode,
    RevisionPlan,
    RevisionRecord,
    RevisionRefused,
    check_acknowledgement,
    check_amendment,
    check_correction,
    opening_words,
    plan_revision,
    record_of,
)
from compliance_rag.corpus.selection_manifest import SelectionManifest
from tests.test_gcs_clause_store import _FakeBucket  # pyright: ignore[reportPrivateUsage]

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs" / "research" / "fixtures" / "privacy-act-1988-C2026C00227.constructs.html"
INSTRUMENT, VERSION = "privacy-act-1988", "C2026C00227"
SELECTION = SelectionManifest(
    author="Piero Frasca",
    reviewed_by="Piero Frasca",
    date="2026-10-15",
    prefixes=("s6/", "sch1/app7/"),
)
DEPLOYED = {
    "s6/1": "In this Act, unless the contrary intention appears: but does not include a person "
    "permanently or temporarily employed in the Australian Capital Territory Government Service "
    "or in the Public Service of the Northern Territory. and includes an order, direction or other "
    "instrument that is of an interim or interlocutory nature. that carries on a credit reporting "
    "business. but does not include:",
    "s6/1/commonwealth-officer": "Commonwealth officer means a person who holds office under, or "
    "is employed by, the Commonwealth, and includes:",
    "s6/1/court-tribunal-order": "court/tribunal order means an order, direction or other "
    "instrument made by:",
    "s6/1/credit-reporting-body": "credit reporting body means:",
    "s6/1/record/d": "a generally available publication; or",
    "sch1/app7/6": "If an organisation (the first organisation) uses or discloses personal "
    "information about an individual: the individual may:",
    "sch1/app7/6/c": "if paragraph (a) applies—request not to receive direct marketing "
    "communications from the first organisation; and",
}
"""What the parser before #187 stored for the seven clauses it parsed wrongly."""
CHANGED = frozenset(f"{INSTRUMENT}:{VERSION}:{path}" for path in DEPLOYED)
BY, WHY = "Piero Frasca", "#178 part 2: republish the corrected parse"


def source() -> str:
    return FIXTURE.read_text(encoding="utf-8")


PIN = sha256_hex(FIXTURE.read_text(encoding="utf-8"))


def parsed() -> ParsedCompilation:
    return parse_compilation(source(), instrument_id=INSTRUMENT, version_id=VERSION)


def corrected_clauses() -> tuple[ClauseRecord, ...]:
    return SELECTION.select(parsed().clauses)


def deployed_clauses() -> tuple[ClauseRecord, ...]:
    """The selection as the deployed bucket, graph and Qdrant hold it."""
    return tuple(
        ClauseRecord.build(
            instrument_id=c.instrument_id,
            version_id=c.version_id,
            clause_path=c.clause_path,
            heading=c.heading,
            text=DEPLOYED[c.clause_path],
        )
        if c.clause_path in DEPLOYED
        else c
        for c in corrected_clauses()
    )


def words(text: str) -> int:
    return len(text.split())


async def deploy_bucket(store: ClauseStore) -> None:
    """The ingest as it ran (#99): the old text, today's anchored HTML (which
    #187 and #192 left byte-identical)."""
    await store.put_clauses(deployed_clauses())
    await store.put_manifest(
        INSTRUMENT,
        VERSION,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html=parsed().annotated_html,
    )


def plan_against(stored: tuple[ClauseRecord, ...], fresh: tuple[ClauseRecord, ...]) -> RevisionPlan:
    manifest = {c.clause_path: canonical_sha256(c.canonical_text) for c in stored}
    return plan_revision(
        fresh,
        source_html=source(),
        annotated_html=parsed().annotated_html,
        stored_version_id=stored[0].version_id,
        stored_manifest=manifest,
        stored_texts={c.clause_path: c.canonical_text for c in stored},
        stored_source_html_sha256=sha256_hex(parsed().annotated_html),
    )


def ack(
    ids: frozenset[str] = CHANGED, *, findings: int = 0, by: str = BY, reason: str = WHY
) -> RevisionAcknowledgement:
    return RevisionAcknowledgement(clause_ids=ids, by=by, reason=reason, findings_citing=findings)


def correction_record(plan: RevisionPlan, revision_id: str = "r1") -> RevisionRecord:
    return record_of(
        plan,
        ack(),
        revision_id=revision_id,
        at=dt.datetime(2026, 10, 15, 9, 0, tzinfo=dt.UTC),
        corpus_sha="c" * 40,
        corpus_tag=None,
    )


def authorization(revision_id: str = "r1") -> RevisionAuthorization:
    return _issue(
        correction_record(plan_against(deployed_clauses(), corrected_clauses()), revision_id)
    )


# ---- the plan --------------------------------------------------------------


def test_the_fixture_reproduces_the_deployed_defect_on_exactly_seven_clauses() -> None:
    plan = plan_against(deployed_clauses(), corrected_clauses())
    assert plan.mode is RevisionMode.CORRECT
    assert {c.new_clause_id for c in plan.changed} == CHANGED
    assert (plan.added, plan.removed, plan.unchanged) == ((), (), 32 - 7)


def test_the_deployed_texts_are_the_parses_own_words_on_other_clauses() -> None:
    """`DEPLOYED` is typed in, so it is checked: the old parser put the same
    words on other clauses, it didn't lose or invent any (ADR-0039, ADR-0041:
    "the multiset of words is identical"). A transcription error in it fails
    here."""
    old = Counter(w for t in DEPLOYED.values() for w in canonicalise(t).split())
    new = Counter(
        w
        for c in corrected_clauses()
        if c.clause_path in DEPLOYED
        for w in c.canonical_text.split()
    )
    assert old == new


def test_each_changed_clause_carries_both_texts_and_both_hashes() -> None:
    plan = plan_against(deployed_clauses(), corrected_clauses())
    record = next(c for c in plan.changed if c.clause_path == "s6/1/record/d")
    assert record.old_text == "a generally available publication; or"
    assert record.new_text == "but does not include: a generally available publication; or"
    assert record.old_sha256 == canonical_sha256(record.old_text)
    assert record.new_sha256 == canonical_sha256(record.new_text)
    assert record.old_clause_id == record.new_clause_id == f"{INSTRUMENT}:{VERSION}:s6/1/record/d"
    assert plan.source_sha256 == PIN
    assert plan.source_html_sha256 == plan.stored_source_html_sha256


def test_the_plan_names_additions_and_removals_by_path() -> None:
    fresh = corrected_clauses()
    stored = tuple(c for c in deployed_clauses() if c.clause_path != "s6/1/record/h")
    stored += (
        ClauseRecord.build(
            instrument_id=INSTRUMENT, version_id=VERSION, clause_path="s6/2", text="gone"
        ),
    )
    plan = plan_against(stored, fresh)
    assert [c.clause_path for c in plan.added] == ["s6/1/record/h"]
    assert [r.clause_path for r in plan.removed] == ["s6/2"]
    assert plan.acknowledgeable == CHANGED | {
        f"{INSTRUMENT}:{VERSION}:s6/1/record/h",
        f"{INSTRUMENT}:{VERSION}:s6/2",
    }


def test_an_identical_parse_plans_nothing() -> None:
    plan = plan_against(corrected_clauses(), corrected_clauses())
    assert plan.empty and plan.unchanged == 32
    with pytest.raises(RevisionRefused, match="changes nothing"):
        check_acknowledgement(plan, ack(frozenset()), findings_citing=0)


def test_a_stored_text_that_doesnt_hash_to_its_manifest_entry_is_drift() -> None:
    stored = deployed_clauses()
    manifest = {c.clause_path: canonical_sha256(c.canonical_text) for c in stored}
    texts = {c.clause_path: c.canonical_text for c in stored}
    texts["s6/1"] = "something else"
    with pytest.raises(RevisionDrift, match="disagrees with itself"):
        plan_revision(
            corrected_clauses(),
            source_html=source(),
            annotated_html=parsed().annotated_html,
            stored_version_id=VERSION,
            stored_manifest=manifest,
            stored_texts=texts,
            stored_source_html_sha256="x",
        )


def test_a_tenants_document_is_not_revised_here() -> None:
    policy = ClauseRecord.build(
        instrument_id="acme/policy",
        version_id="v1",
        clause_path="1",
        text="t",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    with pytest.raises(ValueError, match="library law"):
        plan_against((policy,), (policy,))


# ---- the acknowledgement ---------------------------------------------------


def test_an_exact_acknowledgement_passes() -> None:
    check_acknowledgement(
        plan_against(deployed_clauses(), corrected_clauses()), ack(), findings_citing=0
    )


@pytest.mark.parametrize(
    ("named", "message"),
    [
        (
            CHANGED - {f"{INSTRUMENT}:{VERSION}:s6/1"},
            r"missing \['privacy-act-1988:C2026C00227:s6/1'\]",
        ),
        (CHANGED | {f"{INSTRUMENT}:{VERSION}:s6/1/record"}, r"not in the plan \['privacy-act-1988"),
        (frozenset(c.replace(VERSION, "C2026C00999") for c in CHANGED), "acknowledged exactly"),
    ],
)
def test_a_missing_or_extra_clause_id_is_refused(named: frozenset[str], message: str) -> None:
    plan = plan_against(deployed_clauses(), corrected_clauses())
    with pytest.raises(RevisionRefused, match=message):
        check_acknowledgement(plan, ack(named), findings_citing=0)


@pytest.mark.parametrize(("by", "reason"), [(" ", WHY), (BY, "")])
def test_an_acknowledgement_names_who_and_why(by: str, reason: str) -> None:
    plan = plan_against(deployed_clauses(), corrected_clauses())
    with pytest.raises(RevisionRefused, match="who authorised it and why"):
        check_acknowledgement(plan, ack(by=by, reason=reason), findings_citing=0)


def test_the_findings_count_must_be_acknowledged_exactly() -> None:
    plan = plan_against(deployed_clauses(), corrected_clauses())
    with pytest.raises(RevisionRefused, match="2 finding"):
        check_acknowledgement(plan, ack(findings=0), findings_citing=2)
    check_acknowledgement(plan, ack(findings=2), findings_citing=2)


# ---- a correction is of the same law ---------------------------------------


def test_a_correction_from_other_bytes_than_the_pinned_source_is_refused() -> None:
    plan = plan_against(deployed_clauses(), corrected_clauses())
    check_correction(plan, pinned_source_sha256=PIN)
    with pytest.raises(RevisionRefused, match="not the version's pinned source"):
        check_correction(plan, pinned_source_sha256="0" * 64)


def test_a_correction_that_moves_an_anchor_is_refused() -> None:
    plan = plan_against(deployed_clauses(), corrected_clauses())
    moved = replace(plan, stored_source_html_sha256="0" * 64)
    with pytest.raises(RevisionRefused, match="anchors moved"):
        check_correction(moved, pinned_source_sha256=PIN)


def test_a_correction_never_removes_or_adds_a_clause() -> None:
    fresh = corrected_clauses()
    extra = ClauseRecord.build(
        instrument_id=INSTRUMENT, version_id=VERSION, clause_path="s6/2", text="gone"
    )
    with pytest.raises(RevisionRefused, match="never removes one"):
        check_correction(
            plan_against((*deployed_clauses(), extra), fresh), pinned_source_sha256=PIN
        )
    with pytest.raises(RevisionRefused, match="acknowledged-additions route"):
        check_correction(plan_against(deployed_clauses()[1:], fresh), pinned_source_sha256=PIN)


def test_a_correction_keeps_the_version_and_an_amendment_doesnt() -> None:
    stored = tuple(c.model_copy(update={"version_id": "C2026C00111"}) for c in deployed_clauses())
    amendment = plan_against(stored, corrected_clauses())
    assert amendment.mode is RevisionMode.AMEND
    with pytest.raises(RevisionRefused, match="a correction keeps the version"):
        check_correction(amendment, pinned_source_sha256=PIN)
    check_amendment(amendment, pinned_source_sha256=PIN)
    with pytest.raises(RevisionRefused, match="is the stored version itself"):
        check_amendment(
            plan_against(deployed_clauses(), corrected_clauses()), pinned_source_sha256=PIN
        )
    with pytest.raises(RevisionRefused, match="not the one pinned"):
        check_amendment(amendment, pinned_source_sha256="0" * 64)


def test_an_amendment_that_changes_no_selected_clause_is_still_a_new_version() -> None:
    """A later compilation can leave every clause a selection keeps as it was:
    it is still published and the old one retired, with nothing acknowledged
    by id."""
    stored = tuple(c.model_copy(update={"version_id": "C2026C00111"}) for c in corrected_clauses())
    plan = plan_against(stored, corrected_clauses())
    assert plan.mode is RevisionMode.AMEND and plan.empty
    check_acknowledgement(plan, ack(frozenset()), findings_citing=0)
    record = correction_record(plan)
    assert (record.clauses, record.supersedes) == ((), "C2026C00111")


# ---- the record ------------------------------------------------------------


def test_the_record_carries_each_clauses_hashes_who_why_when_and_the_source() -> None:
    record = correction_record(plan_against(deployed_clauses(), corrected_clauses()))
    assert record.mode is RevisionMode.CORRECT and record.supersedes is None
    assert (record.by, record.reason, record.at) == (BY, WHY, "2026-10-15T09:00:00+00:00")
    assert record.source_sha256 == PIN and record.corpus_sha == "c" * 40
    entry = next(c for c in record.clauses if c.clause_path == "s6/1/credit-reporting-body")
    assert entry == RevisedClause(
        "s6/1/credit-reporting-body",
        canonical_sha256(DEPLOYED["s6/1/credit-reporting-body"]),
        canonical_sha256(
            "credit reporting body means: that carries on a credit reporting business."
        ),
    )
    assert RevisionRecord.from_json(json.loads(json.dumps(record.to_json()))) == record


def test_a_record_is_refused_without_who_and_why_or_with_a_clause_twice() -> None:
    record = correction_record(plan_against(deployed_clauses(), corrected_clauses()))
    with pytest.raises(ValueError, match="who and why"):
        replace(record, by="")
    with pytest.raises(ValueError, match="once"):
        replace(record, clauses=(*record.clauses, record.clauses[0]))
    with pytest.raises(ValueError, match="neither adds nor removes"):
        replace(record, clauses=(RevisedClause("s6/2", None, "a" * 64),))


def test_the_graph_trail_entry_is_adr_0031s_shape_and_names_the_revision() -> None:
    record = correction_record(plan_against(deployed_clauses(), corrected_clauses()))
    entry = json.loads(record.trail_entry("f" * 64))
    assert entry == {
        "at": record.at,
        "by": BY,
        "reason": WHY,
        "revision_id": "r1",
        "mode": "correct",
        "source_sha256": PIN,
        "previous": {"canonical_sha256": "f" * 64},
    }


def test_opening_words_show_where_two_texts_part() -> None:
    assert opening_words("a b c", "a b d") == ("a b c", "a b d")
    old, new = opening_words(
        "In this Act, unless the contrary intention appears: but does not include a person",
        "In this Act, unless the contrary intention appears:",
    )
    assert old == "In this Act, unless the contrary intention appears: but does …"
    assert new == "In this Act, unless the contrary intention appears:"
    # Alike for longer than the opening: shown from just before they part.
    old, new = opening_words(
        "Commonwealth officer means a person who holds office under, or is employed by, the "
        "Commonwealth, and includes:",
        "Commonwealth officer means a person who holds office under, or is employed by, the "
        "Commonwealth, and includes: but does not include a person",
    )
    assert old == "… Commonwealth, and includes:"
    assert new == "… Commonwealth, and includes: but does not include a person"


# ---- the authorization -----------------------------------------------------


def test_an_authorization_is_issued_only_by_corpus_revision() -> None:
    record = correction_record(plan_against(deployed_clauses(), corrected_clauses()))
    with pytest.raises(PermissionError, match="only by CorpusRevision"):
        RevisionAuthorization(record=record, _issuer=object())


def test_an_authorization_covers_its_clauses_with_their_recorded_text_only() -> None:
    granted = authorization()
    fixed = next(c for c in corrected_clauses() if c.clause_path == "s6/1/record/d")
    granted.check_text(fixed.clause_id, fixed.canonical_text)
    with pytest.raises(PermissionError, match="its hash differs"):
        granted.check_text(fixed.clause_id, "a generally available publication; or")
    with pytest.raises(PermissionError, match="not a clause revision r1 writes"):
        granted.check_text(f"{INSTRUMENT}:{VERSION}:s6/1/record/e", "anything")
    with pytest.raises(PermissionError, match="not a clause revision r1 writes"):
        granted.check_text(f"{INSTRUMENT}:C2026C00999:s6/1/record/d", fixed.canonical_text)


# ---- the bucket's half -----------------------------------------------------


def _bucket() -> tuple[_FakeBucket, GCSClauseStore]:
    bucket = _FakeBucket()
    return bucket, GCSClauseStore(bucket)


def revised_clauses() -> tuple[ClauseRecord, ...]:
    """The seven clauses as the corrected parse has them."""
    return tuple(c for c in corrected_clauses() if c.clause_id in CHANGED)


async def test_the_bucket_keeps_the_old_text_as_noncurrent_and_writes_the_manifest_first() -> None:
    bucket, store = _bucket()
    await deploy_bucket(store)
    writes: list[str] = []
    original = bucket.write

    def counting(name: str, data: str) -> None:
        writes.append(name)
        original(name, data)

    bucket.write = counting  # type: ignore[method-assign]
    await store.record_revision(authorization(), revised_clauses())
    manifest_key = f"{INSTRUMENT}/{VERSION}/manifest.json"
    assert writes[0] == manifest_key and writes.count(manifest_key) == 1
    assert len(writes) == 1 + 7
    old_texts = {
        json.loads(data)["canonical_text"]
        for name, _, data in bucket.noncurrent
        if "/clauses/" in name
    }
    assert old_texts == {c.canonical_text for c in deployed_clauses() if c.clause_id in CHANGED}
    on_record = json.loads(bucket.objects[manifest_key])["revisions"]
    assert [r["revision_id"] for r in on_record] == ["r1"]
    assert on_record[0]["clauses"]["s6/1"]["previous_sha256"] == canonical_sha256(DEPLOYED["s6/1"])
    # Finishing writes nothing that already holds its text.
    writes.clear()
    await store.record_revision(authorization(), revised_clauses())
    assert writes == []


async def test_a_bucket_write_that_dies_after_the_manifest_is_finished_by_calling_again() -> None:
    bucket, store = _bucket()
    await deploy_bucket(store)
    original = bucket.write
    objects = 0

    def failing(name: str, data: str) -> None:
        nonlocal objects
        if "/clauses/" in name:
            objects += 1
            if objects == 3:
                raise RuntimeError("429 Too Many Requests")
        original(name, data)

    bucket.write = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await store.record_revision(authorization(), revised_clauses())
    bucket.write = original  # type: ignore[method-assign]
    # The manifest committed the revision; two objects hold the new text.
    assert len(await store.revisions(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID)) == 1
    behind = [
        c
        for c in revised_clauses()
        if (await store.get_clause(c.clause_id, tenant_id=PUBLIC_TENANT_ID)) != c
    ]
    assert len(behind) == 5
    await store.record_revision(authorization(), revised_clauses())
    for clause in revised_clauses():
        assert await store.get_clause(clause.clause_id, tenant_id=PUBLIC_TENANT_ID) == clause


async def test_a_version_never_ingested_is_notrevised_clauses() -> None:
    with pytest.raises(RevisionDrift, match="no completed ingest"):
        await _bucket()[1].record_revision(authorization(), revised_clauses())
