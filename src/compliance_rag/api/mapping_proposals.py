"""Policy mapping proposals over the API (#305, ADR-0053 F6, ADR-0059 Q8 and C2):
`POST /v1/policy-mappings/proposals`, streamed as server-sent events as the claims and
findings of a check are, and `GET /v1/policy-mappings` of what the ledger holds.

**An admin asks, for one policy version** (Q8). Proposing is a run the admin requests from
the mapping queue page (`admin_mappings.py`), never one a policy upload starts: a new policy
version has new clause ids and needs its own approvals. Both routes are admin-only
(`CurrentAdmin`, ADR-0053 U6), read afresh from configuration on every request.

**A proposal is a ledger row and nothing else** (ADR-0018, ADR-0059 C2). The run reads the
graph for the obligation digest (Track A's coverage, #295) and the policies for the prefix,
and writes one place: `policy_mapping`, through `MappingProposer`, which is given no graph
writer and no file writer (a test proves it from the module's imports). The mapping is
`proposed`, its proposer the model id, its approver empty; approving or rejecting it is
#306's. The model's one-line rationale is shown only as `{"text": ..., "source": "model"}`:
its words, labelled, never evidence.

The stream carries stages and a result, never anything unvalidated:
- `coverage` once Track A has answered (the obligation digest is built from it);
- `proposals`: what the run recorded for the version (every mapping it holds, in every
  status, and what this run appended, dropped, and why: `dropped_reasons`, our word for each
  reason, never the model's);
- or `error`.

**Gates, in order, each before the next costs anything.** The sign-in and the admin role;
the policy version, which must be an internal policy's version in force today (404
otherwise: nothing is named that is not one); the reservation
(`LedgerStore.reserve_mapping_run`), atomically: of any number of concurrent requests for one
policy version exactly one holds it, and the rest are a 409 before the stream starts that
call nothing. The reservation is released as failed by every path that ends without recording,
so the run can be made again. Inside the stream, first, offshore: only a registered
build-phase tester may ask (`assert_mapping_run_allowed`, ADR-0032); the synthetic-text gate
over the policy clauses is the proposer's, before it builds a prompt (ADR-0026).

An error event names our word for what went wrong, never the model's or the provider's:
`residency`, `boundary_collision`, `malformed_generation`, `generation_unavailable`,
`policy_corpus_too_large` or `internal`. The streaming and reservation skeleton is
`check_runs.py`'s, shared with a check's runs.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from compliance_rag.adapters.generation_residency import assert_mapping_run_allowed
from compliance_rag.adapters.interfaces import (
    MappingRunAlreadyReserved,
    MappingStatus,
    PolicyMapping,
    PolicyVersion,
)
from compliance_rag.api.check_runs import CheckRun, ErrorBody, event, run_stream
from compliance_rag.api.deps import (
    ApiServices,
    Caller,
    CurrentAdmin,
    counts,
    coverage_on,
    services_of,
    today,
)
from compliance_rag.api.sources import source_href
from compliance_rag.assessment.draft import MalformedGeneration
from compliance_rag.assessment.obligation_digest import ObligationDigest
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.assessment.prompt import DelimiterCollision
from compliance_rag.corpus.display_form import Titles, cite, cited_policy_versions
from compliance_rag.telemetry import current_trace_id

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)

NOT_A_POLICY_VERSION: Final = (
    "that is not a version of one of this workspace's policies that is in force today"
)
ALREADY_RUNNING: Final = "mappings are already being proposed for this policy version"


class ProposeIn(BaseModel):
    """What an admin asks for: one policy version. Ids are checked against the ledger's
    versions in force, so nothing here reaches a prompt unless the ledger holds it."""

    model_config = ConfigDict(extra="forbid")

    instrument_id: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=1, max_length=100)


def _error(failed: Exception) -> ErrorBody | None:
    """Our word for a failure that is the proposing run's own (the rest are
    `check_runs.common_error`'s)."""
    if isinstance(failed, DelimiterCollision):
        return {
            "error": "boundary_collision",
            "message": "a policy clause contains the text that fences it, so nothing was sent "
            "to the model",
        }, False
    if isinstance(failed, MalformedGeneration):
        return {
            "error": "malformed_generation",
            "message": "the model's reply wasn't a valid set of proposals; nothing in it is used",
        }, False
    return None


def _gate(services: ApiServices, who: Caller) -> None:
    """The run's offshore gate, before anything is read or sent (ADR-0032)."""
    assert_mapping_run_allowed(
        generation_region=services.settings.generation_region,
        requester=who.member.email,
        testers=services.settings.offshore_testers,
    )


def mapping_json(mapping: PolicyMapping, titles: Titles) -> dict[str, object]:
    """One mapping as an admin is shown it. The rationale is the model's own words and says
    so; the clause is named as a reader cites it and linked to its source."""
    return {
        "mapping_id": mapping.mapping_id,
        "instrument_id": mapping.instrument_id,
        "version_id": mapping.version_id,
        "policy_clause_id": mapping.policy_clause_id,
        "cite": cite(mapping.policy_clause_id, titles),
        "href": source_href(mapping.policy_clause_id),
        "obligation_id": mapping.obligation_id,
        "kind": mapping.kind.value,
        "status": mapping.status.value,
        "rationale": {"text": mapping.rationale, "source": "model"},
        "proposer": mapping.proposer,
        "approver": mapping.approver,
        "trace_id": mapping.trace_id,
        "proposed_at": None if mapping.proposed_at is None else mapping.proposed_at.isoformat(),
    }


async def mappings_json(
    services: ApiServices, tenant_id: str, mappings: Sequence[PolicyMapping]
) -> list[dict[str, object]]:
    """Mappings as shown, the policy titles read once for all of them (ADR-0049)."""
    titles = await services.ledger.version_titles(
        tenant_id, cited_policy_versions(m.policy_clause_id for m in mappings)
    )
    return [mapping_json(m, titles) for m in mappings]


def status_counts(mappings: Sequence[PolicyMapping]) -> dict[str, int]:
    """How many are in each status, every status named."""
    return {s.value: sum(m.status is s for m in mappings) for s in MappingStatus}


@router.post("/policy-mappings/proposals")
async def propose(request: Request, who: CurrentAdmin, body: ProposeIn) -> StreamingResponse:
    services = services_of(request)
    day = today()
    # Before anything is reserved, read or sent: only an internal policy's version in force
    # today. Nothing else is named back.
    if PolicyVersion(
        body.instrument_id, body.version_id
    ) not in await services.ledger.policy_versions(who.tenant_id, as_of=day):
        raise HTTPException(404, {"error": "not_a_policy_version", "message": NOT_A_POLICY_VERSION})
    held_by = current_trace_id()
    try:
        await services.ledger.reserve_mapping_run(
            who.tenant_id, body.instrument_id, body.version_id, trace_id=held_by
        )
    except MappingRunAlreadyReserved:
        raise HTTPException(409, {"error": "already_running", "message": ALREADY_RUNNING}) from None
    except ValueError:  # the version went away between the read and the reservation
        raise HTTPException(
            404, {"error": "not_a_policy_version", "message": NOT_A_POLICY_VERSION}
        ) from None

    async def release() -> None:
        # Only this request's own reservation: a run that outlived it must not release the
        # request that has since taken it over.
        await services.ledger.fail_mapping_run(
            who.tenant_id, body.instrument_id, body.version_id, trace_id=held_by
        )

    async def stream(run: CheckRun) -> AsyncIterator[str]:
        coverage = await coverage_on(services, who.tenant_id, day)
        yield event(
            "coverage",
            {
                "instrument_id": body.instrument_id,
                "version_id": body.version_id,
                "as_of": day.isoformat(),
                "counts": counts(coverage),
            },
        )
        digest = ObligationDigest.of(coverage)
        policies = await tenant_policies(
            services.ledger, services.clauses, tenant_id=who.tenant_id, as_of=day
        )
        proposer = services.mapping_proposer()
        proposed = await run.spawn(
            lambda: proposer.propose(
                tenant_id=who.tenant_id,
                instrument_id=body.instrument_id,
                version_id=body.version_id,
                digest=digest,
                policies=policies,
            )
        )
        stored = await services.ledger.policy_mappings(
            who.tenant_id, instrument_id=body.instrument_id, version_id=body.version_id
        )
        yield event(
            "proposals",
            {
                "instrument_id": body.instrument_id,
                "version_id": body.version_id,
                "as_of": day.isoformat(),
                "trace_id": proposed.trace_id,
                "proposer": proposed.proposer,
                "accepted": len(proposed.proposals),
                "appended": proposed.appended,
                "dropped": len(proposed.dropped),
                "dropped_reasons": [
                    {"position": d.position, "reason": d.reason.value} for d in proposed.dropped
                ],
                "counts": status_counts(stored),
                "mappings": await mappings_json(services, who.tenant_id, stored),
                "generator": services.settings.generator,
                "cost_usd": proposed.generation.cost_usd,
            },
        )

    return StreamingResponse(
        run_stream(
            services=services,
            who=who,
            subject={"instrument_id": body.instrument_id, "version_id": body.version_id},
            release=release,
            error_of=_error,
            failure="proposing policy mappings",
            body=stream,
            gate=_gate,
        ),
        media_type="text/event-stream",
    )


@router.get("/policy-mappings")
async def get_policy_mappings(
    request: Request,
    who: CurrentAdmin,
    instrument_id: str | None = None,
    version_id: str | None = None,
) -> dict[str, object]:
    """The workspace's policy mappings in every status, in the order they were proposed:
    one policy version's when both `instrument_id` and `version_id` are given. Admin only."""
    if (instrument_id is None) != (version_id is None):
        raise HTTPException(
            422,
            {
                "error": "name_both",
                "message": "name a policy version by its instrument_id and version_id, or neither",
            },
        )
    services = services_of(request)
    if instrument_id is None:
        mappings = await services.ledger.policy_mappings(who.tenant_id)
    else:
        mappings = await services.ledger.policy_mappings(
            who.tenant_id, instrument_id=instrument_id, version_id=version_id
        )
    return {
        "counts": status_counts(mappings),
        "mappings": await mappings_json(services, who.tenant_id, mappings),
    }
