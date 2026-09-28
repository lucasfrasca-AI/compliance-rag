"""Where the converted int8 ONNX reranker artifact lives in the corpus
bucket (issue #14) — shared by `scripts/upload_reranker_artifact.py` (run
locally, once, after conversion) and `scripts/fetch_reranker_artifact.py`
(run in CI, before the fidelity re-check). See `fidelity.py` for the
fp32/int8 fixture and the recorded fp32 rank order itself.

Versioned by revision in the object path, not overwritten in place: a stale
artifact is never silently replaced, and `manifest.json` at the same prefix
is what a fetcher verifies its download against (`sha256` per file) —
"hashed and version-locked" applied to a model artifact the same way
`PHASES.md` already applies it to clause text.
"""

from __future__ import annotations

from typing import Protocol

from compliance_rag.reranker_service.fidelity import BAAI_REVISION

BUCKET_NAME = "compliance-rag-508904-corpus"
ARTIFACT_FILES = ("model.onnx", "model.onnx.data", "config.json", "REVISION")
OBJECT_PREFIX = f"reranker/bge-reranker-v2-m3-int8/{BAAI_REVISION}"
MANIFEST_OBJECT = f"{OBJECT_PREFIX}/manifest.json"


class BlobLike(Protocol):
    """Only the `google.cloud.storage.Blob` methods the two artifact
    scripts actually call. Same reasoning as `adapters/gcs_clause_store.py`
    (which this mirrors, deliberately not importing from — that module is
    the application's only object-storage seam, this is one-off tooling):
    `google-cloud-storage` ships no inline type stubs, so typing against
    the real `Blob` propagates `Unknown` through every call site under
    pyright strict."""

    def upload_from_filename(self, filename: str) -> None: ...
    def upload_from_string(self, data: str, content_type: str | None = None) -> None: ...
    def download_as_text(self) -> str: ...
    def download_to_filename(self, filename: str) -> None: ...


class BucketLike(Protocol):
    def blob(self, name: str, /) -> BlobLike: ...
