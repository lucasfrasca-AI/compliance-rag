"""The golden set's out-of-environment cases (#252, ADR-0051): questions that
name an instrument the tenant doesn't hold, with what an answer to each must
carry and may say.

One YAML file, `corpus/golden/not_in_environment.yaml`, beside `queries.yaml`
and with the same `author` / `reviewed_by` / `date` front matter: a human
assertion that the answer eval enforces, so it is used only once signed
(D-01), and a pending file is refused, as the recall gate refuses unsigned
labels. It is not part of `queries.yaml`: the recall gate embeds every query
there from the checked-in vector cache, and a new question has no cached
vector until someone refreshes it (`scripts/refresh_golden_embeddings.py`).
The delivered-context label here is checked by the answer eval instead, which
runs live.

**What a case checks** (`check_case`), on one live answer:

- **flagged**: `not_in_environment` names each instrument the case lists, by
  the register's name, whatever the model wrote. Deterministic.
- **delivered**: each expected clause is in what the model was given (the
  delivered list, ADR-0045). Deterministic.
- **and one of the two answers the human accepts**, measured by a plain-text
  proxy on the model's prose, which a reviewer reads in the recorded file:
  - it *says the instrument isn't in the environment*: the instrument's name
    near a negation of being in, available in, included in or held in it;
  - or it *answers from what is held, naming it*: the held instrument's name
    appears, and every verified citation is of an instrument the tenant holds.

A case fails, and the run says so, when the flag is missing, when the label
wasn't delivered, or when the answer does neither: the confident answer about
the named instrument the human said must never happen.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compliance_rag.assessment.environment import contains_words, name_words
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    instrument_owner,
    parse_stable_id,
    validate_library_instrument_id,
    validate_review,
    validate_tenant_segment,
)

ENVIRONMENT_CASES_PATH = (
    Path(__file__).resolve().parents[3] / "corpus" / "golden" / "not_in_environment.yaml"
)
PENDING = "PENDING"

_NEGATED_PRESENCE = re.compile(
    r"\b(?:not|isn't|isn\u2019t|is not|aren't|are not|no)\b[^.;]{0,60}?"
    r"\b(?:in (?:this|your|the) environment|environment|available|included|among|provided|"
    r"loaded|held|subscribed|part of)\b",
    re.IGNORECASE,
)


class UnsignedEnvironmentCases(ValueError):
    """Out-of-environment cases nobody has signed: refused, never used (D-01)."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnvironmentCase(_Strict):
    id: str = Field(pattern=r"^GE-\d{2,}$")
    query: str = Field(min_length=1)
    tenant_id: str
    subscribes: tuple[str, ...] = ()
    """The library instruments the tenant subscribes to for this case."""
    as_of: dt.date
    not_in_environment: tuple[str, ...] = Field(min_length=1)
    """The register's name (`known_as`) of each instrument the answer's
    `not_in_environment` must name."""
    expected_clause_ids: tuple[str, ...] = ()
    """Clauses that must be in what the model is given (the delivered list)."""
    cites_instead: str = Field(min_length=1)
    """The held instrument an answer from the clauses provided must name
    ("Privacy Act 1988")."""
    notes: str | None = None

    @field_validator("tenant_id")
    @classmethod
    def _tenant(cls, v: str) -> str:
        return validate_tenant_segment(v)

    @field_validator("subscribes")
    @classmethod
    def _library(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_library_instrument_id(i) for i in v)

    @model_validator(mode="after")
    def _labels_in_the_environment(self) -> EnvironmentCase:
        for clause_id in self.expected_clause_ids:
            instrument_id, _, _ = parse_stable_id(clause_id)
            if not self.holds(instrument_id):
                raise ValueError(
                    f"{self.id}: {clause_id!r} is not in {self.tenant_id!r}'s environment"
                )
        return self

    def holds(self, instrument_id: str) -> bool:
        """Whether the case's tenant can retrieve `instrument_id` (ADR-0020)."""
        owner = instrument_owner(instrument_id)
        return owner == self.tenant_id or (
            owner == PUBLIC_TENANT_ID
            and (self.tenant_id == PUBLIC_TENANT_ID or instrument_id in self.subscribes)
        )


class EnvironmentCases(_Strict):
    author: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    date: str = Field(min_length=1)
    cases: tuple[EnvironmentCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_unique_and_reviewed_by_a_person(self) -> EnvironmentCases:
        ids = [c.id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("out-of-environment case ids must be unique")
        validate_review(self.author, self.reviewed_by)
        if self.is_signed_off:
            dt.date.fromisoformat(self.date)
        return self

    @property
    def is_signed_off(self) -> bool:
        return not (self.reviewed_by.startswith(PENDING) or self.date.startswith(PENDING))


def read_environment_cases(path: Path = ENVIRONMENT_CASES_PATH) -> EnvironmentCases | None:
    """The file as it stands, signed or not (for the review test), or None
    where there is none."""
    if not path.exists():
        return None
    loaded: Any = yaml.safe_load(path.read_text(encoding="utf-8"))  # Any: YAML is untyped
    return EnvironmentCases.model_validate(cast("dict[str, Any]", loaded))  # Any: as above


def load_environment_cases(path: Path = ENVIRONMENT_CASES_PATH) -> EnvironmentCases | None:
    """The signed cases, or None where there are none; a pending file is
    refused."""
    cases = read_environment_cases(path)
    if cases is not None and not cases.is_signed_off:
        raise UnsignedEnvironmentCases(f"{path.name} is awaiting review, not used (D-01)")
    return cases


def _without_year(name: str) -> tuple[str, ...]:
    needle = name_words(name)
    return needle[:-1] if len(needle) > 1 and needle[-1].isdigit() else needle


def _mentions(text: str, name: str) -> bool:
    """Whether `text` names `name`, with or without its year."""
    return contains_words(name_words(text), _without_year(name))


def _asserts(text: str, name: str) -> bool:
    """Whether `text` speaks as the named instrument's law: "under the Spam
    Act you may…", "the Spam Act requires…", not after a negation within a
    few words ("I can't say what the Spam Act requires")."""
    words, needle = name_words(text), _without_year(name)
    n = len(needle)
    for i in range(len(words) - n + 1):
        if not needle or words[i : i + n] != needle:
            continue
        before, after = set(words[max(0, i - 6) : i]), words[i + n : i + n + 3]
        if before & _NEGATIONS:
            continue
        if (
            before & {"under", "per"}
            or set(after) & _ASSERTIVE
            or ("you" in after and set(after) & {"can", "may", "must", "need", "should"})
        ):
            return True
    return False


_NEGATIONS = frozenset({"not", "no", "can", "cannot", "t", "unable", "isn", "without"})
"""Words that, just before a name, read it as not answered from ("can't" is
"can" and "t" once punctuation is dropped)."""
_ASSERTIVE = frozenset(
    {
        "require",
        "requires",
        "say",
        "says",
        "allow",
        "allows",
        "permit",
        "permits",
        "prohibit",
        "prohibits",
        "lets",
        "means",
    }
)


@dataclass(frozen=True, slots=True)
class CaseCheck:
    flagged: bool
    delivered: bool
    says_not_in_environment: bool
    names_held_instrument: bool
    cites_only_held: bool
    speaks_as_named: bool
    """The prose speaks as the named instrument's law: the answer the human
    said must never happen, unless it also says the instrument isn't here."""

    @property
    def acceptable(self) -> bool:
        """The flag, the label, and one of the two answers the human accepts."""
        return (
            self.flagged
            and self.delivered
            and (
                self.says_not_in_environment
                or (
                    self.names_held_instrument and self.cites_only_held and not self.speaks_as_named
                )
            )
        )


def check_case(
    case: EnvironmentCase,
    *,
    flagged: Iterable[str | None],
    named: Iterable[str],
    delivered: Iterable[str],
    answer: str | None,
    cited: Iterable[str],
) -> CaseCheck:
    """One live answer against `case`. `flagged` is each `known_as` of the
    answer's `not_in_environment`, `named` the question's words for each;
    `delivered` the ids the model was given; `answer` its prose, None when
    withheld; `cited` the ids of its verified citations."""
    known = set(flagged)
    words = [*case.not_in_environment, *named]
    text = answer or ""
    says = any(
        _mentions(text[max(0, m.start() - 120) : m.end() + 120], w)
        for m in _NEGATED_PRESENCE.finditer(text)
        for w in words
    )
    cited_ids = list(cited)
    return CaseCheck(
        flagged=all(name in known for name in case.not_in_environment),
        delivered=set(case.expected_clause_ids) <= set(delivered),
        says_not_in_environment=says,
        names_held_instrument=_mentions(text, case.cites_instead),
        cites_only_held=bool(cited_ids)
        and all(case.holds(parse_stable_id(c)[0]) for c in cited_ids),
        speaks_as_named=any(_asserts(text, w) for w in words),
    )
