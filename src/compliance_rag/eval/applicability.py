"""The applicability golden set (issue #117): profiles in, expected obligation
outcomes out — Track A's regression gate, run in CI like the recall gate.

`corpus/golden/applicability.yaml` carries the same `author` / `reviewed_by`
/ `date` front matter as every reviewed artefact. Each **case** is a tenant's
facts on a date (dated evidence, #114) and what Track A must answer:

- `outcomes`: obligation ids by outcome, with `otherwise` for every
  obligation not listed — "possibly" (`unassessable`) is an outcome like the
  other two, never collapsed;
- `acts_as`: the roles the tenant must hold (`true`), might hold
  (`unknown`) or doesn't (`false`);
- `questions`: for an undecided case, exactly the condition ids it must ask
  about — no more (a short-circuited leaf is never asked), no fewer;
- `cites`: clauses an obligation's citation chain must include.

The labels are human judgement (D-01): the model may draft them, the reviewer
corrects and signs them. A case is checked against whatever `GraphStore` it is
given, so the same labels gate the fake and the real graph.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compliance_rag.adapters.interfaces import GraphStore
from compliance_rag.assessment.coverage import Coverage, Outcome, Truth
from compliance_rag.assessment.evidence import (
    FIELD,
    Evidence,
    EvidenceProvenance,
    Facts,
    Period,
)
from compliance_rag.corpus.models import (
    NO_END_DATE,
    parse_stable_id,
    validate_review,
    validate_tenant_id,
)

APPLICABILITY_PATH = (
    Path(__file__).resolve().parents[3] / "corpus" / "golden" / "applicability.yaml"
)
PENDING = "PENDING"


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GoldenFact(_Strict):
    """One fact of a golden profile: for a financial year (`financial_year:
    2025` is FY2025-26) or from a date (`since`)."""

    field: str
    value: str | bool | int | float | dt.date | tuple[str, ...]
    financial_year: int | None = None
    since: dt.date | None = None

    @field_validator("field")
    @classmethod
    def _snake(cls, v: str) -> str:
        if not FIELD.match(v):
            raise ValueError(f"profile field {v!r} is not snake_case")
        return v

    @model_validator(mode="after")
    def _one_period(self) -> Self:
        if (self.financial_year is None) == (self.since is None):
            raise ValueError(f"{self.field}: give exactly one of financial_year or since")
        return self

    @property
    def period(self) -> Period:
        if self.financial_year is not None:
            return Period.financial_year(self.financial_year)
        return Period(self.since or dt.date.min)  # validated: one of the two is set


class Expected(_Strict):
    outcomes: Mapping[Outcome, tuple[str, ...]] = Field(
        default_factory=dict[Outcome, tuple[str, ...]]
    )
    otherwise: Outcome | None = None
    """The outcome of every obligation not listed in `outcomes`."""
    acts_as: Mapping[str, Truth] = Field(default_factory=dict[str, Truth])
    questions: tuple[str, ...] | None = None
    """When given, exactly the condition ids the case must ask about."""
    cites: Mapping[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])
    """Obligation id -> clauses its citation chain must include."""

    @model_validator(mode="after")
    def _each_obligation_once(self) -> Self:
        listed = [o for ids in self.outcomes.values() for o in ids]
        if len(listed) != len(set(listed)):
            raise ValueError("an obligation is listed under two outcomes")
        for clause_ids in self.cites.values():
            for clause_id in clause_ids:
                parse_stable_id(clause_id)
        return self


class ApplicabilityCase(_Strict):
    id: str = Field(pattern=r"^GA-\d{2,}$")
    description: str
    tenant_id: str
    as_of: dt.date
    facts: tuple[GoldenFact, ...] = ()
    expected: Expected
    notes: str | None = None

    @field_validator("tenant_id")
    @classmethod
    def _tenant(cls, v: str) -> str:
        validate_tenant_id(v)
        return v

    def evidence(self) -> tuple[Evidence, ...]:
        return tuple(
            Evidence(
                tenant_id=self.tenant_id,
                evidence_id=f"{self.id}-{n}",
                field=fact.field,
                value=fact.value,
                period=fact.period,
                provenance=EvidenceProvenance.ASSERTED,
                source=f"golden set {self.id}",
                asserted_by="golden set",
            )
            for n, fact in enumerate(self.facts, start=1)
        )


class ApplicabilityGoldenSet(_Strict):
    author: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    date: str = Field(min_length=1)
    compilations: Mapping[str, dt.date]
    """`{instrument_id}:{version_id}` -> the date that compilation is in
    force from: which clauses exist on each `as_of`."""
    cases: tuple[ApplicabilityCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_and_reviewed_by_a_person(self) -> Self:
        ids = [c.id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("applicability case ids must be unique")
        validate_review(self.author, self.reviewed_by)
        return self

    @property
    def is_signed_off(self) -> bool:
        """True once a reviewer has corrected and signed the labels."""
        return not (self.reviewed_by.startswith(PENDING) or self.date.startswith(PENDING))

    def clause_validity(self, clause_ids: set[str]) -> dict[str, tuple[dt.date, dt.date]]:
        """Each clause in force from its compilation's date (the compilations
        named here are the ones loaded; none is superseded yet)."""
        validity: dict[str, tuple[dt.date, dt.date]] = {}
        for clause_id in clause_ids:
            instrument_id, version_id, _ = parse_stable_id(clause_id)
            start = self.compilations.get(f"{instrument_id}:{version_id}")
            if start is not None:
                validity[clause_id] = (start, NO_END_DATE)
        return validity


def load_applicability_set(path: Path = APPLICABILITY_PATH) -> ApplicabilityGoldenSet:
    return ApplicabilityGoldenSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class Mismatch:
    case_id: str
    what: str
    expected: object
    actual: object

    def __str__(self) -> str:
        return f"{self.case_id} {self.what}: expected {self.expected!r}, got {self.actual!r}"


def compare(case: ApplicabilityCase, coverage: Coverage) -> tuple[Mismatch, ...]:
    """Every way `coverage` differs from the case's labels."""
    found: list[Mismatch] = []
    expected = case.expected
    actual = {o.obligation_id: o.outcome for o in coverage.obligations}
    wanted = {oid: outcome for outcome, ids in expected.outcomes.items() for oid in ids}
    for oid in sorted(set(wanted) - set(actual)):
        found.append(Mismatch(case.id, f"{oid} outcome", wanted[oid], "not in force"))
    for oid, outcome in sorted(actual.items()):
        label = wanted.get(oid, expected.otherwise)
        if label is not None and label is not outcome:
            found.append(Mismatch(case.id, f"{oid} outcome", label.value, outcome.value))
    roles = {a.actor_id: a.truth for a in coverage.actors}
    for actor_id, truth in sorted(expected.acts_as.items()):
        if roles.get(actor_id) is not truth:
            found.append(Mismatch(case.id, f"acts_as {actor_id}", truth.value, roles.get(actor_id)))
    if expected.questions is not None:
        asked = sorted({q.condition_id for q in coverage.questions})
        if asked != sorted(expected.questions):
            found.append(Mismatch(case.id, "questions", sorted(expected.questions), asked))
    for oid, clause_ids in sorted(expected.cites.items()):
        cited = set(coverage.obligation(oid).citations) if oid in actual else set[str]()
        missing = sorted(set(clause_ids) - cited)
        if missing:
            found.append(Mismatch(case.id, f"{oid} cites", clause_ids, f"missing {missing}"))
    return tuple(found)


async def check(store: GraphStore, golden: ApplicabilityGoldenSet) -> tuple[Mismatch, ...]:
    """Every case through `store`; the mismatches, if any."""
    found: list[Mismatch] = []
    for case in golden.cases:
        coverage = await store.coverage(
            Facts.of(case.evidence()), tenant_id=case.tenant_id, as_of=case.as_of
        )
        found += compare(case, coverage)
    return tuple(found)
