"""Loaders into the graph (issue #89): corpus structure, and mapping YAML.

Both are idempotent on stable ID — `Neo4jGraph` makes an identical re-write a
no-op and refuses a different one — and both validate everything before the
first write.

**Corpus structure** (`load_instrument_version`): `Instrument`,
`InstrumentVersion` and `Clause` nodes with `PART_OF` and `VERSION_OF`, from
parsed clause records. A new version supersedes the previous one through
`Neo4jGraph.amend` (D-05), and the previous version's clauses are retired on
the same date. Library law only: a policy never gets a persisted node
(CONTEXT.md: Policy), and Aura holds public law only during the build (D-09).
Clause text is not stored in the graph — it lives in Cloud Storage; the
graph carries its canonical hash.

**Mapping YAML** (`load_mappings`): obligations, obligation supersession and
crosswalks between obligations. The rest of the README's shape is validated
but **not loaded**, because loading it needs ontology decisions that belong to
the Phase 2 design session (`DESIGN_SESSION_QUESTIONS`): a file that carries
conditions, `applies_to`, controls or a crosswalk to a control is refused by
name, never half-loaded. Tenant policy mappings are never loaded into the
graph at all (D-09: tenant content stays out of Aura); `status: proposed`
therefore can't be traversed by construction.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from opentelemetry.trace import Span

from compliance_rag import telemetry
from compliance_rag.adapters.graph_query import Direction, GraphQuery
from compliance_rag.adapters.neo4j_graph import (
    Bitemporal,
    GraphCounts,
    Neo4jGraph,
    NodeRef,
    Status,
)
from compliance_rag.corpus.models import ClauseRecord, Scope, canonical_sha256, parse_stable_id
from compliance_rag.graph.mappings import MappingError, MappingSet

DESIGN_SESSION_QUESTIONS = {
    "conditions": "a Condition's valid time — the README gives conditions no dates, and every "
    "node is bitemporal (D-05)",
    "applies_to": "the identity and valid time of Actor, Jurisdiction and Sector nodes that "
    "APPLIES_TO edges point at",
    "controls": "where Control nodes come from (#12's ISO 27001 controls) and their valid time",
}
"""What the loader won't guess. Each is a decision for the Phase 2 design
session; until then a file that needs one is refused, naming it."""


@dataclass(frozen=True, slots=True)
class LoadReport:
    nodes_created: int
    edges_created: int
    counts: GraphCounts
    """Whole-graph totals after the load — the Aura <10% exit criterion."""
    not_loaded: tuple[str, ...] = ()
    """Ids validated but deliberately kept out of the graph (policy mappings)."""


def _version_ref(instrument_id: str, version_id: str) -> NodeRef:
    return NodeRef("InstrumentVersion", f"{instrument_id}:{version_id}")


async def load_instrument_version(
    graph: Neo4jGraph,
    clauses: tuple[ClauseRecord, ...],
    *,
    instrument_since: dt.date,
    effective: dt.date,
    supersedes: str | None,
) -> LoadReport:
    """One instrument version's structure. `instrument_since` is when the
    instrument itself commenced — the same on every load, since the
    `Instrument` node is immutable. `effective` is when this version came
    into force; `supersedes` names the version it replaces, retired then."""
    if not clauses:
        raise ValueError("a version with no clauses")
    instrument_id, version_id = clauses[0].instrument_id, clauses[0].version_id
    for clause in clauses:
        if (clause.instrument_id, clause.version_id) != (instrument_id, version_id):
            raise ValueError(f"{clause.clause_id} is not in {instrument_id}:{version_id}")
        if clause.scope is not Scope.PUBLIC:
            raise ValueError(
                f"{clause.clause_id}: only library law is persisted in the graph — a policy "
                "appears only in the per-request tenant subgraph (CONTEXT.md, D-09)"
            )
    if supersedes == version_id:
        raise ValueError("a version cannot supersede itself")

    created_nodes = created_edges = 0
    with telemetry.span(
        "graph_loader.load_instrument_version",
        **{"graph.instrument_id": instrument_id, "graph.version_id": version_id},
    ) as span:
        instrument = NodeRef("Instrument", instrument_id)
        version = _version_ref(instrument_id, version_id)
        in_force = Bitemporal(valid_from=effective, status=Status.IN_FORCE)
        created_nodes += await graph.put_node(
            instrument, Bitemporal(valid_from=instrument_since, status=Status.IN_FORCE)
        )
        version_properties = {"instrument_id": instrument_id, "version_id": version_id}
        if supersedes is None:
            created_nodes += await graph.put_node(version, in_force, version_properties)
        else:
            old = _version_ref(instrument_id, supersedes)
            await _retire_clauses_of(graph, old, effective)
            await graph.amend(
                old=old,
                new=version,
                effective=effective,
                status=Status.IN_FORCE,
                properties=version_properties,
            )
        created_edges += await graph.put_edge("VERSION_OF", version, instrument)
        for clause in clauses:
            node = NodeRef("Clause", clause.clause_id)
            created_nodes += await graph.put_node(
                node,
                in_force,
                {
                    "instrument_id": instrument_id,
                    "version_id": version_id,
                    "clause_path": clause.clause_path,
                    "canonical_sha256": canonical_sha256(clause.canonical_text),
                    "canonicalisation_version": clause.canonicalisation_version,
                },
            )
            created_edges += await graph.put_edge("PART_OF", node, version)
        report = LoadReport(created_nodes, created_edges, await graph.counts())
        _record(span, report)
        return report


async def _retire_clauses_of(graph: Neo4jGraph, version: NodeRef, effective: dt.date) -> None:
    """A superseded version's clauses stop being in force with it: every
    node carries its own valid time (D-05), not only the version."""
    day_before = effective - dt.timedelta(days=1)
    in_force = (
        GraphQuery(as_of=day_before, max_depth=1)
        .start("InstrumentVersion", stable_id=version.stable_id)
        .follow(("PART_OF",), "Clause", direction=Direction.IN)
        .build()
    )
    for row in await graph.read(in_force):
        await graph.retire_node(NodeRef("Clause", str(row.node["stable_id"])), valid_to=effective)


def _not_yet_loadable(mappings: MappingSet) -> list[str]:
    obligation_ids = {o.id for o in mappings.obligations}
    refusals: list[str] = []
    if mappings.conditions:
        refusals.append(f"conditions: {DESIGN_SESSION_QUESTIONS['conditions']}")
    for obligation in mappings.obligations:
        if not obligation.applies_to.empty:
            refusals.append(f"{obligation.id} applies_to: {DESIGN_SESSION_QUESTIONS['applies_to']}")
        if obligation.requires.conditions:
            refusals.append(f"{obligation.id} requires: {DESIGN_SESSION_QUESTIONS['conditions']}")
        if obligation.requires.controls:
            refusals.append(f"{obligation.id} requires: {DESIGN_SESSION_QUESTIONS['controls']}")
    for crosswalk in mappings.crosswalks:
        for end in (crosswalk.source, crosswalk.target):
            if end not in obligation_ids:
                refusals.append(f"crosswalk to {end}: {DESIGN_SESSION_QUESTIONS['controls']}")
    return refusals


async def _check_clauses_in_force(graph: Neo4jGraph, mappings: MappingSet) -> None:
    """Every obligation's clause is in the graph and in force on the day the
    obligation starts — an obligation about a clause that doesn't exist (or
    didn't yet) is a mapping error, refused before any write."""
    for obligation in mappings.obligations:
        query = (
            GraphQuery(as_of=obligation.valid_from, max_depth=1)
            .start("Clause", stable_id=obligation.clause_id)
            .build()
        )
        if not await graph.read(query):
            raise MappingError(
                f"{obligation.id}: clause {obligation.clause_id} is not in the graph in force "
                f"on {obligation.valid_from} — load the corpus first"
            )


def _supersession_order(mappings: MappingSet) -> list[str]:
    """Obligation ids with every superseded obligation before its successor."""
    by_id = {o.id: o for o in mappings.obligations}
    ordered: list[str] = []

    def visit(identifier: str, path: tuple[str, ...]) -> None:
        if identifier in ordered:
            return
        if identifier in path:
            raise MappingError(f"supersession cycle: {' -> '.join((*path, identifier))}")
        previous = by_id[identifier].supersedes
        if previous is not None:
            visit(previous, (*path, identifier))
        ordered.append(identifier)

    for identifier in sorted(by_id):
        visit(identifier, ())
    return ordered


async def load_mappings(graph: Neo4jGraph, mappings: MappingSet) -> LoadReport:
    refusals = _not_yet_loadable(mappings)
    if refusals:
        raise MappingError(
            "not loadable until the Phase 2 design session decides: " + "; ".join(refusals)
        )
    order = _supersession_order(mappings)
    await _check_clauses_in_force(graph, mappings)

    by_id = {o.id: o for o in mappings.obligations}
    created_nodes = created_edges = 0
    with telemetry.span(
        "graph_loader.load_mappings",
        **{"graph.obligations": len(by_id), "graph.crosswalks": len(mappings.crosswalks)},
    ) as span:
        for identifier in order:
            obligation = by_id[identifier]
            instrument_id, _, _ = parse_stable_id(obligation.clause_id)
            created_nodes += await graph.put_node(
                NodeRef("Obligation", identifier),
                Bitemporal(
                    valid_from=obligation.valid_from,
                    status=Status(obligation.status.value),
                    valid_to=obligation.valid_until,
                ),
                {
                    "clause_id": obligation.clause_id,
                    "instrument_id": instrument_id,
                    "summary": obligation.summary.strip(),
                },
            )
            if obligation.supersedes is not None:
                created_edges += await graph.put_edge(
                    "SUPERSEDES",
                    NodeRef("Obligation", identifier),
                    NodeRef("Obligation", obligation.supersedes),
                )
        for crosswalk in mappings.crosswalks:
            created_edges += await graph.put_edge(
                "CROSSWALKS_TO",
                NodeRef("Obligation", crosswalk.source),
                NodeRef("Obligation", crosswalk.target),
                {"kind": crosswalk.kind.value, "note": crosswalk.note.strip()},
            )
        report = LoadReport(
            created_nodes,
            created_edges,
            await graph.counts(),
            not_loaded=tuple(m.id for m in mappings.policy_mappings),
        )
        _record(span, report)
        return report


def _record(span: Span, report: LoadReport) -> None:
    span.set_attribute("graph.nodes_created", report.nodes_created)
    span.set_attribute("graph.edges_created", report.edges_created)
    span.set_attribute("graph.nodes", report.counts.nodes)
    span.set_attribute("graph.edges", report.counts.edges)
    span.set_attribute("graph.not_loaded", list(report.not_loaded))
