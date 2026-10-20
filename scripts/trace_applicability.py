"""Render Track A's answer for one golden case — the coverage report D-06
requires, each obligation's citation chain, and the questions that would
settle it — from mapping YAML in memory, or from the deployed graph with
`--aura` (read-only: builder-built queries only).

    uv run python -m scripts.trace_applicability GA-02 [--aura] [--obligation OB-PA-APP11-1]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from compliance_rag.adapters.fakes import FakeGraphStore
from compliance_rag.adapters.interfaces import GraphStore
from compliance_rag.assessment.coverage import Coverage, Explanation
from compliance_rag.assessment.coverage_report import render
from compliance_rag.assessment.evidence import Facts
from compliance_rag.corpus.display_form import NO_TITLES, cite
from compliance_rag.eval.applicability import load_applicability_set
from compliance_rag.eval.compilation_clauses import load_compilation_clauses
from compliance_rag.graph.mappings import load_mapping_set

MAPPINGS = Path(__file__).resolve().parents[1] / "corpus" / "mappings"


def tree(node: Explanation, depth: int = 0) -> list[str]:
    cites = (
        f" [{', '.join(cite(c, NO_TITLES) for c in node.clause_ids)}]" if node.clause_ids else ""
    )
    line = f"{'  ' * depth}{node.truth.value.upper():8} {node.condition_id}{cites}"
    return [line, *(row for child in node.because for row in tree(child, depth + 1))]


async def run(case_id: str, *, aura: bool, obligation: str) -> Coverage:
    labels = load_applicability_set()
    case = next(c for c in labels.cases if c.id == case_id)
    facts = Facts.of(case.evidence())
    if aura:
        from compliance_rag.adapters.neo4j_graph import Neo4jGraph
        from compliance_rag.graph.track_a import GraphTrackA
        from compliance_rag.settings import Settings

        async with Neo4jGraph.from_settings(Settings()) as graph:
            return await GraphTrackA(graph).coverage(
                facts, tenant_id=case.tenant_id, as_of=case.as_of
            )
    mappings = load_mapping_set(sorted(MAPPINGS.glob("*.yaml")))
    validity = labels.clause_validity(set(mappings.clause_ids))
    incomplete = {c.clause_id: c.incomplete for c in load_compilation_clauses().clauses}
    store: GraphStore = FakeGraphStore(mappings, clause_validity=validity, incomplete=incomplete)
    return await store.coverage(facts, tenant_id=case.tenant_id, as_of=case.as_of)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case")
    parser.add_argument("--aura", action="store_true")
    parser.add_argument("--obligation", default="OB-PA-APP11-1")
    args = parser.parse_args()
    coverage = asyncio.run(run(args.case, aura=args.aura, obligation=args.obligation))
    chosen = coverage.obligation(args.obligation)
    print(f"{args.obligation}: {chosen.outcome.value} — {chosen.reason}\n")
    print("Citation chain:", ", ".join(cite(c, NO_TITLES) for c in chosen.citations), "\n")
    print("\n".join(tree(chosen.explanation)), "\n")
    print(render(coverage))


if __name__ == "__main__":
    main()
