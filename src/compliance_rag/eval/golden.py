"""The golden set (`CONTEXT.md`: Golden set, issue #27): labelled queries the
regression gate measures retrieval against.

One YAML file, `corpus/golden/queries.yaml`, with the same `author` /
`reviewed_by` / `date` front matter as mapping YAML. The labels are human
judgement (D-01): an LLM may draft query text and propose labels, but an entry
is a label only once `reviewed_by` has corrected and signed it — which is why
`is_signed_off` exists, and why the recall gate (#28) must not treat an
unsigned file as authoritative.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from compliance_rag.corpus.models import (
    instrument_owner,
    parse_stable_id,
    validate_review,
    validate_tenant_segment,
)

GOLDEN_SET_PATH = Path(__file__).resolve().parents[3] / "corpus" / "golden" / "queries.yaml"
PENDING = "PENDING"


class Category(StrEnum):
    EXACT_ID = "exact_id"
    PARAPHRASE = "paraphrase"
    CROSS_CLAUSE = "cross_clause"


class GoldenQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^GQ-\d{2,}$")
    category: Category
    query: str = Field(min_length=1)
    tenant_id: str
    as_of: dt.date
    expected_clause_ids: tuple[str, ...] = Field(min_length=1)
    notes: str | None = None

    @field_validator("tenant_id")
    @classmethod
    def _tenant(cls, v: str) -> str:
        return validate_tenant_segment(v)

    @model_validator(mode="after")
    def _labels_fit_the_category_and_the_scope(self) -> GoldenQuery:
        for clause_id in self.expected_clause_ids:
            instrument_id, _, _ = parse_stable_id(clause_id)
            # One retrieve() is scoped to one tenant's environment (ADR-0020):
            # `_public` sees library law, a tenant sees its own documents here.
            owner = instrument_owner(instrument_id)
            if owner != self.tenant_id:
                raise ValueError(
                    f"{self.id}: {clause_id!r} is not in {self.tenant_id!r}'s environment"
                )
        if self.category is Category.EXACT_ID and self.expected_clause_ids != (self.query.strip(),):
            raise ValueError(f"{self.id}: an exact_id query is its one expected clause id")
        if self.category is not Category.CROSS_CLAUSE and len(self.expected_clause_ids) != 1:
            raise ValueError(f"{self.id}: only cross_clause entries expect several clauses")
        if self.category is Category.CROSS_CLAUSE and len(self.expected_clause_ids) < 2:
            raise ValueError(f"{self.id}: a cross_clause entry expects at least two clauses")
        return self


class GoldenSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    author: str = Field(min_length=1)
    reviewed_by: str = Field(min_length=1)
    date: str = Field(min_length=1)
    queries: tuple[GoldenQuery, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_unique_and_reviewed_by_a_person(self) -> GoldenSet:
        ids = [q.id for q in self.queries]
        if len(ids) != len(set(ids)):
            raise ValueError("golden query ids must be unique")
        validate_review(self.author, self.reviewed_by)
        if self.is_signed_off:
            dt.date.fromisoformat(self.date)  # a signed file carries the real review date
        return self

    @property
    def is_signed_off(self) -> bool:
        """True once a reviewer has corrected and signed the labels."""
        return not (self.reviewed_by.startswith(PENDING) or self.date.startswith(PENDING))


def load_golden_set(path: Path = GOLDEN_SET_PATH) -> GoldenSet:
    return GoldenSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
