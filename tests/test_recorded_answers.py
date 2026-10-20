"""Recorded live generations, re-verified on every build with no LLM (#31).

`scripts/run_answer_eval.py` runs `answer()` over the golden set with live
Claude and records, per query, every citation the model made, its outcome, and
the context it was checked against. This test re-checks each recorded citation
through the real verifier — golden canonical text through `ClauseStore`, a
ledger with every golden version recorded — and requires the outcome the live
run recorded. A change to canonicalisation, the verifier, the ledger check or
the golden corpus that would have turned a verified citation into a blocked
one (or the reverse) fails here, by query id, without calling a model.

One test per recorded file; none until the first live run is recorded.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from compliance_rag.assessment.draft import DraftCitation
from compliance_rag.eval.harness import golden_clause_store, golden_ledger
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier

RECORDED = sorted(
    p
    for p in (Path(__file__).resolve().parents[1] / "corpus" / "golden" / "answer_eval").glob(
        "*.json"
    )
    if not p.stem.endswith("-injection")
)


@pytest.mark.parametrize("path", RECORDED, ids=[p.stem for p in RECORDED])
async def test_recorded_citations_reverify_to_the_same_outcome(path: Path) -> None:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))  # Any: JSON
    # An out-of-environment run (#252) retrieves over the whole compilation.
    whole_compilation = path.stem.endswith("-environment")
    verifier = Verifier(
        await golden_clause_store(whole_compilation=whole_compilation),
        await golden_ledger(),
        CanonicalCitationVerifier(),
    )
    drifted: list[str] = []
    for row in document["rows"]:
        if not row.get("schema_valid"):
            continue
        context = frozenset(row["context"])
        as_of = dt.date.fromisoformat(row["as_of"])
        for recorded in row["final_citations"]:
            citation = DraftCitation(clause_id=recorded["clause_id"], quote=recorded["quote"])
            now = await verifier.check(citation, context=context, as_of=as_of)
            if now.outcome.value != recorded["outcome"]:
                was, became = recorded["outcome"], now.outcome.value
                drifted.append(f"{row['id']} {recorded['clause_id']}: {was} -> {became}")
    assert drifted == []
