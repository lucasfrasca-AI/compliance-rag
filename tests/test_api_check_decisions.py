"""Claim confirmation, claim rejection and verdict acceptance (#301, ADR-0053 F5, ADR-0059
Q3 and C1): `POST /v1/checks/{id}/decisions` and what the report does with the rows.

The same world as the report's tests (`tests/test_api_check_report.py`): the real app, the
sign-in, the lifecycle, Track A over the signed mappings, the golden corpus and a scripted
model. Nothing is billable. The worked example's claims (`tests/findings_support.py`):
- claim 1: personalised offers (D1 3/2 conflicts, D5 1/1 consistent: the policies disagree);
- claim 2: the export to CloudAnalytics Inc (APP 8.1 not addressed, D4 2/1 ambiguous);
- claim 3: the Approved Vendor Register (APP 8.1 conflicts, D4 2/2 consistent);
- claim 4: segment profiles kept 7 years (APP 11.2 not addressed).
The check is the second member's (`uid-second`); the admin is `uid-123`.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.interfaces import (
    CheckDecision,
    DecisionKind,
    FindingOutcome,
    FindingRecord,
    Verdict,
)
from compliance_rag.assessment.decisions import Decisions
from compliance_rag.assessment.evidence import EvidenceProvenance
from tests.api_support import assert_script_writes_no_html
from tests.findings_support import (
    APP8_1,
    CLAIM_1,
    CLAIM_2,
    CLAIM_3,
    CLAIM_4,
    D1_3_2,
    D4_2_1,
    D4_2_2,
    D5_1_1,
    TENANT,
    CheckModel,
    Say,
    honest,
    opening,
    read_findings_prompt,
    worked_claims,
)
from tests.test_api_check_findings import (
    SECRET,
    UNKNOWN_FY2023,
    World,
    checked,
    find,
    submit,
    world,
)
from tests.test_api_check_report import (
    MIXED,
    browser,
    check_json,
    page,
    reported,
    where,
)
from tests.test_api_checks import (
    ADMIN_TOKEN,
    ORIGIN,
    SECOND,
    SUBMITTER_TOKEN,
    THIRD_TOKEN,
    scan,
)

CONFIRM, REJECT = DecisionKind.CLAIM_CONFIRMED.value, DecisionKind.CLAIM_REJECTED.value
ACCEPT, WITHDRAW = DecisionKind.VERDICT_ACCEPTED.value, DecisionKind.VERDICT_WITHDRAWN.value
ADMIN_EMAIL = "lead@acme.example"
STATIC_DIR = pathlib.Path(__file__).parents[1] / "src" / "compliance_rag" / "api" / "static"


async def decide(
    w: World,
    check_id: str,
    body: dict[str, Any],  # Any: JSON
    token: str = SUBMITTER_TOKEN,
) -> httpx.Response:
    async with w.client(token) as client:
        return await client.post(f"/v1/checks/{check_id}/decisions", json=body)


async def claim(w: World, check_id: str, kind: str, index: int, **who: str) -> httpx.Response:
    response = await decide(w, check_id, {"kind": kind, "claim_index": index}, **who)
    assert response.status_code in (200, 201), response.text
    return response


async def verdict(w: World, check_id: str, kind: str, finding_id: str) -> httpx.Response:
    response = await decide(w, check_id, {"kind": kind, "finding_id": finding_id})
    assert response.status_code in (200, 201), response.text
    return response


def finding_in(
    report: dict[str, Any], claim_index: int, target: str
) -> dict[str, Any]:  # Any: JSON
    (found,) = [
        f
        for key in ("conditional", "withheld", "failed")
        for f in report[key]
        if where(f, claim_index, target)
    ] or [f for g in report["groups"] for f in g["findings"] if where(f, claim_index, target)]
    return found


async def report_of(
    w: World, check_id: str, token: str = SUBMITTER_TOKEN
) -> dict[str, Any]:  # Any: JSON
    return (await check_json(w, check_id, token))["report"]


def every_finding(report: dict[str, Any]) -> list[dict[str, Any]]:  # Any: JSON
    return [
        *(f for g in report["groups"] for f in g["findings"]),
        *report["conditional"],
        *report["withheld"],
        *report["failed"],
    ]


# ---- confirming a claim is not evidence, and changes no coverage ------------------------------


async def test_confirming_a_claim_writes_no_evidence_row_and_does_not_change_coverage() -> None:
    w, check_id = await reported()
    evidence = await w.ledger.evidence(TENANT)
    stored = await w.ledger.get_findings(TENANT, check_id)
    assert stored is not None
    before = await report_of(w, check_id)

    for index in (1, 2, 3, 4):
        assert (await claim(w, check_id, CONFIRM, index)).status_code == 201

    assert await w.ledger.evidence(TENANT) == evidence  # nothing recorded as a fact
    after_stored = await w.ledger.get_findings(TENANT, check_id)
    assert after_stored == stored  # the assessment and its coverage, as recorded
    after = await report_of(w, check_id)
    assert after["coverage_statement"] == before["coverage_statement"]  # coverage, as shown
    assert after["assessment_id"] == before["assessment_id"]
    # and it settles no condition: the questionnaire's facts are the same list
    assert [e.evidence_id for e in await w.ledger.evidence(TENANT)] == [
        e.evidence_id for e in evidence
    ]


# ---- a finding's tier is the weakest of its claim's and its verdict's -------------------------


async def test_a_finding_stays_inferred_after_claim_confirmation_alone() -> None:
    w, check_id = await reported()
    assert {f["provenance"] for f in every_finding(await report_of(w, check_id))} == {"inferred"}

    await claim(w, check_id, CONFIRM, 1)
    report = await report_of(w, check_id)
    claim_1 = [f for f in every_finding(report) if f["claim"]["index"] == 1]
    assert claim_1 and {f["provenance"] for f in claim_1} == {"inferred"}
    assert {f["claim"]["provenance"] for f in claim_1} == {"asserted"}  # the claim is lifted
    assert {f["provenance"] for f in every_finding(report)} == {"inferred"}  # no finding is


async def test_a_finding_becomes_asserted_only_once_its_verdict_is_also_accepted() -> None:
    w, check_id = await reported()
    first = finding_in(await report_of(w, check_id), 1, D1_3_2)
    other = finding_in(await report_of(w, check_id), 1, D5_1_1)

    # the verdict alone, on a claim not confirmed: still inferred
    await verdict(w, check_id, ACCEPT, first["finding_id"])
    shown = await report_of(w, check_id)
    assert finding_in(shown, 1, D1_3_2)["provenance"] == "inferred"
    assert finding_in(shown, 1, D1_3_2)["accepted"]["by"] == SECOND

    # both: asserted, and only that finding (the claim's other finding's verdict is not accepted)
    await claim(w, check_id, CONFIRM, 1)
    shown = await report_of(w, check_id)
    assert finding_in(shown, 1, D1_3_2)["provenance"] == "asserted"
    assert finding_in(shown, 1, D5_1_1)["provenance"] == "inferred"
    assert other["finding_id"] != first["finding_id"]
    assert {f["provenance"] for f in every_finding(shown) if f["claim"]["index"] != 1} == {
        "inferred"
    }

    # an acceptance withdrawn is inferred again, and so is a confirmation rejected
    await verdict(w, check_id, WITHDRAW, first["finding_id"])
    assert finding_in(await report_of(w, check_id), 1, D1_3_2)["provenance"] == "inferred"
    await verdict(w, check_id, ACCEPT, first["finding_id"])
    assert finding_in(await report_of(w, check_id), 1, D1_3_2)["provenance"] == "asserted"
    await claim(w, check_id, REJECT, 1)
    await claim(w, check_id, CONFIRM, 1)  # latest wins: confirmed again
    assert finding_in(await report_of(w, check_id), 1, D1_3_2)["provenance"] == "asserted"


async def test_the_page_shows_each_findings_tier_and_the_stored_finding_stays_inferred() -> None:
    w, check_id = await reported()
    first = finding_in(await report_of(w, check_id), 1, D1_3_2)
    await claim(w, check_id, CONFIRM, 1)
    await verdict(w, check_id, ACCEPT, first["finding_id"])
    parsed = scan(await page(w, check_id))
    tiers = [a["data-tier"] for a in parsed.where("p", **{"class": "tier"})]
    assert tiers.count("asserted") == 1 and set(tiers) == {"asserted", "inferred"}
    # what the run recorded is what it was: the tier is computed when read
    stored = await w.ledger.get_findings(TENANT, check_id)
    assert stored is not None
    assert {f.provenance for f in stored.findings} == {EvidenceProvenance.INFERRED}
    # and so is the findings route, which shows the run as recorded
    async with w.client(SUBMITTER_TOKEN) as client:
        recorded = (await client.get(f"/v1/checks/{check_id}/findings")).json()
    assert {f["provenance"] for f in recorded["findings"]} == {"inferred"}


# ---- claim rows are never updated -------------------------------------------------------------


async def test_claim_and_finding_rows_are_never_updated() -> None:
    w, check_id = await reported()
    claims = await w.ledger.get_claims(TENANT, check_id)
    findings = await w.ledger.get_findings(TENANT, check_id)
    assert claims is not None and findings is not None
    first = finding_in(await report_of(w, check_id), 1, D1_3_2)

    await claim(w, check_id, CONFIRM, 1)
    await claim(w, check_id, REJECT, 2)
    await claim(w, check_id, CONFIRM, 2)
    await claim(w, check_id, REJECT, 3)
    await verdict(w, check_id, ACCEPT, first["finding_id"])
    await verdict(w, check_id, WITHDRAW, first["finding_id"])

    assert await w.ledger.get_claims(TENANT, check_id) == claims
    assert {c.provenance for c in claims.claims} == {EvidenceProvenance.INFERRED}
    assert await w.ledger.get_findings(TENANT, check_id) == findings
    # six decisions, six rows, each beside the claim or finding and none in place of one
    assert len(await w.ledger.check_decisions(TENANT, check_id)) == 6
    # the claims the document's extraction JSON shows are the recorded ones, `inferred`
    extracted = (await check_json(w, check_id))["claims"]["claims"]
    assert {c["provenance"] for c in extracted} == {"inferred"}


def test_nothing_in_the_code_updates_or_deletes_a_decision() -> None:
    """The ledger store's only statements on `check_decision` are an INSERT and a SELECT,
    and no code updates or deletes a claim."""
    source = pathlib.Path(__file__).parents[1] / "src" / "compliance_rag"
    for path in source.rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"UPDATE\s+check_decision", text, re.IGNORECASE), path
        assert not re.search(r"DELETE\s+FROM\s+check_decision", text, re.IGNORECASE), path
        assert not re.search(r"UPDATE\s+check_claim", text, re.IGNORECASE), path
        assert not re.search(r"DELETE\s+FROM\s+check_claim", text, re.IGNORECASE), path


# ---- a rejected claim's findings leave the report, and the rejection is shown -----------------


async def test_a_rejected_claims_findings_leave_the_report_and_the_rejection_is_shown() -> None:
    w, check_id = await reported()
    before = await report_of(w, check_id)
    assert [f for f in every_finding(before) if f["claim"]["index"] == 1]
    assert before["disagreements"]  # claim 1: one policy conflicts, another does not
    assert before["rejected_claims"] == []

    await claim(w, check_id, REJECT, 1, token=ADMIN_TOKEN)  # an admin, not the submitter

    report = await report_of(w, check_id)
    assert not [f for f in every_finding(report) if f["claim"]["index"] == 1]
    assert {f["claim"]["index"] for f in every_finding(report)} == {2, 3, 4}
    assert report["disagreements"] == []  # the banner was about that claim
    assert [c["index"] for c in report["claims"]] == [2, 3, 4]
    (rejected,) = report["rejected_claims"]
    assert rejected["claim"]["index"] == 1
    assert rejected["claim"]["decision"]["by"] == ADMIN_EMAIL
    assert rejected["claim"]["decision"]["kind"] == REJECT
    assert rejected["findings_set_aside"] == len(
        [f for f in every_finding(before) if f["claim"]["index"] == 1]
    )
    # its quotes are not cited in the panel: only what the report still shows is
    shown_clauses = {c["clause_id"] for f in every_finding(report) for c in f["citations"]}
    assert {s["clause_id"] for s in report["sources"]} == shown_clauses

    html = await page(w, check_id)
    parsed = scan(html)
    assert parsed.where("section", id="rejected-claims")
    assert f"Rejected</span> by {ADMIN_EMAIL}" in html
    assert 'data-verdict="conflicts"' in html  # claim 3's conflict is still there
    cards = {a["data-finding"] for a in parsed.where("article") if "data-finding" in a}
    assert cards == {f["finding_id"] for f in every_finding(report)}  # nothing of claim 1
    assert not cards & {f["finding_id"] for f in every_finding(before) if f["claim"]["index"] == 1}
    assert "Claim 1</a> in the document" in html  # and the rejected claim itself is named
    # the stored claim is the one extracted
    claims = (await check_json(w, check_id))["claims"]["claims"]
    assert {c["provenance"] for c in claims} == {"inferred"} and len(claims) == 4

    # latest wins: confirming it again brings its findings back, and the rejection is history
    await claim(w, check_id, CONFIRM, 1)
    restored = await report_of(w, check_id)
    assert restored["rejected_claims"] == []
    assert [f["finding_id"] for f in every_finding(restored)] == [
        f["finding_id"] for f in every_finding(before)
    ]
    history = await w.ledger.check_decisions(TENANT, check_id)
    assert [d.kind.value for d in history] == [REJECT, CONFIRM]


async def test_every_kind_of_finding_of_a_rejected_claim_leaves_the_report() -> None:
    cited_outside = f"{APP8_1.rsplit(':', 1)[0]}:s6c/2"

    def script(seen: Any) -> str:  # Any: a findings prompt
        asked = read_findings_prompt(seen)
        if CLAIM_3 in asked.quote:
            return "not a set of findings: " + SECRET  # claim 3 fails
        return honest(
            {
                CLAIM_2: {
                    D4_2_1: Say(
                        "conflicts", cites=((D4_2_1, opening(D4_2_1)), (cited_outside, SECRET))
                    )
                }
            }
        )(seen)

    w, check_id = await reported(CheckModel(findings=script), evidence=UNKNOWN_FY2023)
    before = await report_of(w, check_id)
    for key in ("conditional", "withheld", "failed"):
        assert before[key]
    for index in (1, 2, 3, 4):
        await claim(w, check_id, REJECT, index)
    report = await report_of(w, check_id)
    assert every_finding(report) == [] and report["sources"] == []
    assert report["claims"] == [] and report["claims_without_findings"] == []
    assert [r["claim"]["index"] for r in report["rejected_claims"]] == [1, 2, 3, 4]
    html = await page(w, check_id)
    assert 'data-outcome="withheld"' not in html and 'data-outcome="failed"' not in html
    assert "Rejected claims" in html


async def test_a_claim_with_no_finding_is_listed_as_rejected_not_as_unchecked() -> None:
    picks = {
        CLAIM_1: ((), (D1_3_2, D5_1_1)),
        CLAIM_2: (("OB-PA-APP8-1",), (D4_2_1,)),
        CLAIM_3: (("OB-PA-APP8-1",), (D4_2_2,)),
        CLAIM_4: ((), ()),
    }
    w = await world(CheckModel(claims=worked_claims(picks)))
    check_id = await checked(w)
    _, events = await find(w, check_id)
    assert events[-1][0] == "findings"
    before = await report_of(w, check_id)
    silent = {c["index"] for c in before["claims_without_findings"]}
    assert silent
    index = min(silent)
    await claim(w, check_id, REJECT, index)
    report = await report_of(w, check_id)
    assert index not in {c["index"] for c in report["claims_without_findings"]}
    assert [r["claim"]["index"] for r in report["rejected_claims"]] == [index]
    assert report["rejected_claims"][0]["findings_set_aside"] == 0


# ---- who, when and trace_id -------------------------------------------------------------------


async def test_each_decision_row_records_who_when_and_trace_id(
    span_exporter: InMemorySpanExporter,
) -> None:
    w, check_id = await reported()
    first = finding_in(await report_of(w, check_id), 1, D1_3_2)
    span_exporter.clear()
    await claim(w, check_id, CONFIRM, 1)  # the submitter
    await claim(w, check_id, REJECT, 2, token=ADMIN_TOKEN)  # an admin
    await verdict(w, check_id, ACCEPT, first["finding_id"])

    rows = await w.ledger.check_decisions(TENANT, check_id)
    assert [(r.kind.value, r.user_id, r.email) for r in rows] == [
        (CONFIRM, "uid-second", SECOND),
        (REJECT, "uid-123", ADMIN_EMAIL),
        (ACCEPT, "uid-second", SECOND),
    ]
    assert all(r.decided_at is not None for r in rows)
    stamps = [r.decided_at for r in rows if r.decided_at is not None]
    assert stamps == sorted(stamps)
    spans = [s for s in span_exporter.get_finished_spans() if s.name == "api.check.decision"]
    assert len(spans) == 3
    for row, span in zip(rows, spans, strict=True):
        assert re.fullmatch(r"[0-9a-f]{32}", row.trace_id)
        assert span.context is not None
        assert row.trace_id == format(span.context.trace_id, "032x")  # the request's own
        assert span.attributes is not None
        assert span.attributes["check.decision.kind"] == row.kind.value
        assert span.attributes["check.decision.recorded"] is True
    assert len({r.trace_id for r in rows}) == 3
    # what the report says of it names who and when
    shown = (await report_of(w, check_id))["claims"]
    confirmed = next(c for c in shown if c["index"] == 1)["decision"]
    assert (
        confirmed["by"] == SECOND and confirmed["at"] and confirmed["trace_id"] == rows[0].trace_id
    )


async def test_a_decision_span_carries_no_document_words(
    span_exporter: InMemorySpanExporter,
) -> None:
    w, check_id = await reported()
    span_exporter.clear()
    await claim(w, check_id, CONFIRM, 1)
    everything = json.dumps([dict(s.attributes or {}) for s in span_exporter.get_finished_spans()])
    assert "CloudAnalytics" not in everything and SECOND not in everything


# ---- repeating a decision is one row ----------------------------------------------------------


async def test_a_repeated_decision_appends_nothing_and_says_so() -> None:
    w, check_id = await reported()
    first = await claim(w, check_id, CONFIRM, 1)
    again = await claim(w, check_id, CONFIRM, 1)
    assert first.status_code == 201 and first.json()["recorded"] is True
    assert again.status_code == 200 and again.json()["recorded"] is False
    assert again.json()["standing"]["kind"] == CONFIRM
    assert again.json()["standing"]["trace_id"] == first.json()["standing"]["trace_id"]
    assert len(await w.ledger.check_decisions(TENANT, check_id)) == 1
    finding_id = finding_in(await report_of(w, check_id), 1, D1_3_2)["finding_id"]
    assert (await verdict(w, check_id, ACCEPT, finding_id)).status_code == 201
    assert (await verdict(w, check_id, ACCEPT, finding_id)).json()["recorded"] is False
    assert (await verdict(w, check_id, WITHDRAW, finding_id)).status_code == 201
    assert len(await w.ledger.check_decisions(TENANT, check_id)) == 3


# ---- only a verdict can be accepted ------------------------------------------------------------


async def test_only_a_finding_with_a_verdict_can_be_accepted() -> None:
    cited_outside = f"{APP8_1.rsplit(':', 1)[0]}:s6c/2"

    def script(seen: Any) -> str:  # Any: a findings prompt
        asked = read_findings_prompt(seen)
        if CLAIM_3 in asked.quote:
            return "not a set of findings: " + SECRET
        return honest(
            {
                CLAIM_2: {
                    D4_2_1: Say(
                        "conflicts", cites=((D4_2_1, opening(D4_2_1)), (cited_outside, SECRET))
                    )
                }
            }
        )(seen)

    w, check_id = await reported(CheckModel(findings=script), evidence=UNKNOWN_FY2023)
    report = await report_of(w, check_id)
    others = [f for key in ("conditional", "withheld", "failed") for f in report[key]]
    assert {f["can_accept"] for f in others} == {False}
    for finding in others:
        for kind in (ACCEPT, WITHDRAW):
            refused = await decide(w, check_id, {"kind": kind, "finding_id": finding["finding_id"]})
            assert refused.status_code == 409, finding
            assert refused.json()["detail"]["error"] == "not_a_verdict"
    assert await w.ledger.check_decisions(TENANT, check_id) == ()
    # and the page offers no button for one
    parsed = scan(await page(w, check_id))
    offered = {a["data-finding"] for a in parsed.where("button") if "data-finding" in a}
    assert not offered & {f["finding_id"] for f in others}
    # the same world has verdicts that can: they are offered, and accepted
    verdicts = [f for g in report["groups"] for f in g["findings"]]
    assert verdicts and {f["finding_id"] for f in verdicts} <= offered
    await verdict(w, check_id, ACCEPT, verdicts[0]["finding_id"])


async def test_a_rejected_claims_verdict_cannot_be_accepted_nor_an_unaccepted_one_withdrawn() -> (
    None
):
    w, check_id = await reported()
    first = finding_in(await report_of(w, check_id), 1, D1_3_2)
    refused = await decide(w, check_id, {"kind": WITHDRAW, "finding_id": first["finding_id"]})
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "not_accepted"
    await claim(w, check_id, REJECT, 1)
    refused = await decide(w, check_id, {"kind": ACCEPT, "finding_id": first["finding_id"]})
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "claim_rejected"
    assert [d.kind.value for d in await w.ledger.check_decisions(TENANT, check_id)] == [REJECT]
    await claim(w, check_id, CONFIRM, 1)  # confirmed again: it can be
    await verdict(w, check_id, ACCEPT, first["finding_id"])


# ---- who may act, and what they may name --------------------------------------------------------


async def test_only_the_submitter_and_the_admins_may_decide_and_anyone_else_gets_a_404() -> None:
    w, check_id = await reported()
    finding_id = finding_in(await report_of(w, check_id), 1, D1_3_2)["finding_id"]
    bodies: list[dict[str, Any]] = [  # Any: JSON
        {"kind": CONFIRM, "claim_index": 1},
        {"kind": REJECT, "claim_index": 1},
        {"kind": ACCEPT, "finding_id": finding_id},
        {"kind": WITHDRAW, "finding_id": finding_id},
    ]
    for body in bodies:
        stranger = await decide(w, check_id, body, THIRD_TOKEN)
        gone = await decide(w, "0" * 32, body, THIRD_TOKEN)
        assert stranger.status_code == 404 and gone.status_code == 404
        assert stranger.json() == gone.json()  # the one answer for both
        assert (await decide(w, "not-a-check", body)).status_code == 404
    assert await w.ledger.check_decisions(TENANT, check_id) == ()
    # the submitter and an admin may
    assert (await decide(w, check_id, bodies[0])).status_code == 201
    assert (await decide(w, check_id, bodies[1], ADMIN_TOKEN)).status_code == 201
    # and a stranger's page is a 404 with no button, as ever
    async with browser(w, THIRD_TOKEN) as client:
        assert (await client.get(f"/checks/{check_id}")).status_code == 404


async def test_a_decision_needs_a_signed_in_member_and_the_same_origin() -> None:
    w, check_id = await reported()
    body = {"kind": CONFIRM, "claim_index": 1}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=w.app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        assert (await client.post(f"/v1/checks/{check_id}/decisions", json=body)).status_code == 401
    async with browser(w) as signed_in:  # the session cookie, from another origin
        refused = await signed_in.post(
            f"/v1/checks/{check_id}/decisions",
            json=body,
            headers={"Origin": "https://evil.example"},
        )
        assert refused.status_code == 403
    assert await w.ledger.check_decisions(TENANT, check_id) == ()


async def test_a_claim_or_finding_that_is_not_the_checks_is_not_found() -> None:
    w, check_id = await reported()
    other = await checked(w)  # a second check of the same document: its claims, not findings
    for body in (
        {"kind": CONFIRM, "claim_index": 9},
        {"kind": ACCEPT, "finding_id": "nope"},
        {"kind": ACCEPT, "finding_id": "0" * 32},
    ):
        assert (await decide(w, check_id, body)).status_code == 404
    assert await w.ledger.check_decisions(TENANT, check_id) == ()
    assert other != check_id


async def test_nothing_is_decided_before_the_findings_are_made() -> None:
    w = await world(CheckModel(findings=MIXED))
    check_id = await checked(w)  # claims, no findings
    for body in (
        {"kind": CONFIRM, "claim_index": 1},
        {"kind": REJECT, "claim_index": 1},
        {"kind": ACCEPT, "finding_id": "f"},
    ):
        refused = await decide(w, check_id, body)
        assert refused.status_code == 409 and refused.json()["detail"]["error"] == "not_reported"
    assert await w.ledger.check_decisions(TENANT, check_id) == ()
    # a check with no claims at all is not reported either
    fresh = await world()
    unextracted = await submit(fresh)
    assert (
        await decide(fresh, unextracted, {"kind": CONFIRM, "claim_index": 1})
    ).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"kind": CONFIRM},
        {"kind": CONFIRM, "finding_id": "f1"},
        {"kind": CONFIRM, "claim_index": 1, "finding_id": "f1"},
        {"kind": ACCEPT},
        {"kind": ACCEPT, "claim_index": 1},
        {"kind": "claim_edited", "claim_index": 1},
        {"kind": CONFIRM, "claim_index": 0},
        {"kind": CONFIRM, "claim_index": 26},
        {"kind": CONFIRM, "claim_index": True},
        {"kind": CONFIRM, "claim_index": "1"},
        {"kind": ACCEPT, "finding_id": ""},
        {"kind": ACCEPT, "finding_id": "a b"},
        {"kind": CONFIRM, "claim_index": 1, "synthetic": True},
        {"kind": CONFIRM, "claim_index": 1, "email": "someone@else.example"},
        {"kind": CONFIRM, "claim_index": 1, "user_id": "uid-123"},
        {},
    ],
)
async def test_the_body_is_one_shape_and_any_other_is_a_422(
    body: dict[str, Any],
) -> None:  # Any: JSON
    w, check_id = await reported()
    refused = await decide(w, check_id, body)
    assert refused.status_code == 422, refused.text
    assert await w.ledger.check_decisions(TENANT, check_id) == ()


# ---- the report is a pure read, in one query ----------------------------------------------------


async def test_reading_the_report_writes_no_decision_and_reads_them_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w, check_id = await reported()
    await claim(w, check_id, CONFIRM, 1)
    await claim(w, check_id, REJECT, 2)
    reads: list[str] = []
    original = w.ledger.check_decisions

    async def counted(tenant_id: str, check: str) -> Any:  # Any: the rows
        reads.append(check)
        return await original(tenant_id, check)

    monkeypatch.setattr(w.ledger, "check_decisions", counted)
    first = await page(w, check_id)
    assert reads == [check_id]  # one query for the whole report, not one per claim
    await check_json(w, check_id)
    assert reads == [check_id, check_id]
    assert first == await page(w, check_id)  # and the same bytes
    monkeypatch.undo()
    assert len(await w.ledger.check_decisions(TENANT, check_id)) == 2


async def test_a_page_before_the_findings_reads_no_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = await world()
    check_id = await checked(w)
    reads: list[str] = []
    original = w.ledger.check_decisions

    async def counted(tenant_id: str, check: str) -> Any:  # Any: the rows
        reads.append(check)
        return await original(tenant_id, check)

    monkeypatch.setattr(w.ledger, "check_decisions", counted)
    await page(w, check_id)
    assert reads == []


# ---- the page: state, and buttons that name their decision ---------------------------------------


async def test_the_page_names_each_claims_state_and_offers_its_buttons() -> None:
    w, check_id = await reported()
    report = await report_of(w, check_id)
    first = finding_in(report, 1, D1_3_2)
    parsed = scan(await page(w, check_id))

    # every claim has Confirm and Reject, naming the claim; every verdict an Accept
    claim_buttons = {
        (a["data-decide"], a["data-claim"]) for a in parsed.where("button") if a.get("data-claim")
    }
    assert claim_buttons == {(k, str(i)) for i in (1, 2, 3, 4) for k in (CONFIRM, REJECT)}
    finding_buttons = {
        (a["data-decide"], a["data-finding"])
        for a in parsed.where("button")
        if a.get("data-finding")
    }
    assert finding_buttons == {
        (ACCEPT, f["finding_id"]) for g in report["groups"] for f in g["findings"]
    }
    (root,) = parsed.where("div", id="decisions")
    assert (
        re.fullmatch(r"[0-9a-f]{32}", root["data-check-id"] or "")
        and root["data-check-id"] == check_id
    )
    assert parsed.where("div", id="decision-notice")
    assert "/static/check_decisions.js" in [a["src"] for a in parsed.where("script")]

    # confirm claim 1 and accept its verdict: the page says who, and offers the way back
    await claim(w, check_id, CONFIRM, 1)
    await verdict(w, check_id, ACCEPT, first["finding_id"])
    html = await page(w, check_id)
    parsed = scan(html)
    assert f"Confirmed</span> by {SECOND}" in html
    assert f"Verdict accepted by {SECOND}" in html
    (card,) = parsed.where("article", **{"data-claim": "1", "data-state": "confirmed"})
    assert card is not None
    buttons = {
        (a["data-decide"], a.get("data-claim") or a.get("data-finding"))
        for a in parsed.where("button")
        if "data-decide" in a
    }
    assert (CONFIRM, "1") not in buttons and (REJECT, "1") in buttons  # already confirmed
    assert (WITHDRAW, first["finding_id"]) in buttons and (
        ACCEPT,
        first["finding_id"],
    ) not in buttons
    # the time is shown, in Sydney time, from the recorded stamp
    assert re.search(
        r'Confirmed</span> by [^<]+ on <time datetime="[^"]+">'
        r"\d{1,2} \w{3} \d{4}, \d\d:\d\d</time>",
        html,
    )


async def test_a_rejected_claim_offers_confirm_instead_and_the_page_says_who_rejected_it() -> None:
    w, check_id = await reported()
    await claim(w, check_id, REJECT, 3, token=ADMIN_TOKEN)
    html = await page(w, check_id)
    parsed = scan(html)
    (card,) = parsed.where("article", **{"data-claim": "3", "data-state": "rejected"})
    assert card is not None
    assert f"Rejected</span> by {ADMIN_EMAIL}" in html
    buttons = {
        (a["data-decide"], a.get("data-claim"))
        for a in parsed.where("button")
        if "data-decide" in a
    }
    assert (CONFIRM, "3") in buttons and (REJECT, "3") not in buttons
    # the claim is not in the claims still under review
    (review,) = parsed.where("section", id="claims-review")
    assert review is not None
    reviewed = [a["data-claim"] for a in parsed.where("article", **{"class": "card claim-review"})]
    assert reviewed == ["1", "2", "4"]


async def test_an_emails_markup_reaches_the_page_as_text() -> None:
    """Who decided is text: an address with markup in it is escaped wherever it is shown."""
    w, check_id = await reported()
    hostile = "<b onclick=x>a</b>@acme.example"
    await w.ledger.record_check_decision(
        CheckDecision(
            tenant_id=TENANT,
            check_id=check_id,
            kind=DecisionKind.CLAIM_REJECTED,
            claim_index=1,
            user_id="uid-x",
            email=hostile,
            trace_id="a" * 32,
        )
    )
    html = await page(w, check_id)
    assert hostile not in html and "&lt;b onclick=x&gt;a&lt;/b&gt;@acme.example" in html
    assert not [a for _, a in scan(html).elements if any(k.startswith("on") for k in a)]


async def test_the_decision_script_sends_json_and_writes_no_html() -> None:
    script = (STATIC_DIR / "check_decisions.js").read_text()
    assert_script_writes_no_html(script)
    assert "/decisions`" in script and '"Content-Type": "application/json"' in script
    assert "textContent" in script and "window.location.reload()" in script
    assert 'credentials: "same-origin"' in script
    # the buttons' four kinds are exactly the server's
    kinds = set(re.findall(r'"((?:claim|verdict)_[a-z]+)"', script))
    assert kinds == {k.value for k in DecisionKind}


# ---- the tier, as a rule -------------------------------------------------------------------------


def test_the_tier_rule_is_the_weakest_of_the_claim_and_the_verdict() -> None:
    def decision(kind: DecisionKind, **at: Any) -> CheckDecision:  # Any: claim_index or finding_id
        return CheckDecision("acme", "c", kind, "u", "u@x", "a" * 32, **at)

    shown = FindingRecord("f1", 1, FindingOutcome.VERDICT, verdict=Verdict.CONSISTENT)
    for confirmed, accepted, expected in (
        (False, False, "inferred"),
        (True, False, "inferred"),
        (False, True, "inferred"),
        (True, True, "asserted"),
    ):
        rows: list[CheckDecision] = []
        if confirmed:
            rows.append(decision(DecisionKind.CLAIM_CONFIRMED, claim_index=1))
        if accepted:
            rows.append(decision(DecisionKind.VERDICT_ACCEPTED, finding_id="f1"))
        assert Decisions.of(rows).finding_tier(shown).value == expected
    # latest wins on each
    decided = Decisions.of(
        [
            decision(DecisionKind.CLAIM_CONFIRMED, claim_index=1),
            decision(DecisionKind.VERDICT_ACCEPTED, finding_id="f1"),
            decision(DecisionKind.CLAIM_REJECTED, claim_index=1),
        ]
    )
    assert decided.finding_tier(shown) is EvidenceProvenance.INFERRED
    assert decided.rejected(1) is not None and decided.confirmed(1) is None
    withdrawn = Decisions.of(
        [
            decision(DecisionKind.CLAIM_CONFIRMED, claim_index=1),
            decision(DecisionKind.VERDICT_ACCEPTED, finding_id="f1"),
            decision(DecisionKind.VERDICT_WITHDRAWN, finding_id="f1"),
        ]
    )
    assert withdrawn.finding_tier(shown) is EvidenceProvenance.INFERRED
    # a finding with no verdict has none to accept: it is inferred whatever the rows say
    withheld = FindingRecord("f2", 1, FindingOutcome.CONDITIONAL, unassessable_reason="x")
    both = Decisions.of(
        [
            decision(DecisionKind.CLAIM_CONFIRMED, claim_index=1),
            decision(DecisionKind.VERDICT_ACCEPTED, finding_id="f2"),
        ]
    )
    assert both.finding_tier(withheld) is EvidenceProvenance.INFERRED
