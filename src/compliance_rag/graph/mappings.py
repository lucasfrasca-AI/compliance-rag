"""Mapping YAML (`corpus/mappings/`), parsed and validated before anything
reaches the graph (issue #89; the ontology from the #93 design session,
ADR-0027 and ADR-0028). The shapes are the ones `corpus/mappings/README.md`
documents; this module only enforces them — it authors nothing (D-01).

What a mapping file holds:

- **Obligations** — the requirement a clause imposes. `APPLIES_TO` an
  **Actor** (a role the law defines), optionally narrowed to **Jurisdictions**
  and **Sectors**, and `REQUIRES` obligation-specific **Conditions** or
  **Controls**.
- **Actors** — `QUALIFIED_BY` one condition tree: who holds the role is the
  law's question, answered by evaluating the tree against the tenant's dated
  facts (ADR-0027), never a self-declared tag.
- **Conditions** — a tree: `all_of`, `any_of` and `not` over other
  conditions, down to **leaves** that test one profile fact for one period.
- **Controls** — a standard's controls, crosswalk targets (ADR-0027, Q3).
- **Jurisdictions and Sectors** — reference nodes (ISO 3166-2, ANZSIC).
- **Exceptions** — for an obligation, the clauses that say when it does not
  apply or is displaced (APP 8.2 for APP 8.1). `EXCEPTED_BY` edges from the
  obligation to each clause (#164, ADR-0037). Keyed by the obligation's id, so
  a signed obligation gains them in a file of their own, reviewed on its own.

Everything but a reference node is **grounded**: `DERIVED_FROM` the clauses it
was read from (ADR-0028), one edge per compilation, so an obligation survives
a recompilation by gaining an edge. Everything is **dated** (D-05): its own
valid time, an `end_reason` when it ends (ADR-0029), and `supersedes` for a
replacement of the same kind.

Validation happens in full before the loader writes: front matter through
`validate_review`, every enum, every clause among the file's
`source_clause_ids`, every reference resolving among the files loaded
together, no condition cycle, and every referenced node in force for the whole
interval of what refers to it — except an operand, which only has to overlap
its parent and counts as false on a date it isn't in force (ADR-0027).
References to nodes already in the graph — clauses — are checked by the
loader, which can see the graph.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compliance_rag.corpus.models import (
    MODEL_AUTHOR_PREFIX,
    NO_END_DATE,
    EndReason,
    check_end,
    clause_ancestors,
    parse_stable_id,
    validate_review,
)


class MappingError(ValueError):
    """A mapping file that must not reach the graph."""


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]*$")


class Comparison(StrEnum):
    """How a leaf compares a profile fact with its `value`."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    BEFORE = "before"
    """A date fact earlier than `value` (a date, or a `RelativeDate`)."""
    ON_OR_AFTER = "on_or_after"


class Period(StrEnum):
    """Which fact a leaf reads, relative to `as_of` (the test time). A
    financial year is the Australian one, 1 July to 30 June (Acts
    Interpretation Act 1901 s 2B)."""

    CURRENT = "current"
    """The fact whose period contains `as_of` — a state, such as legal form."""
    CURRENT_FINANCIAL_YEAR = "current_financial_year"
    PREVIOUS_FINANCIAL_YEAR = "previous_financial_year"
    ANY_ENDED_FINANCIAL_YEAR = "any_ended_financial_year"
    """True if the test holds for any financial year that ended by `as_of`
    and after the later of the `since_field` date and `not_before`; false
    only if every such year is known and none holds (s6D(4)(a))."""


class RelativeDate(StrEnum):
    """A date a `before` / `on_or_after` leaf computes from `as_of`."""

    START_OF_CURRENT_FINANCIAL_YEAR = "start_of_current_financial_year"
    START_OF_PREVIOUS_FINANCIAL_YEAR = "start_of_previous_financial_year"


class Operator(StrEnum):
    """A condition node's form (stored on the node, ADR-0027)."""

    ALL_OF = "all_of"
    ANY_OF = "any_of"
    NOT = "not"
    LEAF = "leaf"


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


LeafValue = str | int | float | bool | dt.date | tuple[str, ...] | tuple[int | float, ...]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Dated(_Strict):
    """Valid time for anything that becomes a node (D-05, ADR-0029)."""

    id: str
    valid_from: dt.date
    valid_to: dt.date | None = None
    end_reason: EndReason | None = None
    """Why it ended — required with `valid_to`, absent without (ADR-0029).
    Status is never authored: it is derived from these dates on each `as_of`."""
    supersedes: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"{v!r} is not a valid id")
        return v

    @property
    def valid_until(self) -> dt.date:
        """`valid_to` with the bitemporal sentinel for "still in force"."""
        return self.valid_to or NO_END_DATE

    def covers(self, other: Dated) -> bool:
        """In force for the whole of `other`'s interval."""
        return self.valid_from <= other.valid_from and other.valid_until <= self.valid_until

    def overlaps(self, other: Dated) -> bool:
        """In force on at least one day `other` is."""
        return self.valid_from < other.valid_until and other.valid_from < self.valid_until

    @model_validator(mode="after")
    def _dates(self) -> Self:
        if self.valid_until <= self.valid_from:
            raise ValueError(f"{self.id}: valid_to is not after valid_from")
        try:
            check_end(self.valid_until, self.end_reason)
        except ValueError as exc:
            raise ValueError(f"{self.id}: {exc}") from exc
        if self.supersedes == self.id:
            raise ValueError(f"{self.id} cannot supersede itself")
        return self


def _stable_clause_ids(v: tuple[str, ...]) -> tuple[str, ...]:
    for clause_id in v:
        parse_stable_id(clause_id)
    if len(set(v)) != len(v):
        raise ValueError("a clause is listed twice")
    return v


class Grounded(Dated):
    """`DERIVED_FROM` the clauses it was read from (ADR-0028)."""

    clause_ids: tuple[str, ...] = Field(min_length=1)

    _stable_ids = field_validator("clause_ids")(_stable_clause_ids)

    @property
    def instrument_id(self) -> str:
        return parse_stable_id(self.clause_ids[0])[0]


class Jurisdiction(Dated):
    """A reference node: an ISO 3166 / 3166-2 code (`AU`, `AU-NSW`)."""

    name: str
    source: str


class Sector(Dated):
    """A reference node: an industry classification code (`anzsic-2006:Q`)."""

    name: str
    source: str


class Leaf(_Strict):
    """One profile fact, for one period, tested against one value."""

    field: str
    period: Period = Period.CURRENT
    test: Comparison
    value: LeafValue
    since_field: str | None = None
    not_before: dt.date | None = None

    @field_validator("field", "since_field")
    @classmethod
    def _snake(cls, v: str | None) -> str | None:
        if v is not None and not _FIELD.match(v):
            raise ValueError(f"profile field {v!r} is not snake_case")
        return v

    @model_validator(mode="after")
    def _shape(self) -> Self:
        value = self.value
        if self.test in (Comparison.IN, Comparison.NOT_IN):
            if not isinstance(value, tuple) or not value:
                raise ValueError(f"{self.test} needs a non-empty list value")
        elif isinstance(value, tuple):
            raise ValueError(f"{self.test} compares one value, not a list")
        if self.test in (Comparison.GT, Comparison.GTE, Comparison.LT, Comparison.LTE) and (
            isinstance(value, bool) or not isinstance(value, int | float)
        ):
            raise ValueError(f"{self.test} needs a number")
        if self.test in (Comparison.BEFORE, Comparison.ON_OR_AFTER) and not (
            isinstance(value, dt.date) or value in {r.value for r in RelativeDate}
        ):
            raise ValueError(f"{self.test} needs a date or one of {sorted(RelativeDate)}")
        history = self.period is Period.ANY_ENDED_FINANCIAL_YEAR
        if history != (self.since_field is not None and self.not_before is not None):
            raise ValueError(
                "since_field and not_before go with period any_ended_financial_year, and only it"
            )
        return self


class Condition(Grounded):
    """A node of a condition tree: exactly one of `all_of`, `any_of`, `not`
    or `leaf`. A leaf says what to ask when its fact is missing (D-06)."""

    model_config = _Strict.model_config | ConfigDict(populate_by_name=True)

    description: str
    all_of: tuple[str, ...] | None = None
    any_of: tuple[str, ...] | None = None
    negates: str | None = Field(default=None, alias="not")
    leaf: Leaf | None = None
    question: str | None = None
    unassessable_reason: str | None = None

    @property
    def operator(self) -> Operator:
        if self.all_of is not None:
            return Operator.ALL_OF
        if self.any_of is not None:
            return Operator.ANY_OF
        if self.negates is not None:
            return Operator.NOT
        return Operator.LEAF

    @property
    def operands(self) -> tuple[str, ...]:
        if self.negates is not None:
            return (self.negates,)
        return self.all_of or self.any_of or ()

    @model_validator(mode="after")
    def _form(self) -> Self:
        forms = [f for f in (self.all_of, self.any_of, self.negates, self.leaf) if f is not None]
        if len(forms) != 1:
            raise ValueError(f"{self.id}: exactly one of all_of, any_of, not or leaf")
        operands = self.all_of or self.any_of
        if operands is not None and (len(operands) < 2 or len(set(operands)) != len(operands)):
            raise ValueError(f"{self.id}: all_of / any_of needs two or more distinct conditions")
        if self.id in self.operands:
            raise ValueError(f"{self.id} cannot be its own operand")
        is_leaf = self.leaf is not None
        if is_leaf != (self.question is not None and self.unassessable_reason is not None):
            raise ValueError(
                f"{self.id}: a leaf carries a question and an unassessable_reason (D-06); "
                "a composite carries neither"
            )
        if not is_leaf and (self.question is not None or self.unassessable_reason is not None):
            raise ValueError(f"{self.id}: only a leaf carries a question")
        return self


class Actor(Grounded):
    """A role an instrument defines (`{instrument_id}/actor/{slug}`), held by
    whoever its condition tree says holds it (ADR-0027)."""

    name: str
    description: str
    qualified_by: str

    @model_validator(mode="after")
    def _namespaced(self) -> Self:
        if not self.id.startswith(f"{self.instrument_id}/actor/"):
            raise ValueError(f"{self.id}: an actor id is {self.instrument_id}/actor/<slug>")
        return self


class Control(Grounded):
    """A control of a standard, crosswalk target (ADR-0027). `descriptor` is
    our own words, never the standard's title (PHASES.md: ISO licensing)."""

    descriptor: str


def _distinct(v: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(v)) != len(v):
        raise ValueError(f"listed twice: {sorted({x for x in v if v.count(x) > 1})}")
    return v


class AppliesTo(_Strict):
    actors: tuple[str, ...] = ()
    jurisdictions: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()

    _unique = field_validator("actors", "jurisdictions", "sectors")(_distinct)


class Requires(_Strict):
    conditions: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()

    _unique = field_validator("conditions", "controls")(_distinct)


class Obligation(Grounded):
    summary: str
    applies_to: AppliesTo = AppliesTo()
    requires: Requires = Requires()

    @model_validator(mode="after")
    def _has_an_actor(self) -> Self:
        if not self.applies_to.actors:
            raise ValueError(
                f"{self.id}: applies_to names at least one actor — who an obligation binds "
                "is the law's question, not a default (ADR-0027)"
            )
        return self


class ObligationExceptions(_Strict):
    """An obligation's exceptions (#164, ADR-0037): the clauses that say when
    it does not apply, or is displaced — APP 8.2 for APP 8.1. Which clause
    excepts which is a legal judgement, so it is reviewed mapping content,
    like every other (D-01).

    Keyed by the obligation's id, not written on it: an obligation already
    signed gains its exceptions in a file of their own, reviewed on its own,
    and its signed file is never edited. Loaded as `EXCEPTED_BY` edges from
    the obligation to each clause, one per compilation like `DERIVED_FROM`
    (ADR-0028); once loaded they may grow, never shrink (D-05)."""

    obligation_id: str
    clause_ids: tuple[str, ...] = Field(min_length=1)

    _stable_ids = field_validator("clause_ids")(_stable_clause_ids)


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
    jurisdictions: tuple[Jurisdiction, ...] = ()
    sectors: tuple[Sector, ...] = ()
    conditions: tuple[Condition, ...] = ()
    actors: tuple[Actor, ...] = ()
    controls: tuple[Control, ...] = ()
    obligations: tuple[Obligation, ...] = ()
    exceptions: tuple[ObligationExceptions, ...] = ()
    crosswalks: tuple[Crosswalk, ...] = ()
    policy_mappings: tuple[PolicyMapping, ...] = ()

    @property
    def grounded(self) -> tuple[Grounded, ...]:
        return (*self.conditions, *self.actors, *self.controls, *self.obligations)

    @model_validator(mode="after")
    def _review_and_sources(self) -> Self:
        validate_review(self.author, self.reviewed_by)
        for clause_id in self.source_clause_ids:
            parse_stable_id(clause_id)
        sources = set(self.source_clause_ids)
        for item in self.grounded:
            for clause_id in item.clause_ids:
                if clause_id not in sources:
                    raise ValueError(f"{item.id}: clause {clause_id} is not in source_clause_ids")
        for entry in self.exceptions:
            for clause_id in entry.clause_ids:
                if clause_id not in sources:
                    raise ValueError(
                        f"exceptions of {entry.obligation_id}: clause {clause_id} is not in "
                        "source_clause_ids"
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
    files and kinds, every reference resolving, no condition cycle, every
    referenced node in force for the whole of what refers to it, and every
    supersession gapless with one successor."""

    files: tuple[MappingFile, ...]

    @property
    def jurisdictions(self) -> tuple[Jurisdiction, ...]:
        return tuple(x for f in self.files for x in f.jurisdictions)

    @property
    def sectors(self) -> tuple[Sector, ...]:
        return tuple(x for f in self.files for x in f.sectors)

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return tuple(x for f in self.files for x in f.conditions)

    @property
    def actors(self) -> tuple[Actor, ...]:
        return tuple(x for f in self.files for x in f.actors)

    @property
    def controls(self) -> tuple[Control, ...]:
        return tuple(x for f in self.files for x in f.controls)

    @property
    def obligations(self) -> tuple[Obligation, ...]:
        return tuple(x for f in self.files for x in f.obligations)

    @property
    def obligations_by_id(self) -> dict[str, Obligation]:
        return {o.id: o for o in self.obligations}

    @property
    def conditions_by_id(self) -> dict[str, Condition]:
        return {c.id: c for c in self.conditions}

    @property
    def exceptions(self) -> tuple[ObligationExceptions, ...]:
        return tuple(x for f in self.files for x in f.exceptions)

    @property
    def exceptions_by_obligation(self) -> dict[str, tuple[str, ...]]:
        """Each obligation's exception clauses, by obligation id (#164)."""
        return {e.obligation_id: e.clause_ids for e in self.exceptions}

    @property
    def crosswalks(self) -> tuple[Crosswalk, ...]:
        return tuple(x for f in self.files for x in f.crosswalks)

    @property
    def policy_mappings(self) -> tuple[PolicyMapping, ...]:
        return tuple(x for f in self.files for x in f.policy_mappings)

    @property
    def by_label(self) -> dict[str, tuple[Dated, ...]]:
        """Every dated item, by graph label."""
        return {
            "Jurisdiction": self.jurisdictions,
            "Sector": self.sectors,
            "Condition": self.conditions,
            "Actor": self.actors,
            "Control": self.controls,
            "Obligation": self.obligations,
        }

    @property
    def labels(self) -> dict[str, str]:
        """Each item's graph label, by id."""
        return {item.id: label for label, items in self.by_label.items() for item in items}

    @property
    def grounded(self) -> tuple[tuple[str, Grounded], ...]:
        """Every item `DERIVED_FROM` clauses, with its label."""
        return tuple(
            (label, item)
            for label, items in self.by_label.items()
            for item in items
            if isinstance(item, Grounded)
        )

    @model_validator(mode="after")
    def _references(self) -> Self:
        everything = [item for items in self.by_label.values() for item in items]
        _unique("mapping", [item.id for item in everything])
        _unique("policy mapping", [m.id for m in self.policy_mappings])
        by_kind = {label: {i.id: i for i in items} for label, items in self.by_label.items()}

        def resolve(
            referrer: Dated, label: str, target: str, what: str, *, whole: bool = True
        ) -> None:
            found = by_kind[label].get(target)
            if found is None:
                raise ValueError(f"{referrer.id}: {what} unknown {label.lower()} {target}")
            if not whole and not found.overlaps(referrer):
                raise ValueError(
                    f"{referrer.id} has operand {target}, which is never in force while it is"
                )
            if whole and not found.covers(referrer):
                raise ValueError(
                    f"{referrer.id} ({referrer.valid_from} to {referrer.valid_until}) refers to "
                    f"{target} ({found.valid_from} to {found.valid_until}), which is not in "
                    "force for all of it"
                )

        for condition in self.conditions:
            for operand in condition.operands:
                # An operand may start later or end earlier than its parent: on a
                # date it isn't in force, the provision doesn't exist and counts as
                # false, so repealing one gate is one retirement (ADR-0027, Q1).
                resolve(condition, "Condition", operand, "has operand", whole=False)
        _acyclic(self.conditions_by_id)
        for actor in self.actors:
            resolve(actor, "Condition", actor.qualified_by, "is qualified by")
        for obligation in self.obligations:
            for actor_id in obligation.applies_to.actors:
                resolve(obligation, "Actor", actor_id, "applies to")
            for code in obligation.applies_to.jurisdictions:
                resolve(obligation, "Jurisdiction", code, "applies to")
            for code in obligation.applies_to.sectors:
                resolve(obligation, "Sector", code, "applies to")
            for condition_id in obligation.requires.conditions:
                resolve(obligation, "Condition", condition_id, "requires")
            for control_id in obligation.requires.controls:
                resolve(obligation, "Control", control_id, "requires")
        ends = {**by_kind["Obligation"], **by_kind["Control"]}
        for crosswalk in self.crosswalks:
            for end in (crosswalk.source, crosswalk.target):
                if end not in ends:
                    raise ValueError(
                        f"crosswalk {crosswalk.source} -> {crosswalk.target}: "
                        f"unknown obligation or control {end}"
                    )
            if not ends[crosswalk.source].overlaps(ends[crosswalk.target]):
                raise ValueError(
                    f"crosswalk {crosswalk.source} -> {crosswalk.target}: the two are never "
                    "in force at the same time"
                )
        for label, items in by_kind.items():
            _check_supersession(label, items)
        for mapping in self.policy_mappings:
            if mapping.obligation_id not in by_kind["Obligation"]:
                raise ValueError(f"{mapping.id}: unknown obligation {mapping.obligation_id}")
        self._check_exceptions()
        return self

    def _check_exceptions(self) -> None:
        """An obligation's exceptions name it once, and each is another clause:
        never its own clause, and never one containing it — that would admit
        the whole provision around the obligation (#164's option (b)) by
        another name. Whether a clause is loaded is the loader's to check."""
        obligations = self.obligations_by_id
        seen: set[str] = set()
        for entry in self.exceptions:
            obligation = obligations.get(entry.obligation_id)
            if obligation is None:
                raise ValueError(f"exceptions of unknown obligation {entry.obligation_id}")
            if entry.obligation_id in seen:
                raise ValueError(
                    f"exceptions of {entry.obligation_id} are listed twice: one entry per "
                    "obligation, every compilation's clause in it"
                )
            seen.add(entry.obligation_id)
            for clause_id in entry.clause_ids:
                for own in obligation.clause_ids:
                    if clause_id == own:
                        raise ValueError(
                            f"{obligation.id}: {clause_id} is its own clause, not an exception "
                            "to it"
                        )
                    if clause_id in clause_ancestors(own):
                        raise ValueError(
                            f"{obligation.id}: exception {clause_id} contains its own clause "
                            f"{own}: an exception is the clause that excepts, not the provision "
                            "around the obligation"
                        )


def _unique(kind: str, ids: Iterable[str]) -> frozenset[str]:
    seen: set[str] = set()
    for identifier in ids:
        if identifier in seen:
            raise ValueError(f"{kind} id {identifier} is defined twice (ids are never reused)")
        seen.add(identifier)
    return frozenset(seen)


def _acyclic(conditions: Mapping[str, Condition]) -> None:
    """A condition tree may share subtrees (a DAG) but never loop."""
    done: set[str] = set()

    def visit(identifier: str, path: tuple[str, ...]) -> None:
        if identifier in path:
            raise ValueError(f"condition cycle: {' -> '.join((*path, identifier))}")
        if identifier in done:
            return
        for operand in conditions[identifier].operands:
            visit(operand, (*path, identifier))
        done.add(identifier)

    for identifier in sorted(conditions):
        visit(identifier, ())


def _check_supersession(label: str, items: Mapping[str, Dated]) -> None:
    successors: dict[str, str] = {}
    for item in items.values():
        if item.supersedes is None:
            continue
        earlier = successors.setdefault(item.supersedes, item.id)
        if earlier != item.id:
            raise ValueError(
                f"{item.supersedes} is superseded by both {earlier} and {item.id}: "
                "one item has one successor"
            )
        old = items.get(item.supersedes)
        if old is None:
            raise ValueError(f"{item.id}: supersedes unknown {label.lower()} {item.supersedes}")
        if old.valid_until != item.valid_from:
            raise ValueError(
                f"{item.id} starts {item.valid_from} but {old.id} ends {old.valid_until}: "
                "a supersession leaves no gap or overlap"
            )
        if old.end_reason is not EndReason.SUPERSEDED:
            raise ValueError(
                f"{old.id} is superseded by {item.id}, so its end_reason is superseded, "
                f"not {old.end_reason}"
            )


def words(text: str) -> str:
    """Prose as the graph stores it: YAML's folded line breaks gone,
    single-spaced. One function, so the loader and the in-memory snapshot
    can't store the same text two ways."""
    return " ".join(text.split())


def lineage(items: Mapping[str, Dated], identifier: str) -> tuple[str, ...]:
    """Every item `identifier` supersedes, directly or through a chain, most
    recent first. A node carries it so that an id from any earlier version
    resolves to whatever stands in for it today (ADR-0027)."""
    chain: list[str] = []
    current = items[identifier].supersedes
    while current is not None and current not in chain and current in items:
        chain.append(current)
        current = items[current].supersedes
    return tuple(chain)


def load_mapping_set(paths: Iterable[Path]) -> MappingSet:
    files = tuple(load_mapping_file(p) for p in sorted(paths))
    try:
        return MappingSet(files=files)
    except ValueError as exc:
        raise MappingError(str(exc)) from exc
