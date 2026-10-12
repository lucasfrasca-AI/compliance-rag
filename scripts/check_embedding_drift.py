"""Has `gemini-embedding-001` drifted from the committed golden snapshot?
(issue #28). Re-embeds a deterministic sample of golden texts with the real
`VertexEmbedder` and reports each one's cosine similarity to its stored vector.

Reports, never gates: exit status is 0 whatever the numbers say. A model
updated behind the same name would leave the recall gate green on stale
vectors while production retrieval moved; this is how that gets noticed. Run
locally with Application Default Credentials (CI holds no Vertex identity),
e.g. before relying on the gate's numbers for a decision, or monthly:

    uv run python scripts/check_embedding_drift.py [--sample 10]

Near 1.0 (float32 storage and server-side nondeterminism leave ~1e-6) means
no drift. Clearly below ~0.999 means the model changed: refresh the snapshot
(`scripts/refresh_golden_embeddings.py`), re-run the gate, and record both in
BUILD_LOG. Cost: the sample's tokens at $0.15/1M — fractions of a cent.
"""

from __future__ import annotations

import argparse
import asyncio
import math

from compliance_rag.adapters.vertex_embedder import VertexEmbedder
from compliance_rag.eval.embedding_cache import EmbeddingCache, text_key
from compliance_rag.eval.golden_corpus import golden_texts
from compliance_rag.settings import Settings


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def main(sample: int) -> None:
    cache = EmbeddingCache.load()
    texts = golden_texts()
    step = max(1, len(texts) // sample)
    chosen = texts[::step][:sample]
    fresh = await VertexEmbedder.from_settings(Settings()).embed(chosen)
    scores = [cosine(cache.vectors[text_key(t)], v) for t, v in zip(chosen, fresh, strict=True)]
    print(f"snapshot: {cache.model} ({cache.task_type}, {cache.sdk})")
    print(f"generated {cache.generated_on}")
    for text, score in zip(chosen, scores, strict=True):
        print(f"  {score:.6f}  {text[:70]!r}")
    print(f"min {min(scores):.6f}  mean {sum(scores) / len(scores):.6f}  over {len(scores)} texts")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=10)
    asyncio.run(main(parser.parse_args().sample))
