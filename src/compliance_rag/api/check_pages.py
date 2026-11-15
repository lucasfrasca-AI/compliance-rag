"""The Check pages (#297, ADR-0053 I1, I2 and I6): server-rendered, as the Ask
pages are (`pages.py`), under the same CSP and the same headers.

- `GET /checks/new`: where a member submits a document, a form whose script
  (`static/checks.js`) reads the chosen file and posts it to `POST /v1/checks`
  with the session cookie, then opens the new check. Markdown and plain text
  only, and the page says so.
- `GET /checks/{id}`: the stored document, passage by passage, each passage an
  element with its anchor (`clause-p1`, ...), the same ids the source route
  serves (`/v1/sources/<the document>/v1#clause-p1`), so a later finding opens
  the document at its claim. Nothing is extracted or judged yet (#298, #299).

**Visibility** is `checks.read_check`'s, the one reader the JSON route uses: a
check that is not the caller's (and the caller is not an admin) is the same 404
page as one that does not exist, and nothing is drawn for it. Every response
under `/checks` carries the pages' headers (`pages.PAGE_PREFIXES`).

The document's words reach the browser as text: autoescaped, never marked safe,
in an element with no script, handler or style (`tests/test_api_pages.py` scans
every template for all three).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from compliance_rag.api.checks import MAX_DOCUMENT_BYTES, read_check, source_path
from compliance_rag.api.deps import services_of
from compliance_rag.api.pages import PageCaller, PageError, render
from compliance_rag.corpus.models import clause_anchor_id

router = APIRouter()


def _no_such_check() -> PageError:
    return PageError(404, "No such check", "This check doesn't exist, or it isn't yours to see.")


@router.get("/checks/new", response_class=HTMLResponse)
async def new_check_page(request: Request, _: PageCaller) -> HTMLResponse:
    return render(
        request,
        "check_new.html",
        max_bytes=MAX_DOCUMENT_BYTES,
        max_kib=MAX_DOCUMENT_BYTES // 1024,
    )


@router.get("/checks/{check_id}", response_class=HTMLResponse)
async def check_page(request: Request, who: PageCaller, check_id: str) -> HTMLResponse:
    view = await read_check(services_of(request), who, check_id)
    if view is None:
        raise _no_such_check()
    return render(
        request,
        "check.html",
        check=view.record,
        title=view.title,
        passages=view.passages,
        mine=view.mine,
        source_href=source_path(view.record.instrument_id),
        anchor=clause_anchor_id,
    )
