"""Track A's evaluation (issue #115, ADR-0027): a snapshot of the ontology in
force on `as_of`, evaluated against a tenant's evidence with Kleene
three-valued logic. Pure Python — no Cypher, no LLM. The snapshot is read
from the graph through the query builder (`graph/track_a.py`) or built from
mapping YAML in memory (the fake), and both are evaluated here, once.

- **`all_of`** is false if any operand is false, otherwise unknown if any is
  unknown, otherwise true. **`any_of`** is true if any is true, otherwise
  unknown if any is unknown, otherwise false. **`not`** swaps true and false
  and leaves unknown.
- **A leaf** reads one fact for one period (`Facts`). Missing evidence,
  conflicting evidence or evidence of the wrong type is *unknown*, with the
  leaf's question as the reason — never assumed either way (D-06).
- **An operand not in force on `as_of`** is resolved through its successors
  (`supersedes`), so amending a gate is one supersession. If nothing
  replaces it, the provision no longer exists and it is false, so repealing
  a gate is one retirement (ADR-0027, Q1).
- **An obligation applies** if the tenant holds any of its actors, is in
  one of its jurisdictions and sectors (when it names any), and meets every
  condition it requires. With no clause of it in force on `as_of`, there is
  no text to cite, so it is unassessable for want of a compilation, never
  applicable (ADR-0028).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from compliance_rag.assessment.coverage import (
    ActorCoverage,
    Coverage,
    Explanation,
    ObligationCoverage,
    Outcome,
    Question,
    Truth,
)
from compliance_rag.assessment.evidence import Facts, FactValue, Period, Reading
from compliance_rag.graph.mappings import (
    Comparison,
    Leaf,
    Operator,
    RelativeDate,
)
from compliance_rag.graph.mappings import (
    Period as LeafPeriod,
)


@dataclass(frozen=True, slots=True)
class ConditionSpec:
    id: str
    operator: Operator
    operands: tuple[str, ...]
    description: str
    clause_ids: tuple[str, ...]
    """Its clauses in force on the snapshot's date."""
    leaf: Leaf | None = None
    question: str = ""
    unassessable_reason: str = ""
    supersedes: str | None = None


@dataclass(frozen=True, slots=True)
class ActorSpec:
    id: str
    name: str
    clause_ids: tuple[str, ...]
    qualified_by: str


@dataclass(frozen=True, slots=True)
class ObligationSpec:
    id: str
    instrument_id: str
    summary: str
    clause_ids: tuple[str, ...]
    actors: tuple[str, ...]
    jurisdictions: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything in force on `as_of` that applicability reads."""

    as_of: dt.date
    obligations: Mapping[str, ObligationSpec]
    actors: Mapping[str, ActorSpec]
    conditions: Mapping[str, ConditionSpec]


JURISDICTIONS_FIELD = "operates_in"
"""The evidence field an obligation's jurisdictions are tested against:
ISO 3166 codes the tenant operates in (`AU`, `AU-NSW`)."""
SECTORS_FIELD = "sectors"
"""ANZSIC codes the tenant's business falls in (`anzsic-2006:Q8401`)."""


def evaluate(snapshot: Snapshot, facts: Facts, *, tenant_id: str) -> Coverage:
    evaluator = _Evaluator(snapshot, facts)
    actors = tuple(evaluator.actor(actor_id) for actor_id in sorted(snapshot.actors))
    obligations = tuple(
        evaluator.obligation(snapshot.obligations[i]) for i in sorted(snapshot.obligations)
    )
    return Coverage(
        tenant_id=tenant_id, as_of=snapshot.as_of, obligations=obligations, actors=actors
    )


def explain(snapshot: Snapshot, facts: Facts, condition_id: str) -> Explanation:
    """One condition, evaluated on its own — resolved through `supersedes`
    to whatever stands in for it on the snapshot's date."""
    return _Evaluator(snapshot, facts).condition(condition_id)


class _Evaluator:
    def __init__(self, snapshot: Snapshot, facts: Facts) -> None:
        self._snapshot = snapshot
        self._facts = facts
        self._as_of = snapshot.as_of
        self._memo: dict[str, Explanation] = {}
        self._successor = {
            c.supersedes: c.id for c in snapshot.conditions.values() if c.supersedes is not None
        }

    # ---- conditions ---------------------------------------------------------

    def condition(self, condition_id: str) -> Explanation:
        resolved = self._resolve(condition_id)
        if resolved is None:
            return Explanation(
                condition_id=condition_id,
                operator="absent",
                truth=Truth.FALSE,
                description=f"Not in force on {self._as_of}: the provision does not exist then.",
                clause_ids=(),
                absent=True,
            )
        if resolved not in self._memo:
            self._memo[resolved] = self._evaluate(self._snapshot.conditions[resolved])
        return self._memo[resolved]

    def _resolve(self, condition_id: str) -> str | None:
        """The condition in force on `as_of` for an id: itself, or the
        successor that supersedes it, followed to the end of the chain."""
        seen: set[str] = set()
        current: str | None = condition_id
        while current is not None and current not in seen:
            if current in self._snapshot.conditions:
                return current
            seen.add(current)
            current = self._successor.get(current)
        return None

    def _evaluate(self, spec: ConditionSpec) -> Explanation:
        if spec.operator is Operator.LEAF and spec.leaf is not None:
            return self._leaf(spec, spec.leaf)
        children = tuple(self.condition(o) for o in spec.operands)
        if spec.operator is Operator.NOT:
            (child,) = children
            truth = {Truth.TRUE: Truth.FALSE, Truth.FALSE: Truth.TRUE}.get(
                child.truth, Truth.UNKNOWN
            )
            return _node(spec, truth, children)
        truth, deciding = _combine(spec.operator, children)
        return _node(spec, truth, deciding)

    # ---- leaves ---------------------------------------------------------------

    def _leaf(self, spec: ConditionSpec, leaf: Leaf) -> Explanation:
        if leaf.period is LeafPeriod.ANY_ENDED_FINANCIAL_YEAR:
            return self._history(spec, leaf)
        reading, label = self._reading(leaf)
        if reading.missing or reading.conflict or reading.value is None:
            return self._unknown(spec, leaf, label, reading)
        verdict = _compare(reading.value, leaf.test, leaf.value, self._as_of)
        if verdict is None:
            return self._unknown(spec, leaf, label, reading, conflict=True)
        return _node(
            spec,
            Truth.TRUE if verdict else Truth.FALSE,
            (),
            evidence_ids=tuple(e.evidence_id for e in reading.evidence),
        )

    def _reading(self, leaf: Leaf) -> tuple[Reading, str]:
        current_year = Period.financial_year_of(self._as_of)
        match leaf.period:
            case LeafPeriod.PREVIOUS_FINANCIAL_YEAR:
                year = current_year.previous_financial_year()
                return self._facts.for_period(leaf.field, year), year.label
            case LeafPeriod.CURRENT_FINANCIAL_YEAR:
                return self._facts.for_period(leaf.field, current_year), current_year.label
            case _:
                return self._facts.on(leaf.field, self._as_of), f"on {self._as_of}"

    def _history(self, spec: ConditionSpec, leaf: Leaf) -> Explanation:
        """s6D(4)(a): any financial year that ended by `as_of` and after the
        later of the `since_field` date and `not_before`. True on the first
        year that passes the test; false only if every such year is known."""
        if leaf.since_field is None or leaf.not_before is None:
            raise ValueError(f"{spec.id}: a history leaf names since_field and not_before")
        since = self._facts.on(leaf.since_field, self._as_of)
        floor = leaf.not_before
        since_known = (
            not since.missing
            and not since.conflict
            and isinstance(since.value, dt.date)
            and not isinstance(since.value, dt.datetime)
        )
        if since_known and isinstance(since.value, dt.date):
            floor = max(floor, since.value)
        years = _ended_years(after=floor, as_of=self._as_of)
        read: list[str] = list(e.evidence_id for e in since.evidence)
        unknown_years: list[str] = []
        for year in years:
            reading = self._facts.for_period(leaf.field, year)
            read += [e.evidence_id for e in reading.evidence]
            if reading.missing or reading.conflict or reading.value is None:
                unknown_years.append(year.label)
                continue
            verdict = _compare(reading.value, leaf.test, leaf.value, self._as_of)
            if verdict:
                return _node(
                    spec,
                    Truth.TRUE,
                    (),
                    evidence_ids=tuple(e.evidence_id for e in reading.evidence),
                )
            if verdict is None:
                unknown_years.append(year.label)
        if since_known and not unknown_years:
            return _node(spec, Truth.FALSE, (), evidence_ids=tuple(read))
        missing = tuple(unknown_years) if since_known else (leaf.since_field, *unknown_years)
        question = Question(
            condition_id=spec.id,
            field=leaf.field,
            period=f"each financial year ended after {floor} and by {self._as_of}",
            question=spec.question,
            unassessable_reason=spec.unassessable_reason,
            clause_ids=spec.clause_ids,
            conflict=since.conflict,
            evidence_ids=tuple(read),
            missing_periods=missing,
        )
        return _node(spec, Truth.UNKNOWN, (), question=question, evidence_ids=tuple(read))

    def _unknown(
        self,
        spec: ConditionSpec,
        leaf: Leaf,
        period: str,
        reading: Reading,
        *,
        conflict: bool = False,
    ) -> Explanation:
        evidence_ids = tuple(e.evidence_id for e in reading.evidence)
        question = Question(
            condition_id=spec.id,
            field=leaf.field,
            period=period,
            question=spec.question,
            unassessable_reason=spec.unassessable_reason,
            clause_ids=spec.clause_ids,
            conflict=conflict or reading.conflict,
            evidence_ids=evidence_ids,
        )
        return _node(spec, Truth.UNKNOWN, (), question=question, evidence_ids=evidence_ids)

    # ---- actors and obligations ----------------------------------------------

    def actor(self, actor_id: str) -> ActorCoverage:
        spec = self._snapshot.actors[actor_id]
        return ActorCoverage(
            actor_id=spec.id,
            name=spec.name,
            clause_ids=spec.clause_ids,
            explanation=self.condition(spec.qualified_by),
        )

    def _actors(self, obligation: ObligationSpec) -> Explanation:
        children: list[Explanation] = []
        for actor_id in obligation.actors:
            if actor_id not in self._snapshot.actors:
                continue  # validation keeps an obligation's actors in force with it
            actor = self.actor(actor_id)
            children.append(
                Explanation(
                    condition_id=actor.actor_id,
                    operator="actor",
                    truth=actor.truth,
                    description=f"Holds the role {actor.name}.",
                    clause_ids=actor.clause_ids,
                    because=(actor.explanation,),
                )
            )
        truth, deciding = _combine(Operator.ANY_OF, tuple(children))
        return Explanation(
            condition_id=f"{obligation.id}#actors",
            operator=Operator.ANY_OF.value,
            truth=truth,
            description="Holds a role the obligation applies to.",
            clause_ids=(),
            because=deciding,
        )

    def _membership(
        self, obligation: ObligationSpec, field: str, codes: tuple[str, ...]
    ) -> Explanation:
        """Narrowing by jurisdiction or sector: the tenant's codes (a current
        fact) fall within one of the obligation's — `AU-NSW` within `AU`."""
        reading = self._facts.on(field, self._as_of)
        node_id = f"{obligation.id}#{field}"
        what = "jurisdictions" if field == JURISDICTIONS_FIELD else "sectors"
        if reading.missing or reading.conflict or not isinstance(reading.value, tuple):
            question = Question(
                condition_id=node_id,
                field=field,
                period=f"on {self._as_of}",
                question=f"Which {what} does the business operate in?",
                unassessable_reason=f"The obligation applies only in {', '.join(codes)}, "
                f"and no {what} are recorded.",
                clause_ids=(),
                conflict=reading.conflict,
                evidence_ids=tuple(e.evidence_id for e in reading.evidence),
            )
            return Explanation(
                node_id,
                "leaf",
                Truth.UNKNOWN,
                f"Operates in {', '.join(codes)}.",
                (),
                question=question,
            )
        held = tuple(str(v) for v in reading.value)
        inside = any(_within(h, c) for h in held for c in codes)
        return Explanation(
            node_id,
            "leaf",
            Truth.TRUE if inside else Truth.FALSE,
            f"Operates in {', '.join(codes)}.",
            (),
            evidence_ids=tuple(e.evidence_id for e in reading.evidence),
        )

    def obligation(self, spec: ObligationSpec) -> ObligationCoverage:
        if not spec.clause_ids:
            explanation = Explanation(
                condition_id=spec.id,
                operator="grounding",
                truth=Truth.UNKNOWN,
                description=f"No compilation of {spec.instrument_id} in force on {self._as_of} "
                "is loaded, so there is no text to cite.",
                clause_ids=(),
            )
            return ObligationCoverage(
                obligation_id=spec.id,
                instrument_id=spec.instrument_id,
                summary=spec.summary,
                clause_ids=(),
                outcome=Outcome.UNASSESSABLE,
                explanation=explanation,
                reason=explanation.description,
            )
        parts = [self._actors(spec)]
        if spec.jurisdictions:
            parts.append(self._membership(spec, JURISDICTIONS_FIELD, spec.jurisdictions))
        if spec.sectors:
            parts.append(self._membership(spec, SECTORS_FIELD, spec.sectors))
        parts += [self.condition(c) for c in spec.conditions]
        truth, deciding = _combine(Operator.ALL_OF, tuple(parts))
        explanation = Explanation(
            condition_id=spec.id,
            operator=Operator.ALL_OF.value,
            truth=truth,
            description=spec.summary,
            clause_ids=(),
            because=deciding,
        )
        outcome = Outcome.of(truth)
        return ObligationCoverage(
            obligation_id=spec.id,
            instrument_id=spec.instrument_id,
            summary=spec.summary,
            clause_ids=spec.clause_ids,
            outcome=outcome,
            explanation=explanation,
            reason=_reason(outcome, explanation),
        )


def _combine(
    operator: Operator, children: tuple[Explanation, ...]
) -> tuple[Truth, tuple[Explanation, ...]]:
    """Kleene `all_of` / `any_of`, with the children that decided it. A
    decided result keeps only the *first* decisive child in authored order —
    one reason is enough, and the citation chain stays minimal (#93: in
    Trace 1, s6D(4) never needed asking). An unknown keeps every unknown
    child: each is a question that could change the answer."""
    decisive, neutral = (
        (Truth.FALSE, Truth.TRUE) if operator is Operator.ALL_OF else (Truth.TRUE, Truth.FALSE)
    )
    by_truth = {t: tuple(c for c in children if c.truth is t) for t in Truth}
    if by_truth[decisive]:
        return decisive, by_truth[decisive][:1]
    if by_truth[Truth.UNKNOWN]:
        return Truth.UNKNOWN, by_truth[Truth.UNKNOWN]
    return neutral, children


def _node(
    spec: ConditionSpec,
    truth: Truth,
    because: tuple[Explanation, ...],
    *,
    question: Question | None = None,
    evidence_ids: tuple[str, ...] = (),
) -> Explanation:
    return Explanation(
        condition_id=spec.id,
        operator=spec.operator.value,
        truth=truth,
        description=spec.description,
        clause_ids=spec.clause_ids,
        because=because,
        question=question,
        evidence_ids=evidence_ids,
    )


def _within(held: str, code: str) -> bool:
    """`AU-NSW` is within `AU` (ISO 3166-2 subdivision); `anzsic-2006:Q8401`
    is within `anzsic-2006:Q` (an ANZSIC class within its division)."""
    if held == code:
        return True
    return held.startswith(f"{code}-") if ":" not in code else held.startswith(code)


def _ended_years(*, after: dt.date, as_of: dt.date) -> tuple[Period, ...]:
    """Financial years whose last day is after `after` and that ended on or
    before `as_of`."""
    years: list[Period] = []
    year = Period.financial_year_of(after)
    while year.end <= as_of:
        if year.end - dt.timedelta(days=1) > after:
            years.append(year)
        year = Period.financial_year(year.start.year + 1)
    return tuple(years)


def _date(value: object, as_of: dt.date) -> dt.date | None:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value
    current = Period.financial_year_of(as_of)
    if value == RelativeDate.START_OF_CURRENT_FINANCIAL_YEAR.value:
        return current.start
    if value == RelativeDate.START_OF_PREVIOUS_FINANCIAL_YEAR.value:
        return current.previous_financial_year().start
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _compare(fact: FactValue, test: Comparison, value: object, as_of: dt.date) -> bool | None:
    """The leaf's test on a fact; `None` when the fact's type can't be
    compared, which the caller reports rather than guesses."""
    match test:
        case Comparison.EQ | Comparison.NE:
            same = type(fact) is type(value) and fact == value
            return same if test is Comparison.EQ else not same
        case Comparison.GT | Comparison.GTE | Comparison.LT | Comparison.LTE:
            left, right = _number(fact), _number(value)
            if left is None or right is None:
                return None
            return {
                Comparison.GT: left > right,
                Comparison.GTE: left >= right,
                Comparison.LT: left < right,
                Comparison.LTE: left <= right,
            }[test]
        case Comparison.IN | Comparison.NOT_IN:
            if not isinstance(value, tuple) or isinstance(fact, tuple):
                return None
            options = cast("tuple[object, ...]", value)
            inside = any(type(fact) is type(v) and fact == v for v in options)
            return inside if test is Comparison.IN else not inside
        case Comparison.BEFORE | Comparison.ON_OR_AFTER:
            left, right = _date(fact, as_of), _date(value, as_of)
            if left is None or right is None:
                return None
            return left < right if test is Comparison.BEFORE else left >= right


def _reason(outcome: Outcome, explanation: Explanation) -> str:
    if outcome is Outcome.UNASSESSABLE:
        questions = explanation.questions()
        return "Depends on: " + "; ".join(q.unassessable_reason for q in questions)
    leaves = [n.description for n in explanation.walk() if not n.because and n.description]
    verb = "Applies" if outcome is Outcome.APPLICABLE else "Does not apply"
    return f"{verb}: " + "; ".join(dict.fromkeys(leaves))
