"""The list of a member's checks (#355, ADR-0053 I1, ADR-0059 Q5): `GET /checks`,
server-rendered as the other pages are (`pages.py`), under the same CSP and
headers, and the sidebar's links to the latest few (`pages.nav_sections`).

**Visibility is `CheckRecord.visible_to`'s, applied by the ledger's one read**
(`LedgerStore.list_checks`): a member's list is their own checks, an admin's is
the tenant's, and a check that is neither never appears. Each row links to
`/checks/{id}`, which applies the same rule again (`checks.visible_check`), so a
link that is not theirs is the same 404 as one that does not exist.

**One read.** Each row's title, stage and run state come with it, so a list of
fifty is one query, not fifty-one. The read returns metadata only: the page never
holds a document's or a claim's words.

**Paging.** Newest first, fifty to a page. An older page is `?before=`, the
last row's `created_at` and id, which the ledger continues strictly after, so a
check submitted meanwhile moves no row from one page to the next. A cursor that
is not one is the first page.
"""

from __future__ import annotations

import datetime as dt
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from compliance_rag import telemetry
from compliance_rag.adapters.interfaces import CheckCursor, CheckListing
from compliance_rag.api.deps import UUID4_HEX, services_of
from compliance_rag.api.pages import (
    CHECKS_PATH,
    SIDEBAR_CHECKS,
    PageCaller,
    render,
    sidebar_checks,
)

router = APIRouter()

PAGE_SIZE = 50
_CHECK_ID = re.compile(UUID4_HEX)


def cursor_of(row: CheckListing) -> str:
    """The `?before=` value that continues after `row`."""
    return f"{row.created_at.isoformat()}_{row.check_id}"


def parse_cursor(text: str | None) -> CheckCursor | None:
    """The cursor a `?before=` value names; None for none, or for text that is
    not one (the first page, not an error: nothing is built from it)."""
    if text is None:
        return None
    stamp, _, check_id = text.rpartition("_")
    if _CHECK_ID.fullmatch(check_id) is None:
        return None
    try:
        created_at = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if created_at.tzinfo is None:
        return None
    return CheckCursor(created_at, check_id)


@router.get(CHECKS_PATH, response_class=HTMLResponse)
async def checks_page(request: Request, who: PageCaller, before: str | None = None) -> HTMLResponse:
    cursor = parse_cursor(before)
    with telemetry.span("api.check.list", **{"check.paged": cursor is not None}) as span:
        rows = await services_of(request).ledger.list_checks(
            who.tenant_id,
            who.member.uid,
            admin=bool(request.state.is_admin),
            limit=PAGE_SIZE + 1,  # one more than shown: whether there is an older page
            before=cursor,
        )
        shown = rows[:PAGE_SIZE]
        span.set_attribute("check.listed", len(shown))
    # The sidebar's latest are this page's first, unless the page is an older one.
    request.state.recent_checks = (
        shown[:SIDEBAR_CHECKS] if cursor is None else await sidebar_checks(request, who)
    )
    return render(
        request,
        "check_list.html",
        checks=shown,
        older=cursor_of(shown[-1]) if len(rows) > PAGE_SIZE else None,
        paged=cursor is not None,
        me=who.member.uid,
        admin=bool(request.state.is_admin),
    )
