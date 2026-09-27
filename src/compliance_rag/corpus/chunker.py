"""The chunk token budget, derived from the reranker's own window (issue #6,
`CONTEXT.md`: Chunk).

`gemini-embedding-001` accepts roughly 2048 tokens, so the embedder never
binds the chunk size. `BAAI/bge-reranker-v2-m3` is XLM-R based and scores
query and passage together inside one 512-token window; overflow truncates
the tail of the passage silently, and in legislative prose the tail is
usually where the operative requirement sits. `CHUNK_MAX_TOKENS` is derived
from `RERANKER_MAX_TOKENS` rather than an independent literal, so the
relationship — chunk + query + the model's special tokens must fit inside
the reranker's window — stays true by construction rather than by two
numbers happening to agree today.

D-15 and `SERVICES.md` allow ColBERT rescoring to replace the cross-encoder;
`colbertv2.0` has a different tokenizer and window. If the reranker model
changes, this budget changes with it, and the corpus needs re-chunking and
re-embedding — a cost the reranker benchmark write-up should record
alongside recall and latency when that comparison is made (not yet built;
tracked separately from this issue).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from compliance_rag.corpus.models import Chunk, ClauseRecord
from compliance_rag.settings import Settings

RERANKER_MAX_TOKENS: int = Settings.model_fields["reranker_max_tokens"].default
"""`BAAI/bge-reranker-v2-m3`'s window, single-sourced from
`Settings.reranker_max_tokens`'s declared default (`settings.py`: "fixed by
.env.example") — reading a Pydantic field's default touches no environment
variable and does no I/O, so this stays a plain module-level constant, not
a second literal a test merely pins to the first."""

RESERVED_FOR_QUERY_AND_SPECIALS = 92
"""Headroom inside `RERANKER_MAX_TOKENS` for the query text plus the
model's own special tokens (`[CLS]`, `[SEP]`), so a chunk at the cap still
leaves room for whatever query accompanies it at rerank time."""

CHUNK_MAX_TOKENS = RERANKER_MAX_TOKENS - RESERVED_FOR_QUERY_AND_SPECIALS
"""A retrieval unit's token budget, counted by the *reranker's* tokenizer,
never a word count or character proxy (`CONTEXT.md`: Chunk). 420, pinned —
a future change to either input above is a deliberate edit here too."""


def ensure_within_reranker_window(*, chunk_tokens: int, query_tokens: int) -> None:
    """Fail loudly rather than let the cross-encoder silently truncate.

    Two distinct failure modes, both real: a chunk over `CHUNK_MAX_TOKENS`
    on its own, and a chunk within budget that still overflows once a real
    query joins it at rerank time. `chunk_tokens` is an already-counted
    number — this function takes no text and applies no counter itself, so
    whichever tokenizer produced the count is the one the guard enforces
    (issue #18's chunker is what actually counts real clause text).
    """
    if chunk_tokens > CHUNK_MAX_TOKENS:
        raise ValueError(
            f"chunk is {chunk_tokens} tokens, over the {CHUNK_MAX_TOKENS}-token budget "
            "derived from the reranker window"
        )
    if chunk_tokens + query_tokens > RERANKER_MAX_TOKENS:
        raise ValueError(
            f"chunk ({chunk_tokens} tokens) plus query ({query_tokens} tokens) would exceed "
            f"the reranker's {RERANKER_MAX_TOKENS}-token window"
        )


def chunk_clause(clause: ClauseRecord, *, count_tokens: Callable[[str], int]) -> Chunk:
    """One clause becomes one chunk (issue #18).

    Phase 1's parsers (#17, #20) already emit clauses at the smallest
    citable unit — a subsection, a paragraph, a policy's numbered item —
    so there is no sub-clause splitting to do: a clause whose own text is
    over budget is a parser-side problem to fix upstream, not something
    this function truncates or silently divides. `count_tokens` is a
    parameter, never a model this function loads itself, so calling it
    needs no tokenizer download; the real XLM-R tokenizer over real
    Schedule 1 chunks is issue #21.

    Raises with the clause's own path named, per `ensure_within_reranker_window`
    (issue #6) — never emits a chunk over budget.
    """
    text = clause.canonical_text
    token_count = count_tokens(text)
    try:
        ensure_within_reranker_window(chunk_tokens=token_count, query_tokens=0)
    except ValueError as exc:
        raise ValueError(f"clause {clause.clause_path!r}: {exc}") from exc
    return Chunk(
        # A consequence of the 1:1 clause-to-chunk mapping above, not an
        # assumed identity: reusing the clause's own stable id needs no
        # second id scheme precisely because a chunk can never span more
        # than one clause. If a later change ever lets one clause become
        # several chunks, this equality stops holding and must be revisited.
        chunk_id=clause.clause_id,
        clause_id=clause.clause_id,
        text=text,
        scope=clause.scope,
        tenant_id=clause.tenant_id,
    )


def chunk_clauses(
    clauses: Iterable[ClauseRecord], *, count_tokens: Callable[[str], int]
) -> tuple[Chunk, ...]:
    """`chunk_clause()` over a collection — clause records become chunks."""
    return tuple(chunk_clause(clause, count_tokens=count_tokens) for clause in clauses)


def assert_chunks_fit_reranker_window(
    chunks: Iterable[Chunk], *, query_tokens: int, count_tokens: Callable[[str], int]
) -> None:
    """Every chunk fits the reranker's window against a representative
    query (issue #21) — checked longest-first, by `count_tokens`'s own
    count, so the worst case is what actually fails first rather than
    whichever chunk happened to come first in iteration order.

    `count_tokens` is a parameter here too, same reasoning as `chunk_clause`:
    a stub proves the mechanism (`tests/test_chunk_token_cap.py`), the real
    `BAAI/bge-reranker-v2-m3` tokenizer proves the assumption
    (`corpus/reranker_tokenizer.py`, issue #21) — this function doesn't care
    which.

    On failure, names the offending chunk's `chunk_id` — issue #18's 1:1
    clause-to-chunk mapping means this *is* the clause's own stable id, not
    a separate lookup the caller has to do to find out which clause broke.
    """
    by_length_desc = sorted(chunks, key=lambda c: count_tokens(c.text), reverse=True)
    for chunk in by_length_desc:
        try:
            ensure_within_reranker_window(
                chunk_tokens=count_tokens(chunk.text), query_tokens=query_tokens
            )
        except ValueError as exc:
            raise ValueError(f"chunk {chunk.chunk_id!r}: {exc}") from exc
