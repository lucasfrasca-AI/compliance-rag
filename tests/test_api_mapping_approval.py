"""An approved mapping changes a finding (#306, ADR-0053 F5 and F6, ADR-0059 Q7 and C1): Phase 3
exit criterion 3.

The demonstration is the worked example's claim 4 (`docs/research/phase3-product-options.md`):
"Segment profiles are kept for 7 years after a customer's account closes". The model is scripted
to read it `ambiguous` against APP 11.2 (it turns on whether a law requires the retention) and
`consistent` against Acme's own Data Security and Retention Policy 3.1 (D2 3/1). The admin
approves "D2 3/1 implements OB-PA-APP11-2", and a check made *after* that, over the same stored
document, derives the finding: `consistent` against APP 11.2 through Acme's own approved policy,
cited to both. The check made before it is untouched, and says that an approved mapping would
change it.

Everything is real but the stores and the model, as in `tests/test_api_check_findings.py`: the
app, the sign-in, the lifecycle, Track A over the signed mappings, the golden corpus, the
real verifier, and a model that is a script. Nothing is billable and no live model is called.

The tenant is `acme`. `lead@acme.example` is an admin, `second@acme.example` the member who
submits, `third@acme.example` a member who is neither.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.interfaces import (
    CheckDecision,
    DecisionKind,
    FindingOutcome,
    FindingRecord,
    MappingKind,
    MappingProposal,
    MappingRunRecord,
    MappingStatus,
    PolicyMapping,
    Verdict,
)
from compliance_rag.api import check_claims, questionnaire
from compliance_rag.api.sign_in import STATIC_DIR
from compliance_rag.assessment.coverage import Outcome
from compliance_rag.assessment.decisions import Decisions, weakest
from compliance_rag.assessment.evidence import EvidenceProvenance
from compliance_rag.assessment.mapping_derivation import plan_derivations
from tests.api_support import assert_script_writes_no_html
from tests.findings_support import (
    APP11_2,
    CLAIM_4,
    DAY,
    PICKS,
    TENANT,
    CheckModel,
    Say,
    honest,
    worked_claims,
)
from tests.mapping_support import APP11_2 as OB_APP11_2
from tests.mapping_support import MODEL, SEC_3_1, SEC_3_2, SECURITY
from tests.test_api_check_findings import (
    OFFSHORE,
    World,
    checked,
    extract,
    find,
    submit,
    world,
)
from tests.test_api_check_report import browser, check_json, page, publish_sources
from tests.test_api_checks import (
    ADMIN_TOKEN,
    SUBMITTER_TOKEN,
    THIRD_TOKEN,
    _Page,  # pyright: ignore[reportPrivateUsage]
    scan,
)

OB_APP11_1 = "OB-PA-APP11-1"
OB_APP3_1 = "OB-PA-APP3-1"
"""An obligation that is the agency's alone: Track A says it does not apply to Acme."""
PICKS_WITH_D2 = {**PICKS, CLAIM_4: ((OB_APP11_2,), (SEC_3_1,))}
"""The worked example's picks, with claim 4 also checked against D2 3/1, the policy clause whose
retention rule the mapping says implements APP 11.2."""
SETTLE = "Whether a law requires the retention: APP 11.2(d)."
TRACE = "a" * 32


def model_reading(
    app: str = "ambiguous", policy: str = "consistent", picks: Mapping[str, Any] = PICKS_WITH_D2
) -> CheckModel:
    """A model that reads claim 4 `app` against APP 11.2 and `policy` against D2 3/1, and every
    other target as an honest one does by default."""
    return CheckModel(
        claims=worked_claims(picks),
        findings=honest(
            {
                CLAIM_4: {
                    OB_APP11_2: Say(app, settle_by=SETTLE if app == "ambiguous" else None),
                    SEC_3_1: Say(policy, settle_by=SETTLE if policy == "ambiguous" else None),
                }
            }
        ),
    )


@pytest.fixture(autouse=True)
def _a_fixed_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_claims, "today", lambda: DAY)
    monkeypatch.setattr(questionnaire, "today", lambda: DAY)


async def propose(w: World, *proposals: tuple[str, str, MappingKind]) -> tuple[PolicyMapping, ...]:
    """What #305's run records: each `proposed`, by the model, with its own request's trace."""
    await w.ledger.reserve_mapping_run(TENANT, *SECURITY, trace_id=TRACE)
    await w.ledger.record_mapping_proposals(
        MappingRunRecord(
            tenant_id=TENANT,
            instrument_id=SECURITY[0],
            version_id=SECURITY[1],
            trace_id=TRACE,
            proposer=MODEL,
            proposals=tuple(
                MappingProposal(clause, obligation, kind, "The model's one line.")
                for clause, obligation, kind in proposals
            ),
        )
    )
    return await w.ledger.policy_mappings(TENANT)


async def decide(
    w: World, mapping_id: int, decision: str = "approve", token: str = ADMIN_TOKEN, **body: Any
) -> httpx.Response:
    async with w.client(token) as client:
        return await client.post(
            f"/v1/policy-mappings/{mapping_id}/decision", json={"decision": decision, **body}
        )


async def recheck(w: World, check_id: str, token: str = SUBMITTER_TOKEN) -> httpx.Response:
    async with w.client(token) as client:
        return await client.post(f"/v1/checks/{check_id}/recheck")


async def checked_and_found(w: World, token: str = SUBMITTER_TOKEN) -> str:
    check_id = await checked(w, token)
    _, events = await find(w, check_id, token)
    assert events[-1][0] == "findings", events
    return check_id


async def rechecked(w: World, check_id: str) -> str:
    """The re-check of a check: a new check of the same document, then run as any check is."""
    response = await recheck(w, check_id)
    assert response.status_code == 201, response.text
    new = str(response.json()["check_id"])
    assert new != check_id
    await extract(w, new)
    _, events = await find(w, new)
    assert events[-1][0] == "findings", events
    return new


def against(report: dict[str, Any], claim: int, target: str) -> list[dict[str, Any]]:  # Any: JSON
    """The report's listed findings of one claim against one obligation or policy clause."""
    return [
        f
        for g in report["groups"]
        for f in g["findings"]
        if f["claim"]["index"] == claim
        and target in (f.get("obligation_id"), f.get("policy_clause_id"))
    ]


def having(parsed: _Page, tag: str, attribute: str) -> list[dict[str, str | None]]:
    """The elements of a tag that carry an attribute, a boolean one too (`data-derived`)."""
    return [a for t, a in parsed.elements if t == tag and attribute in a]


async def raw_findings(w: World, check_id: str) -> bytes:
    async with w.client(SUBMITTER_TOKEN) as client:
        response = await client.get(f"/v1/checks/{check_id}/findings")
    assert response.status_code == 200
    return response.content


# ---- the demonstration: exit criterion 3 -----------------------------------------------------


async def test_approving_d2_3_1_implements_app_11_2_turns_claim_4_ambiguous_to_consistent() -> None:
    """#306's demonstration. Claim 4 is `ambiguous` against APP 11.2 until the admin approves that
    D2 3/1 implements it; a check made after, of the same document, says `consistent`, derived
    from D2 3/1's finding and cited to both."""
    model = model_reading()
    w = await world(model)
    await publish_sources(w)
    before_id = await checked_and_found(w)
    before = (await check_json(w, before_id))["report"]
    (ambiguous,) = against(before, 4, OB_APP11_2)
    assert ambiguous["verdict"] == "ambiguous" and ambiguous["derived"] is None
    assert [g["verdict"] for g in before["groups"] if ambiguous in g["findings"]] == ["ambiguous"]
    assert before["would_change"] == []  # nothing is approved yet
    calls_before = len(model.calls)

    mappings = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    (mapping,) = mappings
    assert mapping.status is MappingStatus.PROPOSED
    # a proposal changes nothing: it is a suggestion
    assert await raw_findings(w, before_id)
    approved = await decide(w, mapping.mapping_id)
    assert approved.status_code == 200, approved.text
    assert approved.json()["mapping"]["status"] == "approved" and approved.json()["derives"]

    # ---- the check made before is as it was: the assessment never changes (ADR-0034) ...
    after_old = (await check_json(w, before_id))["report"]
    assert {k: v for k, v in after_old.items() if k != "would_change"} == {
        k: v for k, v in before.items() if k != "would_change"
    }
    # ... and says that a mapping approved since would change it, naming it and the claim
    (would,) = after_old["would_change"]
    assert would["mapping_id"] == mapping.mapping_id
    assert would["policy"]["cite"].endswith("3.1") and would["obligation_id"] == OB_APP11_2
    assert would["approved_by"] == "lead@acme.example"
    assert [c["index"] for c in would["claims"]] == [4]

    # ---- a check made after it, over the same stored document
    after_id = await rechecked(w, before_id)
    report = (await check_json(w, after_id))["report"]
    (derived,) = against(report, 4, OB_APP11_2)
    assert derived["verdict"] == "consistent" and derived["outcome"] == "verdict"
    assert [g["verdict"] for g in report["groups"] if derived in g["findings"]] == ["consistent"]
    assert derived["derived"] is not None and derived["derived"]["mapping"]["mapping_id"] == (
        mapping.mapping_id
    )
    # cited to both the policy clause and the obligation (the ticket's fifth criterion)
    assert [c["clause_id"] for c in derived["citations"]] == [SEC_3_1, APP11_2]
    assert report["would_change"] == []  # this report already has what the mapping adds
    # the model's own reading is never dropped: beneath the derived finding, and it disagrees
    assert derived["direct"]["verdict"] == "ambiguous"
    assert derived["direct_disagrees"] is True
    assert not [
        f
        for g in report["groups"]
        for f in g["findings"]
        if f["finding_id"] == derived["direct"]["finding_id"]
    ]  # listed once, beneath the derived one
    # its source is the claim's finding against D2 3/1, which the report lists as it does any
    (policy,) = against(report, 4, SEC_3_1)
    assert derived["derived"]["from_finding_id"] == policy["finding_id"]
    assert policy["verdict"] == "consistent" and policy["derived"] is None

    # ---- no model was asked to derive it: the second check made the same calls the first did
    assert len(model.calls) - calls_before == calls_before

    # ---- and the old check is still what it was, after the new one exists
    assert (await check_json(w, before_id))["report"]["groups"] == before["groups"]


async def test_the_derived_finding_is_what_the_page_shows_with_the_models_reading_beneath() -> None:
    w = await world(model_reading())
    await publish_sources(w)
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    assert (await decide(w, mapping.mapping_id)).status_code == 200

    # the old report's page: the banner, the button, and the findings as they were
    old = await page(w, first)
    parsed = scan(old)
    (banner,) = having(parsed, "section", "data-would-change")
    assert banner["aria-labelledby"] == "h-would-change"
    (button,) = [b for b in parsed.where("button") if b.get("data-recheck")]
    assert button["data-recheck"] == first
    assert "A mapping approved since would change this" in old
    assert "/static/check_recheck.js" in [a["src"] for a in parsed.where("script")]
    assert not having(parsed, "article", "data-derived")  # nothing derived in it

    new = await rechecked(w, first)
    html = await page(w, new)
    parsed = scan(html)
    (card,) = having(parsed, "article", "data-derived")
    assert card["data-verdict"] == "consistent"
    assert "Derived from your policy" in html and "not read by the model" in html
    (direct,) = parsed.where("div", **{"class": "direct-reading"})
    assert direct["data-disagrees"] == "true"
    assert "direct reading disagrees" in html
    assert not having(parsed, "section", "data-would-change")
    # the coverage statement names the mapping it applied
    assert "Your approved policy mappings applied" in html
    # no accept button on the derived finding (its verdict is the policy finding's), one on the
    # model's own reading
    block = html.split(f'data-finding="{card.get("data-finding")}"', 1)[1].split("</article>")[0]
    assert 'data-decide="verdict_accepted"' in block  # the nested direct reading's
    assert block.count('data-decide="verdict_accepted"') == 1


async def test_a_derived_finding_agrees_or_disagrees_with_the_models_own_and_both_are_shown() -> (
    None
):
    w = await world(model_reading(app="consistent"))
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    await decide(w, mapping.mapping_id)
    report = (await check_json(w, await rechecked(w, first)))["report"]
    (derived,) = against(report, 4, OB_APP11_2)
    assert derived["verdict"] == "consistent" and derived["direct"]["verdict"] == "consistent"
    assert derived["direct_disagrees"] is False
    html = await page(w, first)
    assert "direct reading" not in html  # the first report has no derived finding


async def test_the_derived_conflicts_verdict_carries_over_as_well() -> None:
    """The finding carries over, whatever its verdict: a claim that conflicts with the policy
    clause conflicts with the obligation it implements."""
    w = await world(model_reading(app="not_addressed", policy="conflicts"))
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    await decide(w, mapping.mapping_id)
    report = (await check_json(w, await rechecked(w, first)))["report"]
    (derived,) = against(report, 4, OB_APP11_2)
    assert derived["verdict"] == "conflicts" and derived["direct_disagrees"] is True


async def test_a_derived_ambiguous_finding_carries_the_source_findings_settle_by() -> None:
    w = await world(model_reading(app="not_addressed", policy="ambiguous"))
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    await decide(w, mapping.mapping_id)
    report = (await check_json(w, await rechecked(w, first)))["report"]
    (derived,) = against(report, 4, OB_APP11_2)
    assert derived["verdict"] == "ambiguous"
    assert derived["settle_by"] == {"text": SETTLE, "source": "model"}  # labelled as the model's


# ---- what does not change a finding --------------------------------------------------------------


async def test_a_partially_implements_mapping_changes_nothing() -> None:
    """Approved and recorded, it derives nothing: the old report does not say a re-check would
    change it, and a new check holds no derived finding."""
    w = await world(model_reading())
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.PARTIALLY_IMPLEMENTS))
    response = await decide(w, mapping.mapping_id)
    assert response.status_code == 200
    assert response.json()["mapping"]["status"] == "approved" and not response.json()["derives"]
    assert (await check_json(w, first))["report"]["would_change"] == []
    assert "A mapping approved since would change this" not in await page(w, first)
    second = await rechecked(w, first)
    report = (await check_json(w, second))["report"]
    (only,) = against(report, 4, OB_APP11_2)
    assert only["derived"] is None and only["verdict"] == "ambiguous" and only["direct"] is None
    assert not [f for g in report["groups"] for f in g["findings"] if f["derived"]]
    recorded = await w.ledger.get_findings(TENANT, second)
    assert recorded is not None and not any(f.derived for f in recorded.findings)


@pytest.mark.parametrize("decision", ["reject", None])
async def test_a_rejected_or_still_proposed_mapping_derives_nothing(decision: str | None) -> None:
    w = await world(model_reading())
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    if decision is not None:
        assert (await decide(w, mapping.mapping_id, decision)).status_code == 200
    assert (await check_json(w, first))["report"]["would_change"] == []
    report = (await check_json(w, await rechecked(w, first)))["report"]
    (only,) = against(report, 4, OB_APP11_2)
    assert only["derived"] is None and only["verdict"] == "ambiguous"


async def test_an_approved_mapping_for_an_obligation_that_does_not_apply_derives_nothing() -> None:
    """APP 3.1 is the agency's alone: Track A says it does not apply to Acme, and a finding is
    never against an obligation that does not (`check_findings_write`)."""
    w = await world(model_reading())
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP3_1, MappingKind.IMPLEMENTS))
    await decide(w, mapping.mapping_id)
    assert (await check_json(w, first))["report"]["would_change"] == []
    second = await rechecked(w, first)
    recorded = await w.ledger.get_findings(TENANT, second)
    assert recorded is not None
    assert not [f for f in recorded.findings if f.obligation_id == OB_APP3_1 or f.derived]


async def test_a_mapping_of_a_clause_the_claim_was_not_checked_against_derives_nothing() -> None:
    w = await world(model_reading())
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_2, OB_APP11_1, MappingKind.IMPLEMENTS))
    await decide(w, mapping.mapping_id)
    assert (await check_json(w, first))["report"]["would_change"] == []
    recorded = await w.ledger.get_findings(TENANT, await rechecked(w, first))
    assert recorded is not None and not any(f.derived for f in recorded.findings)


async def test_an_old_assessments_findings_are_byte_unchanged_after_an_approval() -> None:
    """ADR-0034 and ADR-0059 Q7: nothing about an issued assessment moves. The recorded findings
    are the same bytes over the API and the same rows in the ledger, and the report differs
    only by the notice that a mapping approved since would change it."""
    w = await world(model_reading())
    check_id = await checked_and_found(w)
    bytes_before = await raw_findings(w, check_id)
    record_before = await w.ledger.get_findings(TENANT, check_id)
    claims_before = await w.ledger.get_claims(TENANT, check_id)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    await decide(w, mapping.mapping_id)
    await rechecked(w, check_id)  # and a new check beside it does not move it either
    assert await raw_findings(w, check_id) == bytes_before
    assert await w.ledger.get_findings(TENANT, check_id) == record_before
    assert await w.ledger.get_claims(TENANT, check_id) == claims_before
    assert await w.ledger.check_decisions(TENANT, check_id) == ()


# ---- approver and trace_id, on approval and on rejection ----------------------------------------


async def test_the_approver_and_the_trace_id_are_recorded_on_approval_and_on_rejection(
    span_exporter: InMemorySpanExporter,
) -> None:
    w = await world(model_reading())
    one, two = await propose(
        w,
        (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS),
        (SEC_3_2, OB_APP11_1, MappingKind.IMPLEMENTS),
    )
    approved = await decide(w, one.mapping_id)
    rejected = await decide(w, two.mapping_id, "reject", reason="Section 3.2 is about backups.")
    assert approved.status_code == rejected.status_code == 200
    spans = [s for s in span_exporter.get_finished_spans() if s.name == "api.mapping.decide"]
    traces = [format(s.context.trace_id, "032x") for s in spans if s.context is not None]
    assert len(traces) == 2 and len(set(traces)) == 2
    ledger_rows = await w.ledger.policy_mappings(TENANT)
    for row, request_trace, status in zip(
        ledger_rows, traces, (MappingStatus.APPROVED, MappingStatus.REJECTED), strict=True
    ):
        assert row.status is status
        assert row.approver == "uid-123" and row.approver_email == "lead@acme.example"
        assert row.decision_trace_id == request_trace  # this request's, not the proposer's
        assert row.trace_id == TRACE and row.decided_at is not None
    assert ledger_rows[1].rejection_reason == "Section 3.2 is about backups."
    assert ledger_rows[0].rejection_reason is None
    # and what the API says of them
    shown = [approved.json()["mapping"], rejected.json()["mapping"]]
    assert [m["approver"] for m in shown] == ["lead@acme.example"] * 2
    assert [m["decision_trace_id"] for m in shown] == traces
    assert shown[1]["rejection_reason"] == {
        "text": "Section 3.2 is about backups.",
        "source": "admin",
    }  # the admin's words, labelled as theirs
    async with w.client(ADMIN_TOKEN) as client:
        listed = (await client.get("/v1/policy-mappings")).json()
    assert listed["counts"] == {"proposed": 0, "approved": 1, "rejected": 1}
    assert [m["decision_trace_id"] for m in listed["mappings"]] == traces


# ---- who may decide, and what may be asked ------------------------------------------------------


async def test_only_an_admin_decides_and_a_member_is_refused_before_anything_is_read() -> None:
    w = await world(model_reading())
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_1, MappingKind.IMPLEMENTS))
    for token in (SUBMITTER_TOKEN, THIRD_TOKEN):
        assert (await decide(w, mapping.mapping_id, token=token)).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=w.app), base_url="http://test"
    ) as anonymous:
        response = await anonymous.post(
            f"/v1/policy-mappings/{mapping.mapping_id}/decision", json={"decision": "approve"}
        )
    assert response.status_code in {401, 403}
    (still,) = await w.ledger.policy_mappings(TENANT)
    assert still.status is MappingStatus.PROPOSED and still.approver is None


async def test_a_decision_is_final_and_an_unknown_mapping_is_not_found() -> None:
    w = await world(model_reading())
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_1, MappingKind.IMPLEMENTS))
    assert (await decide(w, mapping.mapping_id, "reject")).status_code == 200
    held = (await w.ledger.policy_mappings(TENANT))[0]
    for decision in ("approve", "reject"):
        response = await decide(w, mapping.mapping_id, decision)
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "already_decided"
        assert response.json()["detail"]["status"] == "rejected"
    assert (await w.ledger.policy_mappings(TENANT))[0] == held  # not a column of it moved
    assert (await decide(w, 9999)).status_code == 404


async def test_two_admins_deciding_at_once_leave_one_decision() -> None:
    w = await world(model_reading())
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_1, MappingKind.IMPLEMENTS))
    first, second = await asyncio.gather(
        decide(w, mapping.mapping_id, "approve"), decide(w, mapping.mapping_id, "reject")
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"decision": "maybe"},
        {"decision": "approve", "reason": "Fine."},  # a reason is a rejection's
        {"decision": "reject", "reason": "two\nlines"},
        {"decision": "reject", "reason": "x" * 301},
        {"decision": "reject", "reason": "   "},
        {"decision": "approve", "status": "approved"},  # no other key
        {"decision": "approve", "approver": "someone-else"},  # who decides is the sign-in's
    ],
)
async def test_a_decision_is_one_choice_and_an_optional_reason_on_a_rejection(
    body: dict[str, Any],
) -> None:
    w = await world(model_reading())
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_1, MappingKind.IMPLEMENTS))
    async with w.client(ADMIN_TOKEN) as client:
        response = await client.post(
            f"/v1/policy-mappings/{mapping.mapping_id}/decision", json=body
        )
    assert response.status_code == 422
    assert (await w.ledger.policy_mappings(TENANT))[0].status is MappingStatus.PROPOSED


async def test_a_mapping_id_that_is_not_a_number_is_refused() -> None:
    w = await world(model_reading())
    async with w.client(ADMIN_TOKEN) as client:
        for bad in ("abc", "0", "-1", "1.5"):
            response = await client.post(
                f"/v1/policy-mappings/{bad}/decision", json={"decision": "approve"}
            )
            assert response.status_code == 422, bad


# ---- C1: the tier is the weakest of the claim, the policy verdict and the mapping ----------------


def test_an_approval_never_raises_a_finding_above_its_claims_tier() -> None:
    """ADR-0059 C1, as a rule over every combination: a derived finding's tier is the weakest of
    its claim's, its source verdict's and the mapping's, and the mapping, `asserted` by the
    approval, is the strongest a tier can be, so the finding is never above its claim."""

    def row(kind: DecisionKind, **at: Any) -> CheckDecision:
        return CheckDecision("acme", "c", kind, "u", "u@x", "a" * 32, **at)

    source = FindingRecord(
        "src", 4, FindingOutcome.VERDICT, policy_clause_id=SEC_3_1, verdict=Verdict.CONSISTENT
    )
    derived = FindingRecord(
        "drv",
        4,
        FindingOutcome.VERDICT,
        obligation_id=OB_APP11_2,
        verdict=Verdict.CONSISTENT,
        derived_from="src",
        mapping_id=1,
    )
    for confirmed in (False, True):
        for accepted in (False, True):
            for accepted_derived in (False, True):  # a row about the derived finding itself
                rows: list[CheckDecision] = []
                if confirmed:
                    rows.append(row(DecisionKind.CLAIM_CONFIRMED, claim_index=4))
                if accepted:
                    rows.append(row(DecisionKind.VERDICT_ACCEPTED, finding_id="src"))
                if accepted_derived:
                    rows.append(row(DecisionKind.VERDICT_ACCEPTED, finding_id="drv"))
                decisions = Decisions.of(rows)
                tier = decisions.finding_tier(derived)
                claim = decisions.claim_tier(4)
                # never above its claim
                assert not (
                    tier is EvidenceProvenance.ASSERTED and claim is EvidenceProvenance.INFERRED
                )
                # asserted exactly when the claim is confirmed AND the policy verdict accepted;
                # a stray acceptance of the derived finding itself counts for nothing
                assert (tier is EvidenceProvenance.ASSERTED) == (confirmed and accepted)
                # the approval raises nothing: it is no stronger than the finding it came from
                assert tier is decisions.finding_tier(source)
    assert weakest(EvidenceProvenance.ASSERTED) is EvidenceProvenance.ASSERTED
    assert weakest(EvidenceProvenance.ASSERTED, EvidenceProvenance.INFERRED) is (
        EvidenceProvenance.INFERRED
    )


async def test_a_derived_finding_is_inferred_until_claim_and_policy_verdict_are_decided() -> None:
    w = await world(model_reading())
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    await decide(w, mapping.mapping_id)
    new = await rechecked(w, first)

    async def derived() -> dict[str, Any]:  # Any: JSON
        report = (await check_json(w, new))["report"]
        (found,) = against(report, 4, OB_APP11_2)
        return found

    async def decide_on(**body: Any) -> httpx.Response:  # Any: the decision's fields
        async with w.client(SUBMITTER_TOKEN) as client:
            return await client.post(f"/v1/checks/{new}/decisions", json=body)

    found = await derived()
    assert found["tier"] == "inferred"  # the mapping is approved, and that raises nothing
    policy_id = found["derived"]["from_finding_id"]
    # the policy verdict accepted, the claim not confirmed: still inferred (never above its claim)
    assert (await decide_on(kind="verdict_accepted", finding_id=policy_id)).status_code == 201
    assert (await derived())["tier"] == "inferred"
    # the claim confirmed too: asserted, with all three inputs
    assert (await decide_on(kind="claim_confirmed", claim_index=4)).status_code == 201
    found = await derived()
    assert found["tier"] == "asserted" and found["can_accept"] is False
    assert found["derived"]["source_accepted"]["by"] == "second@acme.example"
    # take the policy verdict back: inferred again
    await decide_on(kind="verdict_withdrawn", finding_id=policy_id)
    assert (await derived())["tier"] == "inferred"
    # a derived finding's verdict is the policy finding's, so it is not accepted itself
    for kind in ("verdict_accepted", "verdict_withdrawn"):
        response = await decide_on(kind=kind, finding_id=found["finding_id"])
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "derived"
    assert [d.kind.value for d in await w.ledger.check_decisions(TENANT, new)] == [
        "verdict_accepted",
        "claim_confirmed",
        "verdict_withdrawn",
    ]


# ---- the re-check -------------------------------------------------------------------------------


async def test_a_recheck_is_a_new_check_of_the_same_stored_document_and_the_old_one_stays() -> None:
    model = model_reading()
    w = await world(model)
    first = await checked_and_found(w)
    old = await check_json(w, first)
    calls = len(model.calls)
    response = await recheck(w, first)
    assert response.status_code == 201, response.text
    made = response.json()
    assert made["rechecks"] == first and made["check_id"] != first
    assert made["href"] == f"/checks/{made['check_id']}"
    assert len(model.calls) == calls  # recording the check asks the model nothing
    new = await check_json(w, made["check_id"])
    # the same words, passage by passage, as a check of their own
    assert [p["text"] for p in new["passages"]] == [p["text"] for p in old["passages"]]
    assert new["title"] == old["title"] and new["filename"] == old["filename"]
    assert new["state"] == "new" and new["report"] is None
    assert new["source_href"] != old["source_href"]  # its own stored version
    before, after = (
        await w.ledger.get_check(TENANT, first),
        await w.ledger.get_check(TENANT, made["check_id"]),
    )
    assert before is not None and after is not None
    assert after.user_id == before.user_id  # the same people see it (ADR-0059 Q5)
    assert after.instrument_id != before.instrument_id and after.version_id == "v1"
    # the first check is untouched
    assert await check_json(w, first) == old
    # and a check can be checked again, as often as it is wanted: each is its own check
    again = await recheck(w, first)
    assert again.status_code == 201 and again.json()["check_id"] not in {first, made["check_id"]}


async def test_a_recheck_is_visible_to_the_submitter_and_the_admins_and_to_no_one_else() -> None:
    w = await world(model_reading())
    first = await checked_and_found(w)
    # an admin may re-check it (they may see it), and the new check is still the submitter's
    response = await recheck(w, first, ADMIN_TOKEN)
    assert response.status_code == 201
    new = response.json()["check_id"]
    for token, status in ((SUBMITTER_TOKEN, 200), (ADMIN_TOKEN, 200), (THIRD_TOKEN, 404)):
        async with w.client(token) as client:
            assert (await client.get(f"/v1/checks/{new}")).status_code == status
    assert (await check_json(w, new, SUBMITTER_TOKEN))["mine"] is True
    # someone who may not see the check cannot re-check it, and is told what they are told for a
    # check that does not exist
    assert (await recheck(w, first, THIRD_TOKEN)).status_code == 404
    assert (await recheck(w, "0" * 32)).status_code == 404
    assert (await recheck(w, "not-a-check")).status_code == 404


async def test_a_check_with_no_report_has_nothing_to_check_again() -> None:
    w = await world(model_reading())
    submitted = await submit(w)
    response = await recheck(w, submitted)
    assert response.status_code == 409 and response.json()["detail"]["error"] == "not_reported"
    extracted = await submit(w)
    await extract(w, extracted)
    assert (await recheck(w, extracted)).status_code == 409


async def test_offshore_only_a_registered_tester_may_check_a_document_again() -> None:
    w = await world(model_reading(), **OFFSHORE)
    first = await checked_and_found(w)
    refused = await recheck(w, first, ADMIN_TOKEN)  # lead@ is not the registered tester
    assert refused.status_code == 403 and refused.json()["detail"]["error"] == "residency"
    assert (await recheck(w, first, SUBMITTER_TOKEN)).status_code == 201


async def test_a_rechecked_report_is_made_after_the_mapping_and_reads_the_current_one() -> None:
    """The mapping is read when the findings are made: approved before the run, it derives;
    approved after, the report says a re-check would."""
    w = await world(model_reading())
    await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    (mapping,) = await w.ledger.policy_mappings(TENANT)
    await decide(w, mapping.mapping_id)
    made_after = await checked_and_found(w)  # a check made from scratch after the approval
    report = (await check_json(w, made_after))["report"]
    (derived,) = against(report, 4, OB_APP11_2)
    assert derived["derived"] is not None and report["would_change"] == []


async def test_a_derivation_the_obligations_clause_cannot_support_is_skipped_and_said() -> None:
    """If APP 11.2 can't be delivered (the model's own finding against it fails, as it does for
    any claim), nothing is derived for it: a derived finding is never given a citation nobody
    verified."""
    w = await world(model_reading(), without=frozenset({APP11_2}))
    first = await checked_and_found(w)
    (mapping,) = await propose(w, (SEC_3_1, OB_APP11_2, MappingKind.IMPLEMENTS))
    await decide(w, mapping.mapping_id)
    response = await recheck(w, first)
    new = response.json()["check_id"]
    await extract(w, new)
    _, events = await find(w, new)
    shown = events[-1][1]
    assert shown["derivation_skipped"] == [f"4:{OB_APP11_2}:{mapping.mapping_id}"]
    recorded = await w.ledger.get_findings(TENANT, new)
    assert recorded is not None and not any(f.derived for f in recorded.findings)


# ---- the planner, as a rule ---------------------------------------------------------------------


def a_mapping(mapping_id: int, clause: str, obligation: str, **changes: Any) -> PolicyMapping:
    fields: dict[str, Any] = {  # Any: record fields
        "mapping_id": mapping_id,
        "tenant_id": TENANT,
        "instrument_id": SECURITY[0],
        "version_id": SECURITY[1],
        "policy_clause_id": clause,
        "obligation_id": obligation,
        "kind": MappingKind.IMPLEMENTS,
        "status": MappingStatus.APPROVED,
        "rationale": "r",
        "proposer": MODEL,
        "approver": "uid",
        "trace_id": TRACE,
        **changes,
    }
    return PolicyMapping(**fields)


def a_policy_finding(finding_id: str, claim: int, clause: str, **changes: Any) -> FindingRecord:
    fields: dict[str, Any] = {  # Any: record fields
        "finding_id": finding_id,
        "claim_index": claim,
        "outcome": FindingOutcome.VERDICT,
        "policy_clause_id": clause,
        "verdict": Verdict.CONSISTENT,
        **changes,
    }
    return FindingRecord(**fields)


def test_the_plan_is_every_approved_implements_mapping_of_a_findings_policy_clause() -> None:
    sources = [
        a_policy_finding("a", 1, SEC_3_1),
        a_policy_finding("b", 2, SEC_3_1, verdict=Verdict.CONFLICTS),
        a_policy_finding("c", 2, SEC_3_2),
        # no verdict to carry over: withheld, failed
        a_policy_finding("d", 3, SEC_3_1, outcome=FindingOutcome.WITHHELD, verdict=None),
        a_policy_finding("e", 4, SEC_3_1, outcome=FindingOutcome.FAILED, verdict=None),
    ]
    approved = a_mapping(1, SEC_3_1, OB_APP11_2)
    plan = plan_derivations(
        sources,
        [
            approved,
            a_mapping(2, SEC_3_1, OB_APP11_1, kind=MappingKind.PARTIALLY_IMPLEMENTS),
            a_mapping(3, SEC_3_1, "OB-PA-APP11-3", status=MappingStatus.PROPOSED),
            a_mapping(4, SEC_3_1, "OB-PA-APP11-4", status=MappingStatus.REJECTED),
            a_mapping(5, SEC_3_2, OB_APP3_1),  # an obligation that does not apply
        ],
        applicable={OB_APP11_1, OB_APP11_2, "OB-PA-APP11-3", "OB-PA-APP11-4"},
    )
    assert [(d.claim_index, d.source.finding_id, d.mapping.mapping_id) for d in plan] == [
        (1, "a", 1),
        (2, "b", 1),
    ]
    # it carries the source's verdict over, whatever it is
    assert [d.source.verdict for d in plan] == [Verdict.CONSISTENT, Verdict.CONFLICTS]


def test_a_derivation_already_made_is_not_planned_again() -> None:
    source = a_policy_finding("a", 1, SEC_3_1)
    approved = a_mapping(1, SEC_3_1, OB_APP11_2)
    already = FindingRecord(
        "x",
        1,
        FindingOutcome.VERDICT,
        obligation_id=OB_APP11_2,
        verdict=Verdict.CONSISTENT,
        derived_from="a",
        mapping_id=1,
    )
    assert plan_derivations([source], [approved], {OB_APP11_2})
    assert not plan_derivations([source, already], [approved], {OB_APP11_2})
    # a derived finding is never itself a source: the plan does not chain
    chained = a_mapping(2, SEC_3_1, OB_APP11_1)
    (only,) = plan_derivations([source, already], [approved, chained], {OB_APP11_1, OB_APP11_2})
    assert only.mapping.mapping_id == 2


def test_an_obligation_track_a_cannot_decide_gets_no_derived_finding() -> None:
    source = a_policy_finding("a", 1, SEC_3_1)
    assert not plan_derivations([source], [a_mapping(1, SEC_3_1, OB_APP11_2)], applicable=set())
    assert (
        Outcome.UNASSESSABLE.value == "unassessable"
    )  # only APPLICABLE is in the set a run passes


# ---- the page's script --------------------------------------------------------------------------


def test_the_recheck_script_sends_the_one_request_and_writes_no_html() -> None:
    script = (STATIC_DIR / "check_recheck.js").read_text()
    assert_script_writes_no_html(script)
    assert "/recheck`" in script and 'method: "POST"' in script
    assert "textContent" in script and 'credentials: "same-origin"' in script
    assert "#run-now" in script and "data-recheck" in script


def test_the_run_script_starts_a_recheck_once_and_cleans_the_address() -> None:
    script = (STATIC_DIR / "check_run.js").read_text()
    assert_script_writes_no_html(script)
    assert '"#run-now"' in script and "replaceState" in script


async def test_the_recheck_script_is_served_beside_the_page() -> None:
    w = await world(model_reading())
    async with browser(w) as client:
        response = await client.get("/static/check_recheck.js")
    assert response.status_code == 200
    assert response.text == (STATIC_DIR / "check_recheck.js").read_text()
    assert json.dumps(response.text)  # text, not markup
    assert (
        re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", response.text) is None
    )
    assert page  # the helper the report tests share
