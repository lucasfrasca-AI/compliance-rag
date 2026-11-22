"""The Check pages (#297, #300; ADR-0053 I1, I2, I3 and I6): server-rendered, as the Ask
pages are (`pages.py`), under the same CSP and the same headers.

- `GET /checks/new`: where a member submits a document, a form whose script
  (`static/checks.js`) reads the chosen file and posts it to `POST /v1/checks`
  with the session cookie, then opens the new check. Markdown and plain text
  only, and the page says so.
- `GET /checks/{id}`: one page in three states, by what the ledger holds
  (`check_report.State`). With no claims it offers "Run check"; with claims and no
  findings it offers the rest of the run; with findings it is the report: the findings
  ordered by verdict, the policies' banner, the coverage statement and the citation panel
  (`_sources.html`). `static/check_run.js` makes the two run requests and shows their
  stages as they stream, then reloads, so the report on screen is always the ledger's,
  drawn by the server (it never builds HTML from the stream). The stored document is below,
  each passage an element with its anchor (`clause-p1`, ...), the same ids the source route
  serves (`/v1/sources/<the document>/v1#clause-p1`), a claim's words marked in it, so a
  claim opens the document at its quote.

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

from compliance_rag.api.check_report import claim_views, passage_views, read_report
from compliance_rag.api.checks import MAX_DOCUMENT_BYTES, source_path
from compliance_rag.api.deps import services_of
from compliance_rag.api.pages import PageCaller, PageError, render

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
    found = await read_report(services_of(request), who, check_id)
    if found is None:
        raise _no_such_check()
    state, report = found
    return render(
        request,
        "check.html",
        check=state.view.record,
        title=state.view.title,
        mine=state.view.mine,
        source_href=source_path(state.view.record.instrument_id),
        state=state.state.value,
        passages=state.view.passages,
        passage_views=passage_views(state.view.passages, state.claims),
        claims=claim_views(state),
        run=state.run,
        report=report,
    )
