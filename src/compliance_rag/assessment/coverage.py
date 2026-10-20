"""Coverage (D-06, issue #115): for one tenant on one date, every obligation
as **applicable**, **not applicable** or **unassessable** — never a score —
each with the reasoning that decided it and the clauses that reasoning cites.

The reasoning is an `Explanation` tree: each evaluated condition, its
three-valued truth, its clauses, and only the children that *decided* it. A
true `any_of` keeps its true children, a false `all_of` its false ones, and
an unknown node its unknown ones. So:

- the clauses of a decided tree are exactly its citation chain ("the
  reasoning *is* the citation set", #93); and
- the unknown leaves of an undecided tree are exactly the questions that
  could change the answer. A leaf short-circuited by a decided sibling is
  never asked.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple, cast


class Truth(StrEnum):
    """Kleene three-valued truth: unknown is a first-class answer."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class Outcome(StrEnum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"
    UNASSESSABLE = "unassessable"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")

    @classmethod
    def of(cls, truth: Truth) -> Outcome:
        return {
            Truth.TRUE: cls.APPLICABLE,
            Truth.FALSE: cls.NOT_APPLICABLE,
            Truth.UNKNOWN: cls.UNASSESSABLE,
        }[truth]


@dataclass(frozen=True, slots=True)
class Question:
    """What would settle an unknown leaf: the fact to ask for, for which
    period, and the clauses that make it matter (D-06)."""

    condition_id: str
    field: str
    period: str
    question: str
    unassessable_reason: str
    clause_ids: tuple[str, ...]
    conflict: bool = False
    """Current evidence disagrees (or covers only part of the period): the
    question is which answer is right, not a new one."""
    evidence_ids: tuple[str, ...] = ()
    missing_periods: tuple[str, ...] = ()
    also_needs: tuple[str, ...] = ()
    """Other fields that would narrow the question (a start date that
    decides which years count)."""


@dataclass(frozen=True, slots=True)
class Explanation:
    """One evaluated condition and the children that decided it."""

    condition_id: str
    operator: str
    truth: Truth
    description: str
    clause_ids: tuple[str, ...]
    because: tuple[Explanation, ...] = ()
    question: Question | None = None
    evidence_ids: tuple[str, ...] = ()
    """For a leaf: the evidence it read — what an assessment can show later."""
    absent: bool = False
    """Not in force on `as_of`: the provision doesn't exist, so it is false."""
    incomplete: tuple[tuple[str, str], ...] = ()
    """For a leaf: the clauses it rests on that are incomplete (ADR-0047), each
    with what is missing. Such a leaf is unknown whatever the evidence, and no
    question settles it."""

    def walk(self) -> Iterator[Explanation]:
        yield self
        for child in self.because:
            yield from child.walk()

    def clauses(self) -> tuple[str, ...]:
        return _ordered(c for node in self.walk() for c in node.clause_ids)

    def questions(self) -> tuple[Question, ...]:
        found = {n.question.condition_id: n.question for n in self.walk() if n.question}
        return tuple(found.values())

    def incompleteness(self) -> tuple[tuple[str, str], ...]:
        """Every incomplete clause an unknown leaf of this tree rests on, once."""
        return tuple(dict.fromkeys(i for n in self.walk() for i in n.incomplete))

    def find(self, condition_id: str) -> Explanation | None:
        return next((n for n in self.walk() if n.condition_id == condition_id), None)


@dataclass(frozen=True, slots=True)
class ActorCoverage:
    """Whether the tenant holds a role on `as_of` (the tenant subgraph's
    `ACTS_AS`, projected per request, never stored)."""

    actor_id: str
    name: str
    clause_ids: tuple[str, ...]
    explanation: Explanation

    @property
    def truth(self) -> Truth:
        return self.explanation.truth


@dataclass(frozen=True, slots=True)
class ObligationCoverage:
    obligation_id: str
    instrument_id: str
    summary: str
    clause_ids: tuple[str, ...]
    """The obligation's own clauses in force on `as_of` (ADR-0028)."""
    outcome: Outcome
    explanation: Explanation
    """The obligation's own tree: its actors (any of), narrowing and
    required conditions (all of)."""
    reason: str
    exception_clause_ids: tuple[str, ...] = ()
    """The clauses in force on `as_of` that say when it does not apply, as the
    reviewed mapping names them, and the clauses inside them (#164, #182,
    ADR-0037, ADR-0044). Not among `citations`: they decide nothing about
    whether it applies. They are what an answer citing it must not leave out
    (`Coverage.track_a_exceptions`)."""
    qualifier_clause_ids: tuple[str, ...] = ()
    """The clauses in force on `as_of` that say how it is satisfied, or what it
    requires, as the reviewed mapping names them, and the clauses inside them
    (#182, ADR-0044). Kept apart from its exceptions, and out of `citations`
    for the same reason (`Coverage.track_a_qualifiers`)."""

    @property
    def citations(self) -> tuple[str, ...]:
        """Every clause the answer rests on: the obligation's, then the
        deciding chain's."""
        return _ordered((*self.clause_ids, *self.explanation.clauses()))

    @property
    def questions(self) -> tuple[Question, ...]:
        return self.explanation.questions() if self.outcome is Outcome.UNASSESSABLE else ()


@dataclass(frozen=True, slots=True)
class TenantSubgraph:
    """The small graph of one company (CONTEXT.md: Tenant subgraph),
    projected per request from its evidence and Track A's result and
    discarded with the request — never persisted (D-14).

    - `organisation`: the tenant itself.
    - `operates_in` / `handles` / `regulated_by`: its current facts
      (`operates_in`, `data_categories`, `regulators`).
    - `acts_as`: the roles it holds (`true`) or might hold (`unknown`) on the
      date — a result of evaluation, never a declaration (ADR-0027).

    Approved policy-to-law mappings join it in Phase 3, from the ledger (#93 Q5)."""

    organisation: str
    operates_in: tuple[str, ...]
    handles: tuple[str, ...]
    regulated_by: tuple[str, ...]
    acts_as: tuple[tuple[str, Truth], ...]


class ExceptionOf(NamedTuple):
    """An obligation's clause and one of its exceptions (#164, ADR-0037)."""

    clause: str
    """The obligation's own clause."""
    exception: str
    """A clause that says when the obligation does not apply."""


class QualifierOf(NamedTuple):
    """An obligation's clause and one of its qualifiers (#182, ADR-0044)."""

    clause: str
    """The obligation's own clause."""
    qualifier: str
    """A clause that says how the obligation is satisfied, or what it requires."""


@dataclass(frozen=True, slots=True)
class Coverage:
    tenant_id: str
    as_of: dt.date
    obligations: tuple[ObligationCoverage, ...]
    actors: tuple[ActorCoverage, ...] = field(default_factory=tuple[ActorCoverage, ...])
    subgraph: TenantSubgraph | None = None

    def with_outcome(self, outcome: Outcome) -> tuple[ObligationCoverage, ...]:
        return tuple(o for o in self.obligations if o.outcome is outcome)

    @property
    def might_apply(self) -> tuple[ObligationCoverage, ...]:
        """Every obligation that applies or might: what Track B's law scope
        is made of. One that doesn't is reported, never retrieved."""
        return tuple(o for o in self.obligations if o.outcome is not Outcome.NOT_APPLICABLE)

    @property
    def questions(self) -> tuple[Question, ...]:
        """Every question across the unassessable obligations, once each."""
        found: dict[str, Question] = {}
        for obligation in self.with_outcome(Outcome.UNASSESSABLE):
            for question in obligation.questions:
                found.setdefault(question.condition_id, question)
        return tuple(found.values())

    @property
    def track_a_clause_ids(self) -> frozenset[str]:
        """The clauses Track A cites for every obligation that applies or
        might, and those obligations' exceptions and qualifiers: what Track B
        may return from library law (D-03, #116, ADR-0037, ADR-0044).
        Not-applicable obligations are reported, never retrieved."""
        return frozenset(
            c
            for o in self.might_apply
            for c in (*o.citations, *o.exception_clause_ids, *o.qualifier_clause_ids)
        )

    @property
    def track_a_exceptions(self) -> frozenset[ExceptionOf]:
        """Each clause of an obligation that applies or might, with each of
        its exceptions (#164, ADR-0037): an answer given the clause is given
        the exception."""
        return frozenset(
            ExceptionOf(clause, exception)
            for o in self.might_apply
            for clause in o.clause_ids
            for exception in o.exception_clause_ids
        )

    @property
    def track_a_qualifiers(self) -> frozenset[QualifierOf]:
        """Each clause of an obligation that applies or might, with each of
        its qualifiers (#182, ADR-0044): an answer given the clause is given
        the qualifier."""
        return frozenset(
            QualifierOf(clause, qualifier)
            for o in self.might_apply
            for clause in o.clause_ids
            for qualifier in o.qualifier_clause_ids
        )

    def obligation(self, obligation_id: str) -> ObligationCoverage:
        return next(o for o in self.obligations if o.obligation_id == obligation_id)

    def actor(self, actor_id: str) -> ActorCoverage:
        return next(a for a in self.actors if a.actor_id == actor_id)


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """One obligation's coverage as the ledger keeps it, per assessment."""

    obligation_id: str
    outcome: Outcome
    reason: str
    citations: tuple[str, ...]
    questions: tuple[Question, ...]

    @classmethod
    def of(cls, obligation: ObligationCoverage) -> CoverageRow:
        return cls(
            obligation.obligation_id,
            obligation.outcome,
            obligation.reason,
            obligation.citations,
            obligation.questions,
        )


def question_json(question: Question) -> dict[str, object]:
    return {
        "condition_id": question.condition_id,
        "field": question.field,
        "period": question.period,
        "question": question.question,
        "unassessable_reason": question.unassessable_reason,
        "clause_ids": list(question.clause_ids),
        "conflict": question.conflict,
        "evidence_ids": list(question.evidence_ids),
        "missing_periods": list(question.missing_periods),
        "also_needs": list(question.also_needs),
    }


def question_of_json(raw: Mapping[str, object]) -> Question:
    def strings(key: str) -> tuple[str, ...]:
        return tuple(str(v) for v in cast("list[object]", raw.get(key, [])))

    return Question(
        condition_id=str(raw["condition_id"]),
        field=str(raw["field"]),
        period=str(raw["period"]),
        question=str(raw["question"]),
        unassessable_reason=str(raw["unassessable_reason"]),
        clause_ids=strings("clause_ids"),
        conflict=bool(raw.get("conflict", False)),
        evidence_ids=strings("evidence_ids"),
        missing_periods=strings("missing_periods"),
        also_needs=strings("also_needs"),
    )


def _ordered(items: Iterator[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))
