"""Embed the golden corpus and golden queries with the real `VertexEmbedder`
and write `corpus/golden/embeddings/gemini-embedding-001-1536.json` (issue #28).

Run locally with Application Default Credentials, never in CI: the recall gate
reads the checked-in file and needs no cloud identity. Vectors already cached
are kept, so a run embeds only new or changed text. Cost: gemini-embedding-001
input tokens at $0.15/1M — the whole golden set is a few thousand tokens.

    uv run python scripts/refresh_golden_embeddings.py
"""

from __future__ import annotations

import asyncio

from compliance_rag.adapters.vertex_embedder import VertexEmbedder
from compliance_rag.eval.embedding_cache import (
    GOLDEN_EMBEDDINGS_PATH,
    EmbeddingCache,
    text_key,
)
from compliance_rag.eval.golden import load_golden_set
from compliance_rag.eval.golden_corpus import golden_chunks
from compliance_rag.settings import Settings


def golden_texts() -> tuple[str, ...]:
    texts = [c.text for c in golden_chunks()] + [q.query for q in load_golden_set().queries]
    return tuple(dict.fromkeys(texts))


async def main() -> None:
    settings = Settings()
    embedder = VertexEmbedder.from_settings(settings)
    try:
        cache = EmbeddingCache.load()
    except FileNotFoundError:
        cache = EmbeddingCache(settings.vertex_embedding_model, embedder.dimensions, {})
    if (cache.model, cache.dimensions) != (settings.vertex_embedding_model, embedder.dimensions):
        raise SystemExit(f"cache is {cache.model}/{cache.dimensions}; settings differ")

    texts = golden_texts()
    wanted = {text_key(t) for t in texts}
    missing = cache.missing(texts)
    vectors = await embedder.embed(missing) if missing else ()
    kept = {k: v for k, v in cache.vectors.items() if k in wanted}
    kept |= {text_key(t): v for t, v in zip(missing, vectors, strict=True)}
    EmbeddingCache(cache.model, cache.dimensions, kept).save()
    dropped = len(cache.vectors) - (len(kept) - len(missing))
    print(
        f"{len(texts)} texts: embedded {len(missing)}, kept {len(kept) - len(missing)}, "
        f"dropped {dropped} stale -> {GOLDEN_EMBEDDINGS_PATH}"
    )


if __name__ == "__main__":
    asyncio.run(main())
