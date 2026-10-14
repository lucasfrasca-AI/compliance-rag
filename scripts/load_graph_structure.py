"""Load an ingested compilation's structure into the deployed graph (Phase 2).

`Instrument`, `InstrumentVersion` and `Clause` nodes with `PART_OF` and
`VERSION_OF`, through `graph/loader.load_instrument_version` — the same pinned
source and the same reviewed selection `scripts/ingest_compilation.py` wrote
to the corpus bucket, so the graph and the clause store hold one clause set.
Structure only: no obligations, conditions or applicability (#93).

Aura Free holds public law only (D-09). Idempotent: a re-run creates nothing
(but the load itself is recorded each time).

The graph records the load (#153, ADR-0034): the corpus version (the newest
`corpus-*` tag over `corpus/instruments/`, with the commit it points at, or the
commit alone if none) when it starts, and that it finished only once the load
returns. Refused up front if the paths the corpus version is a tag over
(`corpus/instruments/` and the code that parses the source) have changes that
aren't committed: that content is in no commit for the version to name. The
checks run before the load is recorded, so a refused load leaves no record, and
again once it holds the kind. A load that fails after its first write stays
open, and blocks another load of the same kind, and any assessment, until
someone abandons it, and then until a load finishes.

    uv run python -m scripts.load_graph_structure [--html path/to/document_1.html]
    uv run python -m scripts.load_graph_structure --abandon LOAD_ID --by NAME --reason WHY

Run as a module: it reuses `scripts/ingest_compilation.py`'s pinned source.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from functools import partial
from pathlib import Path

from compliance_rag.adapters.git_source_versions import UncommittedSource, resolve_source_version
from compliance_rag.adapters.interfaces import SourceKind
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.selection_manifest import parse_selection_manifest
from compliance_rag.graph.loader import (
    check_instrument_version,
    recorded_load,
    write_instrument_version,
)
from compliance_rag.settings import Settings
from scripts.ingest_compilation import INSTRUMENT_ID, MANIFEST, START, VERSION_ID, pinned_source

REPO = Path(__file__).resolve().parents[1]
INSTRUMENT_SINCE = dt.date(1989, 1, 1)
"""Privacy Act 1988 commencement: "1 Jan 1989 (s 2 and gaz 1988, No S399)" —
the compilation's own Endnote 3 (legislation history)."""
EFFECTIVE = dt.date.fromisoformat(START)
"""C2026C00227 is in force from its start date; it is the first compilation
loaded, so nothing is superseded."""


async def main(html_path: Path | None) -> None:
    try:
        version = resolve_source_version(SourceKind.CORPUS, repo=REPO)
    except UncommittedSource as refused:
        raise SystemExit(f"not loaded: {refused}") from None
    parsed = parse_compilation(
        pinned_source(html_path), instrument_id=INSTRUMENT_ID, version_id=VERSION_ID
    )
    selected = parse_selection_manifest(MANIFEST.read_text(encoding="utf-8")).select(parsed.clauses)
    print(f"loading corpus version {version.described}")
    async with Neo4jGraph.from_settings(Settings()) as graph:
        # Every refusal is checked before the load is recorded, so a refused
        # load leaves no record, and again once it holds the kind.
        check = partial(
            check_instrument_version, graph, selected, effective=EFFECTIVE, supersedes=None
        )
        async with recorded_load(graph, version, check=check) as checked:
            report = await write_instrument_version(
                graph,
                selected,
                checked,
                instrument_since=INSTRUMENT_SINCE,
                effective=EFFECTIVE,
                supersedes=None,
            )
    print(f"recorded as the graph's corpus version: {version.described}")
    print(
        f"{len(selected)} clauses: {report.nodes_created} nodes and {report.edges_created} "
        f"edges created; graph now {report.counts.nodes} nodes, {report.counts.edges} edges"
    )


async def abandon(load_id: str, by: str, reason: str) -> None:
    """Close a load that failed and will never finish, so another can begin
    (#153). It doesn't make it a finished load: the version the graph holds
    is what it was."""
    async with Neo4jGraph.from_settings(Settings()) as graph:
        await graph.abandon_load(load_id, by, reason)
    print(f"abandoned load {load_id} (by {by}: {reason})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, default=None)
    parser.add_argument(
        "--abandon",
        metavar="LOAD_ID",
        default="",
        help="close a failed load that is blocking others (with --by and --reason); loads nothing",
    )
    parser.add_argument("--by", default="", help="who abandoned the load")
    parser.add_argument("--reason", default="", help="why")
    args = parser.parse_args()
    if args.abandon:
        asyncio.run(abandon(str(args.abandon), str(args.by), str(args.reason)))
    else:
        asyncio.run(main(args.html))
