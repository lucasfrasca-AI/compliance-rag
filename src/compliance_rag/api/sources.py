"""Click-to-clause (I2, ADR-0053): every citation carries an `href` to a route
that opens its source at the clause.

`GET /v1/sources/{instrument_id}/{version_id}` serves one stored version as a
page, and the citation's `href` adds the clause's anchor, `#clause-...`
(`clause_anchor_id`, ADR-0019). The route is by version, not by clause: the
anchor is the browser's, so a citation costs no lookup.

- **Law** is the stored source HTML (`ClauseStore.get_source_html`), as the
  parser anchored it, served through `sanitise_source_html`.
- **A tenant's document** is rendered from the Markdown it was published as
  (ADR-0048) and named by the title recorded with its version (ADR-0049):
  `render_policy_html`, the same anchors.

**Tenant-scoped, and a miss is never a refusal.** A member opens only what is
in their tenant's environment (D-17): their own tenant's documents and the
library instruments the tenant subscribes to. Anything else (another tenant's
document, an unsubscribed instrument, a version the ledger never recorded, a
user-scoped document of another member, a version that doesn't exist) is the
same 404, so a request confirms nothing about what isn't theirs (ADR-0036's
pattern). A user-scoped version is decided by reading its first clause as the
member: the clause store returns a private clause to its owner alone. A
withdrawn version still opens, so the citation in an old chat does; a purged
one has no stored source and is a 404.

**The page can't run anything.** The CSP is `sandbox; default-src 'none'`
with inline styles and data images only: no script runs even if one got
through, and nothing loads. Stored law is third-party markup, so it is also
sanitised, and a policy's page is escaped from the start.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from compliance_rag.api.deps import ApiServices, Caller, CurrentCaller, services_of
from compliance_rag.assessment.policies import document_order
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    VERSION_ID_RE,
    clause_anchor_id,
    instrument_tenant_prefix,
    parse_stable_id,
    stable_id,
    validate_instrument_id,
)
from compliance_rag.corpus.source_view import render_policy_html, sanitise_source_html
from compliance_rag.telemetry import span

router = APIRouter(prefix="/v1")

SOURCE_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
"""The stored source is served as a document, never as a script: nothing in it
runs, and it loads nothing."""
SOURCE_HEADERS = {
    "Content-Security-Policy": SOURCE_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-cache",
}
NOT_FOUND = "no such source"


def source_href(clause_id: str) -> str:
    """Where a citation of `clause_id` opens: the API's source route for its
    version, at the clause's anchor. Built from the id alone, so a chat read
    back links as the live answer did."""
    instrument_id, version_id, clause_path = parse_stable_id(clause_id)
    return (
        f"/v1/sources/{quote(instrument_id, safe='/')}/{quote(version_id, safe='')}"
        f"#{quote(clause_anchor_id(clause_path), safe='')}"
    )


async def _library_source(
    services: ApiServices, who: Caller, instrument_id: str, version_id: str
) -> str | None:
    if instrument_id not in await services.ledger.subscribed_instruments(who.tenant_id):
        return None
    html = await services.clauses.get_source_html(
        instrument_id, version_id, tenant_id=PUBLIC_TENANT_ID
    )
    return None if html is None else sanitise_source_html(html)


async def _tenant_source(
    services: ApiServices, who: Caller, instrument_id: str, version_id: str
) -> str | None:
    tenant_id = who.tenant_id
    if instrument_tenant_prefix(instrument_id) != tenant_id:
        return None
    titles = await services.ledger.version_titles(tenant_id, [(instrument_id, version_id)])
    title = titles.get((instrument_id, version_id))
    if title is None:  # never recorded: a part-written publish isn't citable
        return None
    manifest = await services.clauses.get_manifest(instrument_id, version_id, tenant_id=tenant_id)
    if not manifest:
        return None
    # A version has one scope and one owner (ADR-0036), so its first clause,
    # read as this member, is the question: a private clause is returned to its
    # owner alone.
    first = stable_id(instrument_id, version_id, min(manifest, key=document_order))
    if (
        await services.clauses.get_clause(first, tenant_id=tenant_id, user_id=who.member.uid)
        is None
    ):
        return None
    markdown = await services.clauses.get_source_html(
        instrument_id, version_id, tenant_id=tenant_id
    )
    return None if markdown is None else render_policy_html(markdown, title=title)


@router.get("/sources/{instrument_id:path}/{version_id}", response_class=HTMLResponse)
async def open_source(
    request: Request,
    who: CurrentCaller,
    instrument_id: str,
    version_id: str,
) -> HTMLResponse:
    services = services_of(request)
    try:
        validate_instrument_id(instrument_id)
    except ValueError:
        raise HTTPException(404, NOT_FOUND) from None
    if not VERSION_ID_RE.match(version_id):
        raise HTTPException(404, NOT_FOUND)
    with span("api.source", instrument_id=instrument_id, version_id=version_id):
        page = await (
            _library_source if instrument_tenant_prefix(instrument_id) is None else _tenant_source
        )(services, who, instrument_id, version_id)
    if page is None:
        raise HTTPException(404, NOT_FOUND)
    return HTMLResponse(page, headers=SOURCE_HEADERS)
