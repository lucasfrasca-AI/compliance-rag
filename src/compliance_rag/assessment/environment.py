"""A question that names an instrument outside the tenant's environment
(#252, ADR-0051).

A tenant's environment is what its retrieval can see on `as_of`: the library
instruments it subscribes to and its own documents (`CONTEXT.md`: Subscription,
Document class). A question can still name something else ("under the spam
act, can I…"), and retrieval then answers it from whatever is nearest, which
for the Spam Act is APP 7 on direct marketing. The verifier can't see the
problem: every quote matches its clause. So the pipeline decides, from the
question alone and before anything is retrieved, which instruments it names
that the environment doesn't hold, and every layer is told
(`NotInEnvironment`): the model in a fenced note, the reader in a structured
field the model never sets.

**Deterministic, no model.** Two matchers read the question, which is the
member's own input, never retrieved text (#29):

- **The register** (`corpus/register/instruments.yaml`): a reviewed list of
  instrument names and aliases, matched case-insensitively as whole words. An
  entry is an instrument the library holds (`library_id`), one it doesn't
  (the Spam Act 2003, the GDPR), or a **part** of a library instrument
  (`part_of`, with the clause-path prefixes it covers): the Notifiable Data
  Breaches scheme is Part IIIC of the Privacy Act, which a tenant can hold
  while the selection holds none of Part IIIC. A part counts as held only
  when a committed selection manifest of its instrument selects into it. The
  register is reviewed content under D-01, so a pending one is refused, never
  used; with none committed the pattern works alone.
- **The pattern**: "<Name> Act [year]", for names the register doesn't hold.
  Conservative: a title-case name before a capitalised "Act" ("Spam Act"); a
  lowercase one only where the words around it read as a statute's name ("under
  the spam act", "does the spam act apply", "spam act 2003"), never the verb
  ("how should the officer act on it"). The all-capitals "ACT" is the
  territory, never a statute.

**Held** is decided against the ledger's record of the environment
(`LedgerStore.environment_titles`): a register entry by its library id (or
by its name in a held document's recorded title, for a tenant that supplied it
as an external upload); a pattern match by its words appearing, in order, in a
held document's recorded title (ADR-0049): "privacy act" is in "Privacy Act
1988"; "privacy act 2020" is not.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compliance_rag.corpus.models import validate_library_instrument_id, validate_review
from compliance_rag.corpus.selection_manifest import parse_selection_manifest

REGISTER_PATH = Path(__file__).resolve().parents[3] / "corpus" / "register" / "instruments.yaml"
INSTRUMENTS_ROOT = Path(__file__).resolve().parents[3] / "corpus" / "instruments"
PENDING = "PENDING"

EnvironmentTitles = Mapping[str, frozenset[str]]
"""A tenant's environment on a date, as the ledger records it: each instrument
its retrieval can see, to the recorded titles of its versions in force."""

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ()/,.'\u2019-]*[A-Za-z0-9)]\Z")
"""A register name, and so what can reach a prompt marker from it: one line,
no `"`, `]`, `;` or `[`."""
_TITLE_WORD_RE = re.compile(r"[a-z0-9]+")
_ACT_RE = re.compile(r"(?<![A-Za-z0-9])(act)(?![A-Za-z0-9])", re.IGNORECASE)
_YEAR_RE = re.compile(r"\s+((?:18|19|20)\d{2})(?![0-9])")
_WORD_BEFORE_RE = re.compile(r"(\(?[A-Za-z][A-Za-z'\u2019-]*\)?)\s+\Z")
_NEXT_RE = re.compile(r"\s*([A-Za-z]+|\S)")
_PATH_PREFIX_RE = re.compile(r"^[a-z0-9]+[a-z0-9/]*\Z")
MAX_NAME_WORDS = 6

STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "although",
        "always",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "cannot",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "each",
        "either",
        "else",
        "even",
        "ever",
        "every",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "however",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "least",
        "less",
        "let",
        "may",
        "me",
        "might",
        "more",
        "most",
        "must",
        "my",
        "neither",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "ought",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "per",
        "please",
        "rather",
        "re",
        "same",
        "shall",
        "she",
        "should",
        "since",
        "so",
        "some",
        "still",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "though",
        "through",
        "thus",
        "to",
        "too",
        "under",
        "until",
        "up",
        "upon",
        "us",
        "very",
        "via",
        "was",
        "we",
        "were",
        "what",
        "whatever",
        "when",
        "whenever",
        "where",
        "whether",
        "which",
        "while",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "yet",
        "you",
        "your",
        "yours",
    ]
)
"""Words that are never part of an instrument's name in the pattern: function
words, so "the Act", "must act" and "how do I act" name nothing."""

TITLE_CONNECTORS = frozenset({"and", "of", "for"})
"""Lowercase words a title-case name may hold between two capitalised ones
("Australian Securities and Investments Commission Act")."""

STATUTE_AFTER = frozenset({"under", "per", "re", "regarding", "concerning", "against"})
STATUTE_AFTER_THE = STATUTE_AFTER | frozenset(
    [
        "in",
        "by",
        "of",
        "with",
        "within",
        "from",
        "about",
        "to",
        "for",
        "on",
        "via",
        "through",
        "beyond",
        "outside",
        "and",
        "or",
        "vs",
        "versus",
        "than",
        "breach",
        "breaches",
        "breached",
        "breaching",
        "violate",
        "violates",
        "violated",
        "violating",
        "comply",
        "complies",
        "complying",
        "contravene",
        "contravenes",
        "contravened",
        "contravening",
    ]
)
"""The word before a lowercase name ("under spam act"), or before the "the" in
front of it ("under the spam act"), that reads it as a statute's."""

STATUTE_CONTINUES = frozenset(
    [
        "apply",
        "applies",
        "applied",
        "require",
        "requires",
        "required",
        "say",
        "says",
        "said",
        "state",
        "states",
        "allow",
        "allows",
        "allowed",
        "permit",
        "permits",
        "permitted",
        "prohibit",
        "prohibits",
        "prohibited",
        "cover",
        "covers",
        "mean",
        "means",
        "define",
        "defines",
        "govern",
        "governs",
        "regulate",
        "regulates",
        "impose",
        "imposes",
        "relevant",
        "s",
        "section",
        "sections",
        "part",
        "parts",
        "schedule",
        "division",
    ]
)
"""The word after "the <name> act" that reads it as a statute ("does the spam
act apply"), whatever came before."""

VERB_FOLLOWS = frozenset(
    [
        "on",
        "as",
        "upon",
        "in",
        "for",
        "to",
        "like",
        "if",
        "when",
        "quickly",
        "promptly",
        "fairly",
        "responsibly",
        "reasonably",
        "lawfully",
        "appropriately",
        "accordingly",
        "immediately",
        "independently",
        "together",
        "alone",
        "now",
        "then",
        "properly",
        "correctly",
        "honestly",
        "ethically",
        "swiftly",
        "decisively",
        "differently",
        "within",
        "without",
        "against",
        "with",
    ]
)
"""The word after "act" that reads it as the verb ("the board act on advice")."""


class UnsignedInstrumentRegister(ValueError):
    """A register nobody has signed: refused, never used (D-01)."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _name(value: str) -> str:
    if not _NAME_RE.match(value) or "  " in value:
        raise ValueError(
            f"{value!r}: a name is one line of letters, digits, spaces and plain punctuation"
        )
    return value


class RegisterEntry(_Strict):
    """One instrument the register knows by name."""

    known_as: str
    """Its name, as a reader is shown it: "Spam Act 2003"."""
    aliases: tuple[str, ...] = ()
    """Other names a question may use for it: "Spam Act". Matched, like
    `known_as`, case-insensitively and as whole words."""
    library_id: str | None = None
    """The library's instrument id, when the library holds it."""
    part_of: str | None = None
    """For a part of a library instrument: that instrument's id."""
    paths: tuple[str, ...] = ()
    """For a part: the clause-path prefixes it covers, as plain string
    prefixes (`s26w` covers `s26wa` to `s26wt`)."""

    @field_validator("known_as")
    @classmethod
    def _known_as(cls, v: str) -> str:
        return _name(v)

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_name(a) for a in v)

    @field_validator("library_id", "part_of")
    @classmethod
    def _library(cls, v: str | None) -> str | None:
        return None if v is None else validate_library_instrument_id(v)

    @field_validator("paths")
    @classmethod
    def _paths(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for prefix in v:
            if not _PATH_PREFIX_RE.match(prefix):
                raise ValueError(f"{prefix!r} is not a clause-path prefix")
        return v

    @model_validator(mode="after")
    def _part_or_instrument(self) -> RegisterEntry:
        if self.library_id is not None and self.part_of is not None:
            raise ValueError(f"{self.known_as}: an instrument or a part of one, not both")
        if (self.part_of is None) != (not self.paths):
            raise ValueError(f"{self.known_as}: a part names its instrument and its paths")
        return self

    @property
    def names(self) -> tuple[str, ...]:
        return (self.known_as, *self.aliases)


class RegisterFile(_Strict):
    """`corpus/register/instruments.yaml`: reviewed content (D-01)."""

    author: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    date: str = Field(min_length=1)
    instruments: tuple[RegisterEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reviewed_and_unambiguous(self) -> RegisterFile:
        validate_review(self.author, self.reviewed_by)
        seen: dict[tuple[str, ...], str] = {}
        for entry in self.instruments:
            for name in entry.names:
                key = _words(name)
                if key in seen and seen[key] != entry.known_as:
                    raise ValueError(f"{name!r} names both {seen[key]} and {entry.known_as}")
                seen[key] = entry.known_as
        return self

    @property
    def is_signed_off(self) -> bool:
        return not (self.reviewed_by.startswith(PENDING) or self.date.startswith(PENDING))


@dataclass(frozen=True, slots=True)
class InstrumentRegister:
    """The signed register, with each part resolved against the committed
    selection: empty when none is committed, so the pattern works alone."""

    entries: tuple[RegisterEntry, ...] = ()
    selected_parts: frozenset[str] = frozenset()
    """The `known_as` of each part a committed selection manifest selects
    into: held wherever its instrument is."""

    def library_name(self, instrument_id: str) -> str:
        """How a reader is shown a library instrument a part is of."""
        for entry in self.entries:
            if entry.library_id == instrument_id:
                return entry.known_as
        return instrument_id


def _overlaps(entry: str, prefix: str) -> bool:
    """Whether a selection manifest entry (`selection_manifest._selects`
    grammar) selects any clause path starting with `prefix`."""
    selected = entry.rstrip("/")
    return selected.startswith(prefix) or prefix == selected or prefix.startswith(f"{selected}/")


def part_selected(entry: RegisterEntry, instruments_root: Path = INSTRUMENTS_ROOT) -> bool:
    """A part is selected when a committed selection manifest of its
    instrument selects into one of its prefixes."""
    if entry.part_of is None:
        raise ValueError(f"{entry.known_as} is not a part")
    for path in sorted((instruments_root / entry.part_of).glob("*/manifest.yaml")):
        manifest = parse_selection_manifest(path.read_text(encoding="utf-8"))
        if any(_overlaps(e, p) for e in manifest.prefixes for p in entry.paths):
            return True
    return False


def load_instrument_register(
    path: Path = REGISTER_PATH, instruments_root: Path = INSTRUMENTS_ROOT
) -> InstrumentRegister:
    """The register at `path`, or an empty one where there is none. A pending
    file is refused (D-01), as `load_mappings.py` refuses a pending mapping."""
    if not path.exists():
        return InstrumentRegister()
    loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))  # Any: YAML is untyped
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} must be a YAML mapping")
    review = RegisterFile.model_validate(cast("dict[str, Any]", loaded))  # Any: as above
    if not review.is_signed_off:
        raise UnsignedInstrumentRegister(f"{path.name} is awaiting review, not used (D-01)")
    selected = frozenset(
        e.known_as
        for e in review.instruments
        if e.part_of is not None and part_selected(e, instruments_root)
    )
    return InstrumentRegister(review.instruments, selected)


@functools.cache
def committed_instrument_register() -> InstrumentRegister:
    """The register in this checkout, read once: what the app uses."""
    return load_instrument_register()


# ---- what a question names -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NamedInstrument:
    """Words of the question that name an instrument."""

    named: str
    """The question's words, as written, whitespace folded to single spaces."""
    start: int
    end: int
    words: tuple[str, ...]
    """The name's words, lowercased: what is looked for in a held title."""
    entry: RegisterEntry | None = None
    """The register's entry, when the register matched it."""


def _words(text: str) -> tuple[str, ...]:
    return tuple(_TITLE_WORD_RE.findall(text.lower()))


def _fold(text: str) -> str:
    return " ".join(text.split())


@functools.cache
def _register_patterns(
    entries: tuple[RegisterEntry, ...],
) -> tuple[tuple[re.Pattern[str], RegisterEntry], ...]:
    return tuple(
        (
            re.compile(
                r"(?<![A-Za-z0-9])"
                + r"\s+".join(re.escape(w) for w in name.split())
                + r"(?![A-Za-z0-9])",
                re.IGNORECASE,
            ),
            entry,
        )
        for entry in entries
        for name in entry.names
    )


def _register_matches(question: str, register: InstrumentRegister) -> list[NamedInstrument]:
    found = [
        (m.start(), m.end(), entry)
        for pattern, entry in _register_patterns(register.entries)
        for m in pattern.finditer(question)
    ]
    # The longest at each place, and never two that overlap.
    found.sort(key=lambda f: (f[0], f[0] - f[1]))
    kept: list[NamedInstrument] = []
    for start, end, entry in found:
        if kept and start < kept[-1].end:
            continue
        text = question[start:end]
        kept.append(NamedInstrument(_fold(text), start, end, _words(text), entry))
    return kept


def _word_before(question: str, pos: int) -> tuple[str, int] | None:
    """The word ending just before `pos`, with only whitespace between."""
    match = _WORD_BEFORE_RE.search(question, 0, pos)
    return (match.group(1), match.start(1)) if match else None


def _bare(word: str) -> str:
    return word.strip("()").lower()


def _is_capitalised(word: str) -> bool:
    return word.strip("(")[:1].isupper()


def _connects(question: str, word: str, at: int) -> bool:
    """Whether `word`, at `at`, joins two capitalised words of a title-case
    name ("Securities and Investments")."""
    left = _word_before(question, at)
    return (
        _bare(word) in TITLE_CONNECTORS
        and not _is_capitalised(word)
        and left is not None
        and _is_capitalised(left[0])
        and _bare(left[0]) not in STOPWORDS
    )


def _balanced(words: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """The longest tail of `words` whose parentheses balance."""
    while words:
        text = " ".join(w for w, _ in words)
        if text.count("(") == text.count(")") and not text.startswith(")"):
            return words
        words = words[1:]
    return words


def _pattern_match(question: str, act: re.Match[str]) -> NamedInstrument | None:
    """A "<Name> Act [year]" at `act`, or None where the words around it read
    as anything but a statute's name."""
    if act.group(1) == "ACT":  # the Australian Capital Territory
        return None
    year = _YEAR_RE.match(question, act.end())
    end = year.end() if year else act.end()
    after = _NEXT_RE.match(question, end)
    next_word = after.group(1).lower() if after else None

    title_case = act.group(1)[0] == "A"
    name: list[tuple[str, int]] = []
    pos = act.start()
    while len(name) < MAX_NAME_WORDS and (found := _word_before(question, pos)) is not None:
        word, at = found
        if _bare(word) in STOPWORDS and not (title_case and name and _connects(question, word, at)):
            break
        name.insert(0, found)
        pos = at
    name = _balanced(name)
    while name and _bare(name[0][0]) in TITLE_CONNECTORS:  # a name never starts with one
        name = name[1:]
    if not name:
        return None
    # The two words before the name: "under the" in "under the spam act".
    first = _word_before(question, name[0][1])
    before = _bare(first[0]) if first else None
    second = _word_before(question, first[1]) if first else None
    before_the = _bare(second[0]) if second and before == "the" else None

    is_title = title_case and all(
        _is_capitalised(w) for w, _ in name if _bare(w) not in TITLE_CONNECTORS
    )
    reads_as_statute = (
        is_title
        or year is not None
        or before in STATUTE_AFTER
        or (
            before == "the"
            and (
                next_word in STATUTE_CONTINUES
                or (before_the in STATUTE_AFTER_THE and next_word not in VERB_FOLLOWS)
            )
        )
    )
    if not reads_as_statute:
        return None
    start = name[0][1]
    text = question[start:end]
    return NamedInstrument(_fold(text), start, end, _words(text))


def named_instruments(question: str, register: InstrumentRegister) -> tuple[NamedInstrument, ...]:
    """Every instrument the question names, in order: the register's names
    first, then the pattern's, for any "Act" the register didn't cover."""
    found = _register_matches(question, register)
    for act in _ACT_RE.finditer(question):
        if any(n.start <= act.start() < n.end for n in found):
            continue
        match = _pattern_match(question, act)
        if match is not None and not any(
            n.start < match.end and match.start < n.end for n in found
        ):
            found.append(match)
    return tuple(sorted(found, key=lambda n: n.start))


# ---- what isn't held ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NotInEnvironment:
    """An instrument the question named that the tenant's environment doesn't
    hold on the request's date. Set by the pipeline from the question, never
    by the model, and carried by every answer to it, verified or withheld."""

    named: str
    """The question's own words for it ("spam act")."""
    known_as: str | None = None
    """The register's name for it ("Spam Act 2003"), when the register knows
    it; None for a name only the pattern found."""
    part_of: str | None = None
    """For a part of an instrument (the Notifiable Data Breaches scheme): the
    instrument it is part of, which the environment may hold whole or not."""

    def __post_init__(self) -> None:
        for value in (self.named, self.known_as, self.part_of):
            if value is not None and (
                not value.strip() or not value.isprintable() or len(value) > 300
            ):
                raise ValueError("a named instrument is one printable line")

    def json(self) -> dict[str, str | None]:
        return {"named": self.named, "known_as": self.known_as, "part_of": self.part_of}

    @classmethod
    def of_json(cls, raw: object) -> NotInEnvironment:
        """A stored element back to its entry; `ValueError` when it isn't one."""
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        element = cast("dict[str, object]", raw)
        if set(element) != {"named", "known_as", "part_of"}:
            raise ValueError("not a named instrument")
        named, known_as, part_of = element["named"], element["known_as"], element["part_of"]
        if not isinstance(named, str) or not all(
            v is None or isinstance(v, str) for v in (known_as, part_of)
        ):
            raise ValueError("not a named instrument")
        return cls(named, cast("str | None", known_as), cast("str | None", part_of))

    @property
    def span_form(self) -> str:
        """One line for a span attribute."""
        shown = self.named if self.known_as is None else f"{self.named} -> {self.known_as}"
        return shown if self.part_of is None else f"{shown} (part of {self.part_of})"


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def _in_a_title(words: tuple[str, ...], environment: EnvironmentTitles) -> bool:
    return any(
        _contains(_words(title), words) for titles in environment.values() for title in titles
    )


def _held(
    named: NamedInstrument, environment: EnvironmentTitles, register: InstrumentRegister
) -> bool:
    entry = named.entry
    if entry is None:
        return _in_a_title(named.words, environment)
    if entry.part_of is not None:
        return entry.part_of in environment and entry.known_as in register.selected_parts
    if entry.library_id is not None and entry.library_id in environment:
        return True
    return any(_in_a_title(_words(name), environment) for name in entry.names)


def not_in_environment(
    question: str, environment: EnvironmentTitles, register: InstrumentRegister
) -> tuple[NotInEnvironment, ...]:
    """Each instrument `question` names that `environment` doesn't hold, once,
    in the order the question names them."""
    absent: dict[str, NotInEnvironment] = {}
    for named in named_instruments(question, register):
        if _held(named, environment, register):
            continue
        entry = named.entry
        key = entry.known_as if entry is not None else " ".join(named.words)
        absent.setdefault(
            key,
            NotInEnvironment(
                named.named,
                known_as=None if entry is None else entry.known_as,
                part_of=None
                if entry is None or entry.part_of is None
                else register.library_name(entry.part_of),
            ),
        )
    return tuple(absent.values())
