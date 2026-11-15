"""The corpus as it stood before #338: s 6DA(2) incomplete, with its signed
readings (ADR-0047, ADR-0052).

#338 completed s6da/2 from the PDF compilation (ADR-0058), so the committed
corpus holds no incomplete clause and `corpus/instruments/` no reading file.
The incomplete-clause state, its readings and Track A's part-year narrowing
stay: the parser marks every image, table and penalty line, and the next one
selected is stored the same way. Their tests run on this: s6da/2 as the parse
of the ePub alone gives it, and the two signed reading files as they were
signed, retired from the corpus to `tests/fixtures/incomplete_readings/`
unchanged.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from compliance_rag.corpus import incomplete_reading
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.incomplete_reading import (
    IncompleteReadings,
    committed_incomplete_readings,
    load_incomplete_readings,
)
from compliance_rag.corpus.models import Chunk, ClauseRecord, Incomplete, canonicalise
from compliance_rag.corpus.parser import FORMULA_NOT_TEXT
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.golden_corpus import gate_clauses

READINGS_ROOT = Path(__file__).resolve().parent / "fixtures" / "incomplete_readings"
"""`incomplete.yaml` and `part_year.yaml`, as signed (2026-10-20, 2026-10-21)."""
S6DA_2 = "privacy-act-1988:C2026C00227:s6da/2"
LEAD_IN = (
    "However, if a business has been carried on for only part of a financial year, its annual "
    "turnover for the financial year is the amount worked out using the formula:"
)
"""s6da/2 as the ePub alone gives it."""
INCOMPLETE: dict[str, Incomplete] = {S6DA_2: (FORMULA_NOT_TEXT,)}
"""The stored corpus's incomplete clauses before #338."""


def signed_readings() -> IncompleteReadings:
    return load_incomplete_readings(READINGS_ROOT)


def before_338(clause: ClauseRecord) -> ClauseRecord:
    """`clause` as the corpus held it before #338."""
    if clause.clause_id != S6DA_2:
        return clause
    return clause.model_copy(
        update={
            "text": LEAD_IN,
            "canonical_text": canonicalise(LEAD_IN),
            "incomplete": INCOMPLETE[S6DA_2],
        }
    )


def gate_clauses_before_338() -> tuple[ClauseRecord, ...]:
    return tuple(before_338(c) for c in gate_clauses())


def gate_chunks_before_338() -> tuple[Chunk, ...]:
    return SyntheticRegistry.load().mark(
        chunk_clauses(gate_clauses_before_338(), count_tokens=count_reranker_tokens)
    )


@pytest.fixture
def readings_before_338(monkeypatch: pytest.MonkeyPatch) -> Iterator[IncompleteReadings]:
    """What the app reads as the committed readings, for one test: the two
    signed files as they stood before #338."""
    signed = signed_readings()
    committed_incomplete_readings.cache_clear()

    def load(root: Path = READINGS_ROOT) -> IncompleteReadings:
        return signed

    monkeypatch.setattr(incomplete_reading, "load_incomplete_readings", load)
    yield signed
    committed_incomplete_readings.cache_clear()
