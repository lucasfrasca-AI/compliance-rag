"""The Phase 1 demo (issue #31): one question in, a verified, verbatim,
versioned citation out, opening at the clause.

Runs `answer()` live on the golden corpus (`eval/harness.live_answer_pipeline`
— real retrieval, the cross-encoder, live Claude behind the residency gate,
the real verifier). Prints the answer and each verified citation — its display
form, version, the canonical text the user is shown (never the model's
words), and a link that opens the stored source HTML at the clause's
synthesized anchor (ADR-0019). Blocked citations are listed as blocked.

    QDRANT_URL=http://localhost:6333 VERTEX_CLAUDE_MODEL=claude-sonnet-5 \\
    VERTEX_CLAUDE_REGION=us uv run python scripts/demo_answer.py \\
        "What must an entity do with personal information it no longer needs?"
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path

from compliance_rag.assessment.pipeline import LIBRARY_QUESTION
from compliance_rag.corpus.display_form import display_form
from compliance_rag.corpus.models import PUBLIC_TENANT_ID
from compliance_rag.eval.harness import clause_link, law_source_html
from compliance_rag.eval.live import live_answer_pipeline
from compliance_rag.settings import Settings

OUT = Path(__file__).resolve().parents[1] / "out" / "demo"
AS_OF = dt.date(2026, 10, 1)


async def main(question: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    source = OUT / "privacy-act-1988-C2026C00227.html"
    source.write_text(law_source_html(), encoding="utf-8")

    async with live_answer_pipeline(Settings()) as pipeline:
        result = await pipeline.answer(question, PUBLIC_TENANT_ID, AS_OF, law=LIBRARY_QUESTION)

    print(f"Q: {question}\n\nA: {result.unverified_draft.answer}\n")
    print(
        f"verified: {result.verified}   trace: {result.trace_id}   cost: ${result.cost_usd:.4f}\n"
    )
    for checked in result.citations:
        clause = checked.clause
        assert clause is not None  # a verified citation always carries its clause
        print(
            f"[{display_form(clause.clause_path, clause.instrument_id)}] "
            f"{clause.instrument_id} {clause.version_id}"
        )
        print(f"  {clause.canonical_text}")
        print(f"  open at the clause: {clause_link(clause, source)}\n")
    for blocked in result.blocked:
        print(f"BLOCKED {blocked.citation.clause_id}: {blocked.outcome.value}")


if __name__ == "__main__":
    asyncio.run(
        main(
            sys.argv[1]
            if len(sys.argv) > 1
            else "What must an entity do with personal information it no longer needs?"
        )
    )
