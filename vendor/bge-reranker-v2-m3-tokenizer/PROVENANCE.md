# `BAAI/bge-reranker-v2-m3` tokenizer — vendored, not fetched (issue #21)

Only the tokenizer, never the 2.27 GB model weights: issue #21 needs real
subword token *counts*, not the reranker's scores, and `sentencepiece` loads
`sentencepiece.bpe.model` directly with no network code path at all — a
stronger determinism guarantee than a warm cache, which can still attempt a
revalidation call. A CI regression gate that must not depend on "a network
fetch nobody controls" (issue #21's own acceptance criterion) gets that
guarantee by construction, not by hoping a cache hits.

## Source

- Hugging Face repo: <https://huggingface.co/BAAI/bge-reranker-v2-m3>
- Pinned revision: `953dc6f` (the repo's current commit as of 2026-09-25;
  fetched via `resolve/953dc6f/...` rather than `resolve/main/...`, so a
  later push to the model repo can never silently change these bytes)
- License: Apache-2.0
- Fetched: 2026-09-25

## Files and their sha256

```
cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865  sentencepiece.bpe.model
7e4c1cc848840aeccdd763458c18dd525eb0f795c992e00ebe9c28554e7db2d4  tokenizer_config.json
8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835  special_tokens_map.json
```

`tokenizer.json` (the Rust `tokenizers`-library format, 17.1 MB) is
deliberately **not** vendored — it's a precomputed duplicate of the same
vocabulary in `sentencepiece.bpe.model` for a different loading path
(`transformers.AutoTokenizer`'s fast tokenizer). Segmentation — the piece
count that matters for a token *budget* — comes entirely from the
SentencePiece model itself, so loading it directly via the `sentencepiece`
package (`src/compliance_rag/corpus/reranker_tokenizer.py`) needs nothing
else, and skipping it saves 17 MB of git history for a file this repo would
never read.

## Re-verifying or updating

```
curl -sSL -o sentencepiece.bpe.model \
  https://huggingface.co/BAAI/bge-reranker-v2-m3/resolve/953dc6f/sentencepiece.bpe.model
shasum -a 256 sentencepiece.bpe.model   # compare against the hash above
```

If `Settings.reranker_model` ever changes (D-15's ColBERT A/B, or a future
reranker swap), these files are replaced wholesale and this file's revision,
hashes and fetch date are updated in the same commit — never edited in
place without updating the hashes, the same discipline `corpus/`'s own
`canonical_sha256` exists to enforce.
