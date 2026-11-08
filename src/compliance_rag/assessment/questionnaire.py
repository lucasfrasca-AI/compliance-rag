"""The questionnaire (issue #304, #123, ADR-0053 I5): what an admin is asked,
derived from Track A and from nothing else.

Every unassessable obligation names its unknown leaves (`Explanation.questions`,
#115). A leaf that a decided sibling short-circuited is not among them, so it
is never asked here: this module reads `ObligationCoverage.questions` and adds
no question of its own. The same fact is asked once, however many conditions
read its field ("one fact feeds every condition that reads its field",
CONTEXT.md: Evidence), and the questions are grouped by the provision they
decide (`s 6D(4)`, `s 6E(1)`, ...), the first clause the leaf rests on.

Each fact is asked for the period the law reads: a **state** on `as_of`
(legal form, when the business started), one **financial year** (s6D(1)'s
previous year), or each financial year a history leaf (s6D(4)(a)) still lacks.
Those are *slots*. A slot is:

- `open`: nothing is recorded for it, so it can be answered;
- `disagree`: two current answers hold different values. Flagged, never
  resolved: a person corrects one (a correction supersedes, the old answer
  staying as the record) until they agree;
- `partial`: the current answers cover only part of the period asked about;
- `unreadable`: one answer is recorded but Track A cannot compare it (a number
  where a yes or no is read).

Only `open` takes a new answer. The others are corrected by superseding the
current one, which this module only locates (`Item.slot`); writing is the
API's, through the ledger's evidence operations.

Nothing here decides applicability or writes anything.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from compliance_rag.assessment.coverage import (
    AnswerKind,
    Coverage,
    Explanation,
    ObligationCoverage,
    Outcome,
    Question,
)
from compliance_rag.assessment.evidence import (
    SETTLES,
    Evidence,
    Facts,
    FactValue,
    Period,
    current_evidence,
    values_differ,
)
from compliance_rag.corpus.display_form import display_form
from compliance_rag.corpus.models import NO_END_DATE, PARAGRAPH_SEGMENT_RE, parse_stable_id

STATE = "state"
"""The slot key of a fact asked on a date (`Question.period` "on 2026-10-12")."""

_STATE_PERIOD = re.compile(r"on \d{4}-\d{2}-\d{2}")
_YEAR_KEY = re.compile(r"fy(\d{4})")

NO_ROUTE = "Where and in which sector the business operates"
"""The route of a question that rests on no clause: an obligation narrowed by
jurisdiction or sector (`operates_in`, `sectors`)."""


class SlotStatus(StrEnum):
    OPEN = "open"
    DISAGREE = "disagree"
    PARTIAL = "partial"
    UNREADABLE = "unreadable"

    @property
    def takes_answer(self) -> bool:
        """Only a slot with nothing recorded is answered; the others are
        corrected, so a second current answer is never added beside the first."""
        return self is SlotStatus.OPEN


@dataclass(frozen=True, slots=True)
class Asked:
    """One condition's question, shown beside the fact it needs."""

    condition_id: str
    question: str
    reason: str
    clause_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Slot:
    """One period of a fact the law reads."""

    key: str
    label: str
    period: Period | None
    """The financial year, or `None` for a state (it holds from a date the
    answer gives, `answer_period`)."""
    status: SlotStatus
    current: tuple[Evidence, ...]
    """The settling answers Track A reads for it, by recording order."""
    superseded: tuple[Evidence, ...]
    """Earlier answers for it that a correction superseded: the record of what
    an earlier assessment read."""


@dataclass(frozen=True, slots=True)
class Item:
    """One fact to ask for."""

    field: str
    answer_kind: AnswerKind
    options: tuple[str, ...]
    asked: tuple[Asked, ...]
    slots: tuple[Slot, ...]
    obligation_ids: tuple[str, ...]
    """The unassessable obligations an answer to it bears on."""

    def slot(self, key: str) -> Slot | None:
        return next((s for s in self.slots if s.key == key), None)


@dataclass(frozen=True, slots=True)
class Route:
    """The facts that decide one provision's route to applying."""

    label: str
    items: tuple[Item, ...]


@dataclass(frozen=True, slots=True)
class Questionnaire:
    as_of: dt.date
    routes: tuple[Route, ...]
    unanswerable: tuple[ObligationCoverage, ...]
    """Unassessable obligations no answer settles: no question names a fact
    for them (no compilation loaded, or an incomplete clause, ADR-0047)."""
    counts: tuple[tuple[Outcome, int], ...] = field(default_factory=tuple[tuple[Outcome, int], ...])
    options: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Each fact a leaf tests against a closed list, with the list: for the
    questions asked and for answers the tree has since settled."""

    def options_of(self, field: str) -> tuple[str, ...]:
        return dict(self.options).get(field, ())

    @property
    def items(self) -> tuple[Item, ...]:
        return tuple(i for r in self.routes for i in r.items)

    def item(self, field: str) -> Item | None:
        return next((i for i in self.items if i.field == field), None)


def answer_kind_of(value: FactValue) -> AnswerKind:
    """What a recorded answer is, from its own type: a correction offers the
    input its value came from."""
    if isinstance(value, bool):
        return AnswerKind.BOOLEAN
    if isinstance(value, int | float):
        return AnswerKind.NUMBER
    if isinstance(value, dt.date):
        return AnswerKind.DATE
    if isinstance(value, tuple):
        return AnswerKind.LIST
    return AnswerKind.TEXT


def answer_period(
    kind: AnswerKind, value: FactValue, *, year: Period | None, holds_from: dt.date | None
) -> Period:
    """The period an answer is recorded for. A year's figure is for that year.
    A date-valued state is recorded with the period it holds from, the date
    itself (CONTEXT.md: Evidence, `Evidence`'s note); any other state holds from
    the date the admin gives. `ValueError` if neither is there."""
    if year is not None:
        return year
    if kind is AnswerKind.DATE and isinstance(value, dt.date):
        return Period(value)
    if holds_from is None:
        raise ValueError("say the date it holds from")
    return Period(holds_from)


def _provision_of(clause_id: str) -> str | None:
    """The subsection a clause sits in, as a reader cites it (`s6d/4/a` is
    "s 6D(4)"), or `None` for a defined term (`s6/1/health-service`): a
    definition is not where a rule lives, so it decides no route."""
    try:
        instrument_id, _, path = parse_stable_id(clause_id)
    except ValueError:
        return None
    segments = path.split("/")
    if any(PARAGRAPH_SEGMENT_RE.match(segment) is None for segment in segments[1:]):
        return None
    return display_form("/".join(segments[:2]), instrument_id)


def route_of(clause_groups: Iterable[Sequence[str]]) -> str:
    """The provision a question decides: the first provision, among the leaf's
    own clauses and then those of the conditions above it, nearest first. The
    leaf for "provides a health service" rests on a definition in s 6(1); the
    rule it feeds is s 6D(4)(b), so its route is s 6D(4)."""
    for clause_ids in clause_groups:
        for clause_id in clause_ids:
            if (provision := _provision_of(clause_id)) is not None:
                return provision
    return NO_ROUTE


def _routes_in(explanation: Explanation) -> dict[str, str]:
    """Each question in a tree, to the route of the leaf that asks it."""
    found: dict[str, str] = {}

    def walk(node: Explanation, above: tuple[tuple[str, ...], ...]) -> None:
        if node.question is not None:
            found.setdefault(node.question.condition_id, route_of((node.clause_ids, *above)))
        for child in node.because:
            walk(child, (node.clause_ids, *above))

    walk(explanation, ())
    return found


def _route_order(label: str) -> tuple[object, ...]:
    """Natural order, so `s 6D(2)` precedes `s 6D(10)` and `s 6E(1)` follows
    `s 6DA(1)`: runs of digits compared as numbers."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.findall(r"\d+|\D+", label)
    )


@dataclass(slots=True)
class _Pending:
    """A fact collecting its questions while the unassessable obligations are
    walked."""

    field: str
    route: str
    kind: AnswerKind
    options: list[str] = dataclasses.field(default_factory=list[str])
    asked: dict[str, Asked] = dataclasses.field(default_factory=dict[str, Asked])
    slots: dict[str, tuple[str, Period | None]] = dataclasses.field(
        default_factory=dict[str, tuple[str, Period | None]]
    )
    """Slot key to its label and its year (`None` for a state)."""
    obligations: list[str] = dataclasses.field(default_factory=list[str])


def year_key(year: Period) -> str:
    return f"fy{year.start.year}"


def year_of_key(key: str) -> Period | None:
    match = _YEAR_KEY.fullmatch(key)
    return Period.financial_year(int(match.group(1))) if match else None


def _slots_of(question: Question) -> list[tuple[str, str, Period | None]] | None:
    """The periods a question needs, or `None` for one this module cannot
    place (a period label it does not know): such a question is not offered."""
    if question.missing_periods:
        years = [Period.of_financial_year_label(p) for p in question.missing_periods]
        if any(y is None for y in years):
            return None
        return [(year_key(y), y.label, y) for y in years if y is not None]
    year = Period.of_financial_year_label(question.period)
    if year is not None:
        return [(year_key(year), year.label, year)]
    if _STATE_PERIOD.fullmatch(question.period):
        return [(STATE, question.period.capitalize(), None)]
    return None


def build(coverage: Coverage, evidence: Iterable[Evidence]) -> Questionnaire:
    """The questionnaire for `coverage`, over the tenant's evidence (every row,
    superseded ones included: they are the history a slot shows)."""
    rows = tuple(evidence)
    settling = tuple(e for e in rows if e.provenance in SETTLES)
    facts = Facts.of(rows)
    current_ids = {e.evidence_id for e in facts.current}
    superseded = tuple(e for e in settling if e.evidence_id not in current_ids)
    as_of = coverage.as_of

    pending: dict[str, _Pending] = {}
    unanswerable: list[ObligationCoverage] = []
    beside: list[tuple[str, Question, str, ObligationCoverage]] = []
    for obligation in coverage.with_outcome(Outcome.UNASSESSABLE):
        placed = False
        routes = _routes_in(obligation.explanation)
        for question in obligation.questions:
            slots = _slots_of(question)
            if slots is None:
                continue
            placed = True
            route = routes.get(question.condition_id, NO_ROUTE)
            item = pending.setdefault(
                question.field,
                _Pending(question.field, route, question.answer_kind or AnswerKind.TEXT),
            )
            if obligation.obligation_id not in item.obligations:
                item.obligations.append(obligation.obligation_id)
            item.options += [o for o in question.options if o not in item.options]
            item.asked.setdefault(
                question.condition_id,
                Asked(
                    question.condition_id,
                    question.question,
                    question.unassessable_reason,
                    question.clause_ids,
                ),
            )
            for key, label, year in slots:
                item.slots.setdefault(key, (label, year))
            beside += [(needed, question, route, obligation) for needed in question.also_needs]
        if not placed:
            unanswerable.append(obligation)

    # after every question has been placed, so a fact some leaf asks in its own
    # words is not also asked as "also needed"
    for needed, parent, route, obligation in beside:
        _also_needed(pending, facts, needed, parent, route, obligation, as_of)

    built = [
        Item(
            field=p.field,
            answer_kind=p.kind,
            options=tuple(p.options),
            asked=tuple(p.asked.values()),
            slots=tuple(
                _slot(facts, superseded, p.field, key, label, year, as_of)
                for key, (label, year) in p.slots.items()
            ),
            obligation_ids=tuple(p.obligations),
        )
        for p in pending.values()
    ]
    grouped: dict[str, list[Item]] = {}
    for item in built:
        grouped.setdefault(pending[item.field].route, []).append(item)
    return Questionnaire(
        as_of=as_of,
        routes=tuple(
            Route(label, tuple(items))
            for label, items in sorted(grouped.items(), key=lambda r: _route_order(r[0]))
        ),
        unanswerable=tuple(unanswerable),
        counts=tuple((o, len(coverage.with_outcome(o))) for o in Outcome),
        options=tuple(_options_in(coverage).items()),
    )


def _options_in(coverage: Coverage) -> dict[str, tuple[str, ...]]:
    """The closed list of every fact some leaf of the evaluated trees tests
    against one, decided or not."""
    found: dict[str, list[str]] = {}
    trees = [o.explanation for o in coverage.obligations] + [a.explanation for a in coverage.actors]
    for tree in trees:
        for node in tree.walk():
            if node.field is not None and node.options:
                known = found.setdefault(node.field, [])
                known += [o for o in node.options if o not in known]
    return {name: tuple(values) for name, values in found.items()}


def _also_needed(
    pending: dict[str, _Pending],
    facts: Facts,
    needed: str,
    parent: Question,
    route: str,
    obligation: ObligationCoverage,
    as_of: dt.date,
) -> None:
    """A fact beside the question that would narrow it (when the business
    started, which decides which years count): asked as a date, once, if it is
    still unknown. It carries the parent's reason, which is why it is asked."""
    reading = facts.on(needed, as_of)
    if not (reading.missing or reading.conflict):
        return
    item = pending.setdefault(needed, _Pending(needed, route, AnswerKind.DATE))
    if obligation.obligation_id not in item.obligations:
        item.obligations.append(obligation.obligation_id)
    if item.asked:
        return  # some leaf asks for it in its own words
    item.asked.setdefault(
        f"{parent.condition_id}#{needed}",
        Asked(
            f"{parent.condition_id}#{needed}",
            f"Also needed: {needed.replace('_', ' ')}.",
            parent.unassessable_reason,
            parent.clause_ids,
        ),
    )
    item.slots.setdefault(STATE, (f"On {as_of}", None))


def _slot(
    facts: Facts,
    superseded: tuple[Evidence, ...],
    field: str,
    key: str,
    label: str,
    year: Period | None,
    as_of: dt.date,
) -> Slot:
    reading = facts.on(field, as_of) if year is None else facts.for_period(field, year)
    if year is None:
        earlier = tuple(e for e in superseded if e.field == field and e.period.contains(as_of))
    else:
        earlier = tuple(e for e in superseded if e.field == field and e.period.overlaps(year))
    status = SlotStatus.OPEN
    if not reading.missing:
        if reading.conflict:
            status = SlotStatus.DISAGREE if values_differ(reading.evidence) else SlotStatus.PARTIAL
        else:
            status = SlotStatus.UNREADABLE
    return Slot(key, label, year, status, reading.evidence, earlier)


@dataclass(frozen=True, slots=True)
class Record:
    """A fact on record, for the "Answers on record" list: the current answers
    for it and what they superseded."""

    field: str
    current: tuple[Evidence, ...]
    superseded: tuple[Evidence, ...]
    disagree: bool
    """Two current answers for overlapping periods hold different values."""


def on_record(evidence: Iterable[Evidence]) -> tuple[Record, ...]:
    """Every settling answer, by field: the current ones (flagged where two
    that overlap disagree) and the ones a correction superseded."""
    settling = tuple(e for e in evidence if e.provenance in SETTLES)
    current = current_evidence(settling)
    now = {e.evidence_id for e in current}
    records: list[Record] = []
    for name in sorted({e.field for e in settling}):
        mine = tuple(e for e in current if e.field == name)
        records.append(
            Record(
                field=name,
                current=tuple(sorted(mine, key=lambda e: (e.period.start, e.period.end))),
                superseded=tuple(
                    e for e in settling if e.field == name and e.evidence_id not in now
                ),
                disagree=any(
                    a.period.overlaps(b.period) and values_differ((a, b))
                    for i, a in enumerate(mine)
                    for b in mine[i + 1 :]
                ),
            )
        )
    return tuple(records)


def is_state(evidence: Evidence) -> bool:
    """An open-ended answer: a state, not a year's figure."""
    return evidence.period.end == NO_END_DATE
