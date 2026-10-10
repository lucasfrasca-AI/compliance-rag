"""Mapping YAML (`corpus/mappings/`), parsed and validated before anything
reaches the graph (issue #89). The shapes are the ones
`corpus/mappings/README.md` documents; this module only enforces them — it
authors nothing (D-01).

Validation happens in full before the loader writes: front matter through
`validate_review`, every enum, every obligation's clause among the file's
`source_clause_ids`, an approved policy mapping naming its approver, and
every reference resolvable within the files loaded together (references to
nodes already in the graph — clauses, controls — are checked by the loader,
which can see the graph).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compliance_rag.adapters.graph_schema import Status
from compliance_rag.corpus.models import (
    MODEL_AUTHOR_PREFIX,
    NO_END_DATE,
    parse_stable_id,
    validate_review,
)


class MappingError(ValueError):
    """A mapping file that must not reach the graph."""


class ConditionKind(StrEnum):
    TURNOVER = "turnover"
    ACTOR = "actor"
    JURISDICTION = "jurisdiction"
    DATA_CATEGORY = "data_category"


class Operator(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"


class CrosswalkKind(StrEnum):
    EQUIVALENT = "equivalent"
    PARTIAL = "partial"
    RELATED = "related"


class PolicyMappingStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ProposedBy(StrEnum):
    LLM = "llm"
    HUMAN = "human"


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AppliesTo(_Strict):
    jurisdictions: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    actor_roles: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.jurisdictions or self.sectors or self.actor_roles)


class Requires(_Strict):
    conditions: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()


class Obligation(_Strict):
    id: str
    clause_id: str
    summary: str
    status: Status
    valid_from: dt.date
    valid_to: dt.date | None = None
    applies_to: AppliesTo = AppliesTo()
    requires: Requires = Requires()
    supersedes: str | None = None

    @field_validator("clause_id")
    @classmethod
    def _stable_id(cls, v: str) -> str:
        parse_stable_id(v)
        return v

    @property
    def valid_until(self) -> dt.date:
        """`valid_to` with the bitemporal sentinel for "still in force"."""
        return self.valid_to or NO_END_DATE

    @model_validator(mode="after")
    def _dates(self) -> Self:
        if self.valid_until <= self.valid_from:
            raise ValueError(f"{self.id}: valid_to is not after valid_from")
        if self.supersedes == self.id:
            raise ValueError(f"{self.id} cannot supersede itself")
        return self


class Condition(_Strict):
    id: str
    kind: ConditionKind
    description: str
    profile_field: str
    operator: Operator
    value: str | int | float | bool | tuple[str | int | float, ...]
    unassessable_reason: str


class Crosswalk(_Strict):
    model_config = _Strict.model_config | ConfigDict(populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")
    kind: CrosswalkKind
    note: str


class PolicyMapping(_Strict):
    id: str
    policy_clause_id: str
    obligation_id: str
    kind: CrosswalkKind
    status: PolicyMappingStatus
    proposed_by: ProposedBy
    approved_by: str | None = None
    approved_date: dt.date | None = None

    @field_validator("policy_clause_id")
    @classmethod
    def _policy_stable_id(cls, v: str) -> str:
        parse_stable_id(v)
        return v

    @model_validator(mode="after")
    def _approval(self) -> Self:
        if self.approved_by and self.approved_by.startswith(MODEL_AUTHOR_PREFIX):
            raise ValueError(f"{self.id}: approved_by must be a person, never a model (D-01)")
        approved = self.status is PolicyMappingStatus.APPROVED
        if approved and not (self.approved_by and self.approved_date):
            raise ValueError(f"{self.id}: approved needs approved_by and approved_date")
        if not approved and (self.approved_by or self.approved_date):
            raise ValueError(f"{self.id}: only an approved mapping names an approver")
        return self


class MappingFile(_Strict):
    author: str
    reviewed_by: str
    date: dt.date
    source_clause_ids: tuple[str, ...] = Field(min_length=1)
    obligations: tuple[Obligation, ...] = ()
    conditions: tuple[Condition, ...] = ()
    crosswalks: tuple[Crosswalk, ...] = ()
    policy_mappings: tuple[PolicyMapping, ...] = ()

    @model_validator(mode="after")
    def _review_and_sources(self) -> Self:
        validate_review(self.author, self.reviewed_by)
        for clause_id in self.source_clause_ids:
            parse_stable_id(clause_id)
        sources = set(self.source_clause_ids)
        for obligation in self.obligations:
            if obligation.clause_id not in sources:
                raise ValueError(
                    f"{obligation.id}: clause {obligation.clause_id} is not in source_clause_ids"
                )
        return self


REQUIRED_FRONT_MATTER = ("author", "reviewed_by", "date", "source_clause_ids")


def load_mapping_file(path: Path) -> MappingFile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise MappingError(f"{path.name}: not a mapping document")
    missing = [k for k in REQUIRED_FRONT_MATTER if k not in raw]
    if missing:
        raise MappingError(f"{path.name}: front matter missing {missing}")
    try:
        return MappingFile.model_validate(raw)
    except ValueError as exc:
        raise MappingError(f"{path.name}: {exc}") from exc


class MappingSet(_Strict):
    """Every mapping file loaded together, checked as one: ids unique across
    files, and every reference to an obligation or condition resolving."""

    files: tuple[MappingFile, ...]

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return tuple(o for f in self.files for o in f.obligations)

    @property
    def obligations_by_id(self) -> dict[str, Obligation]:
        return {o.id: o for o in self.obligations}

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return tuple(c for f in self.files for c in f.conditions)

    @property
    def crosswalks(self) -> tuple[Crosswalk, ...]:
        return tuple(x for f in self.files for x in f.crosswalks)

    @property
    def policy_mappings(self) -> tuple[PolicyMapping, ...]:
        return tuple(m for f in self.files for m in f.policy_mappings)

    @model_validator(mode="after")
    def _references(self) -> Self:
        obligation_ids = _unique("obligation", (o.id for o in self.obligations))
        condition_ids = _unique("condition", (c.id for c in self.conditions))
        _unique("policy mapping", (m.id for m in self.policy_mappings))
        by_id = self.obligations_by_id
        successors: dict[str, str] = {}
        for obligation in self.obligations:
            for condition in obligation.requires.conditions:
                if condition not in condition_ids:
                    raise ValueError(f"{obligation.id}: requires unknown condition {condition}")
            if obligation.supersedes is not None:
                earlier = successors.setdefault(obligation.supersedes, obligation.id)
                if earlier != obligation.id:
                    raise ValueError(
                        f"{obligation.supersedes} is superseded by both {earlier} and "
                        f"{obligation.id}: one obligation has one successor"
                    )
                old = by_id.get(obligation.supersedes)
                if old is None:
                    raise ValueError(
                        f"{obligation.id}: supersedes unknown obligation {obligation.supersedes}"
                    )
                if old.valid_until != obligation.valid_from:
                    raise ValueError(
                        f"{obligation.id} starts {obligation.valid_from} but "
                        f"{old.id} ends {old.valid_until}: a supersession leaves no gap or overlap"
                    )
        for mapping in self.policy_mappings:
            if mapping.obligation_id not in obligation_ids:
                raise ValueError(f"{mapping.id}: unknown obligation {mapping.obligation_id}")
        return self


def _unique(kind: str, ids: Iterable[str]) -> frozenset[str]:
    seen: set[str] = set()
    for identifier in ids:
        if identifier in seen:
            raise ValueError(f"{kind} id {identifier} is defined twice (ids are never reused)")
        seen.add(identifier)
    return frozenset(seen)


def load_mapping_set(paths: Iterable[Path]) -> MappingSet:
    files = tuple(load_mapping_file(p) for p in sorted(paths))
    try:
        return MappingSet(files=files)
    except ValueError as exc:
        raise MappingError(str(exc)) from exc
