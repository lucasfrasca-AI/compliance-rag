"""Embed the golden corpus and golden queries with the real `VertexEmbedder`
and write `corpus/golden/embeddings/gemini-embedding-001-1536.json` (issue #28).

Run locally with Application Default Credentials, never in CI: the recall gate
reads the checked-in file and needs no cloud identity. Every text is
re-embedded on every run, so the file is one snapshot with one
`generated_on` date, never a mix of vectors from different days. Cost:
gemini-embedding-001 input at $0.15/1M tokens — the golden set is ~4.5k
tokens, under a tenth of a cent.

    uv run python scripts/refresh_golden_embeddings.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
from importlib.metadata import version

from compliance_rag.adapters.vertex_embedder import TASK_TYPE, VertexEmbedder
from compliance_rag.eval.embedding_cache import GOLDEN_EMBEDDINGS_PATH, EmbeddingCache, text_key
from compliance_rag.eval.golden_corpus import golden_texts
from compliance_rag.settings import Settings


async def main() -> None:
    settings = Settings()
    embedder = VertexEmbedder.from_settings(settings)
    texts = golden_texts()
    vectors = await embedder.embed(texts)
    EmbeddingCache(
        model=settings.vertex_embedding_model,
        dimensions=embedder.dimensions,
        vectors={text_key(t): v for t, v in zip(texts, vectors, strict=True)},
        generated_on=dt.date.today().isoformat(),
        task_type=TASK_TYPE,
        sdk=f"google-cloud-aiplatform {version('google-cloud-aiplatform')}",
    ).save()
    print(f"{len(texts)} texts embedded -> {GOLDEN_EMBEDDINGS_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
