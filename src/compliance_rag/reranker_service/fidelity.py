"""Fixed query/document fixture for the reranker's fp32-vs-int8 fidelity
check (issue #14), plus the fp32 rank order recorded against it.

Shared by two very different contexts:

- `scripts/export_reranker_onnx.py` (local only, needs torch/optimum):
  recomputes the fp32 order for real every time it converts, and refuses to
  write an artifact if that order doesn't match `FP32_RANK_ORDER` below.
- `tests/test_reranker_onnx_fidelity.py` (runs in CI, plain `onnxruntime`
  only, same as the runtime service): scores the same pairs through the
  converted int8 artifact and checks its rank order against
  `FP32_RANK_ORDER` directly, without ever loading the fp32 PyTorch model —
  which is the point: proving quantization didn't change the ranking must
  not require installing torch/optimum or downloading 2.27GB of fp32
  weights on every CI run (that download is what was overrunning the
  runner's disk — BUILD_LOG.md 2026-09-28).

No official ONNX build of `BAAI/bge-reranker-v2-m3` exists, so the
conversion script converts the official weights directly rather than
trusting a third-party pre-quantized build (its own docstring). `MODEL_ID`
and `BAAI_REVISION` below are what "official weights" means precisely,
pinned so issue #32's benchmark is reproducible against the same weights
every time, not whatever `main` currently points to.
"""

from __future__ import annotations

MODEL_ID = "BAAI/bge-reranker-v2-m3"

BAAI_REVISION = "953dc6f"
"""The exact commit these weights are pulled from — record this alongside
the conversion's BUILD_LOG.md entry. Never `main`: issue #32's benchmark
isn't reproducible against a moving target."""

# A small, fixed, real query/document set — the same "real fixture, not a
# representative sample" standard this repo applies everywhere else
# (`CLAUDE.md`), just for ranking rather than parsing: these are the real
# APP 10/11 sentences from issue #9's spike fixture, not synthetic filler.
FIDELITY_QUERY = (
    "What steps must an entity take to protect personal information "
    "from misuse, interference and loss?"
)
FIDELITY_DOCUMENTS = (
    "An APP entity must take such steps (if any) as are reasonable in the "
    "circumstances to protect the information from misuse, interference and "
    "loss, and from unauthorised access, modification or disclosure.",
    "An APP entity must give the individual access to the personal "
    "information on request, subject to the exceptions in this Schedule.",
    "The weather in Canberra in autumn is mild, with occasional frost "
    "overnight and clear skies through most of the season.",
)
"""Two real, on-topic APP clauses and one deliberately irrelevant sentence —
the ranking a reranker exists to get right (the two real clauses ahead of
the irrelevant one) is exactly what quantization could plausibly disturb."""

FP32_RANK_ORDER = (0, 1, 2)
"""The fp32 PyTorch model's rank order over `FIDELITY_DOCUMENTS`, computed
by `scripts/export_reranker_onnx.py` at `BAAI_REVISION` (locally — the only
place torch runs) and checked in here as the fixed ground truth CI compares
the int8 artifact against. Update by hand, after checking *why* it moved,
if `BAAI_REVISION` or the fixture text ever changes; the export script
refuses to produce an artifact when its freshly computed fp32 order
disagrees with this constant, which is what forces that update to happen
deliberately rather than by CI quietly trusting a stale value."""


def rank_order(scores: list[float]) -> list[int]:
    """Indices sorted by score descending — what the reranker's caller
    actually consumes (`Reranker.rerank`'s `ScoredDocument` ordering), not
    the raw score values quantization is expected to perturb slightly."""
    return sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
