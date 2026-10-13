"""The `LedgerStore` contract (issue #69): one definition, run against
`FakeLedgerStore` here and against `PostgresLedgerStore` on real Postgres
(`tests/test_postgres_ledger_store.py`), so the two can't silently diverge.

It uses only the interface: tenants are registered with `put_profile`,
library instruments are recorded before anyone subscribes to them (the
database requires it), and findings are seeded through a callable each backend
supplies — `LedgerStore` has no finding writer yet.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest

from compliance_rag.adapters.fakes import FakeGraphStore, FakeLedgerStore
from compliance_rag.adapters.interfaces import (
    LedgerStore,
    PolicyVersion,
    Profile,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
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

    # ---- coverage per assessment (#115, D-06)
    await ledger.record_assessment(
        tenant_id="acme",
        assessment_id="cov-1",
        corpus_version="c",
        mapping_version="m",
        as_of=JAN,
        trace_id="trace-cov",
    )
    coverage = await FakeGraphStore(
        load_mapping_set([APPLICABILITY]), clause_validity=APPLICABILITY_CLAUSES
    ).coverage(
        Facts.of([applicability_fact("annual_turnover_aud", 900, LAST_FY)]),
        tenant_id="acme",
        as_of=APPLICABILITY_AS_OF,
    )
    await ledger.record_coverage(
        tenant_id="acme", assessment_id="cov-1", coverage=coverage, trace_id="trace-cov"
    )
    rows = await ledger.coverage_rows("acme", "cov-1")
    assert rows == tuple(
        sorted((CoverageRow.of(o) for o in coverage.obligations), key=lambda r: r.obligation_id)
    )
    unassessable = [r for r in rows if r.outcome is Outcome.UNASSESSABLE]
    assert unassessable and all(r.questions for r in unassessable)
    with pytest.raises(ValueError, match="already recorded"):
        await ledger.record_coverage(
            tenant_id="acme", assessment_id="cov-1", coverage=coverage, trace_id="trace-cov"
        )
    with pytest.raises(ValueError, match="isn't recorded"):
        await ledger.record_coverage(
            tenant_id="acme", assessment_id="cov-404", coverage=coverage, trace_id="t"
        )
    assert await ledger.coverage_rows("other-corp", "cov-1") == ()

    # ---- assessments carry their trace_id
    await ledger.record_assessment(
        tenant_id="acme",
        assessment_id="a1",
        corpus_version="corpus-2026.10",
        mapping_version="mappings-2026.10",
        as_of=JUN,
        trace_id="f" * 32,
    )
    row = await ledger.get_assessment("acme", "a1")
    assert row is not None and row["trace_id"] == "f" * 32 and row["as_of"] == JUN
    assert await ledger.get_assessment("other-corp", "a1") is None

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
