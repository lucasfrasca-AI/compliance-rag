"""Embed every text the recall gates and the golden-corpus tests embed, with
the real `VertexEmbedder`, and write
`corpus/golden/embeddings/gemini-embedding-001-1536.json` (issues #28, #180).

What it embeds is `embedded_texts()`: each chunk of the gate corpus (the whole
compilation's 779 selected clauses and the six Acme policies), each chunk of
the golden excerpt (a subset, so nothing extra in practice), and each golden
query, once.

Run locally with Application Default Credentials, never in CI: the recall gate
reads the checked-in file and needs no cloud identity. Every text is
re-embedded on every run, so the file is one snapshot with one
`generated_on` date, never a mix of vectors from different days; the run
reports how far each text the previous snapshot already held has moved
(1.000000 when the model hasn't). One request per text (`VertexEmbedder`).
Cost: gemini-embedding-001 input at $0.15/1M tokens — ~15k tokens for the
gate corpus, under a cent.

    uv run python -m scripts.refresh_golden_embeddings --dry-run   # counts only, no cloud call
    uv run python -m scripts.refresh_golden_embeddings [--env-file path/to/.env]

`--env-file` is for a checkout with no `.env` of its own, such as a git
worktree: `Settings` reads `.env` from the working directory.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from importlib.metadata import version
from pathlib import Path

from compliance_rag.eval.embedding_cache import (
    GOLDEN_EMBEDDINGS_PATH,
    EmbeddingCache,
    cosine,
    text_key,
)
from compliance_rag.eval.golden_corpus import embedded_texts


def _plan(texts: tuple[str, ...], previous: EmbeddingCache) -> tuple[str, ...]:
    """Print what a run embeds, against the snapshot it replaces; return the
    texts the snapshot already holds."""
    held = tuple(t for t in texts if text_key(t) in previous.vectors)
    dropped = len(previous.vectors.keys() - {text_key(t) for t in texts})
    print(
        f"{len(texts)} texts to embed ({sum(len(t) for t in texts):,} characters): "
        f"{len(held)} already in the {previous.generated_on} snapshot, "
        f"{len(texts) - len(held)} new; {dropped} snapshot vector(s) no longer needed"
    )
    return held


async def main(*, dry_run: bool, env_file: Path | None) -> None:
    texts = embedded_texts()
    previous = EmbeddingCache.load()
    held = _plan(texts, previous)
    if dry_run:
        print("dry run: nothing embedded, nothing written")
        return

    # Imported here so a dry run needs no settings and no cloud client.
    from compliance_rag.adapters.vertex_embedder import TASK_TYPE, VertexEmbedder
    from compliance_rag.settings import Settings

    settings = (
        Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
        if env_file is not None
        else Settings()
    )
    embedder = VertexEmbedder.from_settings(settings)
    vectors = await embedder.embed(texts)
    fresh = EmbeddingCache(
        model=settings.vertex_embedding_model,
        dimensions=embedder.dimensions,
        vectors={text_key(t): v for t, v in zip(texts, vectors, strict=True)},
        generated_on=dt.date.today().isoformat(),
        task_type=TASK_TYPE,
        sdk=f"google-genai {version('google-genai')}",
    )
    fresh.save()
    print(
        f"{len(texts)} texts embedded -> {GOLDEN_EMBEDDINGS_PATH}; "
        f"{embedder.tokens_embedded} tokens, ${embedder.cost_usd:.4f}"
    )
    if held:
        moved = [cosine(previous.vectors[text_key(t)], fresh.vectors[text_key(t)]) for t in held]
        print(
            f"the {len(held)} texts the previous snapshot held: cosine to their old vectors "
            f"min {min(moved):.6f}, mean {sum(moved) / len(moved):.6f}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be embedded; no cloud call"
    )
    parser.add_argument(
        "--env-file", type=Path, default=None, help="the .env to read, if not ./.env"
    )
    arguments = parser.parse_args()
    asyncio.run(main(dry_run=arguments.dry_run, env_file=arguments.env_file))
