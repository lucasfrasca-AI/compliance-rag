"""Load an ingested compilation's structure into the deployed graph (Phase 2).

`Instrument`, `InstrumentVersion` and `Clause` nodes with `PART_OF` and
`VERSION_OF`, through `graph/loader.load_instrument_version` — the same pinned
source and the same reviewed selection `scripts/ingest_compilation.py` wrote
to the corpus bucket, so the graph and the clause store hold one clause set.
Structure only: no obligations, conditions or applicability (#93).

Aura Free holds public law only (D-09). Idempotent: a re-run creates nothing.

    uv run python -m scripts.load_graph_structure [--html path/to/document_1.html]

Run as a module: it reuses `scripts/ingest_compilation.py`'s pinned source.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from pathlib import Path

from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.selection_manifest import parse_selection_manifest
from compliance_rag.graph.loader import load_instrument_version
from compliance_rag.settings import Settings
from scripts.ingest_compilation import INSTRUMENT_ID, MANIFEST, START, VERSION_ID, pinned_source

INSTRUMENT_SINCE = dt.date(1989, 1, 1)
"""Privacy Act 1988 commencement: "1 Jan 1989 (s 2 and gaz 1988, No S399)" —
the compilation's own Endnote 3 (legislation history)."""
EFFECTIVE = dt.date.fromisoformat(START)
"""C2026C00227 is in force from its start date; it is the first compilation
loaded, so nothing is superseded."""


async def main(html_path: Path | None) -> None:
    parsed = parse_compilation(
        pinned_source(html_path), instrument_id=INSTRUMENT_ID, version_id=VERSION_ID
    )
    selected = parse_selection_manifest(MANIFEST.read_text(encoding="utf-8")).select(parsed.clauses)
    async with Neo4jGraph.from_settings(Settings()) as graph:
        report = await load_instrument_version(
            graph,
            selected,
            instrument_since=INSTRUMENT_SINCE,
            effective=EFFECTIVE,
            supersedes=None,
        )
    print(
        f"{len(selected)} clauses: {report.nodes_created} nodes and {report.edges_created} "
        f"edges created; graph now {report.counts.nodes} nodes, {report.counts.edges} edges"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, default=None)
    asyncio.run(main(parser.parse_args().html))
