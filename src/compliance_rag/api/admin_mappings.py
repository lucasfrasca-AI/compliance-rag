"""The mapping queue page (#305, ADR-0053 I5, ADR-0059 Q8): `GET /admin/mappings`.

An admin sees each policy version in force today, with a button, "Propose mappings for
<policy version>", and the policy-to-law mappings the ledger holds for it. The button is the
one way a proposing run starts (Q8): it asks `POST /v1/policy-mappings/proposals` for that
version and the page reloads to show what the ledger holds.

**The approval queue (#306).** Each `proposed` mapping carries two buttons, **Approve** and
**Reject**, and a rejection an optional one-line reason (the admin's own words, shown as
theirs). Either asks `POST /v1/policy-mappings/{id}/decision` (`mapping_decisions.py`) and the
page reloads. A decided mapping has no button: a decision is final, and it shows who decided it
and when. The mappings can be filtered by status (`?status=proposed`, `approved` or `rejected`;
anything else is every status), with the count of each beside its filter, by plain links: no
script is needed to filter. The page says what an approval does: only an approved `implements`
mapping changes a finding, in a check made after it, and an issued report never changes;
`partially_implements` derives nothing. A proposal is shown as a proposal, the model's reasoning
labelled as the model's, and nothing on this page changes a finding by itself.

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

from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from compliance_rag.adapters.interfaces import MappingStatus, PolicyMapping, PolicyVersion
from compliance_rag.api.admin import PageAdmin
from compliance_rag.api.deps import services_of, sydney_label, today
from compliance_rag.api.mapping_proposals import status_counts
from compliance_rag.api.pages import render
from compliance_rag.api.sources import source_href
from compliance_rag.corpus.display_form import cite

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
    """The address of whoever decided it, as it was when they did (#306)."""
    decided_at: str | None
    """When, as the page shows it (Sydney time), with `decided_at_iso` for the element."""
    decided_at_iso: str | None
    rejection_reason: str | None
    """The admin's own words for a rejection; never the model's."""
    decidable: bool
    """Still `proposed`: the only status with Approve and Reject."""
    derives: bool
    """Approved and `implements`: the only mapping that changes a finding."""


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


@dataclass(frozen=True, slots=True)
class StatusFilter:
    """One link of the status filter: every status, then each of the three, with its count."""

    label: str
    href: str
    count: int
    current: bool


def _filters(mappings: Sequence[PolicyMapping], chosen: MappingStatus | None) -> list[StatusFilter]:
    """The filter links (plain links, no script), each with how many mappings it shows."""
    held = status_counts(mappings)
    return [
        StatusFilter("All", "/admin/mappings", len(mappings), chosen is None),
        *(
            StatusFilter(
                status.value.capitalize(),
                f"/admin/mappings?status={status.value}",
                held[status.value],
                chosen is status,
            )
            for status in MappingStatus
        ),
    ]


@router.get("/mappings", response_class=HTMLResponse)
async def mappings_page(
    request: Request, who: PageAdmin, status: str | None = None
) -> HTMLResponse:
    ledger = services_of(request).ledger
    in_force = set(await ledger.policy_versions(who.tenant_id, as_of=today()))
    everything = await ledger.policy_mappings(who.tenant_id)
    # an unknown status is every status: the filter is a view, never a refusal
    chosen = next((s for s in MappingStatus if s.value == status), None)
    mappings = [m for m in everything if chosen is None or m.status is chosen]
    by_version: dict[PolicyVersion, list[PolicyMapping]] = {}
    for m in mappings:
        by_version.setdefault(PolicyVersion(m.instrument_id, m.version_id), []).append(m)
    # Every version in force, and every version that has mappings, in instrument then
    # version order, bytewise (`policy_versions`' own).
    shown = sorted(
        in_force | set(by_version), key=lambda v: (v.instrument_id.encode(), v.version_id.encode())
    )
    # One read of the titles (every cited clause is of a version shown) and one of the runs,
    # whatever the number of versions.
    titles = await ledger.version_titles(
        who.tenant_id, ((v.instrument_id, v.version_id) for v in shown)
    )
    runs = await ledger.mapping_runs(who.tenant_id)
    blocks: list[VersionBlock] = []
    for version in shown:
        run = runs.get((version.instrument_id, version.version_id))
        blocks.append(
            VersionBlock(
                instrument_id=version.instrument_id,
                version_id=version.version_id,
                title=titles.get((version.instrument_id, version.version_id))
                or version.instrument_id,
                in_force=version in in_force,
                running=run is not None and run.running,
                failed=run is not None and run.unfinished,
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
                        approver=m.approver_email,
                        decided_at=None if m.decided_at is None else sydney_label(m.decided_at),
                        decided_at_iso=None if m.decided_at is None else m.decided_at.isoformat(),
                        rejection_reason=m.rejection_reason,
                        decidable=m.status is MappingStatus.PROPOSED,
                        derives=m.derives,
                    )
                    for m in by_version.get(version, ())
                ),
            )
        )
    return render(
        request,
        "mappings.html",
        blocks=blocks,
        filters=_filters(everything, chosen),
        chosen=None if chosen is None else chosen.value,
        awaiting=status_counts(everything)[MappingStatus.PROPOSED.value],
    )
