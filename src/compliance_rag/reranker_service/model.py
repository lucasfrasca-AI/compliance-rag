"""Loads the quantized int8 ONNX reranker and scores query/document pairs
(issue #14). Plain `onnxruntime.InferenceSession` — no `torch`, no
`optimum` in this module or anywhere else the runtime image installs.
`transformers.AutoTokenizer` is used for tokenization only (no model
backend loaded, no network — reads the same vendored files issue #21
already committed at `vendor/bge-reranker-v2-m3-tokenizer/`), which is a
correctness choice over hand-rolling XLM-R's sentence-pair scheme
(`<s> query </s></s> passage </s>`, padding, attention masks) ourselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import onnxruntime as ort
from transformers import AutoTokenizer

from compliance_rag.corpus.chunker import RERANKER_MAX_TOKENS

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TOKENIZER_DIR = _REPO_ROOT / "vendor" / "bge-reranker-v2-m3-tokenizer"
DEFAULT_ONNX_MODEL_PATH = _REPO_ROOT / "models" / "reranker-onnx" / "model.onnx"


class RerankerModel:
    """Wraps the ONNX session and its tokenizer. One instance per process —
    loading either is not free, so the FastAPI app builds exactly one."""

    def __init__(
        self,
        onnx_path: Path = DEFAULT_ONNX_MODEL_PATH,
        tokenizer_dir: Path = DEFAULT_TOKENIZER_DIR,
    ) -> None:
        if not onnx_path.exists():
            raise FileNotFoundError(
                f"no ONNX model at {onnx_path} — run "
                "`uv sync --group export && uv run python scripts/export_reranker_onnx.py` first"
            )
        # transformers and onnxruntime ship weak/partial type stubs; Any
        # here stops that Unknown-ness cascading through every call site
        # below rather than fighting each one individually.
        self._tokenizer: Any = AutoTokenizer.from_pretrained(  # pyright: ignore[reportUnknownMemberType]
            str(tokenizer_dir)
        )
        # Explicit thread pools rather than onnxruntime's own default sizing
        # (from the visible core count): Cloud Run's `reranker` service is a
        # 2-vCPU container serving up to 4 concurrent requests (issue #23),
        # and a default-sized session's thread pools can oversubscribe and
        # thrash under that combination rather than genuinely parallelising.
        session_options: Any = ort.SessionOptions()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        session_options.intra_op_num_threads = 2
        session_options.inter_op_num_threads = 1
        self._session: Any = ort.InferenceSession(
            str(onnx_path), sess_options=session_options, providers=["CPUExecutionProvider"]
        )
        self._input_names: set[str] = {i.name for i in self._session.get_inputs()}

    def token_count(self, query: str, document: str) -> int:
        """Tokens `query` + `document` would occupy with no truncation —
        used to reject an over-long request before scoring it, rather than
        letting the model silently truncate (issue #14 acceptance
        criterion)."""
        encoded = self._tokenizer(query, document, truncation=False)
        return len(encoded["input_ids"])

    def score(self, query: str, documents: tuple[str, ...]) -> list[float]:
        """One logit per document, in the same order as `documents`. Caller
        is responsible for the token-budget check (`token_count`) —
        `truncation=True` here is what the model actually does with an
        over-long pair, which should never be reached because the caller
        already rejected it."""
        pairs = [[query, document] for document in documents]
        encoded = self._tokenizer(
            pairs,
            padding=True,
            truncation=True,
            max_length=RERANKER_MAX_TOKENS,
            return_tensors="np",
        )
        onnx_inputs = {name: value for name, value in encoded.items() if name in self._input_names}
        (logits,) = self._session.run(None, onnx_inputs)
        return [float(x) for x in logits.reshape(-1)]
