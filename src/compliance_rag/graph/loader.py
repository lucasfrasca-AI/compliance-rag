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

**Mapping YAML** (`load_mappings`): the ontology the #93 design session
settled (ADR-0027, ADR-0028) — obligations, actors, condition trees,
controls, jurisdictions and sectors, with `DERIVED_FROM`, `QUALIFIED_BY`,
`HAS_OPERAND`, `APPLIES_TO`, `REQUIRES`, `CROSSWALKS_TO` and `SUPERSEDES`.
Checked against the graph first: every clause an item derives from present
and in force within its interval, and every item already loaded unchanged —
properties and structural edges alike. Tenant policy mappings are never
loaded into the graph (D-09, D-14: tenant content has no persisted node), so a
`status: proposed` mapping can't be traversed by construction.

Correcting a loaded item in place changes a node, which D-05 forbids: such a
load is refused before any write, naming the item. A change of meaning is a
new id with `supersedes` (#93 Q4).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import cast

from opentelemetry.trace import Span

from compliance_rag import telemetry
from compliance_rag.adapters.graph_query import Direction, GraphQuery
from compliance_rag.adapters.graph_schema import (
    WRITER_KEYS,
    Bitemporal,
    EdgeWrite,
    GraphCounts,
    NodeRef,
    NodeWrite,
    Scalar,
    StoredNode,
)
from compliance_rag.adapters.interfaces import GraphAdmin
from compliance_rag.corpus.models import NO_END_DATE, ClauseRecord, Scope, canonical_sha256
from compliance_rag.graph.mappings import (
    Actor,
    Condition,
    Control,
    Dated,
    Grounded,
    Jurisdiction,
    MappingError,
    MappingSet,
    Obligation,
    Sector,
    lineage,
    words,
)


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
    in_force = Bitemporal(valid_from=effective)

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
        created_nodes += await graph.put_node(instrument, Bitemporal(valid_from=instrument_since))
        version_properties = {"instrument_id": instrument_id, "version_id": version_id}
        if supersedes is None:
            created_nodes += await graph.put_node(version, in_force, version_properties)
        else:
            await graph.amend(
                old=version_ref(instrument_id, supersedes),
                new=version,
                effective=effective,
                properties=version_properties,
                retire_with=tuple(old_clauses),
            )
        created_edges += await graph.put_edge("VERSION_OF", version, instrument)
        created_nodes += await graph.put_nodes(
            [
                NodeWrite(
                    NodeRef("Clause", clause.clause_id),
                    in_force,
                    {
                        "instrument_id": instrument_id,
                        "version_id": version_id,
                        "clause_path": clause.clause_path,
                        "canonical_sha256": canonical_sha256(clause.canonical_text),
                        "canonicalisation_version": clause.canonicalisation_version,
                    },
                )
                for clause in clauses
            ]
        )
        created_edges += await graph.put_edges(
            [EdgeWrite("PART_OF", NodeRef("Clause", c.clause_id), version) for c in clauses]
        )
        report = LoadReport(created_nodes, created_edges, await graph.counts())
        _record(span, report)
        return report


def _node_properties(item: Dated, mappings: MappingSet) -> dict[str, Scalar]:
    """What each kind stores besides its stable ID and valid time."""
    match item:
        case Jurisdiction() | Sector():
            return {"code": item.id, "name": item.name, "source": item.source}
        case Condition():
            props: dict[str, Scalar] = {
                "instrument_id": item.instrument_id,
                "operator": item.operator.value,
                "description": words(item.description),
                # Declared on the node, not only as edges: an operand not in
                # force on as_of has no edge on that date but still counts —
                # as false, or as its successor (ADR-0027).
                "operands": item.operands,
            }
            if item.supersedes is not None:
                props["lineage"] = lineage(mappings.conditions_by_id, item.id)
            if item.leaf is not None and item.question and item.unassessable_reason:
                leaf = item.leaf
                props |= {
                    "field": leaf.field,
                    "period": leaf.period.value,
                    "test": leaf.test.value,
                    "value": leaf.value,
                    "question": words(item.question),
                    "unassessable_reason": words(item.unassessable_reason),
                }
                if leaf.since_field is not None and leaf.not_before is not None:
                    props |= {"since_field": leaf.since_field, "not_before": leaf.not_before}
            return props
        case Actor():
            return {
                "instrument_id": item.instrument_id,
                "name": item.name,
                "description": words(item.description),
            }
        case Control():
            return {"instrument_id": item.instrument_id, "descriptor": words(item.descriptor)}
        case Obligation():
            return {"instrument_id": item.instrument_id, "summary": words(item.summary)}
        case _:
            raise TypeError(f"no graph node for {type(item).__name__}")


def _bitemporal(item: Dated) -> Bitemporal:
    return Bitemporal(
        valid_from=item.valid_from, valid_to=item.valid_until, end_reason=item.end_reason
    )


def _outgoing(item: Dated) -> list[tuple[str, NodeRef]]:
    """Every edge an item's own YAML gives it, as (edge type, target)."""
    edges: list[tuple[str, NodeRef]] = []
    if isinstance(item, Grounded):
        edges += [("DERIVED_FROM", NodeRef("Clause", c)) for c in item.clause_ids]
    match item:
        case Condition():
            edges += [("HAS_OPERAND", NodeRef("Condition", c)) for c in item.operands]
        case Actor():
            edges.append(("QUALIFIED_BY", NodeRef("Condition", item.qualified_by)))
        case Obligation():
            edges += [("APPLIES_TO", NodeRef("Actor", a)) for a in item.applies_to.actors]
            edges += [
                ("APPLIES_TO", NodeRef("Jurisdiction", j)) for j in item.applies_to.jurisdictions
            ]
            edges += [("APPLIES_TO", NodeRef("Sector", x)) for x in item.applies_to.sectors]
            edges += [("REQUIRES", NodeRef("Condition", c)) for c in item.requires.conditions]
            edges += [("REQUIRES", NodeRef("Control", c)) for c in item.requires.controls]
        case _:
            pass
    return edges


def _edges(mappings: MappingSet) -> list[EdgeWrite]:
    labels = mappings.labels
    edges: list[EdgeWrite] = []
    for label, items in mappings.by_label.items():
        for item in items:
            node = NodeRef(label, item.id)
            edges += [EdgeWrite(edge_type, node, target) for edge_type, target in _outgoing(item)]
            if item.supersedes is not None:
                edges.append(EdgeWrite("SUPERSEDES", node, NodeRef(label, item.supersedes)))
    for crosswalk in mappings.crosswalks:
        edges.append(
            EdgeWrite(
                "CROSSWALKS_TO",
                NodeRef(labels[crosswalk.source], crosswalk.source),
                NodeRef(labels[crosswalk.target], crosswalk.target),
                {"kind": crosswalk.kind.value, "note": words(crosswalk.note)},
            )
        )
    return edges


MEANING = frozenset({"HAS_OPERAND", "QUALIFIED_BY", "APPLIES_TO", "REQUIRES", "SUPERSEDES"})
"""Edges that carry an item's meaning: once loaded, the set a node has is as
immutable as its properties (D-05) — a different set is a new id.
`DERIVED_FROM` may grow (a new compilation, ADR-0028) but never shrink;
`CROSSWALKS_TO` may grow, but a stored crosswalk's kind and note are fixed."""


def _last_visible(item: Dated, today: dt.date) -> dt.date:
    """The last day of the item we can see: its end, or today."""
    return min(item.valid_until - dt.timedelta(days=1), max(today, item.valid_from))


def _interval(stored: StoredNode) -> tuple[dt.date, dt.date]:
    return (
        cast("dt.date", stored.properties["valid_from"]),
        cast("dt.date", stored.properties["valid_to"]),
    )


async def _check_grounding(graph: GraphAdmin, mappings: MappingSet, today: dt.date) -> None:
    """Every clause an item derives from is in the graph and in force at some
    time within the item's interval, and on the item's last visible day at
    least one of them is: an item may not outlive the text it was read from
    (ADR-0028). An item grounded in many compilations lists them all; each
    only has to overlap the item."""
    grounded = mappings.grounded
    refs = {NodeRef("Clause", c) for _, item in grounded for c in item.clause_ids}
    clauses = await graph.stored(sorted(refs, key=lambda r: r.stable_id))
    for label, item in grounded:
        last = _last_visible(item, today)
        for clause_id in item.clause_ids:
            stored = clauses.get(NodeRef("Clause", clause_id))
            if stored is None:
                raise MappingError(
                    f"{item.id}: clause {clause_id} is not in the graph — load the corpus first"
                )
            start, end = _interval(stored)
            if not (start < item.valid_until and item.valid_from < end):
                raise MappingError(
                    f"{item.id}: clause {clause_id} ({start} to {end}) is never in force while "
                    f"the {label.lower()} is ({item.valid_from} to {item.valid_until})"
                )
        if not any(
            _interval(clauses[NodeRef("Clause", c)])[0]
            <= last
            < _interval(clauses[NodeRef("Clause", c)])[1]
            for c in item.clause_ids
        ):
            raise MappingError(
                f"{item.id}: none of its clauses is in force on {last} — end the "
                f"{label.lower()} with its clause, or derive it from the compilation now in force"
            )


def _refuse(item: Dated, label: str, what: str) -> MappingError:
    return MappingError(
        f"{item.id} is already loaded with different {what}: a loaded {label.lower()} is "
        "immutable (D-05) — supersede it with a new id"
    )


async def _check_unchanged(graph: GraphAdmin, mappings: MappingSet) -> None:
    """An item already loaded is unchanged — its properties, its valid time
    (retiring an open node is the one change allowed), and the edges that
    carry its meaning (`MEANING`); its `DERIVED_FROM` may only grow, and a
    stored crosswalk's kind and note are fixed. A correction in place would
    mutate a node (D-05), so it is refused here, before any write, not
    halfway through. One read for every item, at no date (`stored`)."""
    edges = _edges(mappings)
    refs = [NodeRef(label, item.id) for label, items in mappings.by_label.items() for item in items]
    loaded = await graph.stored(refs)
    for label, items in mappings.by_label.items():
        for item in items:
            stored = loaded.get(NodeRef(label, item.id))
            if stored is None:
                continue
            wanted: dict[str, object] = {
                **_node_properties(item, mappings),
                "valid_from": item.valid_from,
            }
            have = {k: v for k, v in stored.properties.items() if k not in WRITER_KEYS}
            changed = sorted(
                k for k in set(wanted) | set(have) if have.get(k) != _comparable(wanted.get(k))
            )
            if changed:
                raise _refuse(item, label, str(changed))
            end, why = stored.properties["valid_to"], stored.properties.get("end_reason")
            if end != NO_END_DATE and (end, why) != (item.valid_until, item.end_reason):
                raise _refuse(item, label, "valid_to or end_reason")
            mine = [e for e in edges if e.source == stored.node]
            for edge_type in sorted(MEANING):
                wanted_targets = frozenset(e.target for e in mine if e.edge_type == edge_type)
                if stored.targets(edge_type) != wanted_targets:
                    raise _refuse(item, label, f"{edge_type} edges")
            derived = {e.target for e in mine if e.edge_type == "DERIVED_FROM"}
            if not stored.targets("DERIVED_FROM") <= derived:
                raise _refuse(item, label, "DERIVED_FROM edges (a clause was removed)")
            crosswalks = {e.target: e.properties for e in mine if e.edge_type == "CROSSWALKS_TO"}
            for edge in stored.edges:
                if (
                    edge.edge_type == "CROSSWALKS_TO"
                    and edge.target in crosswalks
                    and dict(edge.properties) != dict(crosswalks[edge.target])
                ):
                    raise _refuse(item, label, f"crosswalk to {edge.target.stable_id}")


def _comparable(value: object) -> object:
    """A wanted property as it reads back from the graph (lists as tuples)."""
    if isinstance(value, list):
        return tuple(cast("list[object]", value))
    return value


async def load_mappings(
    graph: GraphAdmin, mappings: MappingSet, *, today: dt.date | None = None
) -> LoadReport:
    """Every node and edge the mapping set describes, after checking the
    graph: clauses present and in force, loaded items unchanged. Nodes first
    (every kind, batched), then edges. Tenant policy mappings are validated
    and reported, never written (D-09, D-14)."""
    attributes = {
        f"graph.{label.lower()}s": len(items) for label, items in mappings.by_label.items()
    }
    with telemetry.span("graph_loader.load_mappings", **attributes) as span:
        await _check_grounding(graph, mappings, today or dt.date.today())
        await _check_unchanged(graph, mappings)
        created_nodes = await graph.put_nodes(
            [
                NodeWrite(
                    NodeRef(label, item.id), _bitemporal(item), _node_properties(item, mappings)
                )
                for label, items in mappings.by_label.items()
                for item in items
            ]
        )
        created_edges = await graph.put_edges(_edges(mappings))
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
