"""The chunk token cap, derived from the reranker window (`CONTEXT.md`:
Chunk, issue #6).

`gemini-embedding-001` accepts ~2048 tokens, so the embedder never binds:
`BAAI/bge-reranker-v2-m3` scores query and passage together in one 512-token
window, and overflow truncates the tail of the clause silently — legislative
prose usually puts the operative requirement there. `CHUNK_MAX_TOKENS` is
derived from the reranker's window rather than an independent literal, so
the relationship (chunk + query + specials <= reranker window) stays true
by construction instead of by two numbers happening to agree today.

This issue ships the derivation and stub-driven tests proving the guard is
wired to whatever counter it's given — not that a real tokenizer agrees.
The real-tokenizer assertion over actual Schedule 1 chunks is issue #21,
gated on the chunker (#18) and Cloud Storage ingest (#19) existing.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from compliance_rag.corpus.chunker import (
    CHUNK_MAX_TOKENS,
    RERANKER_MAX_TOKENS,
    RESERVED_FOR_QUERY_AND_SPECIALS,
    ensure_within_reranker_window,
)
from compliance_rag.settings import Settings


def test_chunk_max_tokens_is_420() -> None:
    """Pinned exact value — `CONTEXT.md`'s Chunk entry commits to 420, not a
    range, so a future change to either input is a deliberate edit here
    too, not a silent drift."""
    assert CHUNK_MAX_TOKENS == 420


def test_chunk_max_tokens_is_derived_not_duplicated() -> None:
    """The relationship the issue exists to keep true, not two numbers that
    happen to agree today."""
    assert CHUNK_MAX_TOKENS == RERANKER_MAX_TOKENS - RESERVED_FOR_QUERY_AND_SPECIALS


def test_reranker_max_tokens_is_single_sourced_from_settings() -> None:
    """`RERANKER_MAX_TOKENS` is read directly from `Settings`'s declared
    default, not a second literal that could quietly diverge from what's
    actually deployed — this assertion is true by construction, and stays
    that way only as long as chunker.py keeps deriving rather than copying."""
    assert Settings.model_fields["reranker_max_tokens"].default == RERANKER_MAX_TOKENS


def test_a_chunk_under_the_cap_and_a_query_that_still_fits_passes() -> None:
    ensure_within_reranker_window(chunk_tokens=CHUNK_MAX_TOKENS, query_tokens=50)


def test_a_chunk_over_chunk_max_tokens_fails_loudly() -> None:
    with pytest.raises(ValueError, match="chunk"):
        ensure_within_reranker_window(chunk_tokens=CHUNK_MAX_TOKENS + 1, query_tokens=0)


def test_a_chunk_plus_query_over_the_reranker_window_fails_loudly() -> None:
    """Individually within budget, but the combination overflows the
    model's actual window — the guard must catch this, not just the
    per-chunk cap, since that's the failure mode the cross-encoder
    silently truncates rather than rejecting."""
    with pytest.raises(ValueError, match="reranker"):
        ensure_within_reranker_window(
            chunk_tokens=CHUNK_MAX_TOKENS, query_tokens=RESERVED_FOR_QUERY_AND_SPECIALS + 1
        )


_WORD_COUNT_STUB: Callable[[str], int] = lambda text: len(text.split())  # noqa: E731
_CHAR_COUNT_STUB: Callable[[str], int] = lambda text: len(text)  # noqa: E731


@pytest.mark.parametrize(
    "counter",
    [_WORD_COUNT_STUB, _CHAR_COUNT_STUB],
    ids=["word_count_stub", "char_count_stub"],
)
def test_the_guard_is_driven_by_whichever_counter_is_injected(
    counter: Callable[[str], int],
) -> None:
    """The guard takes an already-counted token number, not text plus a
    counter — proving it's counter-agnostic (issue #18's actual `chunk()`
    is what applies a specific counter to real text; this proves the guard
    underneath doesn't hardcode an assumption about which one)."""
    text = "word " * 500  # over CHUNK_MAX_TOKENS by either stub above
    with pytest.raises(ValueError, match="chunk"):
        ensure_within_reranker_window(chunk_tokens=counter(text), query_tokens=0)


def test_stub_where_the_right_counter_passes_and_the_wrong_one_would_have_failed() -> None:
    """Issue #6's own acceptance: 'a clause that is under 420 reranker
    tokens but over 420 by word count' — proving the budget is measured
    with the *right* counter, not merely wired to *some* counter. Both
    counts below come from the *same* clause text via two different
    counting functions, not two unrelated magic numbers: whether a real
    subword tokenizer actually undercounts this direction for real
    legislative prose is #21's job (the real XLM-R tokenizer over real
    Schedule 1 chunks) — this only proves the guard respects whichever
    count it's given, in the direction that would matter if it didn't."""
    clause_text = "reasonable " * 450  # exactly 450 words

    def word_count_stub(text: str) -> int:
        return len(text.split())

    def fake_subword_stub(text: str) -> int:
        # A synthetic stand-in for a real subword tokenizer's count on this
        # text, not a claim about how XLM-R actually tokenizes it.
        return round(word_count_stub(text) * 400 / 450)

    assert word_count_stub(clause_text) == 450  # over CHUNK_MAX_TOKENS
    assert fake_subword_stub(clause_text) == 400  # under CHUNK_MAX_TOKENS

    ensure_within_reranker_window(chunk_tokens=fake_subword_stub(clause_text), query_tokens=0)
    with pytest.raises(ValueError, match="chunk"):
        ensure_within_reranker_window(chunk_tokens=word_count_stub(clause_text), query_tokens=0)
