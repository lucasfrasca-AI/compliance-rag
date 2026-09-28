"""Fetch the converted int8 ONNX reranker artifact from the corpus bucket
(issue #14), for CI's fidelity re-check and service tests. Verifies each
downloaded file's sha256 against the uploaded `manifest.json` — a bad
download must fail loudly, not silently feed a corrupt model into the
fidelity test.

    uv run python scripts/fetch_reranker_artifact.py

Requires read access to `gs://<corpus bucket>/reranker/...` — the
`compliance-rag-ci` service account is deliberately scoped down to
`logging.*` only (SERVICES.md: Workload Identity Federation row), so this
needs its own narrow `storage.objectViewer` grant, conditioned to the
`reranker/` prefix, before it can run in CI.
"""

from __future__ import annotations

import hashlib
import json
import sys
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

ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "models" / "reranker-onnx"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    # google-cloud-storage ships no inline stubs; see artifact_storage.BlobLike.
    bucket = cast(
        "BucketLike",
        storage.Client().bucket(BUCKET_NAME),  # pyright: ignore[reportUnknownMemberType]
    )

    print(f"Reading gs://{BUCKET_NAME}/{MANIFEST_OBJECT}")
    manifest_text = bucket.blob(MANIFEST_OBJECT).download_as_text()
    manifest: dict[str, object] = json.loads(manifest_text)
    # manifest.json's shape is upload_reranker_artifact.py's own contract,
    # not schema-validated at this boundary — cast narrows json.loads'
    # necessarily-Any-ish return to what this script actually reads off it.
    expected_hashes = cast("dict[str, str]", manifest["files"])

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    mismatches: list[str] = []
    for name in ARTIFACT_FILES:
        object_name = f"{OBJECT_PREFIX}/{name}"
        dest = ARTIFACT_DIR / name
        print(f"Downloading gs://{BUCKET_NAME}/{object_name} -> {dest}")
        bucket.blob(object_name).download_to_filename(str(dest))
        actual = sha256_of(dest)
        expected = expected_hashes.get(name)
        if actual != expected:
            mismatches.append(f"{name}: expected {expected}, got {actual}")

    if mismatches:
        print("sha256 MISMATCH — refusing a possibly-corrupt artifact:", file=sys.stderr)
        for line in mismatches:
            print(f"  {line}", file=sys.stderr)
        sys.exit(1)

    print(f"Verified {len(ARTIFACT_FILES)} files against manifest.json.")


if __name__ == "__main__":
    main()
