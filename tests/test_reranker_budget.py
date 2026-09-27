"""The real-tokenizer budget test (issue #21): proves the 420-token chunk
budget holds against real legislative text, tokenized by the real reranker
tokenizer — not merely wired to some counter (`tests/test_chunk_token_cap.py`)
and not merely structurally sound (`tests/test_chunker.py`).

Real fixture bytes throughout: `docs/research/fixtures/privacy-act-1988-
C2026C00227.excerpt.html` (issue #9's spike). This excerpt carries Schedule
1's APP 10 and APP 11 in full — not the complete thirteen Australian Privacy
Principles, since no full compilation has been fetched into this repo yet
(that's a real gap, not a simplification: the excerpt is genuinely all the
real Schedule 1 bytes this repo currently has, and this test exercises every
clause in it, per `CLAUDE.md`'s "every available real fixture" standard.
Re-run this test once a full compilation lands to cover the rest of
Schedule 1).

The tokenizer itself is the real `BAAI/bge-reranker-v2-m3` SentencePiece
model, vendored at `vendor/bge-reranker-v2-m3-tokenizer/` and loaded with no
network call (`corpus/reranker_tokenizer.py`) — a regression gate that
depended on a live download or an Actions cache that might miss would not
be the deterministic gate issue #21 asks for.
"""

from __future__ import annotations

from pathlib import Path

from compliance_rag.corpus.chunker import assert_chunks_fit_reranker_window, chunk_clauses
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.selection_manifest import SelectionManifest

FIXTURE = (
    Path(__file__).parent.parent
    / "docs"
    / "research"
    / "fixtures"
    / "privacy-act-1988-C2026C00227.excerpt.html"
)
INSTRUMENT_ID = "privacy-act-1988"
VERSION_ID = "C2026C00227"

# A plausible Track B query (`CONTEXT.md`) against APP 11's own subject
# matter — representative of what actually accompanies a chunk at rerank
# time, not an arbitrary string chosen to be short.
_REPRESENTATIVE_QUERY = (
    "What steps must an entity take to protect personal information "
    "from misuse, interference and loss?"
)


def _schedule_1_chunks():
    html = FIXTURE.read_text(encoding="utf-8")
    parsed = parse_compilation(html, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID)
    selection = SelectionManifest(
        author="Piero Frasca", reviewed_by="Piero Frasca", date="2026-09-25", prefixes=("sch1/",)
    )
    in_scope = selection.select(parsed.clauses)
    return chunk_clauses(in_scope, count_tokens=count_reranker_tokens)


def test_real_schedule_1_fixture_produces_more_than_one_chunk() -> None:
    """A vacuous pass (zero chunks) would prove nothing — guard against the
    fixture or the prefix silently matching nothing."""
    assert len(_schedule_1_chunks()) > 1


def test_every_schedule_1_chunk_fits_the_real_reranker_window_against_a_representative_query() -> (
    None
):
    chunks = _schedule_1_chunks()
    query_tokens = count_reranker_tokens(_REPRESENTATIVE_QUERY)
    assert_chunks_fit_reranker_window(
        chunks, query_tokens=query_tokens, count_tokens=count_reranker_tokens
    )


def test_the_real_tokenizer_actually_counts_more_tokens_than_words_here() -> None:
    """A measured finding, not the assumption this test started with: `BAAI/
    bge-reranker-v2-m3`'s tokenizer is XLM-R based, with one subword
    vocabulary shared across roughly 100 languages, so it fragments plain
    English legal prose into *more* pieces than a monolingual English
    tokenizer would — the opposite of "subword tokenizers usually
    undercount plain English." APP11/1's own sentence is 43 words but 60
    real reranker tokens. Comfortably inside `CHUNK_MAX_TOKENS` either way
    for this fixture, but recording the actual, measured direction is the
    whole reason #21 exists — a word-count stub would have hidden this."""
    chunks = _schedule_1_chunks()
    longest = max(chunks, key=lambda c: count_reranker_tokens(c.text))
    real_tokens = count_reranker_tokens(longest.text)
    word_count = len(longest.text.split())
    assert real_tokens > word_count
    assert real_tokens < 420
