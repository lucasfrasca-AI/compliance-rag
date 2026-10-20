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
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import cast

from compliance_rag.assessment.coverage import (
    ActorCoverage,
    Coverage,
    Explanation,
    ObligationCoverage,
    Outcome,
    Question,
    TenantSubgraph,
    Truth,
)
from compliance_rag.assessment.evidence import Facts, FactValue, Period, Reading
from compliance_rag.corpus.display_form import NO_TITLES, cite
from compliance_rag.corpus.incomplete_reading import (
    IncompleteReadings,
    committed_incomplete_readings,
)
from compliance_rag.corpus.models import Incomplete
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
    lineage: tuple[str, ...] = ()
    """Every condition this one supersedes, through any chain."""


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
    exception_clause_ids: tuple[str, ...] = ()
    """Its exceptions' clauses in force on the snapshot's date, and the
    clauses inside them (ADR-0037, ADR-0044). They decide nothing here: they
    are what Track B must deliver with it."""
    qualifier_clause_ids: tuple[str, ...] = ()
    """Its qualifiers' clauses, likewise (ADR-0044): delivered with it,
    deciding nothing."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything in force on `as_of` that applicability reads."""

    as_of: dt.date
    obligations: Mapping[str, ObligationSpec]
    actors: Mapping[str, ActorSpec]
    conditions: Mapping[str, ConditionSpec]
    incomplete: Mapping[str, Incomplete] = field(default_factory=dict[str, Incomplete])
    """The clauses conditions cite that are incomplete, with why (ADR-0047):
    read off the stored corpus, never from the mapping."""
    readings: IncompleteReadings = field(default_factory=committed_incomplete_readings)
    """How a person read those clauses (`incomplete.yaml`, signed): the words a
    reader is shown, and whether the missing content governs only a part
    year. Joined here, at read time, on the fake and on the graph alike."""


class _YearIs(Enum):
    """Whether a financial year a leaf reads was a part year of the business."""

    FULL = "full"
    """Carried on all of it, or none of it: the clause's missing content
    doesn't govern it, and the figure decides the leaf."""
    PART = "part"
    """It started during the year: the missing content governs it."""
    OPEN = "open"
    """When it started isn't known, so it may have been."""


@dataclass(frozen=True, slots=True)
class _Gap:
    """A clause a leaf rests on that is incomplete, for one reason."""

    clause_id: str
    shown: str
    """What a reader is shown: the signed words, else the parser's detail."""
    started: str | None
    """For missing content that governs only a part year: the fact that says
    when the business started. `None`: it governs every reading."""


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
        tenant_id=tenant_id,
        as_of=snapshot.as_of,
        obligations=obligations,
        actors=actors,
        subgraph=_project(facts, actors, tenant_id=tenant_id, as_of=snapshot.as_of),
    )


def _project(
    facts: Facts, actors: tuple[ActorCoverage, ...], *, tenant_id: str, as_of: dt.date
) -> TenantSubgraph:
    def current(field: str) -> tuple[str, ...]:
        reading = facts.on(field, as_of)
        value = reading.value
        if reading.conflict or not isinstance(value, tuple):
            return ()
        return tuple(str(v) for v in value)

    return TenantSubgraph(
        organisation=tenant_id,
        operates_in=current(JURISDICTIONS_FIELD),
        handles=current("data_categories"),
        regulated_by=current("regulators"),
        acts_as=tuple((a.actor_id, a.truth) for a in actors if a.truth is not Truth.FALSE),
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
            earlier: c.id for c in snapshot.conditions.values() for earlier in c.lineage
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
        """The condition in force on `as_of` for an id: itself, or the one
        whose lineage includes it — however many amendments ago."""
        if condition_id in self._snapshot.conditions:
            return condition_id
        return self._successor.get(condition_id)

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
        """A leaf resting on an incomplete clause (ADR-0047) is unknown, with
        the clause's reason, for every reading the missing content governs. A
        reviewed reading can say that content governs only a part year (a
        financial year the business was carried on for part of): then it is
        unknown only for a year the facts show, or leave open, was one, and a
        full year is decided from the figure as usual."""
        gaps = self._gaps(spec)
        if any(g.started is None for g in gaps) or (gaps and leaf.period is LeafPeriod.CURRENT):
            # Missing content that governs every reading, or a reading that is
            # no financial year: no answer settles it, whatever the evidence.
            return self._unsettled(spec, gaps)
        if leaf.period is LeafPeriod.ANY_ENDED_FINANCIAL_YEAR:
            return self._history(spec, leaf, gaps)
        decided = self._read(spec, leaf)
        if not gaps:
            return decided
        year = self._year(leaf)
        status, started = self._year_is(year, gaps, leaf.field)
        evidence_ids = tuple(dict.fromkeys((*decided.evidence_ids, *started)))
        if status is _YearIs.FULL:
            # Decided from the figure, on the evidence that said it was a full year too.
            return replace(decided, evidence_ids=evidence_ids)
        if status is _YearIs.PART:
            return self._unsettled(spec, gaps, evidence_ids=evidence_ids)
        # Open: when the business started would say whether the year was a
        # part year, so that is asked, beside the figure if it is missing too.
        question = decided.question or self._start_question(spec, (year.label,), gaps, evidence_ids)
        return _node(
            spec,
            Truth.UNKNOWN,
            (),
            question=_also_needing(question, _started_fields(gaps)),
            evidence_ids=evidence_ids,
            incomplete=_pairs(gaps),
        )

    def _start_question(
        self,
        spec: ConditionSpec,
        years: tuple[str, ...],
        gaps: tuple[_Gap, ...],
        evidence_ids: tuple[str, ...],
    ) -> Question:
        """What settles an open year whose figure is known: when the business
        started, which says whether the year was a part year."""
        during, cites = " or ".join(years), _cites(gaps)
        return Question(
            condition_id=spec.id,
            field=_started_fields(gaps)[0],
            period=f"on {self._as_of}",
            question=f"When did the business start? If it was during {during}, that was a "
            f"part year, which {cites} governs.",
            unassessable_reason=f"When the business started is not recorded, so whether "
            f"{during} was a part year, which {cites} governs, can't be settled.",
            clause_ids=spec.clause_ids,
            evidence_ids=evidence_ids,
        )

    def _gaps(self, spec: ConditionSpec) -> tuple[_Gap, ...]:
        """Each incomplete clause the leaf rests on, for each reason, as the
        stored corpus marks it and a person read it."""
        readings = self._snapshot.readings
        gaps: list[_Gap] = []
        for clause_id in spec.clause_ids:
            for incompleteness in self._snapshot.incomplete.get(clause_id, ()):
                read = readings.entry(clause_id, incompleteness.reason)
                only = read.part_year_only if read is not None else None
                gaps.append(
                    _Gap(
                        clause_id,
                        readings.shown(clause_id, incompleteness),
                        only.started if only is not None else None,
                    )
                )
        return tuple(gaps)

    def _year_is(
        self, year: Period, gaps: tuple[_Gap, ...], figure: str
    ) -> tuple[_YearIs, tuple[str, ...]]:
        """Whether `year` was a part year, from when the business started, and
        the evidence that said so. A start on 1 July is a full year; a start
        after the year ended means it wasn't carried on in it at all, which is
        no part year either. With the start unknown, a `figure` recorded for
        the year before shows it was carried on then (ADR-0027) and so, as the
        mapping reads it, without a break all of this one: a full year."""
        found: list[_YearIs] = []
        evidence: list[str] = []
        for started in _started_fields(gaps):
            reading = self._facts.on(started, self._as_of)
            evidence += [e.evidence_id for e in reading.evidence]
            day = reading.value
            if (
                reading.missing
                or reading.conflict
                or not isinstance(day, dt.date)
                or isinstance(day, dt.datetime)
            ):
                before = self._facts.for_period(figure, year.previous_financial_year())
                if before.missing or before.conflict or before.value is None:
                    found.append(_YearIs.OPEN)
                else:
                    evidence += [e.evidence_id for e in before.evidence]
                    found.append(_YearIs.FULL)
            elif year.start < day < year.end:
                found.append(_YearIs.PART)
            else:
                found.append(_YearIs.FULL)
        status = next((s for s in (_YearIs.PART, _YearIs.OPEN) if s in found), _YearIs.FULL)
        return status, tuple(evidence)

    def _unsettled(
        self, spec: ConditionSpec, gaps: tuple[_Gap, ...], *, evidence_ids: tuple[str, ...] = ()
    ) -> Explanation:
        """Unknown, and no answer settles it: the law the leaf reads for this
        reading isn't all in the stored text (ADR-0047)."""
        return _node(spec, Truth.UNKNOWN, (), evidence_ids=evidence_ids, incomplete=_pairs(gaps))

    def _year(self, leaf: Leaf) -> Period:
        current_year = Period.financial_year_of(self._as_of)
        if leaf.period is LeafPeriod.PREVIOUS_FINANCIAL_YEAR:
            return current_year.previous_financial_year()
        return current_year

    def _read(self, spec: ConditionSpec, leaf: Leaf) -> Explanation:
        """The leaf decided from the fact it reads, for its one period."""
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

    def _history(self, spec: ConditionSpec, leaf: Leaf, gaps: tuple[_Gap, ...]) -> Explanation:
        """s6D(4)(a): any financial year that ended by `as_of` and after the
        later of the `since_field` date and `not_before`. True on the first
        year that passes the test; false only if every such year is known.

        With `gaps` (missing content that governs only a part year), a year
        that was a part year — the one the business started in — or may have
        been, because when it started isn't known, is never read from its
        figure: it is unsettled, and only the other years can decide."""
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
        unsettled = False
        open_years: list[str] = []
        for year in years:
            reading = self._facts.for_period(leaf.field, year)
            read += [e.evidence_id for e in reading.evidence]
            if gaps:
                status, started = self._year_is(year, gaps, leaf.field)
                read += started
                if status is not _YearIs.FULL:
                    unsettled = True
                    if status is _YearIs.OPEN:
                        open_years.append(year.label)
                    continue
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
        evidence_ids = tuple(dict.fromkeys(read))
        incomplete = _pairs(gaps) if unsettled else ()
        # With the start date unknown the floor is still never earlier than
        # `not_before`, so every year since then known and none passing is a
        # sound "no". A year with turnover recorded is taken as a year the
        # business was carried on (ADR-0027).
        if not unknown_years and not unsettled:
            return _node(spec, Truth.FALSE, (), evidence_ids=evidence_ids)
        if not unknown_years and not open_years:
            # Every other year is known and none passes, but the part year
            # can't be read: no answer settles it (ADR-0047).
            return _node(spec, Truth.UNKNOWN, (), evidence_ids=evidence_ids, incomplete=incomplete)
        if not unknown_years:
            # Every figure is in; only when it started would say whether an
            # open year was the part year.
            question = self._start_question(spec, tuple(open_years), gaps, evidence_ids)
            return _node(
                spec,
                Truth.UNKNOWN,
                (),
                question=question,
                evidence_ids=evidence_ids,
                incomplete=incomplete,
            )
        needs = () if since_known else (leaf.since_field,)
        if open_years:
            needs += _started_fields(gaps)
        question = Question(
            condition_id=spec.id,
            field=leaf.field,
            period=f"each financial year ended after {floor} and by {self._as_of}",
            question=spec.question,
            unassessable_reason=spec.unassessable_reason,
            clause_ids=spec.clause_ids,
            conflict=since.conflict,
            evidence_ids=evidence_ids,
            missing_periods=tuple(unknown_years),
            also_needs=tuple(dict.fromkeys(needs)),
        )
        return _node(
            spec,
            Truth.UNKNOWN,
            (),
            question=question,
            evidence_ids=evidence_ids,
            incomplete=incomplete,
        )

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
        if not children:
            return Explanation(
                condition_id=f"{obligation.id}#actors",
                operator="actors",
                truth=Truth.UNKNOWN,
                description=f"No role the obligation applies to is in force on {self._as_of}.",
                clause_ids=(),
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
                exception_clause_ids=spec.exception_clause_ids,
                qualifier_clause_ids=spec.qualifier_clause_ids,
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
            exception_clause_ids=spec.exception_clause_ids,
            qualifier_clause_ids=spec.qualifier_clause_ids,
        )


def _combine(
    operator: Operator, children: tuple[Explanation, ...]
) -> tuple[Truth, tuple[Explanation, ...]]:
    """Kleene `all_of` / `any_of`, with the children that decided it. A
    decided result keeps only the *first* decisive child in authored order —
    one reason is enough, and the citation chain stays minimal (#93: in
    Trace 1, s6D(4) never needed asking). An unknown keeps every unknown
    child — each is a question that could change the answer — and an unknown
    `all_of` also keeps the operands already met, as cited context."""
    decisive, neutral = (
        (Truth.FALSE, Truth.TRUE) if operator is Operator.ALL_OF else (Truth.TRUE, Truth.FALSE)
    )
    by_truth = {t: tuple(c for c in children if c.truth is t) for t in Truth}
    if by_truth[decisive]:
        return decisive, by_truth[decisive][:1]
    if by_truth[Truth.UNKNOWN]:
        if operator is Operator.ALL_OF:
            # The conditions already met are why the answer is "possibly"
            # rather than "no" (#93 Trace 2: under the threshold, s6D(1)), so
            # they stay as cited context; only unknown leaves become questions.
            return Truth.UNKNOWN, tuple(c for c in children if c.truth is not Truth.FALSE)
        return Truth.UNKNOWN, by_truth[Truth.UNKNOWN]
    return neutral, children


def _node(
    spec: ConditionSpec,
    truth: Truth,
    because: tuple[Explanation, ...],
    *,
    question: Question | None = None,
    evidence_ids: tuple[str, ...] = (),
    incomplete: tuple[tuple[str, str], ...] = (),
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
        incomplete=incomplete,
    )


def _pairs(gaps: tuple[_Gap, ...]) -> tuple[tuple[str, str], ...]:
    """What an explanation records of each gap: the clause and what a reader
    is shown about it."""
    return tuple(dict.fromkeys((g.clause_id, g.shown) for g in gaps))


def _cites(gaps: tuple[_Gap, ...]) -> str:
    return " and ".join(dict.fromkeys(_cite(g.clause_id) for g in gaps))


def _started_fields(gaps: tuple[_Gap, ...]) -> tuple[str, ...]:
    """The facts that say when the business started, for part-year gaps."""
    return tuple(dict.fromkeys(g.started for g in gaps if g.started is not None))


def _also_needing(question: Question, fields: tuple[str, ...]) -> Question:
    extra = tuple(f for f in fields if f != question.field and f not in question.also_needs)
    return replace(question, also_needs=(*question.also_needs, *extra))


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
        # `end` is exclusive (1 July): a year ending 30 June ended after a
        # start on 30 June, so it counts.
        if year.end > after:
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


def _cite(clause_id: str) -> str:
    return cite(clause_id, NO_TITLES)


def _reason(outcome: Outcome, explanation: Explanation) -> str:
    if outcome is Outcome.UNASSESSABLE:
        questions = explanation.questions()
        # The words a reader is shown: signed, where a person read the clause.
        missing = [
            f"{_cite(clause_id)} is incomplete: {shown}"
            for clause_id, shown in explanation.incompleteness()
        ]
        if not questions:
            if missing:
                return "Unassessable: " + "; ".join(missing)
            unknown = [n.description for n in explanation.walk() if n.truth is Truth.UNKNOWN]
            return "Unassessable: " + (unknown[-1] if unknown else explanation.description)
        return "Depends on: " + "; ".join([*(q.unassessable_reason for q in questions), *missing])
    # Each deciding fact with whether it held: a condition that wasn't met
    # is a reason too, and must never read as if it were (#130).
    label = {Truth.TRUE: "met", Truth.FALSE: "not met", Truth.UNKNOWN: "unknown"}
    leaves = [
        f"{label[n.truth]}: {n.description}"
        for n in explanation.walk()
        if not n.because and n.description
    ]
    verb = "Applies" if outcome is Outcome.APPLICABLE else "Does not apply"
    return f"{verb} — " + "; ".join(dict.fromkeys(leaves))
