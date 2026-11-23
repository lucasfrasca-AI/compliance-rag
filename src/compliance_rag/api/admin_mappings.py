"""The mapping queue page (#305, ADR-0053 I5, ADR-0059 Q8): `GET /admin/mappings`.

An admin sees each policy version in force today, with a button, "Propose mappings for
<policy version>", and the policy-to-law mappings the ledger holds for it, in every status,
**read-only**. The button is the one way a proposing run starts (Q8): it asks
`POST /v1/policy-mappings/proposals` for that version and the page reloads to show what the
ledger holds. Approving and rejecting a mapping are #306's, and the page has no control for
them: a proposal is shown as a proposal, the model's reasoning labelled as the model's, and
nothing on this page changes a finding.

**Admin only, on every request**, as the subscriptions page is: a signed-out request
redirects to `/signin`, a member who is not an admin gets the 403 page (`admin.PageAdmin`).
The page is drawn from the ledger on every request and never computed: a mapping, and
whether a run is going or failed, are what the ledger records, so a reload shows the same.

The versions are the tenant's internal policies in force today in Sydney
(`LedgerStore.policy_versions`), each with the title it was published with; a policy
version that is withdrawn or no longer in force has no button (a version needs to be in the
prefix to be proposed for), but its mappings stay listed.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from compliance_rag.adapters.interfaces import PolicyMapping, PolicyVersion
from compliance_rag.api.admin import PageAdmin
from compliance_rag.api.deps import services_of, today
from compliance_rag.api.pages import render
from compliance_rag.api.sources import source_href
from compliance_rag.corpus.display_form import cite, cited_policy_versions

router = APIRouter(prefix="/admin")


@dataclass(frozen=True, slots=True)
class MappingRow:
    """One mapping as the page draws it."""

    mapping_id: int
    cite: str
    href: str
    obligation_id: str
    kind: str
    status: str
    rationale: str
    proposer: str
    approver: str | None


@dataclass(frozen=True, slots=True)
class VersionBlock:
    """One policy version, its run and its mappings, as the page draws them."""

    instrument_id: str
    version_id: str
    title: str
    in_force: bool
    running: bool
    failed: bool
    mappings: tuple[MappingRow, ...]


@router.get("/mappings", response_class=HTMLResponse)
async def mappings_page(request: Request, who: PageAdmin) -> HTMLResponse:
    ledger = services_of(request).ledger
    in_force = set(await ledger.policy_versions(who.tenant_id, as_of=today()))
    mappings = await ledger.policy_mappings(who.tenant_id)
    by_version: dict[PolicyVersion, list[PolicyMapping]] = {}
    for m in mappings:
        by_version.setdefault(PolicyVersion(m.instrument_id, m.version_id), []).append(m)
    # Every version in force, and every version that has mappings, in instrument then
    # version order, bytewise (`policy_versions`' own).
    shown = sorted(
        in_force | set(by_version), key=lambda v: (v.instrument_id.encode(), v.version_id.encode())
    )
    titles = await ledger.version_titles(
        who.tenant_id, cited_policy_versions(m.policy_clause_id for m in mappings)
    )
    version_titles = await ledger.version_titles(
        who.tenant_id, ((v.instrument_id, v.version_id) for v in shown)
    )
    blocks: list[VersionBlock] = []
    for version in shown:
        run = await ledger.mapping_run(who.tenant_id, version.instrument_id, version.version_id)
        blocks.append(
            VersionBlock(
                instrument_id=version.instrument_id,
                version_id=version.version_id,
                title=version_titles.get((version.instrument_id, version.version_id))
                or version.instrument_id,
                in_force=version in in_force,
                running=run is not None and run.status == "pending" and not run.lapsed,
                failed=run is not None
                and (run.status == "failed" or (run.status == "pending" and run.lapsed)),
                mappings=tuple(
                    MappingRow(
                        mapping_id=m.mapping_id,
                        cite=cite(m.policy_clause_id, titles),
                        href=source_href(m.policy_clause_id),
                        obligation_id=m.obligation_id,
                        kind=m.kind.value,
                        status=m.status.value,
                        rationale=m.rationale,
                        proposer=m.proposer,
                        approver=m.approver,
                    )
                    for m in by_version.get(version, ())
                ),
            )
        )
    return render(request, "mappings.html", blocks=blocks)
