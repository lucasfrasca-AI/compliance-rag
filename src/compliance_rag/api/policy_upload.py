"""What an admin's request says, and every refusal that comes before a policy is written (#303,
ADR-0053 U4 and U5).

**The request has no `synthetic` field** (#78, ADR-0026): an upload is `{filename, content, title,
effective_from?, backdate_reason?}` and refuses any other key (422), and no instrument id, since the
policy is named by the route, never by the body. Whether the document is synthetic is the operator's
registry's call alone, made inside `CorpusLifecycle.publish_version`.

**Order of refusals for an upload**, each before anything is written (`policy_ops.publish_policy`
audits each as `refused`):
1. the policy named (a new version only): 404 if the tenant has no such policy;
2. offshore, an admin who is not a registered build-phase tester: 403 `residency` (ADR-0032), before
   the body is read;
3. the raw request over `MAX_REQUEST_BYTES`: 413; a body that is not JSON of the one shape: 422; the
   document over `MAX_DOCUMENT_BYTES` in UTF-8: 413; a file that is not Markdown: 415 (a PDF is
   #307's, ADR-0053 U2) (`uploads.py`, shared with the Check route);
4. the effective date: out of range, or in the past with no `backdate_reason`, or a reason with a
   date that is not in the past: 422; a date on or before the effective date of the version it
   would replace: 422 (a version cannot end before it starts: ledger `valid_to > valid_from`);
5. the document read as numbered clauses (`corpus/policy_parser.py`): 422 if it holds none;
6. `CorpusLifecycle.check_publishable`: the title, the class, one owner, and offshore the whole file
   being a registered synthetic policy fixture and every chunk and clause registered (ADR-0026): 403
   `residency`, 422 otherwise.

**Which version it replaces** is `policy_versions.superseded_by_new` of the policy the admin chose,
never a version found by title. The version id is the next `v<n>` of that policy
(`policy_versions.next_version_id`), so the admin names no id. A new policy's instrument id is
minted here, random and never made from its title (`mint_policy_id`).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Request
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
)

from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_policy_upload_allowed,
)
from compliance_rag.adapters.interfaces import PolicyVersionRow
from compliance_rag.api.deps import EARLIEST, LATEST, Caller, services_of, today
from compliance_rag.api.policy_audit import PolicyRefused
from compliance_rag.api.uploads import (
    MAX_FILENAME_CHARS,
    MAX_TITLE_CHARS,
    check_document_size,
    check_file_name,
    check_text,
    format_of,
    one_line,
    parse_body,
    read_body,
)
from compliance_rag.corpus.models import (
    ClauseRecord,
    DocumentClass,
    validate_instrument_id,
    validate_title,
)
from compliance_rag.corpus.policy_parser import parse_policy_markdown
from compliance_rag.corpus.policy_versions import (
    PolicyGroup,
    next_version_id,
    superseded_by_new,
)
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.submitted_parser import SubmittedFormat

MAX_REASON_CHARS = 500
UNSUPPORTED_FORMAT = (
    "only a Markdown policy (.md, .markdown) can be uploaded: a PDF is not supported yet "
    "(ADR-0053 U2), and a policy is read from its numbered sections and clauses"
)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
"""ASCII digits only: `\\d` also matches the digits of other scripts."""


def _trimmed(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


Reason = Annotated[
    str,
    BeforeValidator(_trimmed),
    StringConstraints(min_length=1, max_length=MAX_REASON_CHARS),
    AfterValidator(one_line),
]
"""A reason: trimmed, then one non-blank line. One type for the JSON and for the page's form, so
the two layers agree on what is recorded and what is refused."""


class PolicyUpload(BaseModel):
    """What an admin uploads: a Markdown policy, its title and when it takes effect. Five fields
    and no others (`extra="forbid"`): no `synthetic` (#78), no instrument id."""

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
        return check_file_name(v)

    @field_validator("title")
    @classmethod
    def _a_title(cls, v: str) -> str:
        return validate_title(v)

    @field_validator("content")
    @classmethod
    def _text(cls, v: str) -> str:
        return check_text(v)

    @field_validator("effective_from", mode="before")
    @classmethod
    def _an_iso_date(cls, v: object) -> object:
        """`2026-12-01` and nothing else: no timestamp, no number of days or seconds, no digits
        of another script."""
        if v is None:
            return None
        if not isinstance(v, str) or _ISO_DATE.fullmatch(v) is None:
            raise ValueError("a date is YYYY-MM-DD")
        return dt.date.fromisoformat(v)


class Withdrawal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Reason


class Purge(BaseModel):
    """A purge names why, and the number of findings the admin was shown as citing the version:
    the lifecycle refuses unless it is the number now (ADR-0021)."""

    model_config = ConfigDict(extra="forbid")
    reason: Reason
    acknowledged_findings_citing: StrictInt = Field(ge=0)


def mint_policy_id(tenant_id: str) -> str:
    """A new policy's instrument id: random, so never made from its title (ADR-0053 U4), and
    checked as an instrument id here, where it is made, rather than trusted to be one."""
    return validate_instrument_id(f"{tenant_id}/policy-{uuid.uuid4().hex[:12]}")


async def read_upload(request: Request, who: Caller) -> PolicyUpload:
    """The admin's upload, past the refusals that need only the request: who may send it, its size,
    its shape, its format. Raises `UploadRefused`."""
    services = services_of(request)
    try:
        assert_policy_upload_allowed(
            generation_region=services.settings.generation_region,
            uploader=who.member.email,
            testers=services.settings.offshore_testers,
        )
    except GenerationResidencyError as refused:
        raise PolicyRefused(403, "residency", str(refused)) from None
    upload = parse_body(PolicyUpload, await read_body(request, noun="policy"))
    check_document_size(len(upload.content.encode("utf-8")), noun="policy")
    format_of(upload.filename, accepted=(SubmittedFormat.MARKDOWN,), message=UNSUPPORTED_FORMAT)
    return upload


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


def effective_date(
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


async def prepare(
    request: Request, who: Caller, upload: PolicyUpload, group: PolicyGroup | None
) -> Prepared:
    """The rest of the refusals, in the order of the module docstring: the date, the clauses, and
    what the lifecycle refuses before it writes. `group` is the policy a new version is for, None
    for a new policy (which gets a fresh instrument id). Raises `PolicyRefused`."""
    versions = () if group is None else group.versions
    instrument_id = mint_policy_id(who.tenant_id) if group is None else group.instrument_id
    version_id = next_version_id(v.version_id for v in versions)
    raw = upload.content.encode("utf-8")
    effective_from, replaces = effective_date(upload, versions)
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
    try:
        await services_of(request).lifecycle.check_publishable(
            clauses,
            count_tokens=count_reranker_tokens,
            instrument_id=instrument_id,
            tenant_id=who.tenant_id,
            version_id=version_id,
            document_class=DocumentClass.INTERNAL_POLICY,
            title=upload.title,
            uploaded_bytes=raw,
        )
    except GenerationResidencyError as refused:
        raise PolicyRefused(403, "residency", str(refused)) from None
    except ValueError as refused:
        raise PolicyRefused(422, "refused", str(refused)) from None
    return Prepared(instrument_id, version_id, upload, raw, effective_from, replaces, clauses)
