"""Canonical corpus records and the stable ID scheme.

Vocabulary is `CONTEXT.md`; the names here are the names there. A clause is the
smallest citable unit of an instrument and its stable ID is
`{instrument_id}:{version_id}:{clause_path}` (D-02, D-05).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

NO_END_DATE = dt.date.max
"""Sentinel for an open-ended `valid_to` — bitemporal validity never carries
`None` (`CONTEXT.md`: Bitemporal). A real, comparable date lets every
`as_of` check and every Qdrant payload filter (issue #22) be one
unconditional `Range` on `valid_to`, not a range-or-is-null pair."""


def epoch_days(d: dt.date) -> int:
    """`d` as an integer day count — the "epoch days" a Qdrant payload
    index sorts and range-filters on (`valid_from`/`valid_to`, issue #22).
    `date.toordinal()` (proleptic Gregorian, day 1 = 0001-01-01) rather than
    a Unix-epoch offset: monotonic and collision-free is all a `Range`
    filter needs, and `NO_END_DATE.toordinal()` is a real finite int either
    way, so there's no special-casing at the boundary."""
    return d.toordinal()


# One path segment: letters, digits, and the separators legislation actually uses.
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
# The tenant part of a tenant-prefixed instrument id is a tenant id, not a
# segment (CONTEXT.md: Instrument id) — its grammar is "not ':' or '/'"
# (issue #3), not "starts with a letter or digit". Semantic rules that
# _TENANT_PREFIX can't express as a character class (`_public` reserved) are
# enforced by validate_tenant_id() in validate_instrument_id() below.
_TENANT_PREFIX = r"[^/:]+"
INSTRUMENT_ID_RE = re.compile(rf"^(?:(?P<tenant>{_TENANT_PREFIX})/)?(?P<instrument>{_SEGMENT})$")
_INSTRUMENT_ID_BODY = rf"(?:{_TENANT_PREFIX}/)?{_SEGMENT}"
VERSION_ID_RE = re.compile(rf"^{_SEGMENT}$")
# A clause path is slash-separated, hierarchical and lowercase (CONTEXT.md):
# "sch1/app11/1", "s6c/1/a", "art9/2/b". The dot is still accepted inside a
# segment for ids that genuinely contain one, e.g. an instrument id.
CLAUSE_PATH_RE = re.compile(rf"^{_SEGMENT}(?:[./]{_SEGMENT})*$")
STABLE_ID_RE = re.compile(
    rf"^(?P<instrument_id>{_INSTRUMENT_ID_BODY}):(?P<version_id>{_SEGMENT}):"
    rf"(?P<clause_path>{_SEGMENT}(?:[./]{_SEGMENT})*)$"
)


class EndReason(StrEnum):
    """Why a bitemporal record stopped being in force — stored with
    `valid_to`, set once with it (ADR-0029). An open record has none."""

    SUPERSEDED = "superseded"
    REPEALED = "repealed"
    EXPIRED = "expired"


class Status(StrEnum):
    """A record's status on one date: derived from its valid time and
    `EndReason`, never stored (ADR-0029, D-05). Storing it beside the dates
    would be two representations of one fact that can disagree."""

    NOT_YET_COMMENCED = "not_yet_commenced"
    IN_FORCE = "in_force"
    SUPERSEDED = "superseded"
    REPEALED = "repealed"
    EXPIRED = "expired"


def status_on(
    as_of: dt.date, *, valid_from: dt.date, valid_to: dt.date, end_reason: EndReason | None
) -> Status:
    """The status on `as_of`: before `valid_from` it has not commenced; from
    `valid_to` it has ended for its `end_reason`; otherwise it is in force."""
    if as_of < valid_from:
        return Status.NOT_YET_COMMENCED
    if as_of < valid_to:
        return Status.IN_FORCE
    if end_reason is None:
        raise ValueError(f"valid_to {valid_to} is set with no end_reason (ADR-0029)")
    return Status(end_reason.value)


def check_end(valid_to: dt.date, end_reason: EndReason | None) -> None:
    """An end date and an end reason come together or not at all."""
    if (valid_to == NO_END_DATE) != (end_reason is None):
        raise ValueError(
            "valid_to and end_reason are set together: an open record has neither, "
            "an ended one has both (ADR-0029)"
        )


PUBLIC_TENANT_ID = "_public"
"""Reserved tenant id for public law (`CONTEXT.md`).

Public law belongs to no company, but `tenant_id` is NOT NULL on every table
(`PHASES.md`), and a row-level-security policy needs something to match on
rather than a three-valued predicate. So public rows carry this id instead of
NULL, and `tenant_id` always names an owner.
"""


def validate_tenant_segment(value: str) -> str:
    """Reject characters that break the stable-ID grammar (`CONTEXT.md`: Tenant id).

    `:` is the only stable-ID delimiter and `/` appears inside both
    tenant-prefixed instrument ids and clause paths — a tenant id containing
    either makes `{instrument_id}:{version_id}:{clause_path}` ambiguous to
    parse. `PUBLIC_TENANT_ID` passes: it is a legitimate sentinel value
    wherever a record's scope is public (`ClauseRecord`, `Chunk`).
    """
    if not value:
        raise ValueError("tenant_id must not be empty")
    if ":" in value:
        raise ValueError(f"tenant_id must not contain ':': {value!r}")
    if "/" in value:
        raise ValueError(f"tenant_id must not contain '/': {value!r}")
    return value


def validate_tenant_id(value: str) -> str:
    """Validate a *real* tenant id, applied where a tenant enters the system.

    Stricter than `validate_tenant_segment`: also rejects `PUBLIC_TENANT_ID`
    itself, since a company may never register under the name reserved for
    public law. Apply this to `Profile` and any future tenant-creation path;
    apply `validate_tenant_segment` where `PUBLIC_TENANT_ID` is a legitimate
    field value instead (`ClauseRecord.tenant_id`, `Chunk.tenant_id`).
    """
    value = validate_tenant_segment(value)
    if value == PUBLIC_TENANT_ID:
        raise ValueError(f"{PUBLIC_TENANT_ID!r} is reserved and may not be used as a tenant id")
    return value


TenantSegment = Annotated[str, AfterValidator(validate_tenant_segment)]
"""A `tenant_id` field type for records where `PUBLIC_TENANT_ID` is itself a
legitimate value (`ClauseRecord`, `Chunk`). `Profile` uses `validate_tenant_id`
directly instead, since it is a plain dataclass and a real tenant may never be
`PUBLIC_TENANT_ID`."""


def _match_instrument_id(value: str) -> re.Match[str] | None:
    return INSTRUMENT_ID_RE.match(value)


def validate_instrument_id(value: str) -> str:
    """Validate an instrument id (`CONTEXT.md`: Instrument id, issue #4).

    Globally unique on its own (public law), or tenant-prefixed as
    `tenant/instrument` for a company's own policy — at most one `/`. The
    tenant part obeys the tenant-id rules from issue #3, so a public
    instrument can never be prefixed: `validate_tenant_id` rejects
    `PUBLIC_TENANT_ID`.
    """
    match = _match_instrument_id(value)
    if match is None:
        raise ValueError(f"invalid instrument_id: {value!r}")
    tenant = match.group("tenant")
    if tenant is not None:
        validate_tenant_id(tenant)
    return value


MODEL_AUTHOR_PREFIX = "model:"
"""How a reviewed artefact records that a model drafted it
(`corpus/mappings/README.md`: Review)."""


def validate_review(author: str, reviewed_by: str) -> None:
    """The one review rule for every artefact with `author`/`reviewed_by`
    front matter — mapping YAML, selection manifests, the golden set
    (`corpus/mappings/README.md`: Review). A model-drafted file needs a
    distinct human reviewer: the human's review is the judgement D-01
    requires. A human may review their own file, recorded as single-author
    review. A model never reviews — which is also what makes a model author
    need someone distinct."""
    if reviewed_by.startswith(MODEL_AUTHOR_PREFIX):
        raise ValueError("reviewed_by must be a person, never a model (D-01)")
    if not author.strip():
        raise ValueError("author must name who drafted the file")


def validate_library_instrument_id(value: str) -> str:
    """Validate an id a tenant may subscribe to (ADR-0020): a well-formed
    instrument id with no tenant prefix. A tenant's own documents need no
    subscription, and another tenant's are never subscribable."""
    validate_instrument_id(value)
    if instrument_tenant_prefix(value) is not None:
        raise ValueError(f"only library instruments can be subscribed to, not {value!r}")
    return value


def instrument_tenant_prefix(value: str) -> str | None:
    """The tenant prefix of a tenant-prefixed instrument id, or `None` if
    unprefixed (or not a well-formed instrument id — callers that need
    format validation should call `validate_instrument_id` first).
    """
    match = _match_instrument_id(value)
    if match is None:
        return None
    return match.group("tenant")


def instrument_owner(instrument_id: str) -> str:
    """The tenant an instrument's clauses are stored under: its prefix, or
    `_public` for an unprefixed library instrument (ADR-0020)."""
    return instrument_tenant_prefix(instrument_id) or PUBLIC_TENANT_ID


class Scope(StrEnum):
    """Which corpus partition a record belongs to (`CONTEXT.md`, D-14)."""

    PUBLIC = "public"
    TENANT = "tenant"
    USER = "user"


CANONICALISATION_VERSION = 2
"""The current canonicalisation transformation (`CONTEXT.md`: Canonicalisation
version, issue #2). `ClauseRecord` and `Citation` default to this, but the
field itself is a plain int, not validated against it: reconstructing a
stored record must be able to carry an older version untouched, so that a
citation verified under v1 is never silently re-verified under v2. A change
to `canonicalise()` bumps this constant — it is never an edit in place, and
old canonicalise() logic is not kept around: a citation recorded under a
retired version becomes permanently unverifiable rather than silently
re-run under new rules, which is the point (`CONTEXT.md`: Canonicalisation
version).

v2 (issue #9's ePub format spike): folds U+2011 (non-breaking hyphen),
used throughout real legislative text for compound words ("cross-border",
"out-of-date", "de-identified" all use it in the source) and missed by
v1 — a citation quoting an ordinary hyphen would have silently failed to
match.
"""


def canonicalise(text: str) -> str:
    """Canonical form used by the verifier on both sides of the comparison.

    NFC normalisation, curly quotes and dashes (including the non-breaking
    hyphen) folded to ASCII, all whitespace runs collapsed to a single space,
    ends stripped. The verifier compares canonical to canonical (D-02), so
    this function is load-bearing: changing it changes what counts as a
    matching citation (see `CANONICALISATION_VERSION`).
    """
    text = unicodedata.normalize("NFC", text)
    # The ambiguous characters below are the point: this table folds the
    # typographic forms legislation actually uses into ASCII. RUF001 is
    # suppressed per line rather than per file so a stray one elsewhere
    # still gets flagged. Every entry is drawn from a real full-document
    # audit (docs/research/legislation-gov-au-format.md, issue #9), not a
    # speculative list of "characters that might appear".
    for curly, plain in (
        ("‘", "'"),  # noqa: RUF001
        ("’", "'"),  # noqa: RUF001
        ("“", '"'),
        ("”", '"'),
        ("–", "-"),  # noqa: RUF001
        ("—", "-"),
        (" ", " "),  # noqa: RUF001
        ("‑", "-"),  # noqa: RUF001
    ):
        text = text.replace(curly, plain)
    return " ".join(text.split())


def sha256_hex(text: str) -> str:
    """Hex-encoded SHA-256 of `text`, UTF-8 encoded.

    The one place this encoding decision is made — `canonical_sha256` below
    and issue #19's `manifest.json` (`source_html_sha256`, `ClauseStore`'s
    `put_manifest`/`GCSClauseStore`) all hash a piece of stored text the same
    way, so a future change to the encoding is one edit, not three that can
    silently drift apart.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(canonical_text: str) -> str:
    """Hex-encoded SHA-256 of already-canonical text.

    `PHASES.md`'s per-instrument-version `manifest.json` lists every clause
    path with this hash, so `ClauseStore.get_manifest` (issue #15) has a
    single deterministic value to compare without fetching every clause
    object — the same reasoning D-02 gives the verifier's own read path.
    """
    return sha256_hex(canonical_text)


def stable_id(instrument_id: str, version_id: str, clause_path: str) -> str:
    """Build a clause's stable ID, validating each part."""
    validate_instrument_id(instrument_id)
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


def clause_anchor_id(clause_path: str) -> str:
    """The synthesized click-to-clause anchor for a clause (`CONTEXT.md`:
    Position map, issue #9's ePub format spike).

    Law's source markup carries no per-clause anchor of its own — only
    section/Part/Schedule headings do. The parser writes this id into the
    stored HTML at the corresponding position when it ingests a clause;
    citation rendering recomputes the same id from `clause_path`, which it
    already has, rather than reading one off the markup. Pure function of
    `clause_path` alone: same path, same anchor, every time.

    A clause_path segment may itself contain '-' (a defined-term slug like
    `access-seeker`, `CONTEXT.md`: Definition), so joining segments with a
    bare '-' would let two different paths collide — e.g. the segments
    `["s6", "1", "access-seeker"]` and `["s6", "1-access-seeker"]` would
    both naively join to the same string. Each segment's own '-' is
    escaped to '--' before segments are joined on a single '-', which
    keeps "a real segment boundary" and "a hyphen inside one segment"
    distinguishable no matter what the segments contain.
    """
    escaped_segments = (segment.replace("-", "--") for segment in clause_path.split("/"))
    return "clause-" + "-".join(escaped_segments)


_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def definition_slug(term: str) -> str:
    """The `clause_path` segment for one of s6's flat, unnumbered
    Definition paragraphs (`CONTEXT.md`: Definition).

    Lowercase, every run of non-alphanumeric characters folded to a single
    '-', ends stripped: `"ACT enactment"` -> `"act-enactment"`. Pure
    function of the term text alone — same term always yields the same
    slug, no counter, no dependence on parse order or on any other term
    seen. A positional index would silently repoint an already-issued
    citation whenever a later compilation inserts a new term alphabetically
    before an existing one; this only changes if the term itself is
    renamed, and a rename is a new clause_path in the new version_id, via
    ordinary supersession — never a mutation of the old path.

    Two different terms colliding on the same slug is
    `assert_unique_definition_slugs`'s problem, not this function's: taken
    alone, a term always slugs the same way regardless of what else is in
    the section.
    """
    slug = _NON_SLUG_CHARS.sub("-", term.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"term has no sluggable characters: {term!r}")
    return slug


def assert_unique_clause_ids(clause_ids: Iterable[str]) -> None:
    """Raise if one stable ID is produced twice. Two provisions on one path
    would make a citation ambiguous — the stable ID is what the verifier
    looks up (`CONTEXT.md`: Instrument id, issue #97)."""
    duplicates = sorted(k for k, n in Counter(clause_ids).items() if n > 1)
    if duplicates:
        raise ValueError(f"{len(duplicates)} clause id(s) produced twice: {duplicates[:5]}")


def assert_unique_definition_slugs(terms: Iterable[str]) -> None:
    """Raise if two distinct terms in one section produce the same
    `definition_slug` (`CONTEXT.md`: Definition).

    A collision is a hard parse failure, never a silently suffixed `-2`:
    a suffix would be assigned by parse order, reintroducing exactly the
    citation instability a stable ID exists to prevent.
    """
    seen: dict[str, str] = {}
    for term in terms:
        slug = definition_slug(term)
        if slug in seen and seen[slug] != term:
            raise ValueError(
                f"duplicate definition slug {slug!r}: {seen[slug]!r} and {term!r} "
                "both slug to the same clause_path segment"
            )
        seen[slug] = term


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
        return validate_instrument_id(v)


class InstrumentVersion(BaseModel):
    """One compilation of an instrument, identified by the register's own ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: str
    version_id: str
    title: str
    valid_from: dt.date
    valid_to: dt.date = NO_END_DATE
    end_reason: EndReason | None = None
    recorded_at: dt.datetime
    source_url: str | None = None
    supersedes: str | None = Field(
        default=None, description="version_id of the version this one supersedes (D-05)"
    )

    @model_validator(mode="after")
    def _end(self) -> InstrumentVersion:
        check_end(self.valid_to, self.end_reason)
        return self

    def status_on(self, as_of: dt.date) -> Status:
        return status_on(
            as_of, valid_from=self.valid_from, valid_to=self.valid_to, end_reason=self.end_reason
        )

    def is_current(self, as_of: dt.date) -> bool:
        """Whether this version is in force on `as_of` (D-05)."""
        return self.status_on(as_of) is Status.IN_FORCE


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
    canonicalisation_version: int = Field(
        default=CANONICALISATION_VERSION,
        description="Version of canonicalise() that produced canonical_text (issue #2)",
    )
    scope: Scope = Scope.PUBLIC
    tenant_id: TenantSegment = Field(
        default=PUBLIC_TENANT_ID,
        description="`_public` for law; the owning tenant for tenant and user scopes",
    )
    char_start: int | None = Field(
        default=None,
        description="PDF page/character offset for click-to-clause (CONTEXT.md: Position "
        "map). Law uses clause_anchor_id(clause_path) instead — these stay None for law.",
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

    @model_validator(mode="after")
    def _tenant_consistency(self) -> ClauseRecord:
        if self.scope is Scope.PUBLIC and self.tenant_id != PUBLIC_TENANT_ID:
            raise ValueError(f"public scope requires tenant_id {PUBLIC_TENANT_ID!r}")
        if self.scope is not Scope.PUBLIC and self.tenant_id == PUBLIC_TENANT_ID:
            raise ValueError(f"{PUBLIC_TENANT_ID!r} is reserved for public law")
        prefix = instrument_tenant_prefix(self.instrument_id)
        if prefix is not None and prefix != self.tenant_id:
            raise ValueError(
                f"instrument_id tenant prefix {prefix!r} "
                f"does not match tenant_id {self.tenant_id!r}"
            )
        return self

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
        tenant_id: str = PUBLIC_TENANT_ID,
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
    """A retrieval unit derived from a clause, <=`chunker.CHUNK_MAX_TOKENS`
    (420) as counted by the reranker's own tokenizer, not a word count
    (`CONTEXT.md`: Chunk, issue #6).

    A chunk carries its clause, never an obligation: which obligations derive
    from a clause is the graph's (ADR-0030, #129). Chunks live in Qdrant; clauses live
    in Cloud Storage.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    clause_id: str
    text: str
    scope: Scope = Scope.PUBLIC
    tenant_id: TenantSegment = PUBLIC_TENANT_ID
    valid_from: dt.date = dt.date.min
    valid_to: dt.date = NO_END_DATE
    synthetic: bool = False
    """Invented test data, not a customer's document (ADR-0023). Defaults to
    False on purpose: an unmarked tenant chunk counts as real customer data,
    which closes the offshore-generation gate."""

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
    canonicalisation_version: int = Field(
        default=CANONICALISATION_VERSION,
        description="Version the citation was verified under (issue #2)",
    )

    @field_validator("clause_id")
    @classmethod
    def _check_clause_id(cls, v: str) -> str:
        parse_stable_id(v)
        return v
