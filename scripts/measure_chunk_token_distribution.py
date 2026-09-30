"""Real chunk-token distribution over every real fixture in the repo
(issue #26): how many chunks would exceed a given reranker's context
window, measured against the actual reranker tokenizer, not guessed.

Written to answer a specific question — is `answerai-colbert-small-v1`'s
300-token window a real handicap against this corpus's chunks — but kept
general (a `--threshold` argument) since the same question recurs whenever
a candidate reranker's window is smaller than the 420-token chunk cap
(`CONTEXT.md`: Chunk) this repo already commits to.

Only measures what's actually in the repo: the law excerpt fixture (issue
#9's spike — Schedule 1 APP 10/11 plus ss 6, 6C only, not the full
compilation) and the six real synthetic policy documents (issue #11). This
is not the full Phase 1 corpus; see this script's own printed caveat.

    uv run python scripts/measure_chunk_token_distribution.py
    uv run python scripts/measure_chunk_token_distribution.py --threshold 300
"""

from __future__ import annotations

import argparse
from pathlib import Path

from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import Chunk
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.policy_parser import parse_policy
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens

REPO_ROOT = Path(__file__).resolve().parents[1]
LAW_FIXTURE = (
    REPO_ROOT / "docs" / "research" / "fixtures" / "privacy-act-1988-C2026C00227.excerpt.html"
)
POLICY_FIXTURE_DIR = REPO_ROOT / "docs" / "research" / "fixtures" / "synthetic-policies"


def _law_chunks() -> tuple[Chunk, ...]:
    html = LAW_FIXTURE.read_text(encoding="utf-8")
    parsed = parse_compilation(html, instrument_id="privacy-act-1988", version_id="C2026C00227")
    return chunk_clauses(parsed.clauses, count_tokens=count_reranker_tokens)


def _policy_chunks() -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for path in sorted(POLICY_FIXTURE_DIR.glob("*.md")):
        clauses = parse_policy(
            path.read_text(encoding="utf-8"),
            instrument_id=f"acme/{path.stem}",
            version_id="v1",
            tenant_id="acme",
        )
        chunks.extend(chunk_clauses(clauses, count_tokens=count_reranker_tokens))
    return tuple(chunks)


def _percentile(sorted_data: list[int], p: float) -> float:
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_data) - 1)
    if lo == hi:
        return float(sorted_data[lo])
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


def _report(name: str, chunks: tuple[Chunk, ...], threshold: int) -> None:
    tokens = sorted(count_reranker_tokens(c.text) for c in chunks)
    if not tokens:
        print(f"{name}: no chunks")
        return
    over = sum(1 for t in tokens if t > threshold)
    print(
        f"{name}: n={len(tokens)} p50={_percentile(tokens, 0.5):.0f} "
        f"p90={_percentile(tokens, 0.9):.0f} max={max(tokens)} "
        f"over_{threshold}={over} ({100 * over / len(tokens):.0f}%)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=int, default=300)
    args = parser.parse_args()

    law = _law_chunks()
    policy = _policy_chunks()

    print(
        f"Reranker tokenizer counts (vendored XLM-R, issue #21) — "
        f"threshold {args.threshold} tokens\n"
    )
    _report("law", law, args.threshold)
    _report("policy", policy, args.threshold)
    _report("combined", law + policy, args.threshold)
    print(
        "\nCaveat: this is 100% of the real fixture bytes currently in the "
        "repo, not the full Phase 1 corpus — the law fixture covers only "
        "2 of 13 APPs and ss 6/6C (no 6D/6E); no full compilation has been "
        "fetched yet (issue #21's own standing gap). Re-run once it has."
    )


if __name__ == "__main__":
    main()
