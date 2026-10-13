"""Profile facts as dated evidence (issue #114, from the #93 design session).

The law reads facts *for a period*: s6D(1) asks for annual turnover for the
financial year before the test time, s6D(4)(a) for every financial year since
the business started. So a profile fact is not a field on a form but a row of
**evidence**: a field, a value, the period it is true for, how it entered the
system and on whose word (CONTEXT.md: Evidence).

- **Documents inform, humans assert.** Only `asserted` and `parsed` evidence
  settles a condition. An `inferred` claim from a document is shown to the user
  to confirm, and confirming it records new `asserted` evidence; the claim
  itself never settles anything.
- **Append-only.** A correction is new evidence naming the evidence it
  `supersedes`; nothing is overwritten, so the assessment that read the old
  value can still show what it read.
- **Disagreement is not resolved by guessing.** Two current facts for the
  same field over overlapping periods with different values make that fact
  *conflicting*, which the evaluator reports as unknown, with the reason. So
  does a fact that covers only part of the period a rule asks about.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from compliance_rag.corpus.models import NO_END_DATE, validate_tenant_id

FactValue = str | int | float | bool | dt.date | tuple[str, ...]
"""What a profile fact may hold. JSON in the ledger (dates as ISO strings)."""


class EvidenceProvenance(StrEnum):
    """How a fact entered the system (CONTEXT.md: Provenance tier)."""

    PARSED = "parsed"
    ASSERTED = "asserted"
    INFERRED = "inferred"
    EXTERNAL = "external"


SETTLES = frozenset({EvidenceProvenance.PARSED, EvidenceProvenance.ASSERTED})
"""The provenance a condition may be settled by: documents inform, humans
assert (CONTEXT.md)."""


@dataclass(frozen=True, slots=True)
class Period:
    """`[start, end)`: the dates a fact is true for. An open-ended state (a
    legal form) ends at `NO_END_DATE`."""

    start: dt.date
    end: dt.date = NO_END_DATE

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"period end {self.end} is not after its start {self.start}")

    def contains(self, day: dt.date) -> bool:
        return self.start <= day < self.end

    def overlaps(self, other: Period) -> bool:
        return self.start < other.end and other.start < self.end

    @classmethod
    def financial_year(cls, starting: int) -> Period:
        """The Australian financial year starting 1 July `starting` (Acts
        Interpretation Act 1901 s 2B)."""
        return cls(dt.date(starting, 7, 1), dt.date(starting + 1, 7, 1))

    @classmethod
    def financial_year_of(cls, day: dt.date) -> Period:
        return cls.financial_year(day.year if day.month >= 7 else day.year - 1)

    def previous_financial_year(self) -> Period:
        return Period.financial_year(self.start.year - 1)

    @property
    def label(self) -> str:
        """`FY2025-26` for a financial year; ISO dates otherwise."""
        if (
            self.start.month == 7
            and self.start.day == 1
            and self.end == dt.date(self.start.year + 1, 7, 1)
        ):
            return f"FY{self.start.year}-{str(self.start.year + 1)[2:]}"
        end = "open" if self.end == NO_END_DATE else self.end.isoformat()
        return f"{self.start.isoformat()} to {end}"


FIELD = re.compile(r"^[a-z][a-z0-9_]*$")
"""A profile field's name — the same rule as the ledger's `evidence_field`
CHECK and a mapping leaf's `field`, so a name valid in one is valid in all."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """One fact about a tenant: `field` was `value` for `period`, on the
    word of `source` (CONTEXT.md: Evidence).

    - A **date-valued state** (`business_started_on`) is recorded with the
      period it holds from: `Period(start=that date)`, open-ended.
    - `trace_id` is the request that recorded it. The ledger requires one for
      `asserted` evidence (CLAUDE.md: every ledger row from a request).
    - `document_id` names the document a confirmed claim came from.
    - `recorded_at` is set by the ledger when read back, so an assessment can
      be re-run against the evidence as it stood (`Facts.of(..., recorded_by=)`).
    """

    tenant_id: str
    evidence_id: str
    field: str
    value: FactValue
    period: Period
    provenance: EvidenceProvenance
    source: str
    asserted_by: str | None = None
    supersedes: str | None = None
    trace_id: str | None = None
    document_id: str | None = None
    recorded_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        validate_tenant_id(self.tenant_id)
        if not FIELD.match(self.field):
            raise ValueError(f"evidence field {self.field!r} is not snake_case")
        if isinstance(self.value, dt.datetime):
            raise ValueError("a fact is a date, never a datetime")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("a fact is a finite number, never NaN or infinity")
        if not self.source.strip():
            raise ValueError("evidence names its source: who said so, or what it was parsed from")
        if self.provenance is EvidenceProvenance.ASSERTED and not self.asserted_by:
            raise ValueError("asserted evidence names the person who asserted it")
        if self.supersedes == self.evidence_id:
            raise ValueError("evidence cannot supersede itself")


@dataclass(frozen=True, slots=True)
class Reading:
    """What the evidence says about one field for one period: a value, or
    nothing (`missing`), or current facts that disagree or that cover only
    part of the period asked about (`conflict`)."""

    value: FactValue | None
    evidence: tuple[Evidence, ...] = ()
    conflict: bool = False

    @property
    def missing(self) -> bool:
        return not self.evidence


@dataclass(frozen=True, slots=True)
class Facts:
    """A tenant's current evidence, as the evaluator reads it: evidence that
    can settle a condition (`asserted`, `parsed`), less what later settling
    evidence supersedes. An `inferred` row can neither settle a fact nor
    unsettle one by superseding it."""

    current: tuple[Evidence, ...] = field(default_factory=tuple[Evidence, ...])

    @classmethod
    def of(cls, evidence: Iterable[Evidence], *, recorded_by: dt.datetime | None = None) -> Facts:
        """`recorded_by` re-reads the evidence as it stood at that moment —
        what an earlier assessment saw."""
        rows = [
            e
            for e in evidence
            if e.provenance in SETTLES
            and (recorded_by is None or e.recorded_at is None or e.recorded_at <= recorded_by)
        ]
        return cls(current_evidence(rows))

    def _read(
        self, field: str, found: tuple[Evidence, ...], exact: tuple[Evidence, ...]
    ) -> Reading:
        if not found:
            return Reading(None)
        values = {_comparable(e.value) for e in found}
        if len(values) > 1 or not exact:
            return Reading(None, found, conflict=True)
        return Reading(exact[0].value, found)

    def on(self, field: str, day: dt.date) -> Reading:
        """The fact whose period contains `day` — a state on a date."""
        found = tuple(e for e in self.current if e.field == field and e.period.contains(day))
        return self._read(field, found, found)

    def for_period(self, field: str, period: Period) -> Reading:
        """The fact recorded for exactly `period` — a financial year's
        figure. Evidence covering only part of it (a part year) or straddling
        it, alone or beside an exact fact with another value, is a conflict to
        report, never silently ignored."""
        found = tuple(e for e in self.current if e.field == field and e.period.overlaps(period))
        exact = tuple(e for e in found if e.period == period)
        return self._read(field, found, exact)


def _comparable(value: FactValue) -> object:
    """Values compared by type as well as value: `1`, `1.0` and `True` are
    different answers."""
    return (type(value).__name__, tuple(value) if isinstance(value, tuple) else value)


def current_evidence(evidence: Iterable[Evidence]) -> tuple[Evidence, ...]:
    """Evidence nothing in `evidence` supersedes."""
    rows = tuple(evidence)
    superseded = {e.supersedes for e in rows if e.supersedes is not None}
    return tuple(e for e in rows if e.evidence_id not in superseded)


def check_evidence_write(evidence: Evidence) -> None:
    """What the ledger requires of evidence it records, beyond its shape:
    an assertion arrives in a request, so it carries that request's
    `trace_id` (CLAUDE.md)."""
    if evidence.provenance is EvidenceProvenance.ASSERTED and not (
        evidence.trace_id and evidence.trace_id.strip("0")
    ):
        raise ValueError("asserted evidence is recorded with the trace_id of its request")
