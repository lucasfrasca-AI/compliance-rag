"""The reviewed reading of an incomplete clause (#211, ADR-0047): what a reader
is shown about it, and, where the law says so, which years its missing content
governs.

The parser marks a clause incomplete with a closed reason and a generic
detail ("the formula is not available in text form"), and that is what every
store holds: a corpus revision is the only thing that changes it. The parser
can't say what the missing content *is* — that needs the alt text it refuses —
so a person may say it, in a reviewed artefact beside the instrument version's
other reviewed file: `corpus/instruments/{instrument}/{version}/incomplete.yaml`.

Each entry names a clause and the reason it is for, and carries:

- **`shown`**: the words a reader is shown in place of the parser's detail —
  in an answer, a chat read back, the prompt's fence and Track A's reason.
  Joined at read time, never written to a store (ADR-0047's "The signed
  wording").
- **`part_year_only`** (optional): the missing content governs a financial
  year only when the business was carried on for part of it, and `started`
  names the profile fact that says when it started. Track A then holds a
  leaf resting on the clause unassessable only for a year the facts show, or
  leave open, was a part year (ADR-0047, the human's decision 2 on #211).

It is corpus content under D-01: `author`, `reviewed_by`, `date` and
`source_clause_ids`, and a file still pending review is refused, not skipped.
"""

from __future__ import annotations

import datetime as dt
import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compliance_rag.corpus.models import (
    Incomplete,
    Incompleteness,
    IncompleteReason,
    parse_stable_id,
    validate_review,
)

INSTRUMENTS_ROOT = Path(__file__).resolve().parents[3] / "corpus" / "instruments"
FILE_NAME = "incomplete.yaml"
PENDING = "PENDING"

# What a reader is shown goes into the prompt's fence marker, `incomplete (…)`,
# so it can't hold what would reshape it: no `]` (closes the marker), no `;`
# (separates two reasons), no double quote, no line break (#236). Letters, digits,
# spaces, parentheses (balanced, checked below) and plain punctuation.
_SHOWN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 (),.'-]*[A-Za-z0-9)]\Z")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*\Z")


class UnsignedIncompleteReading(ValueError):
    """An `incomplete.yaml` nobody has signed: never used (D-01)."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PartYearOnly(_Strict):
    """The missing content governs a financial year only when the business
    was carried on for part of it."""

    started: str
    """The profile fact that says when the business started to be carried on
    (`business_started_on`), taken as carried on since without a break, as the
    mapping reads it."""

    @field_validator("started")
    @classmethod
    def _snake(cls, v: str) -> str:
        if not _FIELD_RE.match(v):
            raise ValueError(f"profile field {v!r} is not snake_case")
        return v


class IncompleteReading(_Strict):
    """One clause's reason, as a person read it."""

    clause_id: str
    reason: IncompleteReason
    """The parser's reason this entry is for: a clause marked for another
    reason too keeps the parser's detail for that one."""
    shown: str
    part_year_only: PartYearOnly | None = None

    @field_validator("clause_id")
    @classmethod
    def _stable(cls, v: str) -> str:
        parse_stable_id(v)
        return v

    @field_validator("shown")
    @classmethod
    def _safe_in_a_marker(cls, v: str) -> str:
        if not _SHOWN_RE.match(v) or not _balanced(v):
            raise ValueError(
                f"what a reader is shown is one line of words, digits, balanced "
                f"parentheses and plain punctuation: {v!r}"
            )
        return v


def _balanced(text: str) -> bool:
    depth = 0
    for char in text:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            return False
    return depth == 0


class IncompleteReadingFile(_Strict):
    """One `incomplete.yaml`."""

    author: str
    reviewed_by: str
    date: str
    source_clause_ids: tuple[str, ...] = Field(min_length=1)
    clauses: tuple[IncompleteReading, ...] = Field(min_length=1)

    @field_validator("date", mode="before")
    @classmethod
    def _iso(cls, v: object) -> object:
        return v.isoformat() if isinstance(v, dt.date) else v

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        validate_review(self.author, self.reviewed_by)
        keys = [(c.clause_id, c.reason) for c in self.clauses]
        if len(keys) != len(set(keys)):
            raise ValueError("a clause's reason is read once")
        named = {c.clause_id for c in self.clauses}
        if set(self.source_clause_ids) != named:
            raise ValueError(
                f"source_clause_ids {sorted(self.source_clause_ids)} are not the clauses "
                f"read {sorted(named)}"
            )
        if self.is_signed_off:
            dt.date.fromisoformat(self.date)  # a signed file carries the real review date
        return self

    @property
    def is_signed_off(self) -> bool:
        return not (self.reviewed_by.startswith(PENDING) or self.date.startswith(PENDING))


@dataclass(frozen=True, slots=True)
class IncompleteReadings:
    """Every signed entry, by clause and reason."""

    entries: Mapping[tuple[str, IncompleteReason], IncompleteReading] = field(
        default_factory=dict[tuple[str, IncompleteReason], IncompleteReading]
    )

    def entry(self, clause_id: str, reason: IncompleteReason) -> IncompleteReading | None:
        return self.entries.get((clause_id, reason))

    def shown(self, clause_id: str, incompleteness: Incompleteness) -> str:
        """What a reader is shown for this reason: the signed words where a
        person has read the clause, the parser's detail otherwise."""
        found = self.entry(clause_id, incompleteness.reason)
        return found.shown if found is not None else incompleteness.detail

    def shown_json(self, clause_id: str, incomplete: Incomplete) -> list[dict[str, str]]:
        """A clause's reasons as a client is shown them: each reason with the
        words to show for it as its `detail`."""
        return [{"reason": i.reason.value, "detail": self.shown(clause_id, i)} for i in incomplete]


def parse_incomplete_reading_file(
    source: str, *, instrument_id: str, version_id: str
) -> IncompleteReadingFile:
    """One file, for the instrument version whose folder holds it. Refused
    while it is pending review, and if it names a clause of another version."""
    loaded = yaml.safe_load(source)
    if not isinstance(loaded, dict):
        raise ValueError(f"{FILE_NAME} must be a YAML mapping")
    data = cast("dict[str, Any]", loaded)  # Any: yaml.safe_load is untyped
    review = IncompleteReadingFile.model_validate(data)
    if not review.is_signed_off:
        raise UnsignedIncompleteReading(
            f"{instrument_id}/{version_id}/{FILE_NAME} is awaiting review, not used (D-01)"
        )
    for clause in review.clauses:
        found_instrument, found_version, _ = parse_stable_id(clause.clause_id)
        if (found_instrument, found_version) != (instrument_id, version_id):
            raise ValueError(f"{clause.clause_id} is not a clause of {instrument_id}:{version_id}")
    return review


def load_incomplete_readings(root: Path = INSTRUMENTS_ROOT) -> IncompleteReadings:
    """Every `incomplete.yaml` under `root`, one per instrument version."""
    entries: dict[tuple[str, IncompleteReason], IncompleteReading] = {}
    for path in sorted(root.glob(f"*/*/{FILE_NAME}")):
        review = parse_incomplete_reading_file(
            path.read_text(encoding="utf-8"),
            instrument_id=path.parent.parent.name,
            version_id=path.parent.name,
        )
        entries |= {(c.clause_id, c.reason): c for c in review.clauses}
    return IncompleteReadings(entries)


@functools.cache
def committed_incomplete_readings() -> IncompleteReadings:
    """The reviewed files in this checkout, read once: what the app shows."""
    return load_incomplete_readings()
