"""Loaders into the graph (issue #89): corpus structure, and mapping YAML.

Both talk to `GraphAdmin` (adapters/interfaces.py), never to the Neo4j adapter,
and both check everything they can before the first write: a refused load
leaves the graph as it was. Both are idempotent on stable ID — an identical
re-write is a no-op, a different one is refused (D-05: nodes are immutable).

**Corpus structure** (`load_instrument_version`): `Instrument`,
`InstrumentVersion` and `Clause` nodes with `PART_OF` and `VERSION_OF`, from
parsed clause records. A new version supersedes the previous one through
`GraphAdmin.amend`, which retires the previous version *and its clauses* in
the same transaction (D-05 — every node carries its own valid time). Library
law only: a policy never gets a persisted node (CONTEXT.md: Policy, D-14), and
Aura holds public law only during the build (D-09). Clause text is not stored
in the graph — it lives in Cloud Storage; the graph carries its canonical hash.

**Mapping YAML** (`load_mappings`): obligations, obligation supersession and
crosswalks between obligations. The rest of the README's shape is validated
but **not loaded**, because loading it needs ontology decisions that belong to
the Phase 2 design session (`DESIGN_SESSION_QUESTIONS`, issue #93): a file that
carries conditions, `applies_to`, controls or a crosswalk to a control is
refused by name, never half-loaded. Tenant policy mappings are never loaded
into the graph (D-09, D-14: tenant content has no persisted node), so a
`status: proposed` mapping can't be traversed by construction.

Correcting a mapping in place ("edits the existing entry", README) changes a
node, which D-05 forbids once it is loaded: such a load is refused before any
write, naming the obligation. Whether corrections become new ids or a
recorded exception to immutability is a design-session question too.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from opentelemetry.trace import Span

from compliance_rag import telemetry
from compliance_rag.adapters.graph_query import Direction, GraphQuery
from compliance_rag.adapters.graph_schema import Bitemporal, GraphCounts, NodeRef, Status
from compliance_rag.adapters.interfaces import GraphAdmin
from compliance_rag.corpus.models import ClauseRecord, Scope, canonical_sha256, parse_stable_id
from compliance_rag.graph.mappings import MappingError, MappingSet, Obligation

DESIGN_SESSION_QUESTIONS = {
    "conditions": "a Condition's valid time — the README gives conditions no dates, and every "
    "node is bitemporal (D-05)",
    "applies_to": "the identity and valid time of Actor, Jurisdiction and Sector nodes that "
    "APPLIES_TO edges point at",
    "controls": "where Control nodes come from (#12's ISO 27001 controls) and their valid time",
}
"""What the loader won't guess. Each is a decision for the Phase 2 design
session (issue #93); until then a file that needs one is refused, naming it."""


@dataclass(frozen=True, slots=True)
class LoadReport:
    nodes_created: int
    edges_created: int
    counts: GraphCounts
    """Whole-graph totals after the load — the Aura <10% exit criterion."""
    not_loaded: tuple[str, ...] = ()
    """Ids validated but deliberately kept out of the graph (policy mappings)."""


def version_ref(instrument_id: str, version_id: str) -> NodeRef:
    """An instrument version's node: the first two segments of its clauses'
    stable IDs (CONTEXT.md: Clause)."""
    return NodeRef("InstrumentVersion", f"{instrument_id}:{version_id}")


def _validate_version(clauses: tuple[ClauseRecord, ...], supersedes: str | None) -> tuple[str, str]:
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
    return instrument_id, version_id


async def _clauses_in_force(graph: GraphAdmin, version: NodeRef, as_of: dt.date) -> list[NodeRef]:
    query = (
        GraphQuery(as_of=as_of, max_depth=1)
        .start("InstrumentVersion", stable_id=version.stable_id)
        .follow(("PART_OF",), "Clause", direction=Direction.IN)
        .build()
    )
    return [NodeRef("Clause", str(row.node["stable_id"])) for row in await graph.read(query)]


async def load_instrument_version(
    graph: GraphAdmin,
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
    instrument_id, version_id = _validate_version(clauses, supersedes)
    instrument = NodeRef("Instrument", instrument_id)
    version = version_ref(instrument_id, version_id)
    in_force = Bitemporal(valid_from=effective, status=Status.IN_FORCE)

    with telemetry.span(
        "graph_loader.load_instrument_version",
        **{"graph.instrument_id": instrument_id, "graph.version_id": version_id},
    ) as span:
        # Reads first: the superseded version must be in force the day before
        # (or already retired on `effective`, for a re-run — then it and its
        # clauses are found in force on the day before, and retiring them to
        # the same date again is a no-op).
        old_clauses: list[NodeRef] = []
        if supersedes is not None:
            old = version_ref(instrument_id, supersedes)
            day_before = effective - dt.timedelta(days=1)
            in_force_then = GraphQuery(as_of=day_before, max_depth=1).start(
                "InstrumentVersion", stable_id=old.stable_id
            )
            if not await graph.read(in_force_then.build()):
                raise ValueError(
                    f"{old.stable_id} is not in force on {day_before}: nothing to supersede"
                )
            old_clauses = await _clauses_in_force(graph, old, day_before)

        created_nodes = created_edges = 0
        created_nodes += await graph.put_node(
            instrument, Bitemporal(valid_from=instrument_since, status=Status.IN_FORCE)
        )
        version_properties = {"instrument_id": instrument_id, "version_id": version_id}
        if supersedes is None:
            created_nodes += await graph.put_node(version, in_force, version_properties)
        else:
            await graph.amend(
                old=version_ref(instrument_id, supersedes),
                new=version,
                effective=effective,
                status=Status.IN_FORCE,
                properties=version_properties,
                retire_with=tuple(old_clauses),
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


def _not_yet_loadable(mappings: MappingSet) -> list[str]:
    obligation_ids = set(mappings.obligations_by_id)
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
            if end in obligation_ids:
                continue
            if ":" not in end:  # not a clause/control stable ID: a mistyped obligation
                raise MappingError(
                    f"crosswalk {crosswalk.source} -> {crosswalk.target}: unknown obligation {end}"
                )
            refusals.append(f"crosswalk to {end}: {DESIGN_SESSION_QUESTIONS['controls']}")
    return refusals


def _obligation_bitemporal(obligation: Obligation) -> Bitemporal:
    return Bitemporal(
        valid_from=obligation.valid_from, status=obligation.status, valid_to=obligation.valid_until
    )


def _obligation_properties(obligation: Obligation) -> dict[str, str]:
    instrument_id, _, _ = parse_stable_id(obligation.clause_id)
    return {
        "clause_id": obligation.clause_id,
        "instrument_id": instrument_id,
        "summary": obligation.summary.strip(),
    }


async def _preflight(graph: GraphAdmin, mappings: MappingSet, today: dt.date) -> None:
    """Graph checks before any write.

    - Every obligation's clause is in the graph and in force on the day the
      obligation starts, and still in force at the last date the obligation
      covers that we can see (its end, or today) — an obligation about a
      clause that doesn't exist, or that outlives it, is a mapping error.
    - An obligation already loaded is unchanged: a correction in place would
      mutate a node (D-05), refused here rather than halfway through.
    """
    for obligation in mappings.obligations:
        last = min(obligation.valid_until - dt.timedelta(days=1), max(today, obligation.valid_from))
        for as_of in sorted({obligation.valid_from, last}):
            clause = GraphQuery(as_of=as_of, max_depth=1).start(
                "Clause", stable_id=obligation.clause_id
            )
            if not await graph.read(clause.build()):
                raise MappingError(
                    f"{obligation.id}: clause {obligation.clause_id} is not in the graph in "
                    f"force on {as_of} — load the corpus first, or end the obligation with "
                    "its clause"
                )
        loaded = await graph.read(
            GraphQuery(as_of=obligation.valid_from, max_depth=1)
            .start("Obligation", stable_id=obligation.id)
            .build()
        )
        wanted: dict[str, object] = {
            **_obligation_properties(obligation),
            "status": obligation.status.value,
            "valid_from": obligation.valid_from,
        }
        for row in loaded:
            changed = sorted(k for k, v in wanted.items() if row.node.get(k) != v)
            if changed:
                raise MappingError(
                    f"{obligation.id} is already loaded with different {changed}: a loaded "
                    "obligation is immutable (D-05) — supersede it with a new id"
                )


def _supersession_order(mappings: MappingSet) -> list[str]:
    """Obligation ids with every superseded obligation before its successor.
    (Gapless dates already rule out cycles; validation guarantees one
    successor each.)"""
    by_id = mappings.obligations_by_id
    ordered: list[str] = []

    def visit(identifier: str) -> None:
        if identifier in ordered:
            return
        previous = by_id[identifier].supersedes
        if previous is not None:
            visit(previous)
        ordered.append(identifier)

    for identifier in sorted(by_id):
        visit(identifier)
    return ordered


async def load_mappings(
    graph: GraphAdmin, mappings: MappingSet, *, today: dt.date | None = None
) -> LoadReport:
    refusals = _not_yet_loadable(mappings)
    if refusals:
        raise MappingError(
            "not loadable until the Phase 2 design session decides: " + "; ".join(refusals)
        )
    by_id = mappings.obligations_by_id
    with telemetry.span(
        "graph_loader.load_mappings",
        **{"graph.obligations": len(by_id), "graph.crosswalks": len(mappings.crosswalks)},
    ) as span:
        await _preflight(graph, mappings, today or dt.date.today())
        created_nodes = created_edges = 0
        for identifier in _supersession_order(mappings):
            obligation = by_id[identifier]
            node = NodeRef("Obligation", identifier)
            created_nodes += await graph.put_node(
                node, _obligation_bitemporal(obligation), _obligation_properties(obligation)
            )
            if obligation.supersedes is not None:
                created_edges += await graph.put_edge(
                    "SUPERSEDES", node, NodeRef("Obligation", obligation.supersedes)
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
