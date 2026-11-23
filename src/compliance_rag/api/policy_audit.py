"""The refusal and the audit of an admin's policy operations (#303, ADR-0021).

Every outcome of an upload, a withdraw or a purge is a row of the ledger's append-only
`corpus_removal_audit`: who, when (the row's `recorded_at`), what, the reason if any, and the
request's trace. The lifecycle writes the rows for what it authorizes and completes (withdraw and
purge, ADR-0021); this module writes the rest: every refusal before the lifecycle is reached, and
an upload's own `authorized`, `completed` and `failed`.

**A refusal is recorded under what the request named, and nothing it did not.** A refused upload of
a *new policy* names no instrument (none was made), so its row has none, and carries the uploaded
title in its detail. A refused *new version* names the policy it was for and no version id, because
none was created. Neither row invents an id that never existed. An `Attempt` is the request's
claim and is immutable: a stage that learns more (the reason, the title) builds a new one.

A refusal's row names a reason code and words, never a document's text: a parse error that quotes
the offending line is said to the admin, and `PolicyRefused.audit` says only its code.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from dataclasses import dataclass

from fastapi import Request

from compliance_rag import telemetry
from compliance_rag.adapters.interfaces import RemovalAction, RemovalAuditEntry, RemovalOutcome
from compliance_rag.api.deps import Caller, services_of
from compliance_rag.api.uploads import UploadRefused

MAX_AUDIT_DETAIL = 500


class PolicyRefused(UploadRefused):
    """A request the admin's policy routes refuse, in the status and words it is answered with.
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
    ) -> None:
        super().__init__(status, error, message, detail=detail)
        self.audit = audit if audit is not None else f"{error}: {message}"
        """What the audit row says; never a document's text."""
        self.audited = audited
        """Its audit row is written: by the lifecycle (a removal it refused), or by `audited`."""


@dataclass(frozen=True, slots=True)
class Attempt:
    """What a request named, as far as it is known: what an audit row records."""

    action: RemovalAction
    instrument_id: str | None
    """The policy's instrument id; None for a new policy, which names none."""
    version_id: str | None = None
    """Set only once a version is to be written: a refusal names none."""
    reason: str = ""
    title: str | None = None
    """The uploaded title, once the body has been read."""


async def record(
    request: Request, who: Caller, attempt: Attempt, outcome: RemovalOutcome, *, detail: str = ""
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
            findings_citing=None,
            detail=detail[:MAX_AUDIT_DETAIL],
            trace_id=telemetry.current_trace_id_or_none(),
        )
    )


def _as_policy_refusal(refused: UploadRefused) -> PolicyRefused:
    """A refusal from the shared upload readers, as one the audit can say in a word: a validation
    error by where it is, never by its input."""
    audit = f"{refused.error}: {refused.where or refused.message}"
    return PolicyRefused(
        refused.status, refused.error, refused.message, detail=refused.detail, audit=audit
    )


async def audited[T](request: Request, who: Caller, attempt: Attempt, work: Awaitable[T]) -> T:
    """`work`, with a refusal from it audited as `refused` under `attempt` (once: not again if the
    lifecycle already wrote its own row) and raised as a `PolicyRefused`."""
    try:
        return await work
    except UploadRefused as raised:
        refused = raised if isinstance(raised, PolicyRefused) else _as_policy_refusal(raised)
        if not refused.audited:
            detail = refused.audit
            if attempt.title is not None:
                detail = f"title {attempt.title!r}: {detail}"
            await record(request, who, attempt, RemovalOutcome.REFUSED, detail=detail)
            refused.audited = True
        raise refused from None
