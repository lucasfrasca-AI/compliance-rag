"""Submitting a document to be checked (#297, ADR-0053 U1, U2 and U5, ADR-0059).

`POST /v1/checks` takes a Markdown or plain-text document from a member and
stores it as a `submitted_document` through `CorpusLifecycle.submit_document`
and nothing else (#296: no store is written from here), then records the check
in the ledger. **No model is called**: claim extraction is its own request
(`check_claims.py`, #298) and findings another (`check_findings.py`, #299).
`GET /v1/checks/{id}` reads the stored
document back, passage by passage, with its claims and its report once it has them
(`check_report.py`, which owns that route, #300). The pages are `check_pages.py`'s.

**The request has no `synthetic` field** (#78, ADR-0026). The body is
`{filename, content, title?}` and refuses any other key (422), so an upload
cannot say its own content is synthetic. Whether it is, is the operator's
registry's call alone, made inside `submit_document` from the passages the
segmenter cut: a document byte-identical to a registered fixture passes the
residency gate, and any other is refused offshore before a byte is written.

**Order of refusals**, each before anything is written and each before the next
costs anything:
1. the sign-in (401, 403, 503, `deps.py`);
2. offshore, a submitter who is not a registered build-phase tester: 403
   `residency` (ADR-0032), before the body is read;
3. the raw request over `MAX_REQUEST_BYTES`: 413, counted as the body streams
   in and never parsed past the limit (a declared `Content-Length` over it is
   refused before a byte is read);
4. a body that is not JSON of the one shape: 422 (never echoing it);
5. the document over `MAX_DOCUMENT_BYTES` in UTF-8: 413, the bytes counted
   before the document is cut into passages;
6. a file that is not Markdown or plain text: 415 (a PDF is #307's, DOCX waits
   for a customer to name it, ADR-0053 U2);
7. in `submit_document`, before anything is written: a document with no words,
   or cut into more than `MAX_PASSAGES` passages (the lifecycle's own limit,
   `corpus/submitted_parser.py`, so every caller is bound by it): 422;
8. offshore, a passage the synthetic registry does not vouch for, or a document
   that is not a registered fixture byte for byte: 403 `residency`, naming the
   first passage's id and never its text.

**Two writes, in an order that fails safe.** The document first (the
lifecycle's ledger record is its commit point, ADR-0048), then the check. A
request that dies between them leaves a stored document with no check: nobody
can open it (the source route serves a submitted document only through its
check), the member sees an error and submits again as a new check, and nothing
of it is retrieved or cited (it is never chunked). The reverse order would
leave a check pointing at nothing, which the ledger refuses.

**Visibility** (ADR-0059 Q5) is `CheckRecord.visible_to`: the submitter, by the
sign-in's uid (ADR-0036), and the tenant's admins. Anyone else gets the same 404
as for a check that does not exist, on the page, here and on
`/v1/sources/<the document>` (`sources.py` asks the same question), so nothing
confirms a check that is not theirs. The ledger and the clause store read by
tenant and by version, not by owner (#176): the rule is applied here, before
either is read, and a submitted document's source is never served on a tenant
match alone. An admin reads another member's passages as their recorded owner,
after the rule has allowed it, because the clause store gives a private clause
to its owner alone (ADR-0036).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry.trace import Span
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from compliance_rag import telemetry
from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_submission_allowed,
)
from compliance_rag.adapters.interfaces import CheckRecord, ClauseStore
from compliance_rag.api.deps import (
    UUID4_HEX,
    ApiServices,
    Caller,
    CurrentCaller,
    is_admin,
    services_of,
    today,
)
from compliance_rag.api.uploads import (
    MAX_DOCUMENT_BYTES as MAX_DOCUMENT_BYTES,
)
from compliance_rag.api.uploads import (
    MAX_FILENAME_CHARS as MAX_FILENAME_CHARS,
)
from compliance_rag.api.uploads import (
    MAX_REQUEST_BYTES as MAX_REQUEST_BYTES,
)
from compliance_rag.api.uploads import (
    MAX_TITLE_CHARS as MAX_TITLE_CHARS,
)
from compliance_rag.api.uploads import (
    UploadRefused,
    check_document_size,
    check_file_name,
    check_text,
    format_of,
    parse_body,
    read_body,
)
from compliance_rag.corpus.models import (
    ClauseRecord,
    clause_anchor_id,
    stable_id,
    validate_title,
)
from compliance_rag.corpus.submitted_parser import (
    EmptyDocument,
    SubmittedFormat,
    TooManyPassages,
    passage_number,
)

router = APIRouter(prefix="/v1")

VERSION = "v1"
"""A check's document is its instrument's first and only version: a changed
document is submitted again, as a new check."""
READ_CONCURRENCY = 16
"""Clause-store reads in flight at once while a page is drawn (as
`CorpusLifecycle`'s, `_CONCURRENT_READS`)."""
NOT_FOUND = "no such check"
_CHECK_ID = re.compile(UUID4_HEX)

UNSUPPORTED_FORMAT = (
    "only Markdown (.md, .markdown) and plain text (.txt) can be checked: a PDF is not supported "
    "yet, and Word documents are not supported"
)


class CheckSubmission(BaseModel):
    """What a member submits. Three fields and no others (`extra="forbid"`):
    in particular no `synthetic`, which the operator's registry alone decides
    (#78), so a body that names one is a 422."""

    model_config = ConfigDict(extra="forbid")
    filename: Annotated[str, StringConstraints(min_length=1, max_length=MAX_FILENAME_CHARS)]
    content: str
    title: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TITLE_CHARS)] | None = None

    @field_validator("filename")
    @classmethod
    def _a_file_name(cls, v: str) -> str:
        return check_file_name(v)

    @field_validator("title")
    @classmethod
    def _a_title(cls, v: str | None) -> str | None:
        return v if v is None else validate_title(v)

    @field_validator("content")
    @classmethod
    def _text(cls, v: str) -> str:
        return check_text(v)


def _refusal(status: int, error: str, message: str) -> HTTPException:
    return HTTPException(status, {"error": error, "message": message})


async def _body(request: Request) -> bytes:
    """The request's bytes, at most `MAX_REQUEST_BYTES` (`uploads.read_body`, which the policy
    routes share), a refusal as the 413 this route answers."""
    try:
        return await read_body(request, noun="document")
    except UploadRefused as refused:
        raise refused.http() from None


def _submission(body: bytes) -> CheckSubmission:
    try:
        return parse_body(CheckSubmission, body)
    except UploadRefused as refused:
        raise refused.http() from None


def _format_of(filename: str) -> SubmittedFormat:
    try:
        return format_of(
            filename,
            accepted=(SubmittedFormat.MARKDOWN, SubmittedFormat.TEXT),
            message=UNSUPPORTED_FORMAT,
        )
    except UploadRefused as refused:
        raise refused.http() from None


def source_path(instrument_id: str) -> str:
    return f"/v1/sources/{instrument_id}/{VERSION}"


@router.post("/checks", status_code=201)
async def submit_check(request: Request, who: CurrentCaller) -> JSONResponse:
    services = services_of(request)
    settings = services.settings
    with telemetry.span("api.check.submit") as span:
        try:
            assert_submission_allowed(
                generation_region=settings.generation_region,
                submitter=who.member.email,
                testers=settings.offshore_testers,
            )
        except GenerationResidencyError as refused:
            span.set_attribute("check.refused", "residency")
            raise _refusal(403, "residency", str(refused)) from None
        submission = _submission(await _body(request))
        size = len(submission.content.encode("utf-8"))
        span.set_attribute("check.document_bytes", size)
        try:
            check_document_size(size, noun="document")
        except UploadRefused as refused:
            span.set_attribute("check.refused", "document_too_large")
            raise refused.http() from None
        fmt = _format_of(submission.filename)
        span.set_attribute("check.format", fmt.value)
        made = await create_check(
            services,
            span,
            tenant_id=who.tenant_id,
            owner_uid=who.member.uid,
            content=submission.content,
            filename=submission.filename,
            fmt=fmt,
            title=submission.title or submission.filename,
        )
    return JSONResponse(made, status_code=201)


async def create_check(
    services: ApiServices,
    span: Span,
    *,
    tenant_id: str,
    owner_uid: str,
    content: str,
    filename: str,
    fmt: SubmittedFormat,
    title: str,
) -> dict[str, object]:
    """Store a document as a new check's `submitted_document` and record the check, the two
    writes in the order that fails safe (the module docstring), and say what was made. The one
    place a check is made: a submission (`POST /v1/checks`) and a re-check of a stored document
    (`check_recheck.py`, #306) both come here, so a re-check is a check like any other, in a
    version, a manifest and a ledger row of its own. `owner_uid` is whose it is (ADR-0059 Q5):
    the submitter's, who may see it with the tenant's admins. Refused as `submit_check` is, in
    the lifecycle's own order."""
    check_id = uuid.uuid4().hex
    instrument_id = f"{tenant_id}/check-{check_id}"
    span.set_attribute("check.check_id", check_id)
    try:
        # The one write: the lifecycle stores the passages, the source and
        # the version, and refuses offshore any passage the synthetic
        # registry does not vouch for before it writes anything.
        passages = await services.lifecycle.submit_document(
            content,
            document_format=fmt,
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=VERSION,
            submitted_on=today(),
            submitted_by=owner_uid,
            title=title,
        )
    except EmptyDocument:
        raise _refusal(422, "no_words", "the document has no words to check") from None
    except TooManyPassages as refused:
        span.set_attribute("check.refused", "too_many_passages")
        raise _refusal(422, "too_many_passages", str(refused)) from None
    except GenerationResidencyError as refused:
        span.set_attribute("check.refused", "residency")
        raise _refusal(403, "residency", str(refused)) from None
    span.set_attribute("check.passages", passages)
    trace_id = telemetry.current_trace_id()
    await services.ledger.record_check(
        CheckRecord(
            tenant_id=tenant_id,
            check_id=check_id,
            user_id=owner_uid,
            instrument_id=instrument_id,
            version_id=VERSION,
            filename=filename,
            document_format=fmt,
            trace_id=trace_id,
        )
    )
    return {
        "check_id": check_id,
        "title": title,
        "filename": filename,
        "format": fmt.value,
        "passages": passages,
        "href": f"/checks/{check_id}",
        "source_href": source_path(instrument_id),
        "trace_id": trace_id,
    }


# ---- reading a check back ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckView:
    """A check the caller may see: its record, its title and its passages in
    document order."""

    record: CheckRecord
    title: str
    passages: tuple[ClauseRecord, ...]
    mine: bool


async def visible_check(services: ApiServices, who: Caller, check_id: str) -> CheckRecord | None:
    """The check, if it exists in the caller's tenant **and** the caller may see
    it (`CheckRecord.visible_to`); None otherwise, the one answer for both, so
    nothing tells a check that is not theirs from one that does not exist. The
    only place the API decides it, for the page, the JSON and the source
    route."""
    if _CHECK_ID.fullmatch(check_id) is None:
        return None
    record = await services.ledger.get_check(who.tenant_id, check_id)
    return record if record is not None and may_see(services, who, record) else None


def may_see(services: ApiServices, who: Caller, record: CheckRecord) -> bool:
    """`CheckRecord.visible_to` with the caller's role read from configuration
    on this request, as `deps.current_admin` reads it."""
    return record.visible_to(who.member.uid, admin=is_admin(services.settings, who.member.email))


async def read_passages(clauses: ClauseStore, record: CheckRecord) -> tuple[ClauseRecord, ...]:
    """The check's stored passages, in document order, read as their recorded
    owner (ADR-0036): the caller was allowed by `visible_check` before this is
    called, and the clause store gives a private passage to its owner alone. A
    passage the manifest lists that the store cannot give is an integrity
    failure (`LookupError`), never a shorter document."""
    manifest = await clauses.get_manifest(
        record.instrument_id, record.version_id, tenant_id=record.tenant_id
    )
    paths = sorted(manifest, key=passage_number)
    reads = asyncio.Semaphore(READ_CONCURRENCY)

    async def read(path: str) -> ClauseRecord | None:
        async with reads:
            return await clauses.get_clause(
                stable_id(record.instrument_id, record.version_id, path),
                tenant_id=record.tenant_id,
                user_id=record.user_id,
            )

    found = await asyncio.gather(*(read(p) for p in paths))
    passages = tuple(p for p in found if p is not None)
    if len(passages) != len(paths):
        raise LookupError(
            f"{record.instrument_id}:{record.version_id}: the manifest lists "
            f"{len(paths) - len(passages)} passage(s) the clause store does not hold"
        )
    return passages


async def read_check(services: ApiServices, who: Caller, check_id: str) -> CheckView | None:
    """A check the caller may see, with its stored document; None when it is not
    theirs or does not exist. The one reader: the JSON route and the page both
    show what this returns."""
    with telemetry.span("api.check.read", **{"check.check_id": check_id}) as span:
        record = await visible_check(services, who, check_id)
        if record is None:
            span.set_attribute("check.found", False)
            return None
        titles = await services.ledger.version_titles(
            record.tenant_id, [(record.instrument_id, record.version_id)]
        )
        passages = await read_passages(services.clauses, record)
        span.set_attribute("check.found", True)
        span.set_attribute("check.passages", len(passages))
        return CheckView(
            record=record,
            title=titles.get((record.instrument_id, record.version_id), record.filename),
            passages=passages,
            mine=record.user_id == who.member.uid,
        )


def passage_json(p: ClauseRecord) -> dict[str, object]:
    return {
        "passage": p.clause_path,
        "anchor": clause_anchor_id(p.clause_path),
        "heading": p.heading,
        "text": p.text,
        "char_start": p.char_start,
        "char_end": p.char_end,
    }
