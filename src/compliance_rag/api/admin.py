"""The admin panel's first page: subscriptions (#302, ADR-0053 I5 and U6).

`GET /admin/subscriptions` lists the library, every instrument the admin
curates and shares between tenants, with the tenant's tick against each, and a
button to change it. The two writes are `POST /admin/subscriptions/{id}/subscribe`
and `/unsubscribe`; each calls the operation `PUT` and `DELETE
/v1/subscriptions/{id}` call (`tenant.subscribe_tenant`,
`tenant.unsubscribe_tenant`), so the panel enforces nothing of its own: the
guardrails are the adapter's (ADR-0020, D-17). Unsubscribing deletes the
subscription row and touches no clause or retrieval store.

**Admin only, on every request.** Each route takes `PageAdmin`: a signed-out
request redirects to `/signin`, a signed-in non-member gets the 403 page, and a
member who is not an admin gets a 403 page too (`deps.is_admin`, read from
`API_ADMINS` afresh each time). A page for a member shows no link to this one.

**htmx swaps the server's own fragment.** A write sent by htmx (`HX-Request`)
is answered with the panel alone (`_subscriptions.html`), redrawn from the
ledger after the write. The same form posted by a browser without script is
answered 303 back to the page. An id the adapter refuses is said in words
(`role="alert"`) and records nothing; htmx does not swap an error status, so the
fragment of such a refusal is a 200 and the full page's is a 422.

The library is read as the ledger's `_public` environment on today's date in
Sydney (`environment_titles`), the same read the chat's out-of-environment check
uses; its titles are the recorded ones. A subscription the library no longer
lists (the ledger keeps it) is shown, marked, and can be removed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from compliance_rag.api.deps import Caller, is_admin, services_of, today
from compliance_rag.api.pages import PAGE_HEADERS, PageCaller, PageError, render
from compliance_rag.api.tenant import subscribe_tenant, unsubscribe_tenant
from compliance_rag.corpus.models import PUBLIC_TENANT_ID

router = APIRouter(prefix="/admin")

PANEL_PATH = "/admin/subscriptions"


async def page_admin(request: Request, who: PageCaller) -> Caller:
    """The signed-in member, if they are an admin; a 403 page otherwise."""
    if not is_admin(services_of(request).settings, who.member.email):
        raise PageError(403, "Admins only", "Only an admin of this workspace can manage this.")
    return who


PageAdmin = Annotated[Caller, Depends(page_admin)]


@dataclass(frozen=True, slots=True)
class Row:
    """One instrument as the panel draws it."""

    instrument_id: str
    title: str
    subscribed: bool
    in_library: bool


async def _rows(request: Request, who: Caller) -> list[Row]:
    ledger = services_of(request).ledger
    library = await ledger.environment_titles(PUBLIC_TENANT_ID, as_of=today())
    subscribed = await ledger.subscribed_instruments(who.tenant_id)
    rows = [
        Row(
            instrument_id=instrument_id,
            title=" / ".join(sorted(library.get(instrument_id, ()))) or instrument_id,
            subscribed=instrument_id in subscribed,
            in_library=instrument_id in library,
        )
        for instrument_id in library.keys() | subscribed
    ]
    return sorted(rows, key=lambda r: (not r.in_library, r.title.casefold(), r.instrument_id))


async def _panel(
    request: Request, who: Caller, *, fragment: bool, notice: str | None = None, status: int = 200
) -> HTMLResponse:
    return render(
        "_subscriptions.html" if fragment else "subscriptions.html",
        status=status,
        admin=True,
        rows=await _rows(request, who),
        notice=notice,
    )


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


@router.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request, who: PageAdmin) -> HTMLResponse:
    return await _panel(request, who, fragment=False)


async def _change(
    request: Request, who: Caller, instrument_id: str, *, subscribe: bool
) -> Response:
    notice: str | None = None
    try:
        if subscribe:
            await subscribe_tenant(request, who, instrument_id)
        else:
            await unsubscribe_tenant(request, who, instrument_id)
    except ValueError as refused:
        notice = f"Not subscribed: {refused}"
    if _is_htmx(request):
        return await _panel(request, who, fragment=True, notice=notice)
    if notice is not None:
        return await _panel(request, who, fragment=False, notice=notice, status=422)
    return RedirectResponse(PANEL_PATH, status_code=303, headers=PAGE_HEADERS)


@router.post("/subscriptions/{instrument_id}/subscribe")
async def subscribe(request: Request, who: PageAdmin, instrument_id: str) -> Response:
    return await _change(request, who, instrument_id, subscribe=True)


@router.post("/subscriptions/{instrument_id}/unsubscribe")
async def unsubscribe(request: Request, who: PageAdmin, instrument_id: str) -> Response:
    return await _change(request, who, instrument_id, subscribe=False)
