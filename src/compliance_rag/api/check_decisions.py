"""Claim confirmation, claim rejection and verdict acceptance (#301, ADR-0053 F5,
ADR-0059 Q3 and C1): `POST /v1/checks/{check_id}/decisions`.

**What it records.** One request is one of four decisions about a check the caller may see
(`DecisionKind`): a claim **confirmed** or **rejected**, a finding's verdict **accepted** or
its acceptance **withdrawn**. Each is an append-only row (`LedgerStore.record_check_decision`,
ledger 0029) with who (the stable sign-in uid, ADR-0036, and the address shown), when, and the
request's `trace_id`. The current state of a claim or a verdict is its **latest** row:

- a confirmation may be followed by a rejection and the reverse (a rejection is not final: the
  member who rejects a claim by mistake confirms it, and its findings return to the report);
- an acceptance may be withdrawn, and accepted again;
- a request for the state a claim or verdict is already in appends nothing and says so
  (`recorded: false`), so a double click is one row.

**What it never does.** It never updates a claim or a finding (a claim is never promoted or
mutated, ADR-0034), writes no evidence row (a claim of a proposal states an intention, not a
fact about the tenant, so its confirmation settles no condition) and changes no coverage.
What it changes is what the report **computes when it reads**: a claim's tier (`asserted` once
confirmed), a finding's tier (the weakest of its claim's and its verdict's: `asserted` only when
the claim is confirmed and the verdict accepted, `assessment/decisions.py`), and which findings
the report shows (a rejected claim's leave it).

**After the findings run** (Q3): there is nothing to confirm a claim against, or to accept, until
the findings are recorded (409 `not_reported`).

**The rules are the ledger's, not this route's** (`decision_adds_a_row`, applied by every
`LedgerStore` under its own lock): only a finding that has a verdict can be accepted (409
`not_a_verdict`: a withheld, failed or conditional finding has none to accept), not one of a
claim that stands rejected (409 `claim_rejected`), and nothing is withdrawn that is not accepted
(409 `not_accepted`). The store raises `DecisionRefused`; this route only maps it to our words.

**Who may act** is who may see the check (`checks.visible_check`, ADR-0059 Q5): its submitter and
the tenant's admins. Anyone else gets the same 404 as for a check that does not exist, before
anything is read or written. The body is JSON of the one shape and refuses any other key (422);
the session cookie's requests also pass the Origin check (`session.OriginGuard`).
"""

from __future__ import annotations

from typing import Final, Self

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from compliance_rag import telemetry
from compliance_rag.adapters.interfaces import (
    MAX_CLAIMS,
    CheckDecision,
    DecisionKind,
    DecisionRefusal,
    DecisionRefused,
)
from compliance_rag.api.checks import NOT_FOUND, visible_check
from compliance_rag.api.decision_view import standing_json
from compliance_rag.api.deps import CurrentCaller, services_of
from compliance_rag.assessment.decisions import Decisions

router = APIRouter(prefix="/v1")

NOT_REPORTED: Final = "this check has no findings yet, so there is nothing to confirm or accept"
NO_SUCH_CLAIM: Final = "no such claim in this check"
NO_SUCH_FINDING: Final = "no such finding in this check"
REFUSALS: Final = {
    DecisionRefusal.NOT_A_VERDICT: (
        "this finding has no verdict to accept: only a finding with a verdict can be accepted"
    ),
    DecisionRefusal.CLAIM_REJECTED: (
        "this finding's claim is rejected: confirm the claim before accepting"
    ),
    DecisionRefusal.NOT_ACCEPTED: (
        "this verdict was not accepted, so there is no acceptance to withdraw"
    ),
}
"""Our words for each reason the store refuses a decision (`DecisionRefused`): the rules are
the store's (`decision_adds_a_row`), so no request, from this route or another, can break
them. This only maps each to the 409 a member is told."""


class DecisionRequest(BaseModel):
    """What a member decides: the kind, and the claim (by its number) or the finding (by its
    id) it is about. Three fields and no others (`extra="forbid"`)."""

    model_config = ConfigDict(extra="forbid")
    kind: DecisionKind
    claim_index: int | None = Field(default=None, ge=1, le=MAX_CLAIMS, strict=True)
    finding_id: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )

    @model_validator(mode="after")
    def _names_its_subject(self) -> Self:
        if self.kind.of_claim:
            if self.claim_index is None or self.finding_id is not None:
                raise ValueError("a claim decision names its claim and no finding")
        elif self.finding_id is None or self.claim_index is not None:
            raise ValueError("a verdict decision names its finding and no claim")
        return self


def _refusal(status: int, error: str, message: str) -> HTTPException:
    return HTTPException(status, {"error": error, "message": message})


@router.post("/checks/{check_id}/decisions")
async def decide(
    request: Request, who: CurrentCaller, check_id: str, body: DecisionRequest
) -> JSONResponse:
    """Record one decision. 201 with the row's state when one was appended, 200 when the claim
    or verdict was already in that state and nothing was."""
    services = services_of(request)
    with telemetry.span(
        "api.check.decision",
        **{"check.check_id": check_id, "check.decision.kind": body.kind.value},
    ) as span:
        # Before anything is read or written: a check that isn't theirs is not found.
        if await visible_check(services, who, check_id) is None:
            raise HTTPException(404, NOT_FOUND)
        findings = await services.ledger.get_findings(who.tenant_id, check_id)
        extraction = await services.ledger.get_claims(who.tenant_id, check_id)
        if findings is None or extraction is None:
            raise _refusal(409, "not_reported", NOT_REPORTED)
        if body.kind.of_claim:
            if body.claim_index not in {c.claim_index for c in extraction.claims}:
                raise HTTPException(404, NO_SUCH_CLAIM)
        elif not any(f.finding_id == body.finding_id for f in findings.findings):
            raise HTTPException(404, NO_SUCH_FINDING)
        try:
            recorded = await services.ledger.record_check_decision(
                CheckDecision(
                    tenant_id=who.tenant_id,
                    check_id=check_id,
                    kind=body.kind,
                    claim_index=body.claim_index,
                    finding_id=body.finding_id,
                    user_id=who.member.uid,
                    email=who.member.email,
                    trace_id=telemetry.current_trace_id(),
                )
            )
        except DecisionRefused as refused:
            span.set_attribute("check.decision.refused", refused.reason.value)
            raise _refusal(409, refused.reason.value, REFUSALS[refused.reason]) from None
        span.set_attribute("check.decision.recorded", recorded)
        latest = Decisions.of(await services.ledger.check_decisions(who.tenant_id, check_id))
        stands = (
            latest.claims.get(body.claim_index or 0)
            if body.kind.of_claim
            else latest.verdicts.get(body.finding_id or "")
        )
        return JSONResponse(
            status_code=201 if recorded else 200,
            content={
                "check_id": check_id,
                "recorded": recorded,
                "claim_index": body.claim_index,
                "finding_id": body.finding_id,
                "standing": standing_json(stands),
            },
        )
