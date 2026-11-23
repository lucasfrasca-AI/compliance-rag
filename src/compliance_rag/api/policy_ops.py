"""What an admin's policy operations do to the stores, through the lifecycle (#303, ADR-0021).

`publish_policy` writes a new policy or a new version; `remove_version` withdraws or purges one.
Both are one path for every surface: the JSON routes (`admin_policies.py`) and the pages
(`admin_policy_pages.py`) call the same functions, so neither enforces anything of its own. Each
stage that can refuse runs under `policy_audit.audited`, so every refusal is a row, and each is
audited under what the request had named by then (an immutable `Attempt`, rebuilt as the request
tells more).

**Withdraw and purge** are the lifecycle's own operations. The lifecycle audits what it
authorizes, completes or refuses (including a purge whose typed count is not the findings-citing
count now); this module audits the refusals the lifecycle never sees (a version that doesn't
exist, one already withdrawn or purged, a typed count that is not a number).

**Purge** is behind the typed count: the admin is shown `findings_citing` (the lifecycle's
preview) and types it; the lifecycle refuses unless it equals the count now, so a finding written
between the page and the click forces a fresh look.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from fastapi import Request

from compliance_rag import telemetry
from compliance_rag.adapters.corpus_lifecycle import RemovalRefused
from compliance_rag.adapters.interfaces import PolicyVersionRow, RemovalAction, RemovalOutcome
from compliance_rag.api.deps import Caller, services_of
from compliance_rag.api.policy_audit import Attempt, PolicyRefused, audited, record
from compliance_rag.api.policy_lookup import audit_name, find_group, find_version
from compliance_rag.api.policy_upload import prepare, read_upload
from compliance_rag.corpus.models import DocumentClass
from compliance_rag.corpus.policy_versions import VersionStatus
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens


@dataclass(frozen=True, slots=True)
class Published:
    instrument_id: str
    version_id: str
    title: str
    effective_from: dt.date
    supersedes: str | None
    clauses: int
    backdate_reason: str | None


def publish_detail(effective_from: dt.date, replaces: PolicyVersionRow | None) -> str:
    """What a publish's audit rows say of it: the date, and the version it replaces."""
    return f"effective {effective_from}" + (
        "" if replaces is None else f"; replaces {replaces.version_id}"
    )


async def publish_policy(request: Request, who: Caller, *, policy: str | None) -> Published:
    """A new policy (`policy` None) or a new version of the one the admin chose, from this
    request's body: every refusal before a write, the write through the lifecycle, every outcome
    audited. Raises `PolicyRefused`."""
    services = services_of(request)
    with telemetry.span("api.admin.policy.publish", **{"policy.new": policy is None}) as span:
        # A new policy names no instrument; a new version names the policy and no version.
        attempt = Attempt(
            RemovalAction.PUBLISH, None if policy is None else audit_name(who, policy)
        )
        group = (
            None
            if policy is None
            else await audited(request, who, attempt, find_group(request, who, policy))
        )
        upload = await audited(request, who, attempt, read_upload(request, who))
        attempt = replace(attempt, reason=upload.backdate_reason or "", title=upload.title)
        prepared = await audited(request, who, attempt, prepare(request, who, upload, group))
        # Every refusal is behind us. The row names the version that is now to be written.
        attempt = replace(
            attempt, instrument_id=prepared.instrument_id, version_id=prepared.version_id
        )
        span.set_attribute("policy.version", f"{prepared.instrument_id}:{prepared.version_id}")
        span.set_attribute("policy.document_bytes", len(prepared.raw))
        span.set_attribute("policy.clauses", len(prepared.clauses))
        span.set_attribute("policy.backdated", upload.backdate_reason is not None)
        detail = publish_detail(prepared.effective_from, prepared.replaces)
        supersedes = None if prepared.replaces is None else prepared.replaces.version_id
        # The stores are written audited as the lifecycle audits a removal (ADR-0021): authorized
        # before any is touched, then completed or failed.
        await record(request, who, attempt, RemovalOutcome.AUTHORIZED, detail=detail)
        try:
            await services.lifecycle.publish_version(
                prepared.clauses,
                count_tokens=count_reranker_tokens,
                source_html=upload.content,
                instrument_id=prepared.instrument_id,
                tenant_id=who.tenant_id,
                version_id=prepared.version_id,
                effective_from=prepared.effective_from,
                supersedes=supersedes,
                document_class=DocumentClass.INTERNAL_POLICY,
                title=upload.title,
                backdate_reason=upload.backdate_reason,
                uploaded_bytes=prepared.raw,
            )
        except Exception as failed:
            await record(
                request,
                who,
                attempt,
                RemovalOutcome.FAILED,
                detail=f"{detail}; {type(failed).__name__}",
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
        # The version is published: the lifecycle's last write, the ledger's record, is its
        # commit point. If this row's own write fails, the request is a 500 and the version
        # stands with an `authorized` row and no `completed`. That is honest (the upload did
        # happen, and the trail says it was authorized); marking it `failed` would say it did not.
        await record(request, who, attempt, RemovalOutcome.COMPLETED, detail=detail)
        return Published(
            instrument_id=prepared.instrument_id,
            version_id=prepared.version_id,
            title=upload.title,
            effective_from=prepared.effective_from,
            supersedes=supersedes,
            clauses=len(prepared.clauses),
            backdate_reason=upload.backdate_reason,
        )


# ---- withdraw and purge ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Asked:
    """What the admin sent with a removal: the reason, and for a purge the number of findings they
    typed, None when what they typed was not a number."""

    reason: str
    typed: int | None = None


async def findings_citing(
    request: Request, who: Caller, instrument_id: str, version_id: str
) -> int:
    """The number of findings that cite the version: what a purge must be acknowledged with."""
    shown = await services_of(request).lifecycle.preview_removal(
        tenant_id=who.tenant_id, instrument_id=instrument_id, version_id=version_id
    )
    return shown.findings_citing


def removed_status(action: RemovalAction) -> VersionStatus:
    """The status a version has once `action` is done."""
    return VersionStatus.PURGED if action is RemovalAction.PURGE else VersionStatus.WITHDRAWN


async def remove_version(
    request: Request,
    who: Caller,
    policy: str,
    version_id: str,
    action: RemovalAction,
    read: Callable[[], Awaitable[Asked]],
) -> None:
    """Withdraw or purge (`action`) one version, audited whatever the outcome. `read` reads what
    the admin sent, from the JSON or the form; a refusal in it is audited too, as a refusal of this
    request. Raises `PolicyRefused`."""
    attempt = Attempt(action, audit_name(who, policy), version_id)
    asked = await audited(request, who, attempt, read())
    attempt = replace(attempt, reason=asked.reason)
    with telemetry.span(
        f"api.admin.policy.{action.value}", **{"policy.version": f"{policy}:{version_id}"}
    ):
        await audited(
            request, who, attempt, _remove(request, who, policy, version_id, action, asked)
        )


async def _remove(
    request: Request,
    who: Caller,
    policy: str,
    version_id: str,
    action: RemovalAction,
    asked: Asked,
) -> None:
    group, row = await find_version(request, who, policy, version_id)
    lifecycle = services_of(request).lifecycle
    if row.purged:
        raise PolicyRefused(
            409,
            "already_purged" if action is RemovalAction.PURGE else "purged",
            "this version is already purged"
            if action is RemovalAction.PURGE
            else "this version is purged: there is nothing to withdraw",
        )
    if action is RemovalAction.WITHDRAW:
        if row.withdrawn:
            raise PolicyRefused(409, "already_withdrawn", "this version is already withdrawn")
        await _map_removal_refused_to_409(
            lifecycle.withdraw(
                tenant_id=who.tenant_id,
                instrument_id=group.instrument_id,
                version_id=version_id,
                actor=who.member.email,
                reason=asked.reason,
            )
        )
        return
    if asked.typed is None:
        raise PolicyRefused(
            422,
            "count_not_a_number",
            "type the number of findings that cite this version, as numerals",
            audit="count_not_a_number",
        )
    await _map_removal_refused_to_409(
        lifecycle.purge(
            tenant_id=who.tenant_id,
            instrument_id=group.instrument_id,
            version_id=version_id,
            actor=who.member.email,
            reason=asked.reason,
            acknowledged_findings_citing=asked.typed,
        )
    )


async def _map_removal_refused_to_409(removal: Awaitable[None]) -> None:
    """A lifecycle removal, with the refusal it audited itself answered as a 409. Any other
    failure was audited `failed` by the lifecycle, and is raised as it is."""
    try:
        await removal
    except RemovalRefused as refused:
        raise PolicyRefused(409, "refused", str(refused), audited=True) from None
