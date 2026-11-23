"""The admin's policy versions: upload, withdraw and purge (#303; ADR-0053 U4, U5, U6 and I5;
ADR-0021).

**Two surfaces over one operation each.** `GET /admin/policies` and the version page
`GET /admin/policies/<policy>/versions/<version>` are server-rendered (`templates/policies.html`,
`policy_version.html`); the JSON under `/v1/admin/policies` is what the page's script
(`static/admin_policies.js`) posts to and what a client may call. The page's withdraw and purge
forms post to `/admin/policies/...` and call the very functions the JSON routes call, so the page
enforces nothing of its own. Every route is admin only (`CurrentAdmin`, or `PageAdmin` for a page:
a member gets 403 and nothing happens).

**A version is never matched by title** (ADR-0053 U4). The routes name a policy by its slug, the
part of its instrument id after the tenant's prefix (`acme/privacy-policy` is `privacy-policy`),
which the page puts in each row's own button and form. "New policy" (`POST /v1/admin/policies`)
has no way to name an existing one: it makes a new instrument id from a random suffix, so a
document whose title equals an existing policy's title is a second policy with the same title,
never a new version of the first. "New version" (`POST /v1/admin/policies/<slug>/versions`) takes
its policy from the path and ignores the document's title, which is recorded as that version's
own (ADR-0049).

**The request has no `synthetic` field** (#78, ADR-0026): the body is `{filename, content, title,
effective_from?, backdate_reason?}` and refuses any other key (422). Whether the document is
synthetic is the operator's registry's call alone, made inside `CorpusLifecycle.publish_version`.

**Order of refusals for an upload**, each before anything is written, each audited:
1. the policy named (a new version only): 404 if the tenant has no such policy;
2. offshore, an admin who is not a registered build-phase tester: 403 `residency` (ADR-0032),
   before the body is read;
3. the raw request over `MAX_REQUEST_BYTES`: 413, counted as the body streams in;
4. a body that is not JSON of the one shape: 422, never echoing it;
5. the document over `MAX_DOCUMENT_BYTES` in UTF-8: 413, before it is parsed;
6. a file that is not Markdown: 415 (a PDF is #307's, ADR-0053 U2);
7. the effective date: out of range, or in the past with no `backdate_reason`, or a reason with
   a date that is not in the past: 422; a date on or before the effective date of the version
   it would replace: 422 (a version cannot end before it starts);
8. the document read as numbered clauses (`corpus/policy_parser.py`): 422 if it holds none;
9. `CorpusLifecycle.check_publishable`: the title, the class, one owner, and offshore the whole
   file being a registered synthetic policy fixture and every chunk and clause registered
   (ADR-0026): 403 `residency`, 422 otherwise.
Then `publish_version`, which writes the clause store, then retrieval, then the ledger (ADR-0048),
retiring the version it replaces from the effective date, so a future date leaves it in force until
then.

**Which version it replaces** is `policy_versions.superseded_by_new` of the policy the admin chose,
never a version found by title. The version id is the next `v<n>` of that policy
(`policy_versions.next_version_id`), so the admin names no id.

**Audit.** Every outcome writes a row to the ledger's `corpus_removal_audit` (ledger 0029): who,
when (the row's `recorded_at`), what, the reason if any, and the request's `trace_id`. An upload
writes `refused`, or `authorized` before any store is touched and then `completed` or `failed`;
withdraw and purge are audited by the lifecycle (ADR-0021), and this module writes the refusals
the lifecycle never sees (a version that doesn't exist, one already withdrawn or purged, a typed
count that is not a number). A refusal's row names a reason code and words, never a document's
text.

**Purge** (ADR-0021) shows the count of findings that cite the version, from
`CorpusLifecycle.preview_removal`, and takes the admin's typed count
(`acknowledged_findings_citing`): the lifecycle refuses unless it equals the count now, so a
finding written between the page and the click forces a fresh look. The refusal is audited by the
lifecycle.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
)

from compliance_rag import telemetry
from compliance_rag.adapters.corpus_lifecycle import RemovalRefused
from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_policy_upload_allowed,
)
from compliance_rag.adapters.interfaces import (
    PolicyVersionRow,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
)
from compliance_rag.api.admin import PageAdmin
from compliance_rag.api.checks import (
    MAX_DOCUMENT_BYTES,
    MAX_FILENAME_CHARS,
    MAX_REQUEST_BYTES,
    MAX_TITLE_CHARS,
)
from compliance_rag.api.deps import EARLIEST, LATEST, Caller, CurrentAdmin, services_of, today
from compliance_rag.api.pages import PAGE_HEADERS, PageError, render
from compliance_rag.corpus.models import (
    ClauseRecord,
    DocumentClass,
    validate_instrument_id,
    validate_title,
)
from compliance_rag.corpus.policy_parser import parse_policy_markdown
from compliance_rag.corpus.policy_versions import (
    STATUS_WORDS,
    PolicyGroup,
    VersionStatus,
    group_policies,
    next_version_id,
    superseded_by_new,
    version_status,
)
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.submitted_parser import SubmittedFormat, format_of_filename

api_router = APIRouter(prefix="/v1/admin/policies")
page_router = APIRouter(prefix="/admin/policies")

PAGE_PATH = "/admin/policies"
MAX_REASON_CHARS = 500
MAX_FORM_BYTES = 4_096
MAX_AUDIT_DETAIL = 500
UNSUPPORTED_FORMAT = (
    "only a Markdown policy (.md, .markdown) can be uploaded: a PDF is not supported yet "
    "(ADR-0053 U2), and a policy is read from its numbered sections and clauses"
)
_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
_COUNT = re.compile(r"[0-9]{1,9}")
NOTICES = {
    "published": "Published. The new version is listed below.",
    "withdrawn": "Withdrawn. It is out of retrieval at every date, and its text is kept, so "
    "earlier citations still open.",
    "purged": "Purged. Its text and its search entries are deleted; the ledger keeps a marker "
    "of who purged it, when and why.",
}
"""What a page says after a write, picked by a fixed key (`?done=`), never by text from the
request."""


class PolicyRefused(Exception):
    """A request the admin's policy routes refuse, in the words and status it is answered with.
    Nothing was written to a store."""

    def __init__(
        self,
        status: int,
        error: str,
        message: str,
        *,
        detail: object = None,
        audit: str | None = None,
        audited: bool = False,
        findings_citing: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.detail = detail
        """The JSON body when it is not `{error, message}` (a validation error's list)."""
        self.audit = audit if audit is not None else f"{error}: {message}"
        """What the audit row says; never a document's text."""
        self.audited = audited
        """The lifecycle already wrote this refusal's audit row."""
        self.findings_citing = findings_citing

    def http(self) -> HTTPException:
        body = (
            self.detail
            if self.detail is not None
            else {"error": self.error, "message": self.message}
        )
        return HTTPException(self.status, body)


# ---- what a request says ---------------------------------------------------------------


def _one_line(value: str) -> str:
    """A reason or a name: one line of text with a word in it, so it can't reshape a page or
    a log line (`validate_title` is the rule for titles, in a title's words)."""
    try:
        validate_title(value)
    except ValueError:
        raise ValueError("must be one line of text, with no control characters") from None
    return value


Reason = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_REASON_CHARS), AfterValidator(_one_line)
]


class PolicyUpload(BaseModel):
    """What an admin uploads: a Markdown policy, its title and when it takes effect. Five
    fields and no others (`extra="forbid"`): in particular no `synthetic` (#78) and no
    instrument id, since the policy is named by the route, never by the body."""

    model_config = ConfigDict(extra="forbid")
    filename: Annotated[str, StringConstraints(min_length=1, max_length=MAX_FILENAME_CHARS)]
    content: str
    title: Annotated[str, StringConstraints(min_length=1, max_length=MAX_TITLE_CHARS)]
    effective_from: dt.date | None = None
    """Today in Sydney when absent; a future date is allowed, a past one needs a reason."""
    backdate_reason: Reason | None = None

    @field_validator("filename")
    @classmethod
    def _a_file_name(cls, v: str) -> str:
        if "/" in v or "\\" in v:
            raise ValueError("a file name has no path separator")
        return _one_line(v)

    @field_validator("title")
    @classmethod
    def _a_title(cls, v: str) -> str:
        return validate_title(v)

    @field_validator("content")
    @classmethod
    def _text(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("text may not contain NUL")
        try:
            v.encode("utf-8")  # a lone surrogate (valid JSON) has no UTF-8 form
        except UnicodeEncodeError:
            raise ValueError("text must be valid Unicode") from None
        return v

    @field_validator("effective_from", mode="before")
    @classmethod
    def _an_iso_date(cls, v: object) -> object:
        """`2026-12-01` and nothing else: no timestamp, no number of days or seconds."""
        if v is None:
            return None
        if not isinstance(v, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) is None:
            raise ValueError("a date is YYYY-MM-DD")
        return dt.date.fromisoformat(v)


class Withdrawal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Reason


class Purge(BaseModel):
    """A purge names why, and the number of findings the admin was shown as citing the
    version: the lifecycle refuses unless it is the number now (ADR-0021)."""

    model_config = ConfigDict(extra="forbid")
    reason: Reason
    acknowledged_findings_citing: StrictInt = Field(ge=0)


def _invalid(error: ValidationError | None) -> PolicyRefused:
    """422 in the shape the app's validator handler gives (`app.py`): where and what, never the
    input. `None` is a body that is not JSON at all."""
    if error is None:
        detail = [{"loc": ["body"], "msg": "the body is not valid JSON", "type": "json_invalid"}]
    else:
        detail = [
            {"loc": ["body", *e["loc"]], "msg": str(e["msg"]), "type": e["type"]}
            for e in error.errors()
        ]
    where = ", ".join(".".join(str(p) for p in d["loc"]) for d in detail)
    return PolicyRefused(
        422, "invalid_body", "the body is not valid", detail=detail, audit=f"invalid_body: {where}"
    )


def _parsed[M: BaseModel](model: type[M], body: bytes) -> M:
    try:
        raw: Any = json.loads(body)  # Any: untrusted JSON, validated next
        return model.model_validate(raw)
    except ValidationError as error:
        raise _invalid(error) from None
    except (ValueError, RecursionError):  # not JSON: bad syntax, bad UTF-8, too deep
        raise _invalid(None) from None


def _too_big() -> PolicyRefused:
    return PolicyRefused(
        413,
        "too_large",
        f"the request is over {MAX_REQUEST_BYTES:,} bytes; a policy may be up to "
        f"{MAX_DOCUMENT_BYTES:,} bytes",
    )


async def _body(request: Request) -> bytes:
    """The request's bytes, never more than `MAX_REQUEST_BYTES` of them: a declared length over
    the limit is refused before any is read, and an undeclared or understated one as it streams
    past the limit."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        raise _too_big()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_REQUEST_BYTES:
            raise _too_big()
    return bytes(body)


# ---- the ledger's view of a policy -------------------------------------------------------


@dataclass(slots=True)
class Attempt:
    """What an operation was about, as far as it got: what a refusal's audit row names."""

    action: RemovalAction
    instrument_id: str
    version_id: str | None = None
    reason: str = ""
    findings_citing: int | None = None


def _audit_name(who: Caller, policy: str) -> str:
    """The instrument id a refusal for `policy` is audited under: the id itself when it is the
    form of one, so a typo is traceable, else a placeholder (the audit row is text, and a path
    segment may hold anything)."""
    return f"{who.tenant_id}/{policy}" if _SLUG.fullmatch(policy) else f"{who.tenant_id}/(invalid)"


def _instrument_of(who: Caller, policy: str) -> str:
    instrument_id = f"{who.tenant_id}/{policy}"
    if _SLUG.fullmatch(policy) is None:
        raise PolicyRefused(404, "no_such_policy", "this workspace has no such policy")
    try:
        return validate_instrument_id(instrument_id)
    except ValueError:
        raise PolicyRefused(404, "no_such_policy", "this workspace has no such policy") from None


async def _rows(request: Request, who: Caller) -> tuple[PolicyVersionRow, ...]:
    return await services_of(request).ledger.policy_version_rows(who.tenant_id)


async def _group(request: Request, who: Caller, policy: str) -> PolicyGroup:
    """The tenant's policy `policy`, or a 404 for a name the ledger doesn't hold as one of
    this tenant's internal policies. Chosen by id, never by title."""
    instrument_id = _instrument_of(who, policy)
    for group in group_policies(await _rows(request, who)):
        if group.instrument_id == instrument_id:
            return group
    raise PolicyRefused(404, "no_such_policy", "this workspace has no such policy")


async def _version(
    request: Request, who: Caller, policy: str, version_id: str
) -> tuple[PolicyGroup, PolicyVersionRow]:
    group = await _group(request, who, policy)
    for row in group.versions:
        if row.version_id == version_id:
            return group, row
    raise PolicyRefused(404, "no_such_version", "this policy has no such version")


async def _audit(
    request: Request,
    who: Caller,
    attempt: Attempt,
    outcome: RemovalOutcome,
    *,
    detail: str = "",
) -> None:
    """One row of the append-only audit, from this request (its trace id), by this admin."""
    await services_of(request).ledger.record_removal_audit(
        RemovalAuditEntry(
            audit_id=uuid.uuid4().hex,
            tenant_id=who.tenant_id,
            instrument_id=attempt.instrument_id,
            version_id=attempt.version_id,
            action=attempt.action,
            outcome=outcome,
            actor=who.member.email,
            reason=attempt.reason,
            findings_citing=attempt.findings_citing,
            detail=detail[:MAX_AUDIT_DETAIL],
            trace_id=telemetry.current_trace_id_or_none(),
        )
    )


async def _audited[T](request: Request, who: Caller, attempt: Attempt, work: Awaitable[T]) -> T:
    """`work`, with a `PolicyRefused` from it audited as `refused` (once: not again if the
    lifecycle already wrote its own row) and raised on."""
    try:
        return await work
    except PolicyRefused as refused:
        if not refused.audited:
            if refused.findings_citing is not None:
                attempt.findings_citing = refused.findings_citing
            await _audit(request, who, attempt, RemovalOutcome.REFUSED, detail=refused.audit)
            refused.audited = True
        raise


# ---- upload ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Published:
    instrument_id: str
    version_id: str
    title: str
    effective_from: dt.date
    supersedes: str | None
    clauses: int
    backdate_reason: str | None


def slug_of(instrument_id: str) -> str:
    return instrument_id.partition("/")[2]


def version_path(instrument_id: str, version_id: str) -> str:
    return f"{PAGE_PATH}/{slug_of(instrument_id)}/versions/{version_id}"


@dataclass(frozen=True, slots=True)
class Prepared:
    """An upload that has passed every refusal that comes before a write."""

    instrument_id: str
    version_id: str
    upload: PolicyUpload
    raw: bytes
    effective_from: dt.date
    replaces: PolicyVersionRow | None
    clauses: tuple[ClauseRecord, ...]


async def publish_policy(request: Request, who: Caller, *, policy: str | None) -> Published:
    """A new policy (`policy` None) or a new version of the one the admin chose, from this
    request's body: every refusal before a write, the write through the lifecycle, every
    outcome audited (module docstring). Raises `PolicyRefused`."""
    services = services_of(request)
    with telemetry.span("api.admin.policy.publish", **{"policy.new": policy is None}) as span:
        attempt = Attempt(
            RemovalAction.PUBLISH,
            # a new policy gets a fresh id; never one made from its title (ADR-0053 U4)
            f"{who.tenant_id}/policy-{uuid.uuid4().hex[:12]}"
            if policy is None
            else _audit_name(who, policy),
        )
        prepared = await _audited(request, who, attempt, _prepare(request, who, policy, attempt))
        span.set_attribute("policy.version", f"{prepared.instrument_id}:{prepared.version_id}")
        span.set_attribute("policy.document_bytes", len(prepared.raw))
        span.set_attribute("policy.clauses", len(prepared.clauses))
        span.set_attribute("policy.backdated", prepared.upload.backdate_reason is not None)
        what = _what(prepared.effective_from, prepared.replaces)
        # Every refusal is behind us: from here the stores are written, audited as the lifecycle
        # audits a removal (ADR-0021): authorized before any is touched, then completed or failed.
        await _audit(request, who, attempt, RemovalOutcome.AUTHORIZED, detail=what)
        try:
            await services.lifecycle.publish_version(
                prepared.clauses,
                count_tokens=count_reranker_tokens,
                source_html=prepared.upload.content,
                instrument_id=prepared.instrument_id,
                tenant_id=who.tenant_id,
                version_id=prepared.version_id,
                effective_from=prepared.effective_from,
                supersedes=None if prepared.replaces is None else prepared.replaces.version_id,
                document_class=DocumentClass.INTERNAL_POLICY,
                title=prepared.upload.title,
                backdate_reason=prepared.upload.backdate_reason,
                uploaded_bytes=prepared.raw,
            )
        except Exception as failed:
            await _audit(
                request,
                who,
                attempt,
                RemovalOutcome.FAILED,
                detail=f"{what}; {type(failed).__name__}",
            )
            if isinstance(failed, ValueError):
                # Another upload got the version id first, or the stores disagree: nothing here
                # was overwritten (the lifecycle refuses to), and a second try takes the next id.
                raise PolicyRefused(
                    409,
                    "conflict",
                    "another upload took this version first, or the stores hold part of it: "
                    "reload the page and try again",
                    audited=True,
                ) from None
            raise
        await _audit(request, who, attempt, RemovalOutcome.COMPLETED, detail=what)
        return Published(
            instrument_id=prepared.instrument_id,
            version_id=prepared.version_id,
            title=prepared.upload.title,
            effective_from=prepared.effective_from,
            supersedes=None if prepared.replaces is None else prepared.replaces.version_id,
            clauses=len(prepared.clauses),
            backdate_reason=prepared.upload.backdate_reason,
        )


async def _prepare(request: Request, who: Caller, policy: str | None, attempt: Attempt) -> Prepared:
    """Every refusal that comes before a write, in the order of the module docstring."""
    services = services_of(request)
    group = None if policy is None else await _group(request, who, policy)
    if group is not None:
        attempt.instrument_id = group.instrument_id
    instrument_id = attempt.instrument_id
    versions = () if group is None else group.versions
    version_id = next_version_id(v.version_id for v in versions)
    attempt.version_id = version_id
    try:
        assert_policy_upload_allowed(
            generation_region=services.settings.generation_region,
            uploader=who.member.email,
            testers=services.settings.offshore_testers,
        )
    except GenerationResidencyError as refused:
        raise PolicyRefused(403, "residency", str(refused)) from None
    upload = _parsed(PolicyUpload, await _body(request))
    attempt.reason = upload.backdate_reason or ""
    raw = upload.content.encode("utf-8")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise PolicyRefused(
            413,
            "too_large",
            f"the policy is {len(raw):,} bytes; the limit is {MAX_DOCUMENT_BYTES:,}",
        )
    try:
        if format_of_filename(upload.filename) is not SubmittedFormat.MARKDOWN:
            raise ValueError("not Markdown")
    except ValueError:
        raise PolicyRefused(415, "unsupported_format", UNSUPPORTED_FORMAT) from None
    effective_from, replaces = _effective_date(upload, versions)
    try:
        clauses = parse_policy_markdown(
            upload.content,
            instrument_id=instrument_id,
            version_id=version_id,
            tenant_id=who.tenant_id,
        )
    except ValueError:
        # The parser names the line that is wrong, which is the document's own text:
        # said to the admin, never written to the audit row.
        raise PolicyRefused(
            422,
            "unreadable_policy",
            "the policy could not be read as numbered sections and clauses: check that each "
            "clause number matches its section's",
            audit="unreadable_policy",
        ) from None
    if not clauses:
        raise PolicyRefused(
            422,
            "no_clauses",
            "the policy holds no clauses: it needs '## 1. Heading' sections and '1.1 ' "
            "numbered clauses",
        )
    await _gated(
        services.lifecycle.check_publishable(
            clauses,
            count_tokens=count_reranker_tokens,
            instrument_id=instrument_id,
            tenant_id=who.tenant_id,
            version_id=version_id,
            document_class=DocumentClass.INTERNAL_POLICY,
            title=upload.title,
            uploaded_bytes=raw,
        )
    )
    return Prepared(instrument_id, version_id, upload, raw, effective_from, replaces, clauses)


def _what(effective_from: dt.date, replaces: PolicyVersionRow | None) -> str:
    return f"effective {effective_from}" + (
        "" if replaces is None else f"; replaces {replaces.version_id}"
    )


def _effective_date(
    upload: PolicyUpload, versions: tuple[PolicyVersionRow, ...]
) -> tuple[dt.date, PolicyVersionRow | None]:
    """The date the version takes effect and the version it replaces, or the refusal. Today in
    Sydney by default, a future date as it is, a past date only with the reason the admin gave
    (ADR-0053 U4)."""
    day = today()
    effective = upload.effective_from or day
    if not EARLIEST <= effective <= LATEST:
        raise PolicyRefused(
            422, "date_out_of_range", f"the date must be between {EARLIEST} and {LATEST}"
        )
    past = effective < day
    if past and upload.backdate_reason is None:
        raise PolicyRefused(
            422,
            "backdate_reason_required",
            f"{effective} is in the past: a past effective date needs a reason, recorded with "
            "the version",
        )
    if not past and upload.backdate_reason is not None:
        raise PolicyRefused(
            422,
            "backdate_reason_unneeded",
            "a reason is recorded only for an effective date in the past",
        )
    replaces = superseded_by_new(versions)
    if replaces is not None and effective <= replaces.valid_from:
        raise PolicyRefused(
            422,
            "effective_not_after_current",
            f"{replaces.version_id} takes effect on {replaces.valid_from}: a new version must "
            "take effect after the one it replaces. Withdraw that version, or choose a later date",
        )
    return effective, replaces


async def _gated(check: Awaitable[None]) -> None:
    """What the lifecycle refuses before it writes, as the refusal an admin is answered with."""
    try:
        await check
    except GenerationResidencyError as refused:
        raise PolicyRefused(403, "residency", str(refused)) from None
    except ValueError as refused:
        raise PolicyRefused(422, "refused", str(refused)) from None


# ---- withdraw and purge ------------------------------------------------------------------


async def withdraw_version(
    request: Request, who: Caller, policy: str, version_id: str, reason: str, attempt: Attempt
) -> None:
    """ADR-0021's withdraw: out of retrieval at every date, text kept. The lifecycle audits it;
    a refusal here is the caller's to audit (`_audited` over `attempt`)."""
    with telemetry.span(
        "api.admin.policy.withdraw", **{"policy.version": f"{policy}:{version_id}"}
    ):
        group, row = await _version(request, who, policy, version_id)
        attempt.instrument_id = group.instrument_id
        if row.purged:
            raise PolicyRefused(
                409, "purged", "this version is purged: there is nothing to withdraw"
            )
        if row.withdrawn:
            raise PolicyRefused(409, "already_withdrawn", "this version is already withdrawn")
        await _removal(
            services_of(request).lifecycle.withdraw(
                tenant_id=who.tenant_id,
                instrument_id=group.instrument_id,
                version_id=version_id,
                actor=who.member.email,
                reason=reason,
            )
        )


async def findings_citing(
    request: Request, who: Caller, instrument_id: str, version_id: str
) -> int:
    """The number of findings that cite the version: what a purge must be acknowledged with."""
    shown = await services_of(request).lifecycle.preview_removal(
        tenant_id=who.tenant_id, instrument_id=instrument_id, version_id=version_id
    )
    return shown.findings_citing


async def purge_version(
    request: Request,
    who: Caller,
    policy: str,
    version_id: str,
    reason: str,
    typed: int | None,
    attempt: Attempt,
) -> None:
    """ADR-0021's purge, behind the typed count: `typed` is the number of findings the admin
    says cite the version, None when what they typed was not a number. The lifecycle refuses
    unless it is the number now, and audits that refusal; any other refusal is the caller's to
    audit (`_audited` over `attempt`)."""
    with telemetry.span("api.admin.policy.purge", **{"policy.version": f"{policy}:{version_id}"}):
        group, row = await _version(request, who, policy, version_id)
        attempt.instrument_id = group.instrument_id
        if row.purged:
            raise PolicyRefused(409, "already_purged", "this version is already purged")
        if typed is None:
            raise PolicyRefused(
                422,
                "count_not_a_number",
                "type the number of findings that cite this version, as numerals",
                audit="count_not_a_number",
            )
        await _removal(
            services_of(request).lifecycle.purge(
                tenant_id=who.tenant_id,
                instrument_id=group.instrument_id,
                version_id=version_id,
                actor=who.member.email,
                reason=reason,
                acknowledged_findings_citing=typed,
            )
        )


async def _removal(removal: Awaitable[None]) -> None:
    """A lifecycle removal, its refusal (already audited by the lifecycle) as the 409 the admin is
    answered with. Any other failure is audited `failed` by the lifecycle and raised."""
    try:
        await removal
    except RemovalRefused as refused:
        raise PolicyRefused(409, "refused", str(refused), audited=True) from None
    # Any other failure was audited `failed` by the lifecycle, and is raised as it is.


# ---- JSON ------------------------------------------------------------------------------


def version_json(row: PolicyVersionRow, day: dt.date) -> dict[str, object]:
    status = version_status(row, day)
    return {
        "version_id": row.version_id,
        "title": row.title,
        "effective_from": row.valid_from.isoformat(),
        "effective_to": None if row.valid_to is None else row.valid_to.isoformat(),
        "status": status.value,
        "backdate_reason": row.backdate_reason,
        "href": version_path(row.instrument_id, row.version_id),
    }


@api_router.get("")
async def list_policies(request: Request, who: CurrentAdmin) -> JSONResponse:
    day = today()
    groups = group_policies(await _rows(request, who))
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


@api_router.post("", status_code=201)
async def new_policy(request: Request, who: CurrentAdmin) -> JSONResponse:
    """A new policy: always a new instrument, whatever its title is (ADR-0053 U4)."""
    try:
        return _published_json(await publish_policy(request, who, policy=None))
    except PolicyRefused as refused:
        raise refused.http() from None


@api_router.post("/{policy}/versions", status_code=201)
async def new_version(request: Request, who: CurrentAdmin, policy: str) -> JSONResponse:
    """A new version of the policy the path names, never one found by title."""
    try:
        return _published_json(await publish_policy(request, who, policy=policy))
    except PolicyRefused as refused:
        raise refused.http() from None


@api_router.get("/{policy}/versions/{version_id}/preview")
async def preview_route(
    request: Request, who: CurrentAdmin, policy: str, version_id: str
) -> JSONResponse:
    try:
        group, _ = await _version(request, who, policy, version_id)
        count = await findings_citing(request, who, group.instrument_id, version_id)
    except PolicyRefused as refused:
        raise refused.http() from None
    return JSONResponse({"findings_citing": count})


@api_router.post("/{policy}/versions/{version_id}/withdraw")
async def withdraw_route(
    request: Request, who: CurrentAdmin, policy: str, version_id: str
) -> JSONResponse:
    attempt = Attempt(RemovalAction.WITHDRAW, _audit_name(who, policy), version_id)

    async def withdraw() -> None:
        withdrawal = _parsed(Withdrawal, await _body(request))
        attempt.reason = withdrawal.reason
        await withdraw_version(request, who, policy, version_id, withdrawal.reason, attempt)

    try:
        await _audited(request, who, attempt, withdraw())
    except PolicyRefused as refused:
        raise refused.http() from None
    return JSONResponse({"status": VersionStatus.WITHDRAWN.value})


@api_router.post("/{policy}/versions/{version_id}/purge")
async def purge_route(
    request: Request, who: CurrentAdmin, policy: str, version_id: str
) -> JSONResponse:
    attempt = Attempt(RemovalAction.PURGE, _audit_name(who, policy), version_id)

    async def purge() -> None:
        body = _parsed(Purge, await _body(request))
        attempt.reason = body.reason
        await purge_version(
            request,
            who,
            policy,
            version_id,
            body.reason,
            body.acknowledged_findings_citing,
            attempt,
        )

    try:
        await _audited(request, who, attempt, purge())
    except PolicyRefused as refused:
        raise refused.http() from None
    return JSONResponse({"status": VersionStatus.PURGED.value})


# ---- pages -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VersionView:
    row: PolicyVersionRow
    status: str
    status_words: str
    href: str
    active: bool
    """In force or future: a version a reader may still withdraw or purge for a reason."""


def _views(group: PolicyGroup, day: dt.date) -> list[VersionView]:
    views: list[VersionView] = []
    for row in group.versions:
        status = version_status(row, day)
        views.append(
            VersionView(
                row=row,
                status=status.value,
                status_words=STATUS_WORDS[status],
                href=version_path(row.instrument_id, row.version_id),
                active=status in (VersionStatus.IN_FORCE, VersionStatus.FUTURE),
            )
        )
    return views


@dataclass(frozen=True, slots=True)
class PolicyView:
    group: PolicyGroup
    versions: list[VersionView]


def _done(request: Request) -> str | None:
    return NOTICES.get(request.query_params.get("done", ""))


@page_router.get("", response_class=HTMLResponse)
async def policies_page(request: Request, who: PageAdmin) -> HTMLResponse:
    day = today()
    groups = group_policies(await _rows(request, who))
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
        refused.message[0].upper() + refused.message[1:] + ".",
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
        group, row = await _version(request, who, policy, version_id)
        count = await findings_citing(request, who, group.instrument_id, version_id)
    except PolicyRefused as refused:
        raise _not_found(refused) from None
    day = today()
    view = next(v for v in _views(group, day) if v.row.version_id == version_id)
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


@page_router.get("/{policy}/versions/{version_id}", response_class=HTMLResponse)
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


def _redirect(policy: str, version_id: str, done: str) -> RedirectResponse:
    return RedirectResponse(
        f"{PAGE_PATH}/{policy}/versions/{version_id}?done={done}",
        status_code=303,
        headers=PAGE_HEADERS,
    )


def _reason_of(fields: dict[str, str]) -> str:
    """The reason a form carries, held to the same rule as the JSON's, or a `PolicyRefused`."""
    try:
        return Withdrawal(reason=fields.get("reason", "").strip()).reason
    except ValidationError:
        raise PolicyRefused(
            422, "invalid_reason", "give a reason, in one line of up to 500 characters"
        ) from None


@page_router.post("/{policy}/versions/{version_id}/withdraw")
async def withdraw_page(request: Request, who: PageAdmin, policy: str, version_id: str) -> Response:
    fields = await _form(request)
    attempt = Attempt(RemovalAction.WITHDRAW, _audit_name(who, policy), version_id)

    async def withdraw() -> None:
        reason = _reason_of(fields)
        attempt.reason = reason
        await withdraw_version(request, who, policy, version_id, reason, attempt)

    try:
        await _audited(request, who, attempt, withdraw())
    except PolicyRefused as refused:
        if refused.status == 404:
            raise _not_found(refused) from None
        return await _version_page(
            request, who, policy, version_id, notice=refused.message, status=refused.status
        )
    return _redirect(policy, version_id, "withdrawn")


@page_router.post("/{policy}/versions/{version_id}/purge")
async def purge_page(request: Request, who: PageAdmin, policy: str, version_id: str) -> Response:
    fields = await _form(request)
    attempt = Attempt(RemovalAction.PURGE, _audit_name(who, policy), version_id)
    typed_text = fields.get("typed_count", "").strip()
    typed = int(typed_text) if _COUNT.fullmatch(typed_text) else None

    async def purge() -> None:
        reason = _reason_of(fields)
        attempt.reason = reason
        await purge_version(request, who, policy, version_id, reason, typed, attempt)

    try:
        await _audited(request, who, attempt, purge())
    except PolicyRefused as refused:
        if refused.status == 404:
            raise _not_found(refused) from None
        return await _version_page(
            request, who, policy, version_id, notice=refused.message, status=refused.status
        )
    return _redirect(policy, version_id, "purged")
