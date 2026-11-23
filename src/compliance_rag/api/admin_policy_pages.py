"""The admin's policy pages (#303; ADR-0053 I5 and I6): server-rendered.

`GET /admin/policies` lists the workspace's policies with every version's id, effective date,
status and recorded title, and holds the upload forms (a new policy, and a new version of each
policy). `GET /admin/policies/<policy>/versions/<version>` is a version's page: its status, the
preview (the number of findings that cite it), and the withdraw and purge forms. A purge form asks
the admin to type that number.

The upload is script-only, as the Check page's is (no multipart parser is installed):
`static/admin_policies.js` reads the file and posts JSON to `admin_policies.py`, at the route in
each form's `data-action`, which the server wrote for that policy. Withdraw and purge are plain form
posts that work without script; they call the one operation the JSON routes call (`policy_ops`),
and a refusal is said in words on the version's page with the status the JSON gives. Every route is
admin only (`PageAdmin`: a member gets the 403 page). The `?done=` notice is picked from a fixed
table, never drawn from the request.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from compliance_rag.adapters.interfaces import PolicyVersionRow, RemovalAction
from compliance_rag.api.admin import PageAdmin
from compliance_rag.api.deps import Caller, today
from compliance_rag.api.pages import PAGE_HEADERS, PageError, render
from compliance_rag.api.policy_audit import PolicyRefused
from compliance_rag.api.policy_lookup import PAGE_PATH, find_version, rows_of, version_path
from compliance_rag.api.policy_ops import Asked, findings_citing, remove_version
from compliance_rag.api.policy_upload import Withdrawal
from compliance_rag.api.uploads import MAX_DOCUMENT_BYTES
from compliance_rag.corpus.policy_versions import (
    STATUS_WORDS,
    PolicyGroup,
    VersionStatus,
    group_policies,
    version_status,
)

router = APIRouter(prefix=PAGE_PATH)

MAX_FORM_BYTES = 4_096
_COUNT = re.compile(r"[0-9]{1,9}", re.ASCII)
NOTICES = {
    "published": "Published. The new version is listed below.",
    "withdrawn": "Withdrawn. It is out of retrieval at every date, and its text is kept, so "
    "earlier citations still open.",
    "purged": "Purged. Its text and its search entries are deleted; the ledger keeps a marker "
    "of who purged it, when and why.",
}
"""What a page says after a write, picked by a fixed key (`?done=`), never by text from the
request."""
_DONE = {RemovalAction.WITHDRAW: "withdrawn", RemovalAction.PURGE: "purged"}


@dataclass(frozen=True, slots=True)
class VersionView:
    row: PolicyVersionRow
    status: str
    status_words: str
    href: str


@dataclass(frozen=True, slots=True)
class PolicyView:
    group: PolicyGroup
    versions: list[VersionView]


def _views(group: PolicyGroup, day: dt.date) -> list[VersionView]:
    views: list[VersionView] = []
    for row in group.versions:
        status: VersionStatus = version_status(row, day)
        views.append(
            VersionView(
                row=row,
                status=status.value,
                status_words=STATUS_WORDS[status],
                href=version_path(row.instrument_id, row.version_id),
            )
        )
    return views


def _done(request: Request) -> str | None:
    return NOTICES.get(request.query_params.get("done", ""))


@router.get("", response_class=HTMLResponse)
async def policies_page(request: Request, who: PageAdmin) -> HTMLResponse:
    day = today()
    groups = group_policies(await rows_of(request, who))
    return render(
        request,
        "policies.html",
        policies=[PolicyView(g, _views(g, day)) for g in groups],
        today=day.isoformat(),
        max_bytes=MAX_DOCUMENT_BYTES,
        max_kib=MAX_DOCUMENT_BYTES // 1024,
        notice=_done(request),
    )


def _not_found(refused: PolicyRefused) -> PageError:
    return PageError(
        refused.status,
        "No such policy version",
        refused.message[:1].upper() + refused.message[1:] + ".",
    )


async def _version_page(
    request: Request,
    who: Caller,
    policy: str,
    version_id: str,
    *,
    notice: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    try:
        group, row = await find_version(request, who, policy, version_id)
        count = await findings_citing(request, who, group.instrument_id, version_id)
    except PolicyRefused as refused:
        raise _not_found(refused) from None
    view = next(v for v in _views(group, today()) if v.row.version_id == version_id)
    return render(
        request,
        "policy_version.html",
        status=status,
        policy=group,
        version=view,
        row=row,
        findings_citing=count,
        notice=notice if notice is not None else _done(request),
    )


@router.get("/{policy}/versions/{version_id}", response_class=HTMLResponse)
async def version_page(
    request: Request, who: PageAdmin, policy: str, version_id: str
) -> HTMLResponse:
    return await _version_page(request, who, policy, version_id)


async def _form(request: Request) -> dict[str, str]:
    """The urlencoded body of a form post, bounded and with no repeated name. (No multipart
    parser is installed, and these forms have only text.)"""
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != "application/x-www-form-urlencoded":
        raise PageError(415, "Not a form", "This takes a form post.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise PageError(413, "Too large", "That form is larger than any answer needs.")
    try:
        fields = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=10, errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise PageError(400, "Not a form", "That form could not be read.") from None
    if any(len(values) != 1 for values in fields.values()):
        raise PageError(400, "Not a form", "A form field was sent twice.")
    return {name: values[0] for name, values in fields.items()}


async def _removal(
    request: Request, who: Caller, policy: str, version_id: str, action: RemovalAction
) -> Response:
    """Withdraw or purge, from a form: the one path both routes take. The reason is held to the
    JSON's rule (one type, `policy_upload.Reason`), and a typed count that is not ASCII digits is
    no number."""

    async def read() -> Asked:
        fields = await _form(request)
        try:
            reason = Withdrawal(reason=fields.get("reason", "")).reason
        except ValidationError:
            raise PolicyRefused(
                422, "invalid_reason", "give a reason, in one line of up to 500 characters"
            ) from None
        if action is RemovalAction.WITHDRAW:
            return Asked(reason)
        typed = fields.get("typed_count", "").strip()
        return Asked(reason, int(typed) if _COUNT.fullmatch(typed) else None)

    try:
        await remove_version(request, who, policy, version_id, action, read)
    except PolicyRefused as refused:
        if refused.status == 404:
            raise _not_found(refused) from None
        return await _version_page(
            request, who, policy, version_id, notice=refused.message, status=refused.status
        )
    return RedirectResponse(
        f"{PAGE_PATH}/{policy}/versions/{version_id}?done={_DONE[action]}",
        status_code=303,
        headers=PAGE_HEADERS,
    )


@router.post("/{policy}/versions/{version_id}/withdraw")
async def withdraw_page(request: Request, who: PageAdmin, policy: str, version_id: str) -> Response:
    return await _removal(request, who, policy, version_id, RemovalAction.WITHDRAW)


@router.post("/{policy}/versions/{version_id}/purge")
async def purge_page(request: Request, who: PageAdmin, policy: str, version_id: str) -> Response:
    return await _removal(request, who, policy, version_id, RemovalAction.PURGE)
