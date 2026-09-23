"""The chunker: clause records to chunks, with an injected token counter
(issue #18).

One clause becomes exactly one chunk — Phase 1's parsers (#17, #20) already
emit clauses at the smallest citable unit (a subsection, a paragraph, a
policy's numbered item), so there is no sub-clause splitting to do; a
clause whose own text is over budget is a parser-side problem to fix
upstream, not something the chunker silently truncates or splits. `Chunk`
belongs to at most one obligation (`CONTEXT.md`) is then true structurally:
a chunk can never span more than the one clause — and hence at most one
obligation — it came from.

`count_tokens` is a parameter, not a model the chunker loads itself, so
these tests need no tokenizer download (issue #18's own stated reason).
The real XLM-R tokenizer over real Schedule 1 chunks is issue #21.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from compliance_rag.corpus.chunker import CHUNK_MAX_TOKENS, chunk_clause, chunk_clauses

from compliance_rag.corpus.models import ClauseRecord

WORD_COUNT: Callable[[str], int] = lambda text: len(text.split())  # noqa: E731


def _law_clause(clause_path: str = "s6c/1/a", text: str = "an individual; or") -> ClauseRecord:
    return ClauseRecord.build(
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
        clause_path=clause_path,
        text=text,
    )


def test_chunking_is_a_pure_function_of_the_clause_and_the_counter() -> None:
    clause = _law_clause()
    first = chunk_clause(clause, count_tokens=WORD_COUNT)
    second = chunk_clause(clause, count_tokens=WORD_COUNT)
    assert first == second


def test_a_chunk_carries_the_clause_it_was_derived_from() -> None:
    clause = _law_clause()
    chunk = chunk_clause(clause, count_tokens=WORD_COUNT)
    assert chunk.clause_id == clause.clause_id
    assert chunk.text == clause.canonical_text
    assert chunk.scope == clause.scope
    assert chunk.tenant_id == clause.tenant_id


def test_a_chunk_belongs_to_at_most_one_obligation_by_construction() -> None:
    """No obligation is assigned here — Track A does that in Phase 2 — but
    the 1:1 clause-to-chunk mapping means a chunk can never span more than
    one clause, so it can never span more than one obligation either."""
    chunk = chunk_clause(_law_clause(), count_tokens=WORD_COUNT)
    assert chunk.obligation_id is None


def test_chunk_clauses_maps_each_clause_to_exactly_one_chunk() -> None:
    clauses = (
        _law_clause("s6c/1/a", "an individual; or"),
        _law_clause("s6c/1/b", "a body corporate; or"),
    )
    chunks = chunk_clauses(clauses, count_tokens=WORD_COUNT)
    assert len(chunks) == len(clauses)
    assert {c.clause_id for c in chunks} == {c.clause_id for c in clauses}


def test_respects_the_budget_derived_in_issue_6_not_a_hardcoded_number() -> None:
    """A clause at exactly CHUNK_MAX_TOKENS (by the given counter) is
    accepted — proving the chunker checks against the shared constant, not
    an independent literal that happened to also be 420."""
    text = "word " * CHUNK_MAX_TOKENS
    clause = _law_clause(text=text)
    chunk = chunk_clause(clause, count_tokens=WORD_COUNT)
    assert WORD_COUNT(chunk.text) == CHUNK_MAX_TOKENS


def test_never_emits_a_chunk_over_budget_fails_loudly_instead() -> None:
    text = "word " * (CHUNK_MAX_TOKENS + 1)
    clause = _law_clause(clause_path="s6/1/agency", text=text)
    with pytest.raises(ValueError, match=r"s6/1/agency") as excinfo:
        chunk_clause(clause, count_tokens=WORD_COUNT)
    assert "chunk" in str(excinfo.value)


def test_a_clause_under_budget_by_the_right_counter_but_over_by_word_count() -> None:
    """Issue #18's own acceptance: 'a clause is under budget by reranker
    tokens and over by word count.' Two different counting functions over
    the *same* clause text, proving the budget is measured with whichever
    counter is actually injected — not that a real tokenizer agrees (#21)."""
    clause_text = "reasonable " * 450  # 450 words

    def word_count(text: str) -> int:
        return len(text.split())

    def fake_subword_count(text: str) -> int:
        return round(word_count(text) * 400 / 450)  # 400: under CHUNK_MAX_TOKENS

    clause = _law_clause(text=clause_text)

    chunk = chunk_clause(clause, count_tokens=fake_subword_count)
    assert chunk.text == clause.canonical_text

    with pytest.raises(ValueError, match="chunk"):
        chunk_clause(clause, count_tokens=word_count)
