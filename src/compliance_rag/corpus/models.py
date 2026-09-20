"""Canonical corpus records and the stable ID scheme.

Vocabulary is `CONTEXT.md`; the names here are the names there. A clause is the
smallest citable unit of an instrument and its stable ID is
`{instrument_id}:{version_id}:{clause_path}` (D-02, D-05).
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# One path segment: letters, digits, and the separators legislation actually uses.
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
INSTRUMENT_ID_RE = re.compile(rf"^{_SEGMENT}$")
VERSION_ID_RE = re.compile(rf"^{_SEGMENT}$")
# A clause path is dotted or slashed segments: "APP11.1", "art9/para2", "A.8.3".
CLAUSE_PATH_RE = re.compile(rf"^{_SEGMENT}(?:[./]{_SEGMENT})*$")
STABLE_ID_RE = re.compile(
    rf"^(?P<instrument_id>{_SEGMENT}):(?P<version_id>{_SEGMENT}):(?P<clause_path>{_SEGMENT}(?:[./]{_SEGMENT})*)$"
)


class Status(StrEnum):
    """Bitemporal status of an instrument version or clause (D-05)."""

    IN_FORCE = "in_force"
    NOT_YET_COMMENCED = "not_yet_commenced"
    REPEALED = "repealed"


class Scope(StrEnum):
    """Which corpus partition a record belongs to (`CONTEXT.md`, D-14)."""

    PUBLIC = "public"
    TENANT = "tenant"
    USER = "user"


def canonicalise(text: str) -> str:
    """Canonical form used by the verifier on both sides of the comparison.

    NFC normalisation, curly quotes and dashes folded to ASCII, all whitespace
    runs collapsed to a single space, ends stripped. The verifier compares
    canonical to canonical (D-02), so this function is load-bearing: changing it
    changes what counts as a matching citation.
    """
    text = unicodedata.normalize("NFC", text)
    # The ambiguous characters below are the point: this table folds the
    # typographic forms legislation actually uses into ASCII. RUF001 is
    # suppressed per line rather than per file so a stray one elsewhere
    # still gets flagged.
    for curly, plain in (
        ("‘", "'"),  # noqa: RUF001
        ("’", "'"),  # noqa: RUF001
        ("“", '"'),
        ("”", '"'),
        ("–", "-"),  # noqa: RUF001
        ("—", "-"),
        (" ", " "),  # noqa: RUF001
    ):
        text = text.replace(curly, plain)
    return " ".join(text.split())


def stable_id(instrument_id: str, version_id: str, clause_path: str) -> str:
    """Build a clause's stable ID, validating each part."""
    if not INSTRUMENT_ID_RE.match(instrument_id):
        raise ValueError(f"invalid instrument_id: {instrument_id!r}")
    if not VERSION_ID_RE.match(version_id):
        raise ValueError(f"invalid version_id: {version_id!r}")
    if not CLAUSE_PATH_RE.match(clause_path):
        raise ValueError(f"invalid clause_path: {clause_path!r}")
    return f"{instrument_id}:{version_id}:{clause_path}"


def parse_stable_id(value: str) -> tuple[str, str, str]:
    """Split a stable ID back into (instrument_id, version_id, clause_path)."""
    match = STABLE_ID_RE.match(value)
    if match is None:
        raise ValueError(f"not a stable clause ID: {value!r}")
    return (
        match.group("instrument_id"),
        match.group("version_id"),
        match.group("clause_path"),
    )


class Instrument(BaseModel):
    """A law, regulation or standard as a whole. Has versions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    title: str
    jurisdiction: str
    scope: Scope = Scope.PUBLIC

    @field_validator("instrument_id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not INSTRUMENT_ID_RE.match(v):
            raise ValueError(f"invalid instrument_id: {v!r}")
        return v


class InstrumentVersion(BaseModel):
    """One compilation of an instrument, identified by the register's own ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    version_id: str
    title: str
    status: Status
    valid_from: dt.date
    valid_to: dt.date | None = None
    recorded_at: dt.datetime
    source_url: str | None = None
    supersedes: str | None = Field(
        default=None, description="version_id of the version this one supersedes (D-05)"
    )

    @field_validator("valid_to")
    @classmethod
    def _check_interval(cls, v: dt.date | None, info: object) -> dt.date | None:
        return v

    def is_current(self, as_of: dt.date) -> bool:
        """Whether this version is in force on `as_of` (D-05)."""
        if as_of < self.valid_from:
            return False
        if self.valid_to is not None and as_of >= self.valid_to:
            return False
        return self.status is Status.IN_FORCE


class ClauseRecord(BaseModel):
    """The smallest citable unit of an instrument, with its canonical text.

    `canonical_text` is never edited after ingest. The verifier compares a
    citation's span against it (D-02).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    version_id: str
    clause_path: str
    heading: str | None = None
    text: str
    canonical_text: str
    scope: Scope = Scope.PUBLIC
    tenant_id: str | None = Field(
        default=None, description="None for public law; set for tenant and user scopes"
    )
    char_start: int | None = Field(
        default=None, description="Offset in the source document, for click-to-clause"
    )
    char_end: int | None = None
    page: int | None = None

    @property
    def clause_id(self) -> str:
        """The stable ID: `{instrument_id}:{version_id}:{clause_path}`."""
        return stable_id(self.instrument_id, self.version_id, self.clause_path)

    @field_validator("canonical_text")
    @classmethod
    def _already_canonical(cls, v: str) -> str:
        if v != canonicalise(v):
            raise ValueError("canonical_text must already be in canonical form")
        return v

    @classmethod
    def build(
        cls,
        *,
        instrument_id: str,
        version_id: str,
        clause_path: str,
        text: str,
        heading: str | None = None,
        scope: Scope = Scope.PUBLIC,
        tenant_id: str | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
        page: int | None = None,
    ) -> ClauseRecord:
        """Build a record, deriving `canonical_text` from `text`."""
        stable_id(instrument_id, version_id, clause_path)  # validate early
        return cls(
            instrument_id=instrument_id,
            version_id=version_id,
            clause_path=clause_path,
            heading=heading,
            text=text,
            canonical_text=canonicalise(text),
            scope=scope,
            tenant_id=tenant_id,
            char_start=char_start,
            char_end=char_end,
            page=page,
        )


class Chunk(BaseModel):
    """A retrieval unit derived from a clause, <=500 tokens.

    One chunk belongs to at most one obligation (`CONTEXT.md`). Chunks live in
    Qdrant; clauses live in Cloud Storage.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    clause_id: str
    obligation_id: str | None = None
    text: str
    scope: Scope = Scope.PUBLIC
    tenant_id: str | None = None
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None

    @field_validator("clause_id")
    @classmethod
    def _check_clause_id(cls, v: str) -> str:
        parse_stable_id(v)
        return v


class Citation(BaseModel):
    """A span of a clause quoted in an answer. The verifier checks it (D-02)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clause_id: str
    quoted_text: str

    @field_validator("clause_id")
    @classmethod
    def _check_clause_id(cls, v: str) -> str:
        parse_stable_id(v)
        return v
