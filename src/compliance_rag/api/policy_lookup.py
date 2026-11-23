"""Finding a tenant's policy and its versions in the ledger (#303), by id and never by title.

A policy is named in a route by its **slug**: its instrument id without the tenant's prefix
(`acme/privacy-policy` is `privacy-policy`; the tenant is always the caller's, `deps.Caller`). The
slug is a path segment in a fixed form, and the lookup is an exact match on the instrument id the
ledger holds for this tenant's internal policies, so a title, another case, another tenant's id or
a library instrument finds nothing (404).
"""

from __future__ import annotations

import re

from fastapi import Request

from compliance_rag.adapters.interfaces import PolicyVersionRow
from compliance_rag.api.deps import Caller, services_of
from compliance_rag.api.policy_audit import PolicyRefused
from compliance_rag.corpus.models import validate_instrument_id
from compliance_rag.corpus.policy_versions import PolicyGroup, group_policies

PAGE_PATH = "/admin/policies"
_SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}", re.ASCII)


def slug_of(instrument_id: str) -> str:
    return instrument_id.partition("/")[2]


def version_path(instrument_id: str, version_id: str) -> str:
    return f"{PAGE_PATH}/{slug_of(instrument_id)}/versions/{version_id}"


def audit_name(who: Caller, policy: str) -> str:
    """The instrument id a refusal for `policy` is audited under: the id itself when it is the
    form of one, so a typo is traceable, else a placeholder (the audit row is text, and a path
    segment may hold anything)."""
    return f"{who.tenant_id}/{policy}" if _SLUG.fullmatch(policy) else f"{who.tenant_id}/(invalid)"


def _no_such_policy() -> PolicyRefused:
    return PolicyRefused(404, "no_such_policy", "this workspace has no such policy")


def instrument_of(who: Caller, policy: str) -> str:
    """The tenant's instrument id for the slug `policy`, or a 404 for what is not the form of
    one."""
    if _SLUG.fullmatch(policy) is None:
        raise _no_such_policy()
    try:
        return validate_instrument_id(f"{who.tenant_id}/{policy}")
    except ValueError:
        raise _no_such_policy() from None


async def rows_of(request: Request, who: Caller) -> tuple[PolicyVersionRow, ...]:
    return await services_of(request).ledger.policy_version_rows(who.tenant_id)


async def find_group(request: Request, who: Caller, policy: str) -> PolicyGroup:
    """The tenant's policy `policy`, or a 404 for a name the ledger doesn't hold as one of this
    tenant's internal policies. Chosen by id, never by title."""
    instrument_id = instrument_of(who, policy)
    for group in group_policies(await rows_of(request, who)):
        if group.instrument_id == instrument_id:
            return group
    raise _no_such_policy()


async def find_version(
    request: Request, who: Caller, policy: str, version_id: str
) -> tuple[PolicyGroup, PolicyVersionRow]:
    group = await find_group(request, who, policy)
    for row in group.versions:
        if row.version_id == version_id:
            return group, row
    raise PolicyRefused(404, "no_such_version", "this policy has no such version")
