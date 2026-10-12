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
  *conflicting*, which the evaluator reports as unknown, with the reason.
"""

from __future__ import annotations

import datetime as dt
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


@dataclass(frozen=True, slots=True)
class Evidence:
    """One fact about a tenant: `field` was `value` for `period`, on the
    word of `source` (CONTEXT.md: Evidence). `trace_id` is the request that
    recorded it, when one did (CLAUDE.md: every ledger row from a request)."""

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

    def __post_init__(self) -> None:
        validate_tenant_id(self.tenant_id)
        if not self.field.isidentifier() or self.field != self.field.lower():
            raise ValueError(f"evidence field {self.field!r} is not snake_case")
        if not self.source.strip():
            raise ValueError("evidence names its source: who said so, or what it was parsed from")
        if self.provenance is EvidenceProvenance.ASSERTED and not self.asserted_by:
            raise ValueError("asserted evidence names the person who asserted it")
        if self.supersedes == self.evidence_id:
            raise ValueError("evidence cannot supersede itself")


@dataclass(frozen=True, slots=True)
class Reading:
    """What the evidence says about one field for one period: a value, or
    nothing (`missing`), or two current facts that disagree (`conflict`)."""

    value: FactValue | None
    evidence: tuple[Evidence, ...] = ()
    conflict: bool = False

    @property
    def missing(self) -> bool:
        return not self.evidence


@dataclass(frozen=True, slots=True)
class Facts:
    """A tenant's current evidence, as the evaluator reads it: superseded
    evidence and evidence that can't settle a condition (`inferred`,
    `external`) left out."""

    current: tuple[Evidence, ...] = field(default_factory=tuple[Evidence, ...])

    @classmethod
    def of(cls, evidence: Iterable[Evidence]) -> Facts:
        rows = tuple(evidence)
        superseded = {e.supersedes for e in rows if e.supersedes is not None}
        return cls(
            tuple(e for e in rows if e.evidence_id not in superseded and e.provenance in SETTLES)
        )

    def _read(self, field: str, match: Period | dt.date) -> Reading:
        found = tuple(
            e
            for e in self.current
            if e.field == field
            and (e.period.contains(match) if isinstance(match, dt.date) else e.period == match)
        )
        if not found:
            return Reading(None)
        values = {_hashable(e.value) for e in found}
        if len(values) > 1:
            return Reading(None, found, conflict=True)
        return Reading(found[0].value, found)

    def on(self, field: str, day: dt.date) -> Reading:
        """The fact whose period contains `day` — a state on a date."""
        return self._read(field, day)

    def for_period(self, field: str, period: Period) -> Reading:
        """The fact recorded for exactly `period` — a financial year's figure."""
        return self._read(field, period)


def _hashable(value: FactValue) -> object:
    return tuple(value) if isinstance(value, tuple) else value


def current_evidence(evidence: Iterable[Evidence]) -> tuple[Evidence, ...]:
    """Evidence no later evidence supersedes, whatever its provenance."""
    rows = tuple(evidence)
    superseded = {e.supersedes for e in rows if e.supersedes is not None}
    return tuple(e for e in rows if e.evidence_id not in superseded)
