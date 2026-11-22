"""The `LedgerStore` contract (issue #69): one definition, run against
`FakeLedgerStore` here and against `PostgresLedgerStore` on real Postgres
(`tests/test_postgres_ledger_store.py`), so the two can't silently diverge.

It uses only the interface: tenants are registered with `put_profile`,
library instruments are recorded before anyone subscribes to them (the
database requires it), and findings are seeded through a callable each backend
supplies — `LedgerStore` has no finding writer yet.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import pytest

from compliance_rag.adapters.fakes import FakeGraphStore, FakeLedgerStore
from compliance_rag.adapters.interfaces import (
    CHECK_PAGE_MAX,
    FINDINGS_RESERVATION_TTL,
    RESERVATION_TTL,
    AssessmentRecord,
    BlockedCitation,
    ChatAnswer,
    ChatCitation,
    ChatExchange,
    ChatTurn,
    CheckCursor,
    CheckNotExtracted,
    CheckRecord,
    CheckRun,
    CheckStage,
    ClaimExtractionRecord,
    ClaimRecord,
    ExtractionAlreadyReserved,
    FailureReason,
    FindingCitation,
    FindingOutcome,
    FindingRecord,
    FindingsAlreadyReserved,
    FindingsRecord,
    LedgerStore,
    PolicyVersion,
    PrefixMeasure,
    Profile,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
    SourceKind,
    SourceVersion,
    TurnOutcome,
    TurnRole,
    Unavailability,
    UsageRecord,
    Verdict,
    WithheldReason,
)
from compliance_rag.assessment.coverage import CoverageRow, Outcome, Question
from compliance_rag.assessment.environment import NotInEnvironment
from compliance_rag.assessment.evidence import (
    Evidence,
    EvidenceProvenance,
    FactRow,
    Facts,
    FactsSnapshot,
    Period,
)
from compliance_rag.corpus.models import DocumentClass, Incompleteness, IncompleteReason
from compliance_rag.corpus.submitted_parser import SubmittedFormat
from compliance_rag.graph.mappings import load_mapping_set
from tests.test_applicability import AS_OF as APPLICABILITY_AS_OF
from tests.test_applicability import CLAUSES as APPLICABILITY_CLAUSES
from tests.test_applicability import FIXTURE as APPLICABILITY
from tests.test_applicability import LAST_FY
from tests.test_applicability import fact as applicability_fact

SeedFinding = Callable[[str, str], Awaitable[None]]
"""(tenant_id, cited clause_id) -> records one finding citing that clause."""

LAW = "privacy-act-1988"
POLICY = "acme/privacy-policy"
JAN, JUN = dt.date(2026, 1, 1), dt.date(2026, 6, 1)
SHA_CORPUS, SHA_MAPPING = "c" * 40, "1a2b3c4d" * 5


async def assert_ledger_contract(ledger: LedgerStore, seed_finding: SeedFinding) -> None:
    # ---- tenants and profiles
    await ledger.put_profile(Profile("acme", jurisdictions=("AU",), sectors=("retail",)))
    await ledger.put_profile(Profile("other-corp"))
    profile = await ledger.get_profile("acme")
    assert profile is not None and profile.jurisdictions == ("AU",)
    assert profile.sectors == ("retail",)
    assert await ledger.get_profile("nobody") is None

    # ---- dated evidence, append-only (#114)
    turnover = Evidence(
        tenant_id="acme",
        evidence_id="ev-1",
        field="annual_turnover_aud",
        value=2_400_000,
        period=Period.financial_year(2025),
        provenance=EvidenceProvenance.ASSERTED,
        source="CFO, questionnaire",
        asserted_by="cfo@acme.example",
        trace_id="trace-ev",
    )
    started = Evidence(
        tenant_id="acme",
        evidence_id="ev-2",
        field="business_started_on",
        value=dt.date(2019, 3, 1),
        period=Period(dt.date(2019, 3, 1)),
        provenance=EvidenceProvenance.PARSED,
        source="ASIC register extract",
    )
    forms = Evidence(
        tenant_id="acme",
        evidence_id="ev-3",
        field="legal_forms",
        value=("body_corporate",),
        period=Period(dt.date(2019, 3, 1)),
        provenance=EvidenceProvenance.INFERRED,
        source="D3 §1.1",
    )
    for evidence in (turnover, started, forms):
        await ledger.record_evidence(evidence)
    corrected = Evidence(
        tenant_id="acme",
        evidence_id="ev-4",
        field="annual_turnover_aud",
        value=2_500_000,
        period=Period.financial_year(2025),
        provenance=EvidenceProvenance.ASSERTED,
        source="CFO, audited accounts",
        asserted_by="cfo@acme.example",
        supersedes="ev-1",
        trace_id="trace-ev-4",
        document_id=None,
    )
    await ledger.record_evidence(corrected)
    # Every row, superseded included, round-trips exactly (a date stays a date),
    # stamped with when the ledger recorded it.
    stored = await ledger.evidence("acme")
    assert all(e.recorded_at is not None for e in stored)
    assert tuple(replace(e, recorded_at=None) for e in stored) == (
        turnover,
        started,
        forms,
        corrected,
    )
    assert await ledger.evidence("other-corp") == ()
    for refused in (
        turnover,  # append-only: the same id again
        replace(corrected, evidence_id="ev-5"),  # ev-1 already has a successor
        replace(corrected, evidence_id="ev-6", supersedes="ev-404"),  # supersedes nothing
        replace(turnover, tenant_id="nobody", evidence_id="ev-7"),  # unregistered tenant
        replace(turnover, evidence_id="ev-8", trace_id=None),  # an assertion outside a request
        replace(started, evidence_id="ev-9", supersedes="ev-4"),  # another field's correction
    ):
        with pytest.raises(ValueError):
            await ledger.record_evidence(refused)
    assert len(await ledger.evidence("acme")) == 4

    # ---- an assessment and its coverage, in one write (#115, D-06, #153)
    coverage = await FakeGraphStore(
        load_mapping_set([APPLICABILITY]), clause_validity=APPLICABILITY_CLAUSES
    ).coverage(
        Facts.of([applicability_fact("annual_turnover_aud", 900, LAST_FY)]),
        tenant_id="acme",
        as_of=APPLICABILITY_AS_OF,
    )
    rows = tuple(
        sorted((CoverageRow.of(o) for o in coverage.obligations), key=lambda r: r.obligation_id)
    )
    tagged = SourceVersion(SourceKind.CORPUS, sha=SHA_CORPUS, tag="corpus-2026.10")
    untagged = SourceVersion(SourceKind.MAPPING, sha=SHA_MAPPING)
    first = AssessmentRecord(
        tenant_id="acme",
        assessment_id="cov-1",
        user_id="cfo@acme.example",
        corpus=tagged,
        mapping=untagged,
        as_of=JAN,
        trace_id="trace-cov",
    )
    await ledger.record_coverage_assessment(first, rows)
    assert await ledger.coverage_rows("acme", "cov-1") == rows
    unassessable = [r for r in rows if r.outcome is Outcome.UNASSESSABLE]
    assert unassessable and all(r.questions for r in unassessable)
    # The tag and the commit it pointed at come back as written, and an
    # untagged version stays untagged; the assessment carries its trace_id.
    recorded = await ledger.get_assessment("acme", "cov-1")
    assert recorded == first
    assert recorded is not None and recorded.mapping.untagged and recorded.mapping.tag is None
    assert await ledger.get_assessment("other-corp", "cov-1") is None
    assert await ledger.coverage_rows("other-corp", "cov-1") == ()

    def assessment(assessment_id: str, **changes: Any) -> AssessmentRecord:  # Any: record fields
        fields: dict[str, Any] = {  # Any: record fields
            "assessment_id": assessment_id,
            "as_of": JUN,
            "trace_id": "f" * 32,
            **changes,
        }
        return replace(first, **fields)

    # A tenant with nothing in force on its date still has an assessment.
    await ledger.record_coverage_assessment(assessment("none-in-force"), ())
    assert await ledger.coverage_rows("acme", "none-in-force") == ()
    # Refused whole: nothing of a refused assessment is left behind.
    reasonless = replace(rows[0], obligation_id="OB-NO-REASON", reason=" ")
    uncited = replace(rows[0], obligation_id="OB-UNCITED", outcome=Outcome.APPLICABLE, citations=())
    for refused, refused_rows, why in (
        (assessment("cov-1"), rows, "already recorded"),
        (assessment("a2", tenant_id="nobody"), rows, "isn't registered"),
        (assessment("a3", user_id=" "), rows, "who asked"),
        (assessment("a4", trace_id=""), rows, "trace_id"),
        (assessment("a5", corpus=untagged), rows, "corpus version and a mapping version"),
        (assessment("a6"), (rows[0], rows[0]), "twice"),
        (assessment("a7"), (*rows, reasonless), "says why"),
        (assessment("a8"), (*rows, uncited), "cites its clauses"),
        # Several faults at once get the same refusal from every store: what
        # the record and its rows say is checked before what only the store
        # knows (the tenant, the id).
        (assessment("cov-1", user_id=" "), rows, "who asked"),
        (assessment("a9", tenant_id="nobody", trace_id=""), rows, "trace_id"),
        (assessment("cov-1"), (rows[0], rows[0]), "twice"),
        (assessment("a10", tenant_id="nobody"), (*rows, uncited), "cites its clauses"),
        (assessment("cov-1", tenant_id="nobody"), rows, "isn't registered"),
    ):
        with pytest.raises(ValueError, match=why):
            await ledger.record_coverage_assessment(refused, refused_rows)
    for assessment_id in ("a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9", "a10"):
        assert await ledger.get_assessment("acme", assessment_id) is None
        assert await ledger.coverage_rows("acme", assessment_id) == ()
    # A repeated id leaves the first assessment and its rows as they were.
    assert await ledger.get_assessment("acme", "cov-1") == recorded
    assert await ledger.coverage_rows("acme", "cov-1") == rows

    # ---- usage rows: real token counts and cost, keyed to the request (#31)
    for tenant_id, usage_id in (("acme", "u-1"), ("_public", "u-2")):
        await ledger.record_usage(
            UsageRecord(
                tenant_id=tenant_id,
                usage_id=usage_id,
                trace_id="e" * 32,
                model="m",
                input_tokens=1200,
                output_tokens=300,
                cached_prefix_hit=True,
                cost_usd=0.0123,
            )
        )
    (row_usage,) = await ledger.usage("acme")
    assert (row_usage.trace_id, row_usage.input_tokens, row_usage.cached_prefix_hit) == (
        "e" * 32,
        1200,
        True,
    )
    assert abs(row_usage.cost_usd - 0.0123) < 1e-9
    assert len(await ledger.usage("_public")) == 1  # spend on library law has an owner (ADR-0025)

    # ---- library versions, then subscriptions to them (ADR-0020, ADR-0025)
    for instrument in (LAW, "iso-27001"):
        await ledger.record_version(
            tenant_id="_public",
            instrument_id=instrument,
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
            document_class=DocumentClass.LIBRARY_LAW,
            title="The document's title",
        )
    await ledger.subscribe(tenant_id="acme", instrument_id=LAW, subscribed_by="u1")
    await ledger.subscribe(tenant_id="acme", instrument_id=LAW, subscribed_by="u2")  # idempotent
    await ledger.subscribe(tenant_id="acme", instrument_id="iso-27001", subscribed_by="u1")
    await ledger.subscribe(tenant_id="other-corp", instrument_id=LAW, subscribed_by="u1")
    await ledger.unsubscribe(tenant_id="acme", instrument_id=LAW)
    await ledger.unsubscribe(tenant_id="acme", instrument_id="never-subscribed")  # no-op
    assert await ledger.subscribed_instruments("acme") == frozenset({"iso-27001"})
    assert await ledger.subscribed_instruments("other-corp") == frozenset({LAW})
    for tenant_id, instrument_id in (("acme", POLICY), ("_public", LAW)):
        with pytest.raises(ValueError):
            await ledger.subscribe(
                tenant_id=tenant_id, instrument_id=instrument_id, subscribed_by="u"
            )

    # ---- version validity is a record of its own (ADR-0021 mechanism 3)
    await ledger.record_version(
        tenant_id="acme",
        instrument_id=POLICY,
        version_id="v1",
        valid_from=JAN,
        supersedes=None,
        document_class=DocumentClass.INTERNAL_POLICY,
        title="Acme Privacy Policy",
    )
    await ledger.record_version(
        tenant_id="acme",
        instrument_id=POLICY,
        version_id="v2",
        valid_from=JUN,
        supersedes="v1",
        document_class=DocumentClass.INTERNAL_POLICY,
        title='Acme "Customer" Privacy Statement',  # renamed in v2
    )
    await ledger.end_version(tenant_id="acme", instrument_id=POLICY, version_id="v1", valid_to=JUN)
    assert await ledger.version_validity(
        tenant_id="acme", instrument_id=POLICY, version_id="v1"
    ) == (JAN, JUN)
    assert await ledger.version_validity(
        tenant_id="acme", instrument_id=POLICY, version_id="v2"
    ) == (JUN, None)
    assert await ledger.version_validity(
        tenant_id="_public", instrument_id=LAW, version_id="v1"
    ) == (JAN, None)
    assert (
        await ledger.version_validity(tenant_id="acme", instrument_id=POLICY, version_id="v9")
        is None
    )
    # ---- each version's recorded title, read in one batch (ADR-0049)
    assert await ledger.version_titles(
        "acme", [(POLICY, "v1"), (POLICY, "v2"), (POLICY, "v9"), (LAW, "v1")]
    ) == {
        (POLICY, "v1"): "Acme Privacy Policy",
        # A renamed document's earlier version keeps the title it was cited by.
        (POLICY, "v2"): 'Acme "Customer" Privacy Statement',
    }
    assert await ledger.version_titles("other-corp", [(POLICY, "v1")]) == {}
    assert await ledger.version_titles("acme", []) == {}
    for bad in ("", "   ", "Acme\nPrivacy Policy", "Acme\u2028Policy", "Acme\x00Policy"):
        with pytest.raises(ValueError, match="title"):
            await ledger.record_version(
                tenant_id="acme",
                instrument_id=POLICY,
                version_id="v8",
                valid_from=JUN,
                supersedes=None,
                document_class=DocumentClass.INTERNAL_POLICY,
                title=bad,
            )
    assert (
        await ledger.version_validity(tenant_id="acme", instrument_id=POLICY, version_id="v8")
        is None
    )  # refused before anything was recorded
    with pytest.raises(ValueError, match="already recorded"):
        await ledger.record_version(
            tenant_id="acme",
            instrument_id=POLICY,
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
            document_class=DocumentClass.INTERNAL_POLICY,
            title="The document's title",
        )
    with pytest.raises(KeyError):
        await ledger.end_version(
            tenant_id="acme", instrument_id=POLICY, version_id="v9", valid_to=JUN
        )
    # Set once (D-05, ADR-0029): the same date again is a no-op, another is refused.
    await ledger.end_version(tenant_id="acme", instrument_id=POLICY, version_id="v1", valid_to=JUN)
    with pytest.raises(ValueError, match="set once"):
        await ledger.end_version(
            tenant_id="acme", instrument_id=POLICY, version_id="v1", valid_to=JAN
        )

    # ---- document class, and a tenant's in-force policies (#148)
    await ledger.put_profile(Profile("beta-corp"))
    mar, jul, next_year = dt.date(2026, 3, 1), dt.date(2026, 7, 1), dt.date(2027, 1, 1)

    async def record(
        instrument_id: str,
        version_id: str,
        valid_from: dt.date,
        document_class: DocumentClass = DocumentClass.INTERNAL_POLICY,
        supersedes: str | None = None,
    ) -> None:
        await ledger.record_version(
            tenant_id="beta-corp",
            instrument_id=instrument_id,
            version_id=version_id,
            valid_from=valid_from,
            supersedes=supersedes,
            document_class=document_class,
            title="The document's title",
        )

    await record("beta/handbook", "v1", JAN)
    await record("beta/handbook", "v2", JUN, supersedes="v1")
    await ledger.end_version(
        tenant_id="beta-corp", instrument_id="beta/handbook", version_id="v1", valid_to=JUN
    )
    await record("beta/code-of-conduct", "v1", JAN)
    await ledger.mark_withdrawn(
        tenant_id="beta-corp",
        instrument_id="beta/code-of-conduct",
        version_id="v1",
        withdrawn_by="u",
    )
    await record("beta/old-policy", "v1", JAN)
    await ledger.mark_purged(
        tenant_id="beta-corp",
        instrument_id="beta/old-policy",
        version_id=None,
        purged_by="u",
        reason="test",
    )
    await record("beta/iso-extract", "v1", JAN, DocumentClass.EXTERNAL_UPLOAD)
    await record("beta/proposal", "v1", JAN, DocumentClass.SUBMITTED_DOCUMENT)
    await record("beta/remote-work", "v1", next_year)
    await record("beta/acceptable-use", "v1", JAN)
    # In force, not withdrawn or purged, an internal policy, sorted by instrument.
    assert await ledger.policy_versions("beta-corp", as_of=mar) == (
        PolicyVersion("beta/acceptable-use", "v1"),
        PolicyVersion("beta/handbook", "v1"),
    )
    assert await ledger.policy_versions("beta-corp", as_of=jul) == (
        PolicyVersion("beta/acceptable-use", "v1"),
        PolicyVersion("beta/handbook", "v2"),
    )
    assert PolicyVersion("beta/remote-work", "v1") in await ledger.policy_versions(
        "beta-corp", as_of=next_year
    )
    # valid_to is exclusive: on the day v2 takes over, v1 is gone.
    assert await ledger.policy_versions("beta-corp", as_of=JUN) == (
        PolicyVersion("beta/acceptable-use", "v1"),
        PolicyVersion("beta/handbook", "v2"),
    )
    assert await ledger.policy_versions("nobody", as_of=mar) == ()
    # A purge marks the versions there are; a later upload of the same
    # instrument is a fresh version, unmarked and in force.
    await record("beta/old-policy", "v2", JUN)
    assert PolicyVersion("beta/old-policy", "v2") in await ledger.policy_versions(
        "beta-corp", as_of=jul
    )
    assert await ledger.purge_marker(
        tenant_id="beta-corp", instrument_id="beta/old-policy", version_id="v1"
    ) == ("u", "test")
    assert (
        await ledger.purge_marker(
            tenant_id="beta-corp", instrument_id="beta/old-policy", version_id="v2"
        )
        is None
    )
    # The ledger answers with each instrument's class.
    for instrument_id, document_class in (
        ("beta/handbook", DocumentClass.INTERNAL_POLICY),
        ("beta/iso-extract", DocumentClass.EXTERNAL_UPLOAD),
        ("beta/proposal", DocumentClass.SUBMITTED_DOCUMENT),
    ):
        assert (
            await ledger.document_class(tenant_id="beta-corp", instrument_id=instrument_id)
            is document_class
        )
    assert (
        await ledger.document_class(tenant_id="_public", instrument_id=LAW)
        is DocumentClass.LIBRARY_LAW
    )
    assert await ledger.document_class(tenant_id="beta-corp", instrument_id="beta/none") is None
    # A submitted document is never an internal policy, though it is in force,
    # unwithdrawn and the tenant's: not in the cached prefix (D-11), however
    # late it is asked for.
    for as_of in (mar, JUN, jul, next_year):
        in_force = await ledger.policy_versions("beta-corp", as_of=as_of)
        assert all(v.instrument_id != "beta/proposal" for v in in_force)
    # The class is the instrument's, and library law is `_public`'s alone.
    with pytest.raises(ValueError, match="internal_policy, not external_upload"):
        await record("beta/handbook", "v3", next_year, DocumentClass.EXTERNAL_UPLOAD)
    assert (
        await ledger.version_validity(
            tenant_id="beta-corp", instrument_id="beta/handbook", version_id="v3"
        )
        is None
    )
    with pytest.raises(ValueError, match="library law"):
        await record("beta/a-statute", "v1", JAN, DocumentClass.LIBRARY_LAW)
    with pytest.raises(ValueError, match="library law"):
        await ledger.record_version(
            tenant_id="_public",
            instrument_id="some-act",
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
            document_class=DocumentClass.INTERNAL_POLICY,
            title="The document's title",
        )

    # ---- findings citing, by stable-id prefix
    await seed_finding("acme", f"{POLICY}:v1:3/1")
    await seed_finding("acme", f"{POLICY}:v2:1/1")
    await seed_finding("other-corp", f"{POLICY}:v1:3/1")
    assert await ledger.count_findings_citing("acme", f"{POLICY}:v1:") == 1
    assert await ledger.count_findings_citing("acme", f"{POLICY}:") == 2
    assert await ledger.count_findings_citing("acme", "iso-27001:") == 0
    # Across tenants, by exact clause id: what a corpus revision acknowledges
    # (ADR-0042). A prefix of a cited id is not a citation of it.
    assert await ledger.count_findings_citing_clauses(frozenset({f"{POLICY}:v1:3/1"})) == 2
    assert (
        await ledger.count_findings_citing_clauses(
            frozenset({f"{POLICY}:v1:3/1", f"{POLICY}:v2:1/1"})
        )
        == 3
    )
    assert await ledger.count_findings_citing_clauses(frozenset({f"{POLICY}:v1:3"})) == 0
    assert await ledger.count_findings_citing_clauses(frozenset()) == 0

    # ---- withdraw and purge markers
    await ledger.mark_withdrawn(
        tenant_id="acme", instrument_id=POLICY, version_id="v1", withdrawn_by="admin"
    )
    assert await ledger.is_withdrawn(tenant_id="acme", instrument_id=POLICY, version_id="v1")
    assert not await ledger.is_withdrawn(tenant_id="acme", instrument_id=POLICY, version_id="v2")
    await ledger.mark_purged(
        tenant_id="acme", instrument_id=POLICY, version_id="v1", purged_by="u1", reason="hold"
    )
    await ledger.mark_withdrawn(  # idempotent: the first withdrawal stands
        tenant_id="acme", instrument_id=POLICY, version_id="v1", withdrawn_by="someone-else"
    )
    assert await ledger.is_withdrawn(tenant_id="acme", instrument_id=POLICY, version_id="v1")
    for remove in (ledger.mark_withdrawn, ledger.mark_purged):
        with pytest.raises(KeyError):  # nothing recorded to mark
            await _remove(remove, "acme", POLICY, "v9")
        with pytest.raises(ValueError, match="library law"):
            await _remove(remove, "_public", LAW, "v1")
    marker = ledger.purge_marker
    assert await marker(tenant_id="acme", instrument_id=POLICY, version_id="v1") == ("u1", "hold")
    assert await marker(tenant_id="acme", instrument_id=POLICY, version_id="v2") is None

    # ---- removal audit, in the order it happened
    for audit_id, outcome in (("au-1", RemovalOutcome.AUTHORIZED), ("au-2", RemovalOutcome.FAILED)):
        await ledger.record_removal_audit(
            RemovalAuditEntry(
                audit_id=audit_id,
                tenant_id="acme",
                instrument_id=POLICY,
                version_id=None,
                action=RemovalAction.PURGE,
                outcome=outcome,
                actor="u1",
                reason="cleanup",
                findings_citing=2,
            )
        )
    trail = await ledger.removal_audit("acme")
    assert [(e.audit_id, e.outcome) for e in trail] == [
        ("au-1", RemovalOutcome.AUTHORIZED),
        ("au-2", RemovalOutcome.FAILED),
    ]
    assert trail[0].findings_citing == 2 and trail[0].version_id is None
    assert await ledger.removal_audit("other-corp") == ()


async def _remove(
    method: Callable[..., Awaitable[None]], tenant_id: str, instrument_id: str, version_id: str
) -> None:
    kwargs = {"tenant_id": tenant_id, "instrument_id": instrument_id, "version_id": version_id}
    if method.__name__ == "mark_withdrawn":
        await method(**kwargs, withdrawn_by="u")
    else:
        await method(**kwargs, purged_by="u", reason="r")


async def test_the_fake_ledger_satisfies_the_contract() -> None:
    ledger = FakeLedgerStore()

    async def seed(tenant_id: str, clause_id: str) -> None:
        ledger.seed_finding(tenant_id, clause_id)

    await assert_ledger_contract(ledger, seed)


# ---- chat history (#162) -------------------------------------------------------------

APP11 = "privacy-act-1988:C2026C00227:sch1/app11/1"
APP12 = "privacy-act-1988:C2026C00227:sch1/app12/1"
POLICY_CLAUSE = f"{POLICY}:v1:3/1"
S6DA_2 = "privacy-act-1988:C2026C00227:s6da/2"
S16A_1 = "privacy-act-1988:C2026C00227:s16a/1"
FORMULA_GAP = Incompleteness(
    reason=IncompleteReason.IMAGE_NOT_TEXT, detail="the formula is not available in text form"
)
TABLE_GAP = Incompleteness(
    reason=IncompleteReason.TABLE_NOT_TEXT, detail="the table is not available in text form"
)
PENALTY_GAP = Incompleteness(
    reason=IncompleteReason.PENALTY_NOT_PARSED, detail="the penalty is not in the clause text"
)
MODEL_PROSE = "Section 6C(2) says: a sentence the model made up that must never be kept."
VERIFIED = ChatAnswer(
    TurnOutcome.VERIFIED,
    text="Acme must take reasonable steps.",
    citations=(ChatCitation(APP11, "C2026C00227"), ChatCitation(POLICY_CLAUSE, "v1")),
)


def an_exchange(chat_id: str, **changes: Any) -> ChatExchange:  # Any: exchange fields
    fields: dict[str, Any] = {  # Any: exchange fields
        "tenant_id": "acme",
        "chat_id": chat_id,
        "user_id": "uid-1",
        "question": "What must Acme do to protect the personal information it holds?",
        "answer": VERIFIED,
        "as_of": JAN,
        "trace_id": "a" * 32,
        **changes,
    }
    return ChatExchange(**fields)


async def assert_chat_contract(ledger: LedgerStore) -> None:
    """Chat history, recorded as the next two turns of a chat (#162). The
    member is `Member.uid`; a chat is that member's alone."""
    await ledger.put_profile(Profile("acme"))
    await ledger.put_profile(Profile("other-corp"))

    async def turns(
        chat_id: str, user_id: str = "uid-1", tenant_id: str = "acme"
    ) -> tuple[ChatTurn, ...]:
        return await ledger.chat_turns(tenant_id, user_id, chat_id)

    # ---- an exchange starts a chat: the question, then what the member was shown
    first = an_exchange("chat-1")
    await ledger.record_chat_exchange(first)
    stored = await turns("chat-1")
    assert [(t.turn_index, t.role) for t in stored] == [(0, TurnRole.USER), (1, TurnRole.ASSISTANT)]
    assert all(t.created_at is not None for t in stored)
    # Every field comes back as written: the member, the request's trace and
    # date, the verified text and its citations (clause and version).
    assert tuple(replace(t, created_at=None) for t in stored) == first.turns(0)
    question, answer = stored
    assert (question.content, question.outcome, question.citations) == (first.question, None, ())
    assert answer.content == VERIFIED.text and answer.outcome is TurnOutcome.VERIFIED
    assert answer.citations == VERIFIED.citations and answer.blocked == ()
    assert {t.user_id for t in stored} == {"uid-1"} and {t.trace_id for t in stored} == {"a" * 32}
    assert {t.as_of for t in stored} == {JAN}

    # ---- the next exchange continues it, on its own request's date and trace
    second = an_exchange("chat-1", question="And for agencies?", as_of=JUN, trace_id="b" * 32)
    await ledger.record_chat_exchange(second)
    stored = await turns("chat-1")
    assert [t.turn_index for t in stored] == [0, 1, 2, 3]
    assert tuple(replace(t, created_at=None) for t in stored[2:]) == second.turns(2)
    assert [t.created_at for t in stored] == sorted(t.created_at for t in stored if t.created_at)

    # ---- a withheld answer holds the reason and the blocked ids, and no text
    blocked = ChatAnswer(TurnOutcome.BLOCKED, blocked=(APP12, APP12))
    uncited = ChatAnswer(TurnOutcome.UNCITED)
    for number, shown in enumerate((blocked, uncited), start=3):
        await ledger.record_chat_exchange(
            an_exchange("chat-1", answer=shown, trace_id=f"{number}" * 32)
        )
    stored = await turns("chat-1")
    assert [(t.outcome, t.content, t.citations, t.blocked) for t in stored[5::2]] == [
        (TurnOutcome.BLOCKED, None, (), (APP12, APP12)),
        (TurnOutcome.UNCITED, None, (), ()),
    ]

    # ---- and a failed one, only a marker: the question is the member's own
    for number, failure in enumerate(
        (TurnOutcome.RESIDENCY, TurnOutcome.MALFORMED_GENERATION, TurnOutcome.INTERNAL), start=5
    ):
        await ledger.record_chat_exchange(
            an_exchange("failed", answer=ChatAnswer(failure), trace_id=f"{number}" * 32)
        )
    failed = await turns("failed")
    assert [(t.outcome, t.content, t.citations, t.blocked) for t in failed[1::2]] == [
        (TurnOutcome.RESIDENCY, None, (), ()),
        (TurnOutcome.MALFORMED_GENERATION, None, (), ()),
        (TurnOutcome.INTERNAL, None, (), ()),
    ]
    assert {t.content for t in failed[0::2]} == {first.question}

    # ---- a chat is its member's alone: another member's and another tenant's
    # read as empty, as one that never existed does
    assert await turns("chat-1", user_id="uid-2") == ()
    assert await turns("chat-1", tenant_id="other-corp") == ()
    assert await turns("chat-1", user_id="uid-2", tenant_id="other-corp") == ()
    assert await turns("no-such-chat") == ()
    # A different tenant's chat can have the same id, and doesn't touch this one.
    theirs = an_exchange("chat-1", tenant_id="other-corp", user_id="uid-9", question="Theirs?")
    await ledger.record_chat_exchange(theirs)
    assert (await turns("chat-1", "uid-9", "other-corp"))[0].content == "Theirs?"
    assert len(await turns("chat-1")) == 8

    # ---- refused whole, and nothing is left behind
    before = (await turns("chat-1"), await ledger.chats("acme", "uid-1"))
    for refused, why in (
        (an_exchange("chat-1", trace_id=" "), "trace_id"),
        (an_exchange("chat-1", user_id=" "), "belongs to a member"),
        (an_exchange("chat-1", question=" "), "has text"),
        (an_exchange("chat-1", question="a\x00b"), "NUL"),
        (
            an_exchange(
                "chat-1", answer=ChatAnswer(TurnOutcome.VERIFIED, "a\x00b", VERIFIED.citations)
            ),
            "NUL",
        ),
        (an_exchange("chat-1", tenant_id="nobody"), "isn't registered"),
        (an_exchange("chat-1", user_id="uid-2"), "another member"),
        (an_exchange("chat-new", tenant_id="nobody"), "isn't registered"),
        # Several faults at once get the same refusal from every store.
        (an_exchange("chat-1", user_id="uid-2", trace_id=" "), "trace_id"),
        (an_exchange("chat-1", tenant_id="nobody", question=" "), "has text"),
    ):
        with pytest.raises(ValueError, match=why):
            await ledger.record_chat_exchange(refused)
    assert (await turns("chat-1"), await ledger.chats("acme", "uid-1")) == before
    assert await turns("chat-new") == ()
    assert await turns("chat-1", user_id="uid-2") == ()

    # ---- a member's list: theirs only, the chat with the latest turn first
    assert [c.chat_id for c in await ledger.chats("acme", "uid-1")] == ["failed", "chat-1"]
    await ledger.record_chat_exchange(an_exchange("chat-1", question="One more?"))
    listed = await ledger.chats("acme", "uid-1")
    assert [c.chat_id for c in listed] == ["chat-1", "failed"]
    latest, earlier = listed
    assert (latest.turns, latest.first_question) == (10, first.question)
    assert (earlier.turns, earlier.first_question) == (6, first.question)
    assert latest.started_at <= latest.updated_at and earlier.started_at <= earlier.updated_at
    assert latest.started_at < earlier.updated_at < latest.updated_at
    assert latest.started_at == stored[0].created_at
    assert await ledger.chats("acme", "uid-2") == ()
    assert [c.chat_id for c in await ledger.chats("other-corp", "uid-9")] == ["chat-1"]
    assert await ledger.chats("other-corp", "uid-1") == ()
    assert await ledger.chats("nobody", "uid-1") == ()

    # ---- appends racing for one chat are numbered in commit order, none refused,
    # and each exchange's two turns stay together
    await asyncio.gather(
        *(
            ledger.record_chat_exchange(
                an_exchange("racing", trace_id=f"{n}" * 32, question=f"q{n}")
            )
            for n in range(4)
        )
    )
    racing = await turns("racing")
    assert [t.turn_index for t in racing] == list(range(8))
    for question, answer in zip(racing[0::2], racing[1::2], strict=True):
        assert (question.role, answer.role) == (TurnRole.USER, TurnRole.ASSISTANT)
        assert (
            question.trace_id == answer.trace_id and question.content == f"q{question.trace_id[0]}"
        )
    # and recorded in that order too: a chat's latest turn is the one numbered
    # last, so a list ordered by recording time agrees with the turns
    stamps = [t.created_at for t in racing if t.created_at]
    assert len(stamps) == 8 and stamps == sorted(stamps)

    # ---- a citation's incompleteness is kept as the member was shown it (#228,
    # ADR-0047): each citation its own, several reasons in order, none for a
    # complete clause, and the clause's id and version untouched by it
    flagged = ChatAnswer(
        TurnOutcome.VERIFIED,
        text="Annual turnover is worked out by formula.",
        citations=(
            ChatCitation(APP11, "C2026C00227"),
            ChatCitation(S6DA_2, "C2026C00227", (FORMULA_GAP,)),
            ChatCitation(S16A_1, "C2026C00227", (TABLE_GAP, PENALTY_GAP)),
        ),
    )
    await ledger.record_chat_exchange(an_exchange("flagged", answer=flagged))
    _, shown = await turns("flagged")
    assert shown.citations == flagged.citations
    assert [c.incomplete for c in shown.citations if isinstance(c, ChatCitation)] == [
        (),
        (FORMULA_GAP,),
        (TABLE_GAP, PENALTY_GAP),
    ]
    # An answer recorded without any is read back without any.
    await ledger.record_chat_exchange(an_exchange("unflagged"))
    plain = (await turns("unflagged"))[1].citations
    assert plain and all(isinstance(c, ChatCitation) and c.incomplete == () for c in plain)

    # ---- an instrument the question named that wasn't held is kept with the
    # answer as the member was shown it (#252): verified or withheld, in order
    spam = NotInEnvironment("spam act", known_as="Spam Act 2003")
    ndb = NotInEnvironment("NDB scheme", known_as="NDB", part_of="Privacy Act 1988")
    for chat_id, shown in (
        ("named-verified", replace(VERIFIED, not_in_environment=(spam, ndb))),
        (
            "named-blocked",
            ChatAnswer(TurnOutcome.BLOCKED, blocked=(APP12,), not_in_environment=(spam,)),
        ),
        ("named-uncited", ChatAnswer(TurnOutcome.UNCITED, not_in_environment=(spam,))),
    ):
        await ledger.record_chat_exchange(an_exchange(chat_id, answer=shown))
        question, answer = await turns(chat_id)
        assert question.not_in_environment == ()
        assert answer.not_in_environment == shown.not_in_environment
    assert (await turns("unflagged"))[1].not_in_environment == ()

    # ---- an answer turn says which generator wrote it (ledger 0020, #292); a
    # question never does, and a caller that doesn't say records none
    for number, generator in enumerate(("demo", "claude", "gemini", None), start=7):
        await ledger.record_chat_exchange(
            an_exchange("generated", generator=generator, trace_id=f"{number}" * 32)
        )
    generated = await turns("generated")
    assert [t.generator for t in generated[1::2]] == ["demo", "claude", "gemini", None]
    assert {t.generator for t in generated[0::2]} == {None}


async def assert_environment_titles_contract(ledger: LedgerStore) -> None:
    """What a tenant's retrieval can see on a date, as titles (#252): its own
    documents in force, never withdrawn or purged, and the library
    instruments it subscribes to; `_public` sees the whole library."""
    await ledger.put_profile(Profile("acme"))
    await ledger.put_profile(Profile("other-corp"))

    async def record(tenant: str, instrument: str, version: str, title: str, **kw: Any) -> None:
        library = tenant == "_public"
        await ledger.record_version(
            tenant_id=tenant,
            instrument_id=instrument,
            version_id=version,
            valid_from=kw.get("valid_from", JAN),
            supersedes=kw.get("supersedes"),
            document_class=kw.get(
                "document_class",
                DocumentClass.LIBRARY_LAW if library else DocumentClass.INTERNAL_POLICY,
            ),
            title=title,
        )

    await record("_public", LAW, "C1", "Privacy Act 1988")
    await record("_public", "spam-act-2003", "C1", "Spam Act 2003")
    await record("acme", POLICY, "v1", "Acme — Privacy Policy")
    await record(
        "acme", POLICY, "v2", "Acme — Privacy Policy (2026)", valid_from=JUN, supersedes="v1"
    )
    await ledger.end_version(tenant_id="acme", instrument_id=POLICY, version_id="v1", valid_to=JUN)
    supplied = {"document_class": DocumentClass.EXTERNAL_UPLOAD}
    await record("acme", "acme/supplied-act", "v1", "A Supplied Act 2020", **supplied)
    await record("acme", "acme/withdrawn", "v1", "Withdrawn Act 2021", **supplied)
    await ledger.mark_withdrawn(
        tenant_id="acme", instrument_id="acme/withdrawn", version_id="v1", withdrawn_by="u"
    )
    await record("other-corp", "other-corp/policy", "v1", "Their Policy")
    # A submitted document is the subject of a check, never part of the
    # environment (ADR-0053 U1): in force and unwithdrawn, and still not there.
    await record(
        "acme",
        "acme/customer-insights-proposal",
        "v1",
        "Customer insights programme",
        document_class=DocumentClass.SUBMITTED_DOCUMENT,
    )
    await ledger.subscribe(tenant_id="acme", instrument_id=LAW, subscribed_by="setup")

    held = {
        LAW: frozenset({"Privacy Act 1988"}),
        "acme/supplied-act": frozenset({"A Supplied Act 2020"}),
    }
    assert await ledger.environment_titles("acme", as_of=JAN) == {
        **held,
        POLICY: frozenset({"Acme — Privacy Policy"}),
    }
    # v1 retired on JUN, and v2 in force from it.
    assert await ledger.environment_titles("acme", as_of=JUN) == {
        **held,
        POLICY: frozenset({"Acme — Privacy Policy (2026)"}),
    }
    # Before anything was in force, nothing; another tenant sees only its own.
    assert await ledger.environment_titles("acme", as_of=JAN - dt.timedelta(days=1)) == {}
    assert await ledger.environment_titles("other-corp", as_of=JAN) == {
        "other-corp/policy": frozenset({"Their Policy"})
    }
    assert await ledger.environment_titles("_public", as_of=JAN) == {
        LAW: frozenset({"Privacy Act 1988"}),
        "spam-act-2003": frozenset({"Spam Act 2003"}),
    }
    await ledger.unsubscribe(tenant_id="acme", instrument_id=LAW)
    assert LAW not in await ledger.environment_titles("acme", as_of=JAN)


async def test_the_fake_ledger_satisfies_the_environment_contract() -> None:
    await assert_environment_titles_contract(FakeLedgerStore())


async def test_the_fake_ledger_satisfies_the_chat_contract() -> None:
    await assert_chat_contract(FakeLedgerStore())


# ---- checks (#297) ---------------------------------------------------------------------


def a_check(**changes: Any) -> CheckRecord:  # Any: record fields
    fields: dict[str, Any] = {  # Any: record fields
        "tenant_id": "acme",
        "check_id": "c" * 32,
        "user_id": "uid-1",
        "instrument_id": "acme/check-1",
        "version_id": "v1",
        "filename": "proposal.md",
        "document_format": SubmittedFormat.MARKDOWN,
        "trace_id": "a" * 32,
    }
    fields.update(changes)
    return CheckRecord(**fields)


async def assert_check_contract(ledger: LedgerStore) -> None:
    """A check is one member's submission of a document, pointing at the
    `submitted_document` version the ledger already holds (#297, ADR-0059).
    Append-only, one per version, and read by tenant (visibility is the API's,
    `CheckRecord.visible_to`)."""
    await ledger.put_profile(Profile("acme"))
    await ledger.put_profile(Profile("other-corp"))

    async def document(instrument_id: str, tenant_id: str = "acme") -> None:
        await ledger.record_version(
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
            document_class=DocumentClass.SUBMITTED_DOCUMENT,
            title="A document",
        )

    # ---- nothing before it is recorded
    assert await ledger.get_check("acme", "c" * 32) is None
    assert await ledger.check_of_version("acme", "acme/check-1", "v1") is None

    # ---- a check points at a document the ledger has: not at nothing
    with pytest.raises(ValueError, match="isn't recorded"):
        await ledger.record_check(a_check())
    assert await ledger.get_check("acme", "c" * 32) is None

    # ---- recorded, and stamped by the ledger
    await document("acme/check-1")
    await ledger.record_check(a_check())
    stored = await ledger.get_check("acme", "c" * 32)
    assert stored is not None and stored.created_at is not None
    assert stored == a_check(created_at=stored.created_at)
    assert stored.document_format is SubmittedFormat.MARKDOWN
    assert await ledger.check_of_version("acme", "acme/check-1", "v1") == stored

    # ---- tenant-keyed: another tenant's read finds nothing
    assert await ledger.get_check("other-corp", "c" * 32) is None
    assert await ledger.check_of_version("other-corp", "acme/check-1", "v1") is None
    assert await ledger.get_check("acme", "d" * 32) is None

    # ---- append-only: an id, or a version, is checked once
    await document("acme/check-2")
    with pytest.raises(ValueError, match="already"):
        await ledger.record_check(a_check(instrument_id="acme/check-2"))  # same id
    with pytest.raises(ValueError, match="already"):
        await ledger.record_check(a_check(check_id="e" * 32))  # same version
    assert await ledger.check_of_version("acme", "acme/check-2", "v1") is None
    await ledger.record_check(a_check(check_id="e" * 32, instrument_id="acme/check-2"))

    # ---- only a submitted document is checked
    await ledger.record_version(
        tenant_id="acme",
        instrument_id="acme/policy",
        version_id="v1",
        valid_from=JAN,
        supersedes=None,
        document_class=DocumentClass.INTERNAL_POLICY,
        title="A policy",
    )
    with pytest.raises(ValueError, match="not a submitted document"):
        await ledger.record_check(a_check(check_id="f" * 32, instrument_id="acme/policy"))

    # ---- what a check names, refused before anything is written
    await document("acme/check-3")
    for change, reason in (
        ({"trace_id": " "}, "trace_id"),
        ({"user_id": ""}, "submitter"),
        ({"filename": "  "}, "file"),
        ({"check_id": ""}, "id"),
        ({"instrument_id": "other-corp/check-3"}, "tenant's"),
        ({"instrument_id": "check-3"}, "tenant's"),
        ({"tenant_id": "ghost", "instrument_id": "ghost/check-3"}, "registered"),
    ):
        with pytest.raises(ValueError, match=reason):
            await ledger.record_check(a_check(**{"check_id": "1" * 32, **change}))
    assert await ledger.get_check("acme", "1" * 32) is None
    assert await ledger.check_of_version("acme", "acme/check-3", "v1") is None


def a_claim(index: int = 1, **changes: Any) -> ClaimRecord:  # Any: record fields
    fields: dict[str, Any] = {  # Any: record fields
        "claim_index": index,
        "passage": f"p{index}",
        "quote_start": 10 * index,
        "quote_end": 10 * index + 7,
        "quote_sha256": "ab" * 32,
        "summary": "Profiles are kept for 7 years.",
        "obligation_ids": ("OB-PA-APP11-1", "OB-PA-APP11-2"),
        "policy_clause_ids": (),
    }
    fields.update(changes)
    return ClaimRecord(**fields)


def an_extraction(*claims: ClaimRecord, **changes: Any) -> ClaimExtractionRecord:  # Any: fields
    fields: dict[str, Any] = {  # Any: record fields
        "tenant_id": "acme",
        "check_id": "c" * 32,
        "as_of": JUN,
        "trace_id": "a" * 32,
        "claims_dropped": 1,
        "claims": claims
        or (a_claim(1), a_claim(2, obligation_ids=(), policy_clause_ids=("a:v:1",))),
    }
    fields.update(changes)
    return ClaimExtractionRecord(**fields)


async def assert_claims_contract(ledger: LedgerStore) -> None:
    """The claims extracted from a check's document (#298): the extraction is
    reserved atomically before the model is called, then completed once with its
    claims, whole, as `inferred`, or released as failed; read by tenant and in
    order; never replaced. The ledger holds a claim's span and hash, never its
    words."""
    await ledger.put_profile(Profile("acme"))
    await ledger.put_profile(Profile("other-corp"))
    for instrument_id in ("acme/check-1", "acme/check-2", "acme/check-3", "acme/check-4"):
        await ledger.record_version(
            tenant_id="acme",
            instrument_id=instrument_id,
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
            document_class=DocumentClass.SUBMITTED_DOCUMENT,
            title="A document",
        )
    check_1, check_2, check_3, check_4 = "c" * 32, "e" * 32, "f" * 32, "9" * 32

    # ---- nothing before it is recorded; a check the tenant holds is needed
    assert await ledger.get_claims("acme", check_1) is None
    with pytest.raises(ValueError, match="holds no check"):
        await ledger.reserve_extraction("acme", check_1, as_of=JUN, trace_id="a" * 32)
    with pytest.raises(ValueError, match=r"holds no check|reserved"):
        await ledger.record_claims(an_extraction())
    await ledger.record_check(a_check())

    # ---- a reservation is what lets claims be recorded, and it is refused if blank
    with pytest.raises(ValueError, match="reserved"):
        await ledger.record_claims(an_extraction())
    with pytest.raises(ValueError, match="trace_id"):
        await ledger.reserve_extraction("acme", check_1, as_of=JUN, trace_id=" ")
    await ledger.reserve_extraction("acme", check_1, as_of=JUN, trace_id="a" * 32)
    assert await ledger.get_claims("acme", check_1) is None  # pending is not extracted
    with pytest.raises(ExtractionAlreadyReserved):
        await ledger.reserve_extraction("acme", check_1, as_of=JUN, trace_id="b" * 32)

    # ---- recorded whole, stamped by the ledger, numbered in order
    await ledger.record_claims(an_extraction())
    stored = await ledger.get_claims("acme", check_1)
    assert stored is not None and stored.created_at is not None
    assert stored == an_extraction(created_at=stored.created_at)
    assert [c.claim_index for c in stored.claims] == [1, 2]
    assert stored.claims[0].obligation_ids == ("OB-PA-APP11-1", "OB-PA-APP11-2")  # in order
    assert stored.claims[1].obligation_ids == ()
    assert stored.claims[1].policy_clause_ids == ("a:v:1",)
    assert {c.provenance.value for c in stored.claims} == {"inferred"}
    assert (stored.as_of, stored.trace_id, stored.claims_dropped) == (JUN, "a" * 32, 1)

    # ---- tenant-keyed: another tenant's read finds nothing
    assert await ledger.get_claims("other-corp", check_1) is None
    assert await ledger.get_claims("acme", "d" * 32) is None

    # ---- once per check: never replaced, never reserved again, never released
    with pytest.raises(ValueError, match="already extracted"):
        await ledger.record_claims(an_extraction(a_claim(1, summary="Replaced.")))
    with pytest.raises(ExtractionAlreadyReserved):
        await ledger.reserve_extraction("acme", check_1, as_of=JUN, trace_id="b" * 32)
    await ledger.fail_extraction("acme", check_1)  # no-op on an extraction that is done
    assert await ledger.get_claims("acme", check_1) == stored

    # ---- an extraction with no claim is recorded too, so a check is extracted once
    await ledger.record_check(a_check(check_id=check_2, instrument_id="acme/check-2"))
    await ledger.reserve_extraction("acme", check_2, as_of=JUN, trace_id="a" * 32)
    await ledger.record_claims(an_extraction(check_id=check_2, claims=(), claims_dropped=0))
    empty = await ledger.get_claims("acme", check_2)
    assert empty is not None and empty.claims == () and empty.claims_dropped == 0
    with pytest.raises(ExtractionAlreadyReserved):
        await ledger.reserve_extraction("acme", check_2, as_of=JUN, trace_id="b" * 32)

    # ---- of concurrent requests exactly one holds the reservation
    await ledger.record_check(a_check(check_id=check_3, instrument_id="acme/check-3"))
    raced = await asyncio.gather(
        *(
            ledger.reserve_extraction("acme", check_3, as_of=JUN, trace_id=f"{n}" * 32)
            for n in range(1, 7)
        ),
        return_exceptions=True,
    )
    assert [r for r in raced if r is None] == [None]
    assert all(isinstance(r, ExtractionAlreadyReserved) for r in raced if r is not None)

    # ---- a failed extraction is released: it can be reserved again, and only then recorded
    await ledger.fail_extraction("acme", check_3)
    assert await ledger.get_claims("acme", check_3) is None
    with pytest.raises(ValueError, match="reserved"):
        await ledger.record_claims(an_extraction(check_id=check_3))  # not holding it any more
    await ledger.reserve_extraction("acme", check_3, as_of=JUN, trace_id="c" * 32)

    # ---- what an extraction names, refused before anything is written, and the
    # reservation is not spent by a refusal
    for changes, reason in (
        ({"trace_id": " "}, "trace_id"),
        ({"claims_dropped": -1}, "not negative"),
        ({"claims": tuple(a_claim(n) for n in range(1, 27))}, "at most 25"),
        ({"claims": (a_claim(2),)}, "numbered"),
        ({"claims": (a_claim(1), a_claim(3))}, "numbered"),
        ({"claims": (a_claim(1, provenance=EvidenceProvenance.ASSERTED),)}, "inferred"),
        ({"claims": (a_claim(1, passage="3"),)}, "passage path"),
        ({"claims": (a_claim(1, passage="p0"),)}, "passage path"),
        ({"claims": (a_claim(1, quote_end=10),)}, "span"),
        ({"claims": (a_claim(1, quote_start=-1),)}, "span"),
        ({"claims": (a_claim(1, quote_sha256="xyz"),)}, "sha256"),
        ({"claims": (a_claim(1, summary="two\nlines"),)}, "one non-blank line"),
        ({"claims": (a_claim(1, summary="  "),)}, "one non-blank line"),
    ):
        with pytest.raises(ValueError, match=reason):
            await ledger.record_claims(an_extraction(check_id=check_3, **changes))
    assert await ledger.get_claims("acme", check_3) is None
    await ledger.record_claims(an_extraction(check_id=check_3))  # the reservation was not spent
    done = await ledger.get_claims("acme", check_3)
    assert done is not None and len(done.claims) == 2

    # ---- a fresh reservation is not taken over (the time-out is each store's own test)
    await ledger.record_check(a_check(check_id=check_4, instrument_id="acme/check-4"))
    await ledger.reserve_extraction("acme", check_4, as_of=JUN, trace_id="d" * 32)
    with pytest.raises(ExtractionAlreadyReserved):  # not yet: someone may be working on it
        await ledger.reserve_extraction("acme", check_4, as_of=JUN, trace_id="e" * 32)


async def test_the_fake_ledger_satisfies_the_claims_contract() -> None:
    await assert_claims_contract(FakeLedgerStore())


async def test_the_fake_ledger_satisfies_the_check_contract() -> None:
    await assert_check_contract(FakeLedgerStore())


def test_a_check_is_visible_to_its_submitter_and_to_admins_alone() -> None:
    record = a_check()
    assert record.visible_to("uid-1", admin=False)
    assert record.visible_to("uid-2", admin=True)
    assert not record.visible_to("uid-2", admin=False)
    assert not record.visible_to("", admin=False)
    assert not a_check(user_id="uid-2").visible_to("uid-1", admin=False)


async def assert_refusal_outcomes_contract(ledger: LedgerStore) -> None:
    """A refused generation and an over-cap corpus are their own outcomes, kept
    with their one reason or measure (#289), and read back as recorded."""
    await ledger.put_profile(Profile("acme"))
    unavailable = ChatAnswer(TurnOutcome.GENERATION_UNAVAILABLE, unavailable=Unavailability.QUOTA)
    too_large = ChatAnswer(
        TurnOutcome.POLICY_CORPUS_TOO_LARGE, prefix=PrefixMeasure(31_204, 30_000)
    )
    for number, shown in enumerate((unavailable, too_large, ChatAnswer(TurnOutcome.INTERNAL))):
        await ledger.record_chat_exchange(
            an_exchange("refused", answer=shown, trace_id=f"{number}" * 32)
        )
    stored = await ledger.chat_turns("acme", "uid-1", "refused")
    assert [(t.outcome, t.content, t.citations, t.blocked) for t in stored[1::2]] == [
        (TurnOutcome.GENERATION_UNAVAILABLE, None, (), ()),
        (TurnOutcome.POLICY_CORPUS_TOO_LARGE, None, (), ()),
        (TurnOutcome.INTERNAL, None, (), ()),
    ]
    assert [(t.unavailable, t.prefix) for t in stored[1::2]] == [
        (Unavailability.QUOTA, None),
        (None, PrefixMeasure(31_204, 30_000)),
        (None, None),
    ]
    # the question turns hold no detail; and the turns are exactly as written
    assert [(t.unavailable, t.prefix) for t in stored[0::2]] == [(None, None)] * 3
    assert tuple(replace(t, created_at=None) for t in stored[:2]) == an_exchange(
        "refused", answer=unavailable, trace_id="0" * 32
    ).turns(0)


async def test_the_fake_ledger_satisfies_the_refusal_outcomes_contract() -> None:
    await assert_refusal_outcomes_contract(FakeLedgerStore())


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: ChatAnswer(TurnOutcome.GENERATION_UNAVAILABLE), id="no-reason"),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.INTERNAL, unavailable=Unavailability.QUOTA),
            id="a-reason-on-another-outcome",
        ),
        pytest.param(
            lambda: ChatAnswer(
                TurnOutcome.GENERATION_UNAVAILABLE,
                unavailable=Unavailability.QUOTA,
                prefix=PrefixMeasure(1, 2),
            ),
            id="both-details",
        ),
        pytest.param(lambda: ChatAnswer(TurnOutcome.POLICY_CORPUS_TOO_LARGE), id="no-measure"),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.VERIFIED, "t", prefix=PrefixMeasure(1, 2)),
            id="a-measure-on-a-verified-answer",
        ),
        pytest.param(lambda: PrefixMeasure(-1, 2), id="negative-estimate"),
        pytest.param(lambda: PrefixMeasure(1, -2), id="negative-cap"),
        pytest.param(lambda: PrefixMeasure(1.5, 2), id="fractional"),  # pyright: ignore[reportArgumentType]
        pytest.param(lambda: PrefixMeasure(True, 2), id="a-bool"),
    ],
)
def test_a_refusal_names_its_one_detail_and_no_other_turn_does(build: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        build()


def test_both_refusals_are_failures_so_they_carry_no_flag() -> None:
    assert TurnOutcome.GENERATION_UNAVAILABLE.failed and TurnOutcome.POLICY_CORPUS_TOO_LARGE.failed
    with pytest.raises(ValueError, match="marker only"):
        ChatAnswer(
            TurnOutcome.GENERATION_UNAVAILABLE,
            unavailable=Unavailability.QUOTA,
            not_in_environment=(NotInEnvironment("spam act"),),
        )


@pytest.mark.parametrize(
    ("build", "why"),
    [
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.BLOCKED, MODEL_PROSE, (), (APP12,)),
            "holds no text",
            id="blocked-with-text",
        ),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.UNCITED, MODEL_PROSE), "holds no text", id="uncited-text"
        ),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.INTERNAL, MODEL_PROSE), "holds no text", id="error-text"
        ),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.UNCITED, None, VERIFIED.citations),
            "holds no text",
            id="withheld-with-citations",
        ),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.BLOCKED), "names the citations", id="blocked-0"
        ),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.UNCITED, blocked=(APP12,)),
            "names the citations",
            id="uncited-names-blocked",
        ),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.BLOCKED, blocked=(MODEL_PROSE,)),
            "stable clause ID",
            id="blocked-id-is-prose",
        ),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.VERIFIED, "text"),
            "text and citations",
            id="verified-uncited",
        ),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.VERIFIED, " ", VERIFIED.citations),
            "text and citations",
            id="verified-blank",
        ),
        pytest.param(
            lambda: ChatAnswer(TurnOutcome.VERIFIED, "t", VERIFIED.citations, (APP12,)),
            "none blocked",
            id="verified-and-blocked",
        ),
        pytest.param(
            lambda: ChatCitation(APP11, "v9"), "not a clause of version v9", id="wrong-version"
        ),
        pytest.param(
            lambda: ChatAnswer(
                TurnOutcome.INTERNAL, not_in_environment=(NotInEnvironment("spam act"),)
            ),
            "marker only",
            id="failed-with-a-flag",
        ),
        pytest.param(lambda: NotInEnvironment(" "), "printable line", id="blank-flag"),
        pytest.param(lambda: NotInEnvironment("a\nb"), "printable line", id="multi-line-flag"),
    ],
)
def test_what_an_answer_turn_may_hold_is_fixed_when_the_answer_is_built(
    build: Callable[[], object], why: str
) -> None:
    """ADR-0003: a withheld or failed answer can't carry the model's text, and
    a verified one must carry its citations. No store is ever handed one that
    does otherwise, whichever store it is: this is the type's, not the
    ledger's, so it isn't part of the shared contract."""
    with pytest.raises(ValueError, match=why):
        build()


async def _a_checked_document(ledger: LedgerStore) -> None:
    await ledger.put_profile(Profile("acme"))
    await ledger.record_version(
        tenant_id="acme",
        instrument_id="acme/check-1",
        version_id="v1",
        valid_from=JAN,
        supersedes=None,
        document_class=DocumentClass.SUBMITTED_DOCUMENT,
        title="A document",
    )
    await ledger.record_check(a_check())


async def test_the_fake_ledger_takes_over_a_reservation_held_past_the_time_out() -> None:
    """A request that died holding one (the process was killed, so nothing marked
    it failed) must not block its check for good (`RESERVATION_TTL`)."""
    ledger = FakeLedgerStore()
    await _a_checked_document(ledger)
    await ledger.reserve_extraction("acme", "c" * 32, as_of=JUN, trace_id="a" * 32)
    key = ("acme", "c" * 32)
    held = ledger._extractions[key]  # pyright: ignore[reportPrivateUsage]
    ledger._extractions[key] = replace(  # pyright: ignore[reportPrivateUsage]
        held, reserved_at=held.reserved_at - RESERVATION_TTL - dt.timedelta(seconds=1)
    )
    await ledger.reserve_extraction("acme", "c" * 32, as_of=JUN, trace_id="b" * 32)  # taken over
    await ledger.record_claims(an_extraction(trace_id="b" * 32))
    # and one that is done is never taken over, however old
    done = ledger._extractions[key]  # pyright: ignore[reportPrivateUsage]
    ledger._extractions[key] = replace(  # pyright: ignore[reportPrivateUsage]
        done, reserved_at=done.reserved_at - dt.timedelta(days=1)
    )
    with pytest.raises(ExtractionAlreadyReserved):
        await ledger.reserve_extraction("acme", "c" * 32, as_of=JUN, trace_id="c" * 32)


# ---- findings (#299) -------------------------------------------------------------------


def a_citation(clause_id: str = "privacy-act-1988:C2026C00227:sch1/app8/1") -> FindingCitation:
    return FindingCitation(
        clause_id=clause_id,
        version_id=clause_id.split(":")[1],
        quoted_text="reasonable in the circumstances",
        canonicalisation_version=1,
    )


def a_verdict(claim: int = 1, **changes: Any) -> FindingRecord:  # Any: record fields
    fields: dict[str, Any] = {  # Any: record fields
        "finding_id": f"f{claim}" + "0" * 30,
        "claim_index": claim,
        "outcome": FindingOutcome.VERDICT,
        "obligation_id": "OB-PA-APP8-1",
        "verdict": Verdict.NOT_ADDRESSED,
        "citations": (a_citation(),),
    }
    fields.update(changes)
    return FindingRecord(**fields)


QUESTION = Question(
    condition_id="c1",
    field="annual_turnover_aud",
    period="FY2023-24",
    question="What was annual turnover in 2023-24?",
    unassessable_reason="turnover for 2023-24 is not known",
    clause_ids=("privacy-act-1988:C2026C00227:s6d/1",),
)


FACTS = FactsSnapshot(
    (
        FactRow(
            "e1",
            "annual_turnover_aud",
            2_500_000,
            dt.date(2025, 7, 1),
            dt.date(2026, 7, 1),
            EvidenceProvenance.ASSERTED,
        ),
        FactRow(
            "e2",
            "business_started_on",
            dt.date(2015, 3, 2),
            dt.date(2015, 3, 2),
            dt.date(9999, 12, 31),
            EvidenceProvenance.ASSERTED,
            supersedes="e0",
        ),
        FactRow(
            "e3",
            "legal_form",
            "body_corporate",
            dt.date(2015, 3, 2),
            dt.date(9999, 12, 31),
            EvidenceProvenance.PARSED,
        ),
    )
)
"""The facts a run was decided on: a number, a date and a word, one superseding another."""


def findings_coverage() -> tuple[CoverageRow, ...]:
    """Three obligations: one applicable, one unassessable with its question, one not
    applicable (which no finding is ever against)."""
    return (
        CoverageRow(
            "OB-PA-APP11-1",
            Outcome.APPLICABLE,
            "applies",
            ("privacy-act-1988:C2026C00227:sch1/app11/1",),
            (),
        ),
        CoverageRow("OB-PA-APP3-1", Outcome.NOT_APPLICABLE, "agency only", ("x:v:1",), ()),
        CoverageRow("OB-PA-APP8-1", Outcome.UNASSESSABLE, "turnover not known", (), (QUESTION,)),
    )


def a_findings_run(
    check_id: str = "c" * 32, *findings: FindingRecord, **changes: Any
) -> FindingsRecord:  # Any: record fields
    assessment = AssessmentRecord(
        tenant_id="acme",
        assessment_id=changes.pop("assessment_id", "assess-" + check_id[:4]),
        user_id="lead@acme.example",
        corpus=SourceVersion(SourceKind.CORPUS, sha=SHA_CORPUS, tag="corpus-2026.10"),
        mapping=SourceVersion(SourceKind.MAPPING, sha=SHA_MAPPING),
        as_of=JUN,
        trace_id=changes.pop("trace_id", "a" * 32),
        check_id=check_id,
        facts=changes.pop("facts", FACTS),
    )
    fields: dict[str, Any] = {  # Any: record fields
        "assessment": assessment,
        "coverage": findings_coverage(),
        "findings": findings
        or (
            a_verdict(1, obligation_id="OB-PA-APP11-1"),
            a_verdict(
                1,
                finding_id="f1p" + "0" * 29,
                obligation_id=None,
                policy_clause_id="acme/privacy-policy:v1:3/1",
                verdict=Verdict.AMBIGUOUS,
                settle_by="Whether a segment profile is account information.",
                citations=(a_citation("acme/privacy-policy:v1:3/1"),),
            ),
            FindingRecord(
                finding_id="f1c" + "0" * 29,
                claim_index=1,
                outcome=FindingOutcome.CONDITIONAL,
                obligation_id="OB-PA-APP8-1",
                unassessable_reason="turnover not known",
            ),
            FindingRecord(
                finding_id="f2w" + "0" * 29,
                claim_index=2,
                outcome=FindingOutcome.WITHHELD,
                obligation_id="OB-PA-APP11-1",
                detail=WithheldReason.CITATION_BLOCKED,
                blocked=(BlockedCitation("privacy-act-1988:C2026C00227:s6c/2", "not_in_context"),),
            ),
            FindingRecord(
                finding_id="f2f" + "0" * 29,
                claim_index=2,
                outcome=FindingOutcome.FAILED,
                policy_clause_id="acme/privacy-policy:v1:3/1",
                detail=FailureReason.NO_FINDING_RETURNED,
            ),
        ),
        "usage": (
            UsageRecord(
                "acme", "u1" + "0" * 30, "a" * 32, "gemini-3.5-flash", 1200, 90, False, 0.01
            ),
            UsageRecord(
                "acme", "u2" + "0" * 30, "a" * 32, "gemini-3.5-flash", 1300, 80, True, 0.002
            ),
        ),
    }
    fields.update(changes)
    return FindingsRecord(**fields)


async def assert_findings_contract(ledger: LedgerStore) -> None:
    """The findings of a check (#299, ADR-0053 F1-F6): the run is reserved
    atomically before any model is called, then the assessment, its coverage, every
    finding and the usage land in one transaction, or none of it does; read back as
    written, by tenant; never replaced, so an earlier assessment keeps what it said
    (ADR-0034)."""
    await ledger.put_profile(Profile("acme"))
    await ledger.put_profile(Profile("other-corp"))
    for instrument_id in ("acme/check-1", "acme/check-2", "acme/check-3"):
        await ledger.record_version(
            tenant_id="acme",
            instrument_id=instrument_id,
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
            document_class=DocumentClass.SUBMITTED_DOCUMENT,
            title="A document",
        )
    check_1, check_2, check_3 = "c" * 32, "e" * 32, "f" * 32

    # ---- nothing is there before it is recorded; there is a check to run it for
    assert await ledger.get_findings("acme", check_1) is None
    with pytest.raises(ValueError, match="holds no check"):
        await ledger.reserve_findings("acme", check_1, trace_id="a" * 32)
    await ledger.record_check(a_check())
    await ledger.record_check(a_check(check_id=check_2, instrument_id="acme/check-2"))
    await ledger.record_check(a_check(check_id=check_3, instrument_id="acme/check-3"))

    # ---- the claims come first: there is nothing to check without them
    with pytest.raises(CheckNotExtracted):
        await ledger.reserve_findings("acme", check_1, trace_id="a" * 32)
    for check in (check_1, check_2, check_3):
        await ledger.reserve_extraction("acme", check, as_of=JUN, trace_id="a" * 32)
        await ledger.record_claims(an_extraction(check_id=check))

    # ---- a reservation is what lets findings be recorded
    with pytest.raises(ValueError, match="reserved"):
        await ledger.record_findings(a_findings_run(check_1))
    with pytest.raises(ValueError, match="trace_id"):
        await ledger.reserve_findings("acme", check_1, trace_id=" ")
    await ledger.reserve_findings("acme", check_1, trace_id="a" * 32)
    assert await ledger.get_findings("acme", check_1) is None  # pending is not made
    with pytest.raises(FindingsAlreadyReserved):  # not yet: someone may be working on it
        await ledger.reserve_findings("acme", check_1, trace_id="b" * 32)

    # ---- released as failed, it can be reserved again; a release that finds nothing pending
    # does nothing
    await ledger.fail_findings("acme", check_1, trace_id="a" * 32)
    await ledger.fail_findings("acme", check_2, trace_id="a" * 32)  # never reserved
    await ledger.reserve_findings("acme", check_1, trace_id="a" * 32)
    with pytest.raises(ValueError, match="reserved"):
        await ledger.record_findings(a_findings_run(check_2))  # check_2 was never reserved

    # ---- a refused record writes nothing: no assessment, no coverage, no usage
    await ledger.fail_findings("acme", check_3, trace_id="a" * 32)  # nothing reserved: a no-op
    for change, reason in (
        ("ambiguous_without_settle_by", "settle"),
        ("settle_by_on_a_verdict", "only an ambiguous"),
        ("verdict_without_citation", "cites at least one"),
        ("withheld_with_a_verdict", "if and only if"),
        ("withheld_without_a_reason", "says why"),
        ("withheld_with_a_failure_reason", "withheld reason"),
        ("conditional_against_a_decided_obligation", "conditional if and only if"),
        ("a_decided_obligation_is_not_conditional", "conditional if and only if"),
        ("not_applicable_obligation", "applies or might"),
        ("an_obligation_outside_the_coverage", "applies or might"),
        ("claim_beyond_the_extraction", "not one of the check's claims"),
        ("both_targets", "one obligation or one policy clause"),
        ("neither_target", "one obligation or one policy clause"),
        ("a_claim_twice_against_one_target", "one finding against each"),
        ("provenance_asserted", "inferred"),
        ("foreign_usage", "tenant's"),
        ("no_check", "check it judged"),
        ("no_facts", "facts it was decided on"),
    ):
        base = a_findings_run(check_1)
        findings = list(base.findings)
        run = base
        if change == "ambiguous_without_settle_by":
            findings[1] = replace(findings[1], settle_by=None)
        elif change == "settle_by_on_a_verdict":
            findings[0] = replace(findings[0], settle_by="Something.")
        elif change == "verdict_without_citation":
            findings[0] = replace(findings[0], citations=())
        elif change == "withheld_with_a_verdict":
            findings[3] = replace(findings[3], verdict=Verdict.CONSISTENT)
        elif change == "withheld_without_a_reason":
            findings[3] = replace(findings[3], detail=None)
        elif change == "conditional_against_a_decided_obligation":
            findings = [
                FindingRecord(
                    finding_id="fc" + "0" * 30,
                    claim_index=1,
                    outcome=FindingOutcome.CONDITIONAL,
                    obligation_id="OB-PA-APP11-1",
                    unassessable_reason="x",
                )
            ]
        elif change == "a_decided_obligation_is_not_conditional":
            findings = [a_verdict(1, obligation_id="OB-PA-APP8-1")]
        elif change == "not_applicable_obligation":
            findings[0] = replace(findings[0], obligation_id="OB-PA-APP3-1")
        elif change == "an_obligation_outside_the_coverage":
            findings[0] = replace(findings[0], obligation_id="OB-NOT-LISTED")
        elif change == "claim_beyond_the_extraction":
            findings[0] = replace(findings[0], claim_index=3)
        elif change == "both_targets":
            findings[0] = replace(findings[0], policy_clause_id="acme/privacy-policy:v1:3/1")
        elif change == "neither_target":
            findings[0] = replace(findings[0], obligation_id=None)
        elif change == "a_claim_twice_against_one_target":
            findings.append(replace(findings[0], finding_id="fdup" + "0" * 28))
        elif change == "provenance_asserted":
            findings[0] = replace(findings[0], provenance=EvidenceProvenance.ASSERTED)
        elif change == "foreign_usage":
            run = replace(
                base,
                usage=(
                    UsageRecord("other-corp", "ux" + "0" * 30, "a" * 32, "m", 1, 1, False, 0.0),
                ),
            )
        elif change == "no_check":
            run = replace(base, assessment=replace(base.assessment, check_id=None))
        elif change == "no_facts":
            run = replace(base, assessment=replace(base.assessment, facts=None))
        elif change == "withheld_with_a_failure_reason":
            findings[3] = replace(findings[3], detail=FailureReason.NO_FINDING_RETURNED)
        if findings != list(base.findings):
            run = replace(base, findings=tuple(findings))
        with pytest.raises(ValueError, match=reason):
            await ledger.record_findings(run)
        assert await ledger.get_findings("acme", check_1) is None, change
        assert await ledger.get_assessment("acme", base.assessment.assessment_id) is None, change
        assert await ledger.coverage_rows("acme", base.assessment.assessment_id) == (), change
        assert await ledger.usage("acme") == (), change
    # the reservation was not spent by any of them
    assert await ledger.count_findings_citing("acme", "privacy-act-1988:") == 0

    # ---- recorded whole, as written: the assessment (with both versions), its coverage,
    # every finding, and the usage, in one transaction
    run = a_findings_run(check_1)
    await ledger.record_findings(run)
    stored = await ledger.get_findings("acme", check_1)
    assert stored is not None
    assert stored.assessment == run.assessment
    assert stored.assessment.check_id == check_1
    # the facts the run was decided on come back as written, and are what they hashed
    assert stored.assessment.facts == FACTS
    assert stored.assessment.facts is not None and stored.assessment.facts.sha256 == FACTS.sha256
    assert (await ledger.get_assessment("acme", run.assessment.assessment_id)) is not None
    assert stored.coverage == tuple(sorted(run.coverage, key=lambda r: r.obligation_id))
    ordered = sorted(
        run.findings,
        key=lambda f: (
            f.claim_index,
            f.obligation_id is None,
            f.obligation_id or f.policy_clause_id,
        ),
    )
    assert list(stored.findings) == ordered
    assert sorted(u.usage_id for u in stored.usage) == sorted(u.usage_id for u in run.usage)
    assert {f.provenance for f in stored.findings} == {EvidenceProvenance.INFERRED}
    # the assessment is also an assessment: read as one, with its coverage and versions
    assert await ledger.get_assessment("acme", run.assessment.assessment_id) == run.assessment
    assert await ledger.coverage_rows("acme", run.assessment.assessment_id) == stored.coverage
    # a verdict is only on the verdict rows; a withheld finding stores none, only the clauses
    # whose citations blocked it
    by_id = {f.finding_id: f for f in stored.findings}
    withheld = by_id["f2w" + "0" * 29]
    assert withheld.verdict is None and withheld.citations == () and withheld.settle_by is None
    assert withheld.blocked == (
        BlockedCitation("privacy-act-1988:C2026C00227:s6c/2", "not_in_context"),
    )
    conditional = by_id["f1c" + "0" * 29]
    assert conditional.verdict is None and conditional.unassessable_reason == "turnover not known"
    assert by_id["f1p" + "0" * 29].settle_by == "Whether a segment profile is account information."
    # the usage is in the ledger's usage, under the run's trace
    spent = await ledger.usage("acme")
    assert {u.usage_id for u in spent} == {u.usage_id for u in run.usage}
    assert {u.trace_id for u in spent} == {"a" * 32}
    # what is cited counts as cited (a withheld finding's blocked clause is not)
    assert await ledger.count_findings_citing("acme", "privacy-act-1988:") == 1
    assert await ledger.count_findings_citing("acme", "acme/privacy-policy:") == 1

    # ---- tenant-keyed, and never replaced
    assert await ledger.get_findings("other-corp", check_1) is None
    assert await ledger.get_findings("acme", check_2) is None
    with pytest.raises(FindingsAlreadyReserved):
        await ledger.reserve_findings("acme", check_1, trace_id="c" * 32)
    with pytest.raises(ValueError, match="already"):
        await ledger.record_findings(
            a_findings_run(
                check_1, a_verdict(1, obligation_id="OB-PA-APP11-1"), assessment_id="again"
            )
        )
    assert await ledger.get_findings("acme", check_1) == stored  # unchanged
    assert await ledger.get_assessment("acme", "again") is None

    # ---- another check, run against other facts, is another assessment: the first is untouched
    await ledger.reserve_findings("acme", check_2, trace_id="d" * 32)
    firm = tuple(
        CoverageRow(
            r.obligation_id,
            Outcome.APPLICABLE if r.obligation_id == "OB-PA-APP8-1" else r.outcome,
            "applies" if r.obligation_id == "OB-PA-APP8-1" else r.reason,
            ("privacy-act-1988:C2026C00227:sch1/app8/1",)
            if r.obligation_id == "OB-PA-APP8-1"
            else r.citations,
            () if r.obligation_id == "OB-PA-APP8-1" else r.questions,
        )
        for r in findings_coverage()
    )
    await ledger.record_findings(
        a_findings_run(
            check_2,
            a_verdict(1, finding_id="g1" + "0" * 30, obligation_id="OB-PA-APP8-1"),
            coverage=firm,
            usage=(),
            trace_id="d" * 32,
        )
    )
    second = await ledger.get_findings("acme", check_2)
    assert second is not None and second.assessment.assessment_id != stored.assessment.assessment_id
    assert [f.outcome for f in second.findings] == [FindingOutcome.VERDICT]
    assert second.usage == ()
    assert await ledger.get_findings("acme", check_1) == stored  # the earlier one says what it said

    # ---- a run that outlived its reservation records nothing: only the request that holds
    # the reservation may complete it, or release it
    await ledger.reserve_findings("acme", check_3, trace_id="e" * 32)
    await ledger.fail_findings("acme", check_3, trace_id="f" * 32)  # not the holder: nothing
    with pytest.raises(FindingsAlreadyReserved):
        await ledger.reserve_findings("acme", check_3, trace_id="9" * 32)  # still held by `e`
    with pytest.raises(ValueError, match="no longer"):  # another request's run is not the holder
        await ledger.record_findings(a_findings_run(check_3, trace_id="f" * 32))
    await ledger.fail_findings("acme", check_3, trace_id="e" * 32)  # the holder releases it
    await ledger.reserve_findings("acme", check_3, trace_id="d" * 32)  # and a successor takes it
    spent = len(await ledger.usage("acme"))
    with pytest.raises(ValueError, match="no longer"):  # the stale run, finishing late
        await ledger.record_findings(a_findings_run(check_3, trace_id="e" * 32))
    assert await ledger.get_findings("acme", check_3) is None
    assert len(await ledger.usage("acme")) == spent  # not even its usage: the caller's, apart
    await ledger.fail_findings("acme", check_3, trace_id="e" * 32)  # and it can't release theirs
    with pytest.raises(FindingsAlreadyReserved):
        await ledger.reserve_findings("acme", check_3, trace_id="8" * 32)
    successor = a_verdict(1, finding_id="h1" + "0" * 30, obligation_id="OB-PA-APP11-1")
    await ledger.record_findings(a_findings_run(check_3, successor, usage=(), trace_id="d" * 32))
    assert await ledger.get_findings("acme", check_3) is not None


async def test_the_fake_ledger_satisfies_the_findings_contract() -> None:
    await assert_findings_contract(FakeLedgerStore())


async def test_the_fake_ledger_takes_over_a_findings_reservation_held_past_the_time_out() -> None:
    ledger = FakeLedgerStore()
    await _a_checked_document(ledger)
    await ledger.reserve_extraction("acme", "c" * 32, as_of=JUN, trace_id="a" * 32)
    await ledger.record_claims(an_extraction())
    await ledger.reserve_findings("acme", "c" * 32, trace_id="a" * 32)
    key = ("acme", "c" * 32)
    held = ledger._findings_runs[key]  # pyright: ignore[reportPrivateUsage]
    ledger._findings_runs[key] = replace(  # pyright: ignore[reportPrivateUsage]
        held, reserved_at=held.reserved_at - FINDINGS_RESERVATION_TTL - dt.timedelta(seconds=1)
    )
    await ledger.reserve_findings("acme", "c" * 32, trace_id="b" * 32)  # taken over
    await ledger.record_findings(a_findings_run("c" * 32, trace_id="b" * 32))
    done = ledger._findings_runs[key]  # pyright: ignore[reportPrivateUsage]
    ledger._findings_runs[key] = replace(  # pyright: ignore[reportPrivateUsage]
        done, reserved_at=done.reserved_at - dt.timedelta(days=1)
    )
    with pytest.raises(FindingsAlreadyReserved):  # one that is done is never taken over
        await ledger.reserve_findings("acme", "c" * 32, trace_id="c" * 32)


# ---- the list of checks (#355) ---------------------------------------------------------


async def assert_check_list_contract(ledger: LedgerStore) -> None:
    """The list of checks (#355): newest first, by visibility (`CheckRecord.visible_to`),
    each with the stage the ledger holds, in one read, metadata only, and paged by a
    cursor."""
    await ledger.put_profile(Profile("acme"))
    await ledger.put_profile(Profile("other-corp"))

    ids = {n: f"{n:x}" * 32 for n in range(1, 10)}  # 1111..., 2222..., ..., 9999...

    async def submitted(n: int, uid: str, title: str, tenant: str = "acme") -> str:
        check_id = ids[n]
        await ledger.record_version(
            tenant_id=tenant,
            instrument_id=f"{tenant}/check-{n}",
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
            document_class=DocumentClass.SUBMITTED_DOCUMENT,
            title=title,
        )
        await ledger.record_check(
            a_check(
                tenant_id=tenant,
                check_id=check_id,
                user_id=uid,
                instrument_id=f"{tenant}/check-{n}",
                filename=f"file-{n}.md",
            )
        )
        return check_id

    async def extracted(check_id: str, trace_id: str = "a" * 32) -> None:
        await ledger.reserve_extraction("acme", check_id, as_of=JUN, trace_id=trace_id)
        await ledger.record_claims(an_extraction(check_id=check_id, trace_id=trace_id))

    # nothing to list, for anyone
    assert await ledger.list_checks("acme", "uid-1", admin=False, limit=10) == ()
    assert await ledger.list_checks("acme", "uid-1", admin=True, limit=10) == ()

    # one check in each state, oldest first
    await submitted(1, "uid-1", "Alpha")  # submitted
    await submitted(2, "uid-2", "Beta")
    await extracted(ids[2])  # claims
    await submitted(3, "uid-1", "Gamma")
    await extracted(ids[3])
    await ledger.reserve_findings("acme", ids[3], trace_id="a" * 32)
    await ledger.record_findings(a_findings_run(ids[3]))  # findings
    await submitted(4, "uid-1", "Delta")
    await ledger.reserve_extraction("acme", ids[4], as_of=JUN, trace_id="a" * 32)  # running
    await submitted(5, "uid-2", "Epsilon")
    await ledger.reserve_extraction("acme", ids[5], as_of=JUN, trace_id="a" * 32)
    await ledger.fail_extraction("acme", ids[5])  # submitted, failed
    await submitted(6, "uid-1", "Zeta")
    await extracted(ids[6])
    await ledger.reserve_findings("acme", ids[6], trace_id="a" * 32)  # claims, running
    await submitted(7, "uid-2", "Eta")
    await extracted(ids[7])
    await ledger.reserve_findings("acme", ids[7], trace_id="a" * 32)
    await ledger.fail_findings("acme", ids[7], trace_id="a" * 32)  # claims, failed
    # a check in another tenant never shows, to its own submitter or anyone
    await submitted(8, "uid-1", "Theta", tenant="other-corp")

    # ---- an admin has the tenant's, newest first, each with its stage and title
    everything = await ledger.list_checks("acme", "admin-uid", admin=True, limit=50)
    assert [c.check_id for c in everything] == [ids[n] for n in (7, 6, 5, 4, 3, 2, 1)]
    assert [(c.title, c.stage, c.run) for c in everything] == [
        ("Eta", CheckStage.CLAIMS, CheckRun.FAILED),
        ("Zeta", CheckStage.CLAIMS, CheckRun.RUNNING),
        ("Epsilon", CheckStage.SUBMITTED, CheckRun.FAILED),
        ("Delta", CheckStage.SUBMITTED, CheckRun.RUNNING),
        ("Gamma", CheckStage.FINDINGS, None),
        ("Beta", CheckStage.CLAIMS, None),
        ("Alpha", CheckStage.SUBMITTED, None),
    ]
    assert everything[0].user_id == "uid-2" and everything[0].filename == "file-7.md"
    stamps = [c.created_at for c in everything]
    assert stamps == sorted(stamps, reverse=True)
    stored = await ledger.get_check("acme", ids[7])
    assert stored is not None and everything[0].created_at == stored.created_at

    # ---- a member has their own and no one else's
    mine = await ledger.list_checks("acme", "uid-1", admin=False, limit=50)
    assert [c.check_id for c in mine] == [ids[n] for n in (6, 4, 3, 1)]
    assert {c.user_id for c in mine} == {"uid-1"}
    theirs = await ledger.list_checks("acme", "uid-2", admin=False, limit=50)
    assert [c.check_id for c in theirs] == [ids[n] for n in (7, 5, 2)]
    assert await ledger.list_checks("acme", "stranger", admin=False, limit=50) == ()
    # the same uid in another tenant is that tenant's alone
    other = await ledger.list_checks("other-corp", "uid-1", admin=False, limit=50)
    assert [(c.check_id, c.title) for c in other] == [(ids[8], "Theta")]
    other_admin = await ledger.list_checks("other-corp", "x", admin=True, limit=5)
    assert [c.check_id for c in other_admin] == [ids[8]]
    assert await ledger.list_checks("ghost", "uid-1", admin=True, limit=5) == ()

    # ---- a page is at most `limit`, and the cursor continues strictly after its last row
    first = await ledger.list_checks("acme", "admin-uid", admin=True, limit=3)
    assert [c.check_id for c in first] == [ids[n] for n in (7, 6, 5)]
    cursor = CheckCursor(first[-1].created_at, first[-1].check_id)
    second = await ledger.list_checks("acme", "admin-uid", admin=True, limit=3, before=cursor)
    assert [c.check_id for c in second] == [ids[n] for n in (4, 3, 2)]
    cursor = CheckCursor(second[-1].created_at, second[-1].check_id)
    third = await ledger.list_checks("acme", "admin-uid", admin=True, limit=3, before=cursor)
    assert [c.check_id for c in third] == [ids[1]]
    cursor = CheckCursor(third[-1].created_at, third[-1].check_id)
    assert await ledger.list_checks("acme", "admin-uid", admin=True, limit=3, before=cursor) == ()
    # a cursor applies the visibility rule too
    after = CheckCursor(mine[0].created_at, mine[0].check_id)
    rest = await ledger.list_checks("acme", "uid-1", admin=False, limit=9, before=after)
    assert [c.check_id for c in rest] == [ids[n] for n in (4, 3, 1)]
    # at one instant, rows are ordered by id, descending: the cursor's id is the tie-break
    same_instant = CheckCursor(first[0].created_at, "0" * 32)  # the newest row's id is above it
    older = await ledger.list_checks("acme", "a", admin=True, limit=9, before=same_instant)
    assert [c.check_id for c in older] == [ids[n] for n in (6, 5, 4, 3, 2, 1)]
    same_instant = CheckCursor(first[0].created_at, "~" * 32)
    top = await ledger.list_checks("acme", "a", admin=True, limit=1, before=same_instant)
    assert [c.check_id for c in top] == [ids[7]]

    # ---- a limit that is not a page is refused, not clipped
    for limit in (0, -1, CHECK_PAGE_MAX + 1):
        with pytest.raises(ValueError, match="limit"):
            await ledger.list_checks("acme", "uid-1", admin=True, limit=limit)
    assert len(await ledger.list_checks("acme", "uid-1", admin=True, limit=CHECK_PAGE_MAX)) == 7

    # ---- a run done is at rest: findings recorded, nothing running
    await ledger.fail_findings("acme", ids[6], trace_id="a" * 32)
    await ledger.reserve_findings("acme", ids[6], trace_id="b" * 32)  # failed, taken again
    own_finding = a_verdict(1, finding_id="f6" + "0" * 30, obligation_id="OB-PA-APP11-1")
    await ledger.record_findings(a_findings_run(ids[6], own_finding, usage=(), trace_id="b" * 32))
    done = {c.check_id: c for c in await ledger.list_checks("acme", "uid-1", admin=False, limit=9)}
    assert (done[ids[6]].stage, done[ids[6]].run) == (CheckStage.FINDINGS, None)
    # a failed extraction, run again to its end, is claims
    await ledger.reserve_extraction("acme", ids[5], as_of=JUN, trace_id="b" * 32)
    await ledger.record_claims(an_extraction(check_id=ids[5], trace_id="b" * 32))
    again = {c.check_id: c for c in await ledger.list_checks("acme", "uid-2", admin=False, limit=9)}
    assert (again[ids[5]].stage, again[ids[5]].run) == (CheckStage.CLAIMS, None)


async def test_the_fake_ledger_satisfies_the_check_list_contract() -> None:
    await assert_check_list_contract(FakeLedgerStore())


async def test_the_fake_ledger_does_not_call_a_reservation_past_its_time_out_running() -> None:
    """A request that died holding a reservation is not running anything (`CheckRun`)."""
    ledger = FakeLedgerStore()
    await _a_checked_document(ledger)
    await ledger.reserve_extraction("acme", "c" * 32, as_of=JUN, trace_id="a" * 32)
    (row,) = await ledger.list_checks("acme", "uid-1", admin=False, limit=5)
    assert (row.stage, row.run) == (CheckStage.SUBMITTED, CheckRun.RUNNING)
    key = ("acme", "c" * 32)
    held = ledger._extractions[key]  # pyright: ignore[reportPrivateUsage]
    ledger._extractions[key] = replace(  # pyright: ignore[reportPrivateUsage]
        held, reserved_at=held.reserved_at - RESERVATION_TTL - dt.timedelta(seconds=1)
    )
    (row,) = await ledger.list_checks("acme", "uid-1", admin=False, limit=5)
    assert (row.stage, row.run) == (CheckStage.SUBMITTED, None)
