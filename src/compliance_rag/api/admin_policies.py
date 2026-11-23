"""The admin's policy versions, as JSON (#303; ADR-0053 U4, U5, U6 and I5; ADR-0021).

`/v1/admin/policies` is what the page's script (`static/admin_policies.js`) posts to and what a
client may call. Every route is admin only (`CurrentAdmin`: a member gets 403 and nothing happens).
The pages are `admin_policy_pages.py`'s, and both call the same operations (`policy_ops.py`), so
neither enforces anything of its own.

- `GET /v1/admin/policies`: the policies with their versions and statuses.
- `POST /v1/admin/policies`: a **new policy**. It always makes a new instrument, whatever its title
  is, so a document whose title equals an existing policy's is a second policy and never a version
  of the first (ADR-0053 U4).
- `POST /v1/admin/policies/<policy>/versions`: a **new version** of the policy the path names,
  never one found by title.
- `GET .../versions/<version>/preview`: the number of findings citing the version.
- `POST .../versions/<version>/withdraw` and `.../purge` (ADR-0021).

What a request may say, and the order of refusals, are `policy_upload.py`'s; what is audited and
how, `policy_audit.py`'s.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from compliance_rag import telemetry
from compliance_rag.adapters.interfaces import PolicyVersionRow, RemovalAction
from compliance_rag.api.deps import CurrentAdmin, today
from compliance_rag.api.policy_audit import PolicyRefused
from compliance_rag.api.policy_lookup import find_version, rows_of, slug_of, version_path
from compliance_rag.api.policy_ops import (
    Asked,
    Published,
    findings_citing,
    publish_policy,
    remove_version,
    removed_status,
)
from compliance_rag.api.policy_upload import Purge, Withdrawal
from compliance_rag.api.uploads import parse_body, read_body
from compliance_rag.corpus.policy_versions import group_policies, version_status

router = APIRouter(prefix="/v1/admin/policies")


def version_json(row: PolicyVersionRow, day: dt.date) -> dict[str, object]:
    return {
        "version_id": row.version_id,
        "title": row.title,
        "effective_from": row.valid_from.isoformat(),
        "effective_to": None if row.valid_to is None else row.valid_to.isoformat(),
        "status": version_status(row, day).value,
        "backdate_reason": row.backdate_reason,
        "href": version_path(row.instrument_id, row.version_id),
    }


@router.get("")
async def list_policies(request: Request, who: CurrentAdmin) -> JSONResponse:
    day = today()
    groups = group_policies(await rows_of(request, who))
    return JSONResponse(
        {
            "policies": [
                {
                    "instrument_id": g.instrument_id,
                    "policy": g.slug,
                    "title": g.title,
                    "versions": [version_json(v, day) for v in g.versions],
                }
                for g in groups
            ]
        }
    )


def _published_json(published: Published) -> JSONResponse:
    return JSONResponse(
        {
            "instrument_id": published.instrument_id,
            "policy": slug_of(published.instrument_id),
            "version_id": published.version_id,
            "title": published.title,
            "effective_from": published.effective_from.isoformat(),
            "supersedes": published.supersedes,
            "backdate_reason": published.backdate_reason,
            "clauses": published.clauses,
            "href": version_path(published.instrument_id, published.version_id),
            "trace_id": telemetry.current_trace_id(),
        },
        status_code=201,
    )


@router.post("", status_code=201)
async def new_policy(request: Request, who: CurrentAdmin) -> JSONResponse:
    """A new policy: always a new instrument, whatever its title is (ADR-0053 U4)."""
    try:
        return _published_json(await publish_policy(request, who, policy=None))
    except PolicyRefused as refused:
        raise refused.http() from None


@router.post("/{policy}/versions", status_code=201)
async def new_version(request: Request, who: CurrentAdmin, policy: str) -> JSONResponse:
    """A new version of the policy the path names, never one found by title."""
    try:
        return _published_json(await publish_policy(request, who, policy=policy))
    except PolicyRefused as refused:
        raise refused.http() from None


@router.get("/{policy}/versions/{version_id}/preview")
async def preview_route(
    request: Request, who: CurrentAdmin, policy: str, version_id: str
) -> JSONResponse:
    try:
        group, _ = await find_version(request, who, policy, version_id)
        count = await findings_citing(request, who, group.instrument_id, version_id)
    except PolicyRefused as refused:
        raise refused.http() from None
    return JSONResponse({"findings_citing": count})


async def _removal(
    request: Request, who: CurrentAdmin, policy: str, version_id: str, action: RemovalAction
) -> JSONResponse:
    """Withdraw or purge, from a JSON body: the one path both routes take."""

    async def read() -> Asked:
        raw = await read_body(request, noun="policy")
        if action is RemovalAction.WITHDRAW:
            return Asked(parse_body(Withdrawal, raw).reason)
        purge = parse_body(Purge, raw)
        return Asked(purge.reason, purge.acknowledged_findings_citing)

    try:
        await remove_version(request, who, policy, version_id, action, read)
    except PolicyRefused as refused:
        raise refused.http() from None
    return JSONResponse({"status": removed_status(action).value})


@router.post("/{policy}/versions/{version_id}/withdraw")
async def withdraw_route(
    request: Request, who: CurrentAdmin, policy: str, version_id: str
) -> JSONResponse:
    return await _removal(request, who, policy, version_id, RemovalAction.WITHDRAW)


@router.post("/{policy}/versions/{version_id}/purge")
async def purge_route(
    request: Request, who: CurrentAdmin, policy: str, version_id: str
) -> JSONResponse:
    return await _removal(request, who, policy, version_id, RemovalAction.PURGE)
