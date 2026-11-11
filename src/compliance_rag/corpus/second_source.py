"""A clause whose text has a second source (#338, ADR-0058).

A compilation's clauses are parsed from one pinned source, the Register's
unpacked ePub (ADR-0019). Where that source can't give a clause's content as
text, the parser stores the clause as it is and marks it incomplete
(ADR-0047). The Register publishes the same compilation in a second
rendering, the PDF, and where that rendering *does* hold the content as text,
a **completion** takes it from there: the words the PDF sets, arranged in the
form a person signed, appended to the clause the ePub gave, and the one
reason it completes taken off the clause's state.

A completion is applied to a parse (`parse_compilation(..., completions=)`),
never to a store: every store gets it the way it gets any parse, through
ingest or a corpus revision (ADR-0042). It is refused unless the clause it
names is in the parse, is marked incomplete for exactly the reason it
completes, and comes out as the canonical text the person signed.

Pure: no network, no storage. Building a completion from the PDF is
`corpus/pdf_formula.py`'s.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from compliance_rag.corpus.models import ClauseRecord, Incompleteness, canonicalise


class CompletionRefused(ValueError):
    """A completion that doesn't fit the parse it is applied to: nothing is
    completed, and the parse is not used."""


@dataclass(frozen=True, slots=True)
class SecondSource:
    """Where a clause's added words come from: a rendering of the same
    compilation, pinned by its own sha256, and where in it."""

    clause_path: str
    sha256: str
    url: str
    locator: str
    """Where in the source the words are, as a person finds them: "page 62"."""

    def to_json(self) -> dict[str, str]:
        return {"sha256": self.sha256, "url": self.url, "locator": self.locator}

    @classmethod
    def from_json(cls, clause_path: str, entry: Mapping[str, object]) -> SecondSource:
        return cls(clause_path, str(entry["sha256"]), str(entry["url"]), str(entry["locator"]))


def second_sources_json(sources: Sequence[SecondSource]) -> dict[str, dict[str, str]]:
    return {s.clause_path: s.to_json() for s in sources}


def second_sources_of_json(raw: object) -> tuple[SecondSource, ...]:
    """The stored form back; absent (every record and file written before
    #338) is none."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"second sources are a mapping, not {type(raw).__name__}")
    entries: dict[object, object] = raw  # pyright: ignore[reportUnknownVariableType]
    sources: list[SecondSource] = []
    for path, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError(f"second source of {path!r} is not a mapping")
        fields: dict[str, object] = entry  # pyright: ignore[reportUnknownVariableType]
        sources.append(SecondSource.from_json(str(path), fields))
    return tuple(sources)


@dataclass(frozen=True, slots=True)
class ClauseCompletion:
    """The words a second source supplies for one clause, and what they
    complete."""

    source: SecondSource
    text: str
    """The words appended to the clause's own text, as the signed form
    arranges them."""
    completes: Incompleteness
    """The reason the clause is marked incomplete that these words remove."""
    canonical_text: str
    """The clause's whole canonical text as a person signed it: the
    completed clause must come out exactly so."""

    @property
    def clause_path(self) -> str:
        return self.source.clause_path


def apply_completions(
    clauses: Sequence[ClauseRecord], completions: Sequence[ClauseCompletion]
) -> tuple[ClauseRecord, ...]:
    """`clauses` with each completion applied, in order; every other clause
    as it was. Refused (`CompletionRefused`) unless each completion names a
    clause of the parse, once, that is marked incomplete for the reason it
    completes, and the completed clause's canonical text is the signed one."""
    by_path = {c.clause_path: c for c in completions}
    if len(by_path) != len(completions):
        raise CompletionRefused("a clause is completed once")
    found = {c.clause_path for c in clauses}
    missing = sorted(set(by_path) - found)
    if missing:
        raise CompletionRefused(f"no clause {missing} in the parse to complete")
    return tuple(
        _completed(clause, by_path[clause.clause_path]) if clause.clause_path in by_path else clause
        for clause in clauses
    )


def _completed(clause: ClauseRecord, completion: ClauseCompletion) -> ClauseRecord:
    where = clause.clause_id
    if completion.completes not in clause.incomplete:
        raise CompletionRefused(
            f"{where} is not marked {completion.completes.reason.value} "
            f"({completion.completes.detail!r}) in this parse, so there is nothing for "
            f"{completion.source.locator} of {completion.source.sha256[:12]}… to complete"
        )
    text = f"{clause.text} {completion.text}"
    canonical = canonicalise(text)
    if canonical != completion.canonical_text:
        raise CompletionRefused(
            f"{where}: completed, the clause reads {canonical!r}, not the signed "
            f"{completion.canonical_text!r}"
        )
    remaining = tuple(i for i in clause.incomplete if i != completion.completes)
    return clause.model_copy(
        update={"text": text, "canonical_text": canonical, "incomplete": remaining}
    )
