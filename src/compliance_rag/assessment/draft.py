"""The generator's structured output, validated before anything uses it
(issue #29, CLAUDE.md: "structured outputs are validated against a schema
before use").

A draft is the model's claim, not a citation anyone sees yet: `quote` is the
model's pointer into a clause, which the verifier (#30) checks against
canonical text and blocks on mismatch. What a user is shown as the cited text
is that canonical text, read from `ClauseStore` — the model never authors it
(D-02).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from compliance_rag.corpus.models import parse_stable_id

_FENCED = re.compile(r"^```(?:json)?\s*\n(?P<body>.*)\n```$", re.DOTALL)


class DraftCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clause_id: str
    quote: str = Field(min_length=1)

    @field_validator("clause_id")
    @classmethod
    def _stable_id(cls, v: str) -> str:
        parse_stable_id(v)
        return v


class DraftAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str = Field(min_length=1)
    citations: tuple[DraftCitation, ...]


class MalformedGeneration(ValueError):
    """The model's reply isn't a valid `DraftAnswer`; nothing in it is used."""


def parse_draft(text: str) -> DraftAnswer:
    """Strict: one JSON object matching `DraftAnswer`, optionally inside a
    single ```json fence — the one wrapper models commonly add. Anything else
    is refused whole, never partially salvaged."""
    body = text.strip()
    fenced = _FENCED.match(body)
    if fenced:
        body = fenced.group("body").strip()
    try:
        return DraftAnswer.model_validate_json(body)
    except ValidationError as exc:
        raise MalformedGeneration(f"generation is not a valid draft answer: {exc}") from exc
