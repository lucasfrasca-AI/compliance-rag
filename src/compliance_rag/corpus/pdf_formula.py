"""s 6DA(2)'s formula from the PDF compilation, checked against the form a
person signed (#338, ADR-0058; `docs/research/s6da-2-pdf-extraction.md`).

The pinned ePub sets the formula as an image, so the parse stores s 6DA(2)
incomplete (ADR-0047). The Register's PDF of the same compilation sets it as
text: seven spans of words, one symbol-font glyph for the operator, and a
vector line for the fraction bar. The words are parse-grade; how a fraction
is written in a line of text is not something the PDF says. So:

- **the PDF supplies the words.** `extract_page` reads every span and every
  horizontal rule of one page, with pymupdf, from bytes whose sha256 is the
  pin; `read_formula` sorts the formula's spans into its blocks by geometry
  alone: the operator is the page's one span in the signed operator font; the
  formula is the spans in the operator's type size; the bar is the one rule
  inside them; a span over the bar's width is the numerator above it and the
  denominator below it; every other span is left of the operator. Each
  block's lines are read top to bottom.
- **the signed form supplies the structure** (`formula.yaml` beside the
  version's manifest): the reading convention (numerator above the bar, the
  operator glyph the font's code stands for) and the linear form a fraction
  is written in. It also records the words, and the PDF's words must be
  exactly those, in the same order, block by block.

Anything else fails loudly: other bytes, a page whose geometry reads
differently (a span that straddles the bar, no bar or two, an operator in
another font or glyph), or words that differ from the signed ones. Nothing
is completed from a reading that doesn't match.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

import pymupdf
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compliance_rag.corpus.models import (
    Incompleteness,
    canonicalise,
    parse_stable_id,
    validate_review,
)
from compliance_rag.corpus.second_source import ClauseCompletion, SecondSource

FILE_NAME = "formula.yaml"
PENDING = "PENDING"
BLOCKS = ("left", "numerator", "denominator")
_FORM_FIELDS = frozenset({*BLOCKS, "operator"})
_SIZE_TOLERANCE = 0.01
"""Type sizes compare equal within this (points): the PDF's are floats."""


class FormulaGeometryError(ValueError):
    """The page doesn't read as the signed convention says a formula does."""


class FormulaMismatch(ValueError):
    """The PDF's words are not the signed words: other words, or another
    order, in a block."""


class UnsignedFormula(ValueError):
    """A signed form nobody has signed: never used (D-01)."""


# --------------------------------------------------------------------------
# The page, as the PDF sets it
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PdfSpan:
    text: str
    font: str
    size: float
    bbox: tuple[float, float, float, float]
    """x0, y0, x1, y1 in points; y grows downwards."""

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]


@dataclass(frozen=True, slots=True)
class PdfRule:
    """A stroked horizontal line: a page rule, or a fraction bar."""

    x0: float
    x1: float
    y: float


@dataclass(frozen=True, slots=True)
class PdfPage:
    """One page's text spans and horizontal rules, in the PDF's own order."""

    sha256: str
    """The PDF's bytes it was read from."""
    page: int
    """1-based, as a reader counts pages."""
    spans: tuple[PdfSpan, ...]
    rules: tuple[PdfRule, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "page": self.page,
            "spans": [
                {"text": s.text, "font": s.font, "size": s.size, "bbox": list(s.bbox)}
                for s in self.spans
            ],
            "rules": [{"x0": r.x0, "x1": r.x1, "y": r.y} for r in self.rules],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> PdfPage:
        # The fixture's own JSON, read as `to_json` wrote it.
        spans = cast("list[dict[str, Any]]", data["spans"])  # Any: json.loads
        rules = cast("list[dict[str, Any]]", data["rules"])  # Any: json.loads
        return cls(
            sha256=str(data["sha256"]),
            page=int(cast("int", data["page"])),
            spans=tuple(
                PdfSpan(
                    text=str(s["text"]),
                    font=str(s["font"]),
                    size=float(s["size"]),
                    bbox=cast("tuple[float, float, float, float]", tuple(map(float, s["bbox"]))),
                )
                for s in spans
            ),
            rules=tuple(PdfRule(float(r["x0"]), float(r["x1"]), float(r["y"])) for r in rules),
        )


def _r(value: float) -> float:
    """Two decimals: what a reading depends on, and stable across pymupdf
    builds."""
    return round(float(value), 2)


def extract_page(pdf: bytes, *, page: int) -> PdfPage:
    """Every non-blank text span and every stroked horizontal rule of `page`
    (1-based), with pymupdf. Reads text as the PDF's text layer gives it:
    nothing is guessed, mapped or reordered here."""
    document = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        # pymupdf's own stubs leave these untyped (as in policy_parser.py), hence the
        # ignores; each value is converted where it is read.
        count = int(document.page_count)  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
        if not 1 <= page <= count:
            raise FormulaGeometryError(f"the PDF has no page {page}")
        source = document[page - 1]
        raw = cast(
            "dict[str, Any]",  # Any: pymupdf is untyped
            source.get_text("rawdict"),  # pyright: ignore[reportUnknownMemberType]
        )
        spans: list[PdfSpan] = []
        for block in cast("list[dict[str, Any]]", raw["blocks"]):
            for line in cast("list[dict[str, Any]]", block.get("lines", [])):
                for span in cast("list[dict[str, Any]]", line["spans"]):
                    text = "".join(str(c["c"]) for c in span["chars"])
                    if not text.strip():
                        continue
                    x0, y0, x1, y1 = (float(v) for v in span["bbox"])
                    spans.append(
                        PdfSpan(
                            text=text,
                            font=str(span["font"]),
                            size=_r(span["size"]),
                            bbox=(_r(x0), _r(y0), _r(x1), _r(y1)),
                        )
                    )
        rules: list[PdfRule] = []
        drawings = source.get_drawings()  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        for drawing in cast("list[dict[str, Any]]", drawings):
            for item in cast("list[tuple[Any, ...]]", drawing["items"]):
                if item[0] != "l":
                    continue
                start, end = item[1], item[2]
                if _r(start.y) == _r(end.y) and _r(start.x) != _r(end.x):
                    rules.append(
                        PdfRule(_r(min(start.x, end.x)), _r(max(start.x, end.x)), _r(start.y))
                    )
        return PdfPage(
            sha256=hashlib.sha256(pdf).hexdigest(),
            page=page,
            spans=tuple(spans),
            rules=tuple(rules),
        )
    finally:
        document.close()


# --------------------------------------------------------------------------
# The signed form
# --------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PdfSourceRef(_Strict):
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # Rust regex: `$` is end of text
    page: int = Field(ge=1)
    """1-based."""


class OperatorConvention(_Strict):
    font: str
    """The font the operator is set in; the page must hold exactly one span in it."""
    extracted: str
    """What the PDF's text layer gives for the glyph (its ToUnicode map)."""
    reads_as: str
    """The character the glyph is: the signed reading of the font's code."""


class Convention(_Strict):
    numerator: Literal["above_the_bar"]
    operator: OperatorConvention
    form: str
    """How the formula is written in a line: `{left}`, `{operator}`,
    `{numerator}` and `{denominator}`, each once, and nothing else in braces."""

    @field_validator("form")
    @classmethod
    def _fields(cls, v: str) -> str:
        names = [name for _, name, _, _ in string.Formatter().parse(v) if name is not None]
        if sorted(names) != sorted(_FORM_FIELDS):
            raise ValueError(f"the form names each of {sorted(_FORM_FIELDS)} once: {v!r}")
        return v


class FormulaWords(_Strict):
    left: str
    numerator: str
    denominator: str


class SignedFormula(_Strict):
    clause_id: str
    completes: Incompleteness
    source: PdfSourceRef
    convention: Convention
    words: FormulaWords
    canonical_text: str
    """The clause's whole canonical text, as signed."""

    @field_validator("clause_id")
    @classmethod
    def _stable(cls, v: str) -> str:
        parse_stable_id(v)
        return v

    @field_validator("canonical_text")
    @classmethod
    def _canonical(cls, v: str) -> str:
        if canonicalise(v) != v:
            raise ValueError("the signed text is in canonical form")
        return v

    @property
    def clause_path(self) -> str:
        return parse_stable_id(self.clause_id)[2]


class SignedFormulaFile(_Strict):
    author: str
    reviewed_by: str
    date: str
    source_clause_ids: tuple[str, ...] = Field(min_length=1)
    formulas: tuple[SignedFormula, ...] = Field(min_length=1)

    @field_validator("date", mode="before")
    @classmethod
    def _iso(cls, v: object) -> object:
        return v.isoformat() if isinstance(v, dt.date) else v

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        validate_review(self.author, self.reviewed_by)
        named = [f.clause_id for f in self.formulas]
        if len(named) != len(set(named)):
            raise ValueError("a clause's formula is signed once")
        if set(self.source_clause_ids) != set(named):
            raise ValueError(
                f"source_clause_ids {sorted(self.source_clause_ids)} are not the clauses "
                f"completed {sorted(named)}"
            )
        if self.is_signed_off:
            dt.date.fromisoformat(self.date)
        return self

    @property
    def is_signed_off(self) -> bool:
        return not (self.reviewed_by.startswith(PENDING) or self.date.startswith(PENDING))


def parse_signed_formula_file(
    source: str, *, instrument_id: str, version_id: str
) -> SignedFormulaFile:
    """One `formula.yaml`, for the instrument version whose folder holds it.
    Refused while pending review, and if it names another version's clause."""
    loaded = yaml.safe_load(source)
    if not isinstance(loaded, dict):
        raise ValueError(f"{FILE_NAME} must be a YAML mapping")
    data = cast("dict[str, Any]", loaded)  # Any: yaml.safe_load is untyped
    signed = SignedFormulaFile.model_validate(data)
    if not signed.is_signed_off:
        raise UnsignedFormula(
            f"{instrument_id}/{version_id}/{FILE_NAME} is awaiting review, not used (D-01)"
        )
    for formula in signed.formulas:
        found_instrument, found_version, _ = parse_stable_id(formula.clause_id)
        if (found_instrument, found_version) != (instrument_id, version_id):
            raise ValueError(f"{formula.clause_id} is not a clause of {instrument_id}:{version_id}")
    return signed


def load_signed_formulas(version_dir: Path) -> tuple[SignedFormula, ...]:
    """The signed formulas of the instrument version `version_dir` holds
    (`corpus/instruments/{instrument}/{version}/`); none if it has no
    `formula.yaml`."""
    path = version_dir / FILE_NAME
    if not path.exists():
        return ()
    return parse_signed_formula_file(
        path.read_text(encoding="utf-8"),
        instrument_id=version_dir.parent.name,
        version_id=version_dir.name,
    ).formulas


# --------------------------------------------------------------------------
# The reading
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FormulaReading:
    """The formula as the page's geometry sets it, under the convention."""

    left: str
    operator: str
    numerator: str
    denominator: str


def _lines(spans: Sequence[PdfSpan]) -> str:
    return " ".join(s.text.strip() for s in sorted(spans, key=lambda s: (s.y0, s.x0)))


def read_formula(page: PdfPage, convention: Convention) -> FormulaReading:
    """The formula on `page`, by geometry alone (module docstring)."""
    operator_font = convention.operator.font
    operators = [s for s in page.spans if s.font == operator_font]
    if len(operators) != 1:
        raise FormulaGeometryError(
            f"page {page.page} has {len(operators)} span(s) in {operator_font}, "
            "not the one operator"
        )
    operator = operators[0]
    if operator.text != convention.operator.extracted:
        raise FormulaGeometryError(
            f"the operator in {operator_font} reads {operator.text!r}, not the signed "
            f"{convention.operator.extracted!r}"
        )
    terms = [
        s
        for s in page.spans
        if s is not operator and abs(s.size - operator.size) <= _SIZE_TOLERANCE
    ]
    if not terms:
        raise FormulaGeometryError(f"no text in the operator's type size ({operator.size} pt)")
    top, bottom = min(s.y0 for s in terms), max(s.y1 for s in terms)
    bars = [r for r in page.rules if top < r.y < bottom]
    if len(bars) != 1:
        raise FormulaGeometryError(
            f"{len(bars)} rule(s) inside the formula (y {top} to {bottom}), not one fraction bar"
        )
    bar = bars[0]
    if not operator.x1 <= bar.x0:
        raise FormulaGeometryError("the operator is not before the fraction")
    blocks: dict[str, list[PdfSpan]] = {name: [] for name in BLOCKS}
    for span in terms:
        centre = (span.x0 + span.x1) / 2
        if bar.x0 <= centre <= bar.x1:
            if span.y1 <= bar.y:
                blocks["numerator"].append(span)
            elif span.y0 >= bar.y:
                blocks["denominator"].append(span)
            else:
                raise FormulaGeometryError(f"{span.text!r} straddles the fraction bar")
        elif span.x1 <= operator.x0:
            blocks["left"].append(span)
        else:
            raise FormulaGeometryError(
                f"{span.text!r} is neither left of the operator nor over the bar"
            )
    empty = [name for name, spans in blocks.items() if not spans]
    if empty:
        raise FormulaGeometryError(f"no text in the formula's {empty}")
    return FormulaReading(
        left=_lines(blocks["left"]),
        operator=convention.operator.reads_as,
        numerator=_lines(blocks["numerator"]),
        denominator=_lines(blocks["denominator"]),
    )


def check_reading(reading: FormulaReading, signed: SignedFormula) -> None:
    """The PDF's words are the signed words, block by block, in order."""
    for block in BLOCKS:
        found, expected = getattr(reading, block), getattr(signed.words, block)
        if found.split() != expected.split():
            raise FormulaMismatch(
                f"{signed.clause_id}: the PDF's {block} reads {found!r}, the signed form "
                f"{expected!r}"
            )


def completion_from_page(page: PdfPage, signed: SignedFormula) -> ClauseCompletion:
    """The completion `signed` describes, from a page already read. Refused
    unless the page is the signed source's page, of its bytes, and reads as
    the signed words."""
    if (page.sha256, page.page) != (signed.source.sha256, signed.source.page):
        raise FormulaMismatch(
            f"{signed.clause_id}: the page read is page {page.page} of {page.sha256}, not the "
            f"signed page {signed.source.page} of {signed.source.sha256}"
        )
    reading = read_formula(page, signed.convention)
    check_reading(reading, signed)
    text = signed.convention.form.format(
        left=reading.left,
        operator=reading.operator,
        numerator=reading.numerator,
        denominator=reading.denominator,
    )
    return ClauseCompletion(
        source=SecondSource(
            clause_path=signed.clause_path,
            sha256=signed.source.sha256,
            url=signed.source.url,
            locator=f"page {signed.source.page}",
        ),
        text=text,
        completes=signed.completes,
        canonical_text=signed.canonical_text,
    )


def formula_completion(pdf: bytes, signed: SignedFormula) -> ClauseCompletion:
    """The completion, from the PDF's bytes: refused unless they are the
    signed source's (its sha256)."""
    digest = hashlib.sha256(pdf).hexdigest()
    if digest != signed.source.sha256:
        raise FormulaMismatch(
            f"{signed.clause_id}: the PDF read has sha256 {digest}, not the signed "
            f"{signed.source.sha256}: the Register served other bytes"
        )
    return completion_from_page(extract_page(pdf, page=signed.source.page), signed)
