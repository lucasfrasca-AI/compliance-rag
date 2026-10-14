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
    AssessmentRecord,
    ChatAnswer,
    ChatCitation,
    ChatExchange,
    ChatTurn,
    LedgerStore,
    PolicyVersion,
    Profile,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
    SourceKind,
    SourceVersion,
    TurnOutcome,
    TurnRole,
    UsageRecord,
)
from compliance_rag.assessment.coverage import CoverageRow, Outcome
from compliance_rag.assessment.evidence import Evidence, EvidenceProvenance, Facts, Period
from compliance_rag.corpus.models import DocumentClass
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
    )
    await ledger.record_version(
        tenant_id="acme",
        instrument_id=POLICY,
        version_id="v2",
        valid_from=JUN,
        supersedes="v1",
        document_class=DocumentClass.INTERNAL_POLICY,
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
    with pytest.raises(ValueError, match="already recorded"):
        await ledger.record_version(
            tenant_id="acme",
            instrument_id=POLICY,
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
            document_class=DocumentClass.INTERNAL_POLICY,
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
        )

    # ---- findings citing, by stable-id prefix
    await seed_finding("acme", f"{POLICY}:v1:3/1")
    await seed_finding("acme", f"{POLICY}:v2:1/1")
    await seed_finding("other-corp", f"{POLICY}:v1:3/1")
    assert await ledger.count_findings_citing("acme", f"{POLICY}:v1:") == 1
    assert await ledger.count_findings_citing("acme", f"{POLICY}:") == 2
    assert await ledger.count_findings_citing("acme", "iso-27001:") == 0

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
    # What a turn may hold is fixed when the answer is built (ADR-0003): a
    # withheld or failed answer can't carry the model's text, and a verified
    # one must carry its citations.
    for build, why in (
        (lambda: ChatAnswer(TurnOutcome.BLOCKED, MODEL_PROSE, (), (APP12,)), "holds no text"),
        (lambda: ChatAnswer(TurnOutcome.UNCITED, MODEL_PROSE), "holds no text"),
        (lambda: ChatAnswer(TurnOutcome.INTERNAL, MODEL_PROSE), "holds no text"),
        (lambda: ChatAnswer(TurnOutcome.UNCITED, None, VERIFIED.citations), "holds no text"),
        (lambda: ChatAnswer(TurnOutcome.BLOCKED), "names the citations"),
        (lambda: ChatAnswer(TurnOutcome.UNCITED, blocked=(APP12,)), "names the citations"),
        (lambda: ChatAnswer(TurnOutcome.BLOCKED, blocked=(MODEL_PROSE,)), "stable clause ID"),
        (lambda: ChatAnswer(TurnOutcome.VERIFIED, "text"), "text and citations"),
        (lambda: ChatAnswer(TurnOutcome.VERIFIED, " ", VERIFIED.citations), "text and citations"),
        (
            lambda: ChatAnswer(TurnOutcome.VERIFIED, "t", VERIFIED.citations, (APP12,)),
            "none blocked",
        ),
        (lambda: ChatCitation(APP11, "v9"), "not a clause of version v9"),
    ):
        with pytest.raises(ValueError, match=why):
            build()

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


async def test_the_fake_ledger_satisfies_the_chat_contract() -> None:
    await assert_chat_contract(FakeLedgerStore())
