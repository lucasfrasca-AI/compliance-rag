"""Export a *candidate* cross-encoder to int8 ONNX for the reranker benchmark
(issue #32) — the same recipe as the production export
(`scripts/export_reranker_onnx.py`: optimum ONNX export, then
`quantize_dynamic` to QInt8), so a candidate is compared like-for-like.

Writes `models/candidates/<name>/` (gitignored): `model.onnx`, `config.json`,
the model's own tokenizer, and `REVISION` — the exact Hugging Face commit
resolved at export time. Not a production artefact; nothing deploys it.

    uv run --group export python scripts/export_candidate_reranker.py BAAI/bge-reranker-base
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

from huggingface_hub import HfApi
from onnxruntime.quantization import (  # pyright: ignore[reportMissingTypeStubs]
    QuantType,
    quantize_dynamic,  # pyright: ignore[reportUnknownVariableType]
)
from optimum.onnxruntime import ORTModelForSequenceClassification
from transformers import AutoTokenizer

ROOT = Path(__file__).resolve().parents[1] / "models" / "candidates"


def main(model_id: str) -> None:
    revision = HfApi().model_info(model_id).sha
    if revision is None:
        raise SystemExit(f"could not resolve a revision for {model_id}")
    out = ROOT / model_id.split("/")[-1]
    if out.exists():
        shutil.rmtree(out)
    fp32 = out / "_fp32"
    print(f"Exporting {model_id}@{revision} to ONNX (fp32)...")
    ort_model: Any = ORTModelForSequenceClassification.from_pretrained(  # pyright: ignore[reportUnknownMemberType]
        model_id, revision=revision, export=True
    )
    ort_model.save_pretrained(fp32)
    print("Quantizing to int8...")
    quantize_dynamic(  # pyright: ignore[reportUnknownVariableType]
        model_input=str(fp32 / "model.onnx"),
        model_output=str(out / "model.onnx"),
        weight_type=QuantType.QInt8,
        use_external_data_format=True,
    )
    shutil.copy(fp32 / "config.json", out / "config.json")
    shutil.rmtree(fp32)
    tokenizer: Any = AutoTokenizer.from_pretrained(model_id, revision=revision)  # pyright: ignore[reportUnknownMemberType]
    tokenizer.save_pretrained(out)
    (out / "REVISION").write_text(f"{model_id}@{revision}\n", encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main(sys.argv[1])
