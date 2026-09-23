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
