"""What a tenant's policy versions are, said once (#303, ADR-0053 U4, ADR-0021).

Pure: it reads the rows the ledger lists (`PolicyVersionRow`) and a day, and
touches nothing. The admin page and its API both draw from these, so a version
cannot be "in force" on the page and something else in the JSON.

**Status** is derived from what the ledger records, never stored (ADR-0029 for
law, the same here). In the order a reader needs it: *purged* (the text is gone;
the row stays as the marker), *withdrawn* (out of retrieval at every date, text
kept), *future* (its effective date is after the day), *superseded* (a later
version took over on or before the day), otherwise *in force*.

**Which version a new one replaces** is the instrument's version that has not
ended, is neither withdrawn nor purged, and has the latest effective date. It is
found from the instrument id the admin chose on the page, never from a title:
two policies may carry the same title, and a title is only a label (ADR-0053 U4).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from compliance_rag.adapters.interfaces import PolicyVersionRow

_NUMBERED = re.compile(r"v(\d+)")


class VersionStatus(StrEnum):
    IN_FORCE = "in_force"
    FUTURE = "future"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"
    PURGED = "purged"


STATUS_WORDS = {
    VersionStatus.IN_FORCE: "In force",
    VersionStatus.FUTURE: "Future",
    VersionStatus.SUPERSEDED: "Superseded",
    VersionStatus.WITHDRAWN: "Withdrawn",
    VersionStatus.PURGED: "Purged",
}


def version_status(row: PolicyVersionRow, today: dt.date) -> VersionStatus:
    """The version's status on `today`. Purged outranks withdrawn: a purged
    version is gone, whether or not it was withdrawn first."""
    if row.purged:
        return VersionStatus.PURGED
    if row.withdrawn:
        return VersionStatus.WITHDRAWN
    if row.valid_from > today:
        return VersionStatus.FUTURE
    if row.valid_to is not None and row.valid_to <= today:
        return VersionStatus.SUPERSEDED
    return VersionStatus.IN_FORCE


def next_version_id(existing: Iterable[str]) -> str:
    """`v<n>`, one past the highest `v<n>` the instrument has, or `v1`. Every id
    the ledger holds counts, a purged version's included: the ledger keeps its
    row, and an id is never used twice. An id that is not of that form (a
    version the operator published under another name) is not counted, and is
    stepped over if `v<n>` happens to be taken."""
    taken = set(existing)
    highest = max((int(m[1]) for v in taken if (m := _NUMBERED.fullmatch(v))), default=0)
    number = highest + 1
    while f"v{number}" in taken:
        number += 1
    return f"v{number}"


def superseded_by_new(rows: Iterable[PolicyVersionRow]) -> PolicyVersionRow | None:
    """The version a new one would replace: the instrument's that has not ended,
    is neither withdrawn nor purged, and has the latest effective date; None when
    there is none (a first version, or every earlier one withdrawn or purged).
    `rows` are one instrument's."""
    open_ended = [r for r in rows if r.valid_to is None and not r.withdrawn and not r.purged]
    return max(open_ended, key=lambda r: (r.valid_from, r.version_id), default=None)


@dataclass(frozen=True, slots=True)
class PolicyGroup:
    """One policy: its instrument id and its versions, oldest first."""

    instrument_id: str
    versions: tuple[PolicyVersionRow, ...]

    @property
    def slug(self) -> str:
        """The instrument id without its tenant prefix: the one path segment a
        route names the policy by (the tenant is always the caller's)."""
        return self.instrument_id.partition("/")[2]

    @property
    def title(self) -> str:
        """The latest version's recorded title that still exists, else the
        latest of any: what the policy is called now. Versions keep their own
        titles (ADR-0049); this is only the heading."""
        live = [v for v in self.versions if not v.purged]
        return max(live or self.versions, key=lambda v: (v.valid_from, v.version_id)).title


def group_policies(rows: Iterable[PolicyVersionRow]) -> tuple[PolicyGroup, ...]:
    """The rows by instrument, each group's versions oldest first by effective
    date, the groups by title (case-folded) then instrument id."""
    by_instrument: dict[str, list[PolicyVersionRow]] = {}
    for row in rows:
        by_instrument.setdefault(row.instrument_id, []).append(row)
    groups = [
        PolicyGroup(
            instrument_id,
            tuple(sorted(versions, key=lambda v: (v.valid_from, v.version_id))),
        )
        for instrument_id, versions in by_instrument.items()
    ]
    return tuple(sorted(groups, key=lambda g: (g.title.casefold(), g.instrument_id)))
