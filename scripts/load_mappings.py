"""Load every reviewed mapping file in `corpus/mappings/` into the deployed
graph (Aura Free: public law only, D-09), then log its size against Aura
Free's limits (PHASES.md Phase 2 exit: under 10%).

Refuses before touching the graph if any file is still awaiting review: a
model-drafted file is corpus content only once a person has signed it (D-01).
And if the paths the mapping version is a tag over (`corpus/mappings/` and the
code that loads it) have changes that aren't committed: the version the load
records is a tag over a commit (#153, ADR-0034), and uncommitted content is in
none.

The graph records the load: the mapping version (the newest `mappings-*` tag
over this content, with the commit it points at, or the commit alone if none)
when it starts, and that it finished only once the load returns. The checks
(every D-05 refusal) run before the load is recorded, so a refused load leaves
no record, and again once it holds the kind. A load that fails after its first
write stays open, and blocks another load of the same kind, and any
assessment, until someone abandons it, and then until a load finishes.

A grow-only edge (an obligation's clause, exception or qualifier) the mappings
no longer give is removed only as an acknowledged erratum (#245, ADR-0050):
`--erratum SOURCE EDGE_TYPE TARGET`, once per edge, naming exactly the edges
removed, with `--by` and `--reason`. That is a dry run: it checks the load
against the graph and prints what it would remove, writing nothing and
recording no load. `--apply` removes them and loads.

    uv run python -m scripts.load_mappings [--today 2026-10-12]
    uv run python -m scripts.load_mappings --correct ID [ID ...] --by NAME --reason WHY
    uv run python -m scripts.load_mappings --erratum SOURCE EDGE_TYPE TARGET \
        [--erratum ...] --by NAME --reason WHY [--apply]
    uv run python -m scripts.load_mappings --abandon LOAD_ID --by NAME --reason WHY
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
from compliance_rag.graph.loader import (
    CheckedMappings,
    CorrectionAcknowledgement,
    ErratumAcknowledgement,
    ErratumEdge,
    check_mappings,
    recorded_load,
    write_mappings,
)
from compliance_rag.graph.mappings import MappingError, load_mapping_set
from compliance_rag.settings import Settings

REPO = Path(__file__).resolve().parents[1]
MAPPINGS = REPO / "corpus" / "mappings"
AURA_FREE_NODES, AURA_FREE_RELATIONSHIPS = 200_000, 400_000
"""Aura Free's documented limits."""
PENDING = "PENDING"


def unreviewed(paths: list[Path]) -> list[str]:
    mappings = load_mapping_set(paths)
    return [
        path.name
        for path, file in zip(sorted(paths), mappings.files, strict=True)
        if file.reviewed_by.startswith(PENDING)
    ]


def planned(found: CheckedMappings, errata: ErratumAcknowledgement) -> list[str]:
    """What a dry run of an erratum shows: exactly the edges it would remove,
    and any wording it would correct, before anything is written."""
    lines = [f"remove as an erratum, by {errata.by} ({errata.reason}):"]
    removing = sorted(ErratumEdge.of(e) for e in found.errata)
    lines += [f"  {edge}" for edge in removing] or [
        "  nothing: every edge it names was already removed by an erratum (a re-run)"
    ]
    if found.corrections:
        corrected = sorted({c.node.stable_id for c in found.corrections})
        lines.append(f"correct the wording of: {', '.join(corrected)}")
    return lines


async def main(
    today: dt.date,
    corrections: CorrectionAcknowledgement | None,
    errata: ErratumAcknowledgement | None = None,
    *,
    apply: bool = False,
) -> None:
    """Load the mappings. With `errata` and without `apply`, a dry run: the
    checks only, against the graph, printing what the erratum would remove;
    nothing is written and no load is recorded. It doesn't take the hold on
    the kind, so it can pass while another mapping load is open, and `--apply`
    is then refused until that load finishes or is abandoned."""
    paths = sorted(MAPPINGS.glob("*.yaml"))
    waiting = unreviewed(paths)
    if waiting:
        raise SystemExit(f"awaiting review, not loaded: {waiting} (D-01)")
    try:
        version = resolve_source_version(SourceKind.MAPPING, repo=REPO)
    except UncommittedSource as refused:
        raise SystemExit(f"not loaded: {refused}") from None
    mappings = load_mapping_set(paths)
    dry_run = errata is not None and not apply
    print(f"{'dry run of' if dry_run else 'loading'} mapping version {version.described}")
    async with Neo4jGraph.from_settings(Settings()) as graph:
        # Every refusal is checked before the load is recorded, so a refused
        # load leaves no record, and again once it holds the kind.
        check = partial(
            check_mappings,
            graph,
            mappings,
            today=today,
            corrections=corrections,
            errata=errata,
        )
        if dry_run and errata is not None:
            try:
                found = await check()
            except MappingError as refused:
                raise SystemExit(f"refused: {refused}") from None
            print("\n".join(planned(found, errata)))
            print("dry run: nothing written, no load recorded; --apply to remove and load")
            return
        async with recorded_load(graph, version, check=check) as found:
            report = await write_mappings(graph, mappings, found, corrections, errata)
    nodes, edges = report.counts.nodes, report.counts.edges
    print(f"recorded as the graph's mapping version: {version.described}")
    if found.errata:
        print(f"removed as an erratum: {', '.join(str(ErratumEdge.of(e)) for e in found.errata)}")
    print(
        f"{report.nodes_created} nodes and {report.edges_created} edges created; graph now "
        f"{nodes} nodes ({nodes / AURA_FREE_NODES:.1%} of Aura Free) and {edges} edges "
        f"({edges / AURA_FREE_RELATIONSHIPS:.1%})"
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
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument(
        "--correct",
        nargs="+",
        default=[],
        help="ids whose wording (only) changed, acknowledged for in-place correction (ADR-0031)",
    )
    parser.add_argument(
        "--erratum",
        nargs=3,
        action="append",
        default=[],
        metavar=("SOURCE", "EDGE_TYPE", "TARGET"),
        help="a grow-only edge that was never true, removed as an acknowledged erratum "
        "(ADR-0050); once per edge, naming exactly the edges removed. A dry run without --apply",
    )
    parser.add_argument(
        "--apply", action="store_true", help="remove the --erratum edges and load (not a dry run)"
    )
    parser.add_argument(
        "--abandon",
        metavar="LOAD_ID",
        default="",
        help="close a failed load that is blocking others (with --by and --reason); loads nothing",
    )
    parser.add_argument(
        "--by", default="", help="who made the correction or erratum, or abandoned the load"
    )
    parser.add_argument("--reason", default="", help="why")
    args = parser.parse_args()
    if args.abandon:
        if args.correct or args.erratum:
            parser.error(
                "--abandon loads nothing: it can't be combined with --correct or --erratum"
            )
        asyncio.run(abandon(str(args.abandon), str(args.by), str(args.reason)))
        raise SystemExit(0)
    if args.apply and not args.erratum:
        parser.error("--apply applies an --erratum; a load without one is never a dry run")
    acknowledged: CorrectionAcknowledgement | None = None
    if args.correct:
        acknowledged = CorrectionAcknowledgement(
            frozenset(str(i) for i in args.correct), by=str(args.by), reason=str(args.reason)
        )
    erratum: ErratumAcknowledgement | None = None
    if args.erratum:
        try:
            erratum = ErratumAcknowledgement(
                frozenset(ErratumEdge(str(s), str(t), str(c)) for s, t, c in args.erratum),
                by=str(args.by),
                reason=str(args.reason),
            )
        except MappingError as refused:
            parser.error(str(refused))
    asyncio.run(main(args.today, acknowledged, erratum, apply=bool(args.apply)))
