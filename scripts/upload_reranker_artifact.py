"""Upload the converted int8 ONNX reranker artifact to the corpus bucket
(issue #14). Run once after `export_reranker_onnx.py`, whenever
`fidelity.BAAI_REVISION` changes:

    uv run python scripts/upload_reranker_artifact.py

This is what lets CI verify the artifact's fp32-vs-int8 fidelity
(`tests/test_reranker_onnx_fidelity.py`) without installing torch/optimum
or downloading 2.27GB of fp32 weights on every run — CI fetches the
already-converted, already-verified int8 artifact from here instead
(`.github/workflows/ci.yml`). Object layout, versioned by revision so a
stale artifact is never overwritten silently:

    reranker/bge-reranker-v2-m3-int8/{BAAI_REVISION}/model.onnx
    reranker/bge-reranker-v2-m3-int8/{BAAI_REVISION}/model.onnx.data
    reranker/bge-reranker-v2-m3-int8/{BAAI_REVISION}/config.json
    reranker/bge-reranker-v2-m3-int8/{BAAI_REVISION}/REVISION
    reranker/bge-reranker-v2-m3-int8/{BAAI_REVISION}/manifest.json

`manifest.json` (`{"model_id", "revision", "files": {name: sha256}}`) is
what a fetcher checks its download against — the same "hashed and
version-locked" standard `corpus/mappings/manifest.json` already applies to
clause text (`PHASES.md`), just for a model artifact instead of a clause.

Separate module from `adapters/gcs_clause_store.py` on purpose: that
adapter is the only place the *application* reads and writes object
storage (`CLAUDE.md`: "No Cypher, Qdrant, Vertex or SQL calls outside
adapters/"). This is one-off build tooling, like `export_reranker_onnx.py`
itself, not a runtime code path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from google.cloud import storage

from compliance_rag.reranker_service.artifact_storage import (
    ARTIFACT_FILES,
    BUCKET_NAME,
    MANIFEST_OBJECT,
    OBJECT_PREFIX,
    BucketLike,
)
from compliance_rag.reranker_service.fidelity import BAAI_REVISION, MODEL_ID

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "models" / "reranker-onnx"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    missing = [name for name in ARTIFACT_FILES if not (ARTIFACT_DIR / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"missing {missing} under {ARTIFACT_DIR} — run "
            "`uv run python scripts/export_reranker_onnx.py` first"
        )

    # google-cloud-storage ships no inline stubs; see artifact_storage.BlobLike.
    bucket = cast(
        "BucketLike",
        storage.Client().bucket(BUCKET_NAME),  # pyright: ignore[reportUnknownMemberType]
    )
    hashes: dict[str, str] = {}
    for name in ARTIFACT_FILES:
        path = ARTIFACT_DIR / name
        digest = sha256_of(path)
        hashes[name] = digest
        object_name = f"{OBJECT_PREFIX}/{name}"
        print(f"Uploading {path} ({path.stat().st_size:,} bytes, sha256 {digest}) -> {object_name}")
        bucket.blob(object_name).upload_from_filename(str(path))

    manifest = {"model_id": MODEL_ID, "revision": BAAI_REVISION, "files": hashes}
    print(f"Writing {MANIFEST_OBJECT}")
    bucket.blob(MANIFEST_OBJECT).upload_from_string(
        json.dumps(manifest, indent=2, sort_keys=True), content_type="application/json"
    )

    print(f"\nDone. gs://{BUCKET_NAME}/{OBJECT_PREFIX}/")
    for name, digest in hashes.items():
        print(f"  {name}: {digest}")


if __name__ == "__main__":
    main()
