"""Real subword token counting for the reranker's own tokenizer (issue #21).

`chunker.py`'s `CHUNK_MAX_TOKENS` is derived from `Settings.reranker_max_tokens`
on the *assumption* that some real tokenizer's count of a real clause agrees
with whatever counter chunked it. Issues #6 and #18 prove the guard and the
1:1 mapping with stub counters — deliberately, since that's what proves the
mechanism works, not that any particular number holds. This module is the
other half: the actual `BAAI/bge-reranker-v2-m3` tokenizer, vendored at
`vendor/bge-reranker-v2-m3-tokenizer/` (see that directory's `PROVENANCE.md`),
loaded from a local file with no network code path at all — which is why a
CI regression gate can depend on it without depending on a network fetch
nobody controls.

`sentencepiece.SentencePieceProcessor.EncodeAsIds` gives the exact piece
segmentation the real model's tokenizer produces for a string; counting its
length is the content-token count `chunker.ensure_within_reranker_window`
expects (`chunk_tokens`, `query_tokens`). It adds no special tokens itself —
`chunker.RESERVED_FOR_QUERY_AND_SPECIALS` already reserves headroom for
those, so this function's job is exactly and only real subword counting.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import sentencepiece as spm

_TOKENIZER_MODEL = (
    Path(__file__).resolve().parents[3]
    / "vendor"
    / "bge-reranker-v2-m3-tokenizer"
    / "sentencepiece.bpe.model"
)


@lru_cache(maxsize=1)
def _processor() -> spm.SentencePieceProcessor:
    processor = spm.SentencePieceProcessor()
    processor.Load(str(_TOKENIZER_MODEL))
    return processor


def count_reranker_tokens(text: str) -> int:
    """The number of subword pieces the real reranker tokenizer produces
    for `text` — no special tokens added, no truncation, no padding."""
    return len(_processor().EncodeAsIds(text))
