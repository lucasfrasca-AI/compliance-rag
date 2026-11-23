"""Checking a document again, so an approved mapping can change its findings (#306, ADR-0059 Q7,
ADR-0034): `POST /v1/checks/{check_id}/recheck`.

**An issued report never changes.** Approving a policy mapping (`mapping_decisions.py`) affects
the *next* check, not one already made: the assessment keeps what it said, and the document it
judged. A report made before a mapping was approved says so (`check_report.py`: "a mapping
approved since would change this") and offers this one click.

**What it does.** It makes a **new check from the same stored document**, a new
`submitted_document` version and a new ledger check (`checks.create_check`: the one place a
check is made), and says where it is. The document is read back from the bucket as it was
submitted, byte for byte (the stored source, the same text the first check's passages were cut
from), so the new check judges the same words, and offshore the registry's whole-document hash
vouches for it as it did the first time. **Nothing of the first check is touched**: its claims,
its findings, its assessment and its decisions stay as they were, and it still opens.

**Visible to the same people** (ADR-0059 Q5): the new check is the first one's submitter's, so
its submitter and the tenant's admins may see it, whoever asked for it. Whoever asked must be
allowed to see the first one (404 otherwise, as everywhere: nothing confirms a check that is not
theirs) and, offshore, must be a registered build-phase tester, as for a submission
(`assert_submission_allowed`, ADR-0032).

**Then the check runs as a normal one does.** This route records the check and calls no model;
the new check's page runs the claims and then the findings by the same two requests a submitted
check's page does (`check_claims.py`, `check_findings.py`), and the findings run reads the
tenant's approved mappings and derives from them (`assessment/mapping_derivation.py`). The page
the button leads to starts the run itself (`static/check_run.js`, the `#run-now` address).

Only a check that has a report can be checked again (409 `not_reported`): there is nothing to
change in a check that was never run, and a check that is part-way through its run has its own.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from compliance_rag import telemetry
from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_submission_allowed,
)
from compliance_rag.api.checks import NOT_FOUND, VERSION, create_check, visible_check
from compliance_rag.api.deps import CurrentCaller, services_of

router = APIRouter(prefix="/v1")

NOT_REPORTED: Final = "this check has no report yet, so there is nothing to check again"


@router.post("/checks/{check_id}/recheck")
async def recheck(request: Request, who: CurrentCaller, check_id: str) -> JSONResponse:
    """A new check of the same stored document: 201 with the new check, as a submission's."""
    services = services_of(request)
    settings = services.settings
    with telemetry.span("api.check.recheck", **{"check.rechecks": check_id}) as span:
        # Before anything is read, stored or written: a check that isn't theirs is not found.
        check = await visible_check(services, who, check_id)
        if check is None:
            raise HTTPException(404, NOT_FOUND)
        try:
            assert_submission_allowed(
                generation_region=settings.generation_region,
                submitter=who.member.email,
                testers=settings.offshore_testers,
            )
        except GenerationResidencyError as refused:
            span.set_attribute("check.refused", "residency")
            raise HTTPException(403, {"error": "residency", "message": str(refused)}) from None
        if await services.ledger.get_findings(who.tenant_id, check_id) is None:
            raise HTTPException(409, {"error": "not_reported", "message": NOT_REPORTED})
        source = await services.clauses.get_source_html(
            check.instrument_id, check.version_id, tenant_id=check.tenant_id
        )
        if source is None:
            raise LookupError(f"{check.instrument_id}:{VERSION}: no stored source")
        titles = await services.ledger.version_titles(
            check.tenant_id, [(check.instrument_id, check.version_id)]
        )
        made = await create_check(
            services,
            span,
            tenant_id=check.tenant_id,
            owner_uid=check.user_id,  # visible to the same people as the first check
            content=source,
            filename=check.filename,
            fmt=check.document_format,
            title=titles.get((check.instrument_id, check.version_id), check.filename),
        )
    return JSONResponse({**made, "rechecks": check_id}, status_code=201)
