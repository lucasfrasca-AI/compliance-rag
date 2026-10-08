"""Real `gemini-embedding-001` vectors for the golden corpus and queries,
checked in so the recall gate (issue #28) runs on every build with no cloud
identity and no network call.

The vectors are what `VertexEmbedder` itself returned —
`scripts/refresh_golden_embeddings.py` is the only writer — so the gate
measures the production embedder, not a stand-in. A text with no cached vector
fails loudly: a silent fallback would let an edited query or a re-parsed clause
be measured against something other than its real embedding.

Stored as JSON, one base64 float32 row per text, keyed by the sha256 of the
text: standard library only, diffable metadata, about a megabyte.
"""

from __future__ import annotations

import base64
import hashlib
import json
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


class EmbeddingCacheMiss(LookupError):
    """A text the cache has no real vector for — refresh the cache."""


@dataclass(frozen=True, slots=True)
class EmbeddingCache:
    model: str
    dimensions: int
    vectors: dict[str, tuple[float, ...]]

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
        return cls(model=str(raw["model"]), dimensions=dimensions, vectors=vectors)

    def save(self, path: Path = GOLDEN_EMBEDDINGS_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = {
            key: base64.b64encode(array("f", vector).tobytes()).decode("ascii")
            for key, vector in sorted(self.vectors.items())
        }
        document = {"model": self.model, "dimensions": self.dimensions, "vectors": encoded}
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
