"""Approving or rejecting a proposed policy mapping (#306, ADR-0053 F5, F6 and I5, ADR-0059 Q7
and C1, ADR-0027 point 7): `POST /v1/policy-mappings/{mapping_id}/decision`, admin only.

**What an approval is.** An admin reads a proposal (the policy clause, the obligation, the kind,
the model's one-line reasoning, labelled as the model's) and decides it: `approve` or `reject`,
with an optional one-line reason for a rejection. The mapping's `status` moves from `proposed`
to `approved` or `rejected` **exactly once** (`LedgerStore.decide_mapping`, ledger 0032), and the
row records who (the stable sign-in uid and the address shown), when, and the **`trace_id` of
this request**, on an approval and on a rejection alike. `trace_id` stays the proposing
request's, so the mapping keeps both. A request to decide a mapping that is already decided is
a 409, never a second decision, whatever it asks for.

**What an approval changes, and what it does not.** Only an approved `implements` mapping
derives a finding, and only in a check made *after* it (`assessment/mapping_derivation.py`,
`check_findings.py`): **an issued assessment never changes** (ADR-0034, ADR-0059 Q7). The report
of an older check says that a mapping approved since would change it and offers a re-check
(`check_recheck.py`), which issues a new assessment. A `partially_implements` mapping can be
approved and recorded and derives nothing. A rejected mapping derives nothing and is never
proposed again (`record_mapping_proposals` keeps each pair once, whatever its status). Nothing
here writes evidence, a finding, a graph node or a file, and no model is called.

**Who.** `CurrentAdmin`, read afresh from configuration on every request (ADR-0053 U6): a member
who is not an admin gets a 403 before the mapping is read. The mapping is the admin's tenant's
alone: another tenant's id is a 404, as one that does not exist.

**The body is one shape and no other key** (`extra="forbid"`): `{"decision": "approve" |
"reject", "reason": <one line>?}`. A reason with an approval is refused (422): a reason is a
rejection's, the admin's own words, shown as theirs and never evidence.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from compliance_rag import telemetry
from compliance_rag.adapters.interfaces import (
    MAX_REJECTION_REASON_CHARS,
    MappingAlreadyDecided,
    MappingDecision,
    MappingNotFound,
    MappingStatus,
)
from compliance_rag.api.deps import CurrentAdmin, services_of
from compliance_rag.api.mapping_proposals import mapping_json
from compliance_rag.corpus.display_form import cited_policy_versions

router = APIRouter(prefix="/v1")

NOT_FOUND: Final = "no such policy mapping"
ALREADY_DECIDED: Final = "this mapping was already decided, and a decision is final"


class DecisionBody(BaseModel):
    """What an admin decides. Two fields and no others."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True, min_length=1, max_length=MAX_REJECTION_REASON_CHARS
            ),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def _reason_is_a_rejections(self) -> Self:
        if self.reason is not None:
            if self.decision != "reject":
                raise ValueError("only a rejection carries a reason")
            if "\n" in self.reason or "\r" in self.reason or "\x00" in self.reason:
                raise ValueError("a reason is one line")
        return self


@router.post("/policy-mappings/{mapping_id}/decision")
async def decide_mapping(
    request: Request,
    who: CurrentAdmin,
    mapping_id: Annotated[int, Path(ge=1, le=2**62)],
    body: DecisionBody,
) -> dict[str, object]:
    """Record the decision: 200 with the mapping as it now stands. 404 for a mapping the admin's
    tenant does not hold, 409 for one already decided."""
    services = services_of(request)
    status = MappingStatus.APPROVED if body.decision == "approve" else MappingStatus.REJECTED
    with telemetry.span(
        "api.mapping.decide",
        **{"mapping.id": mapping_id, "mapping.decision": status.value},
    ) as span:
        trace_id = telemetry.current_trace_id()
        try:
            decided = await services.ledger.decide_mapping(
                MappingDecision(
                    tenant_id=who.tenant_id,
                    mapping_id=mapping_id,
                    status=status,
                    approver=who.member.uid,
                    approver_email=who.member.email,
                    trace_id=trace_id,
                    reason=body.reason,
                )
            )
        except MappingNotFound:
            span.set_attribute("mapping.refused", "not_found")
            raise HTTPException(404, {"error": "not_found", "message": NOT_FOUND}) from None
        except MappingAlreadyDecided as already:
            span.set_attribute("mapping.refused", "already_decided")
            raise HTTPException(
                409,
                {
                    "error": "already_decided",
                    "message": ALREADY_DECIDED,
                    "status": already.status.value,
                },
            ) from None
        span.set_attribute("mapping.kind", decided.kind.value)
        span.set_attribute("mapping.derives", decided.derives)
        titles = await services.ledger.version_titles(
            who.tenant_id, cited_policy_versions([decided.policy_clause_id])
        )
        return {
            "mapping": mapping_json(decided, titles),
            # what the decision changes: a check made from now on, never one already issued
            "derives": decided.derives,
        }
