"""Real `gemini-embedding-001` vectors for the golden corpus and queries,
checked in so the recall gate (issue #28) runs on every build with no cloud
identity and no network call.

The vectors are what `VertexEmbedder` itself returned —
`scripts/refresh_golden_embeddings.py` is the only writer — so the gate
measures the production embedder, not a stand-in. A text with no cached vector
fails loudly: a silent fallback would let an edited query or a re-parsed clause
be measured against something other than its real embedding.

Stored as JSON, one base64 float32 row per text, keyed by the sha256 of the
text: standard library only, diffable metadata, one line per text. Exact
float32, because that is what Qdrant stores, so the gate measures the vectors
production searches. About 8.3 KB a text: some 6.8 MB for the gate's ~824
texts (#180), where the whole compilation replaced a 75-clause excerpt. A raw
float32 file would be a quarter smaller on disk but only ~8% smaller in git,
whose zlib packing recovers most of base64's overhead (the 137-text snapshot:
848 KB packed as JSON, 782 KB as raw floats), and it would lose the per-text
diff; float16 would halve it but would not be what Qdrant stores.

The file is a snapshot: it records the exact model string, task type, SDK
version and the date it was generated. If Google updates the model behind the
same name, committed and production vectors diverge while the gate stays
green — `scripts/check_embedding_drift.py` re-embeds a sample and reports
cosine similarity against the snapshot. It reports; it never gates.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from array import array
from dataclasses import dataclass
from pathlib import Path

from compliance_rag.adapters.interfaces import Embedder

GOLDEN_EMBEDDINGS_PATH = (
    Path(__file__).resolve().parents[3]
    / "corpus"
    / "golden"
    / "embeddings"
    / "gemini-embedding-001-1536.json"
)


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """How far a fresh vector is from a stored one: 1.0 is the same direction."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class EmbeddingCacheMiss(LookupError):
    """A text the cache has no real vector for — refresh the cache."""


@dataclass(frozen=True, slots=True)
class EmbeddingCache:
    model: str
    dimensions: int
    vectors: dict[str, tuple[float, ...]]
    generated_on: str
    """ISO date the vectors were produced — when the snapshot was taken."""
    task_type: str
    sdk: str
    """The client library and version that called the model."""

    @classmethod
    def load(cls, path: Path = GOLDEN_EMBEDDINGS_PATH) -> EmbeddingCache:
        raw = json.loads(path.read_text(encoding="utf-8"))
        dimensions = int(raw["dimensions"])
        vectors: dict[str, tuple[float, ...]] = {}
        for key, encoded in raw["vectors"].items():
            row = array("f")
            row.frombytes(base64.b64decode(encoded))
            if len(row) != dimensions:
                raise ValueError(f"{key}: {len(row)} dimensions, expected {dimensions}")
            vectors[key] = tuple(row)
        return cls(
            model=str(raw["model"]),
            dimensions=dimensions,
            vectors=vectors,
            generated_on=str(raw["generated_on"]),
            task_type=str(raw["task_type"]),
            sdk=str(raw["sdk"]),
        )

    def save(self, path: Path = GOLDEN_EMBEDDINGS_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = {
            key: base64.b64encode(array("f", vector).tobytes()).decode("ascii")
            for key, vector in sorted(self.vectors.items())
        }
        document = {
            "model": self.model,
            "dimensions": self.dimensions,
            "task_type": self.task_type,
            "sdk": self.sdk,
            "generated_on": self.generated_on,
            "vectors": encoded,
        }
        path.write_text(json.dumps(document, indent=0) + "\n", encoding="utf-8")

    def missing(self, texts: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(t for t in texts if text_key(t) not in self.vectors)


class CachedEmbedder(Embedder):
    """Serves checked-in real vectors; refuses any text it doesn't hold."""

    def __init__(self, cache: EmbeddingCache) -> None:
        self._cache = cache

    @property
    def dimensions(self) -> int:
        return self._cache.dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        missing = self._cache.missing(texts)
        if missing:
            raise EmbeddingCacheMiss(
                f"{len(missing)} text(s) have no cached {self._cache.model} vector — run "
                f"scripts/refresh_golden_embeddings.py. First: {missing[0][:80]!r}"
            )
        return tuple(self._cache.vectors[text_key(t)] for t in texts)
