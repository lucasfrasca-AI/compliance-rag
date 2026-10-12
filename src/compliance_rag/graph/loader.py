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
    Bitemporal,
    EdgeWrite,
    GraphCounts,
    GraphRow,
    NodeRef,
    NodeWrite,
    Scalar,
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


def _strip(text: str) -> str:
    """YAML's folded scalars end in a newline; the graph stores the words."""
    return " ".join(text.split())


def _node_properties(item: Dated) -> dict[str, Scalar]:
    """What each kind stores besides its stable ID and valid time."""
    match item:
        case Jurisdiction() | Sector():
            return {"code": item.id, "name": item.name, "source": item.source}
        case Condition():
            props: dict[str, Scalar] = {
                "instrument_id": item.instrument_id,
                "operator": item.operator.value,
                "description": _strip(item.description),
            }
            if item.leaf is not None:
                leaf = item.leaf
                props |= {
                    "field": leaf.field,
                    "period": leaf.period.value,
                    "test": leaf.test.value,
                    "value": leaf.value,
                    "question": _strip(item.question or ""),
                    "unassessable_reason": _strip(item.unassessable_reason or ""),
                }
                if leaf.since_field is not None and leaf.not_before is not None:
                    props |= {"since_field": leaf.since_field, "not_before": leaf.not_before}
            return props
        case Actor():
            return {
                "instrument_id": item.instrument_id,
                "name": item.name,
                "description": _strip(item.description),
            }
        case Control():
            return {"instrument_id": item.instrument_id, "descriptor": _strip(item.descriptor)}
        case Obligation():
            return {"instrument_id": item.instrument_id, "summary": _strip(item.summary)}
        case _:
            raise TypeError(f"no graph node for {type(item).__name__}")


def _bitemporal(item: Dated) -> Bitemporal:
    return Bitemporal(
        valid_from=item.valid_from, valid_to=item.valid_until, end_reason=item.end_reason
    )


STRUCTURE = (
    ("Condition", "HAS_OPERAND", "Condition"),
    ("Actor", "QUALIFIED_BY", "Condition"),
    ("Obligation", "APPLIES_TO", "Actor"),
    ("Obligation", "APPLIES_TO", "Jurisdiction"),
    ("Obligation", "APPLIES_TO", "Sector"),
    ("Obligation", "REQUIRES", "Condition"),
    ("Obligation", "REQUIRES", "Control"),
)
"""The edges that carry an item's meaning: once loaded, the set a node has is
as immutable as its properties (D-05) — a different set is a new id."""


def _structure(item: Dated, edge_type: str, target_label: str) -> frozenset[str]:
    """The targets `item` has for one structural edge shape."""
    match (item, edge_type, target_label):
        case (Condition(), "HAS_OPERAND", _):
            return frozenset(item.operands)
        case (Actor(), "QUALIFIED_BY", _):
            return frozenset({item.qualified_by})
        case (Obligation(), "APPLIES_TO", "Actor"):
            return frozenset(item.applies_to.actors)
        case (Obligation(), "APPLIES_TO", "Jurisdiction"):
            return frozenset(item.applies_to.jurisdictions)
        case (Obligation(), "APPLIES_TO", "Sector"):
            return frozenset(item.applies_to.sectors)
        case (Obligation(), "REQUIRES", "Condition"):
            return frozenset(item.requires.conditions)
        case (Obligation(), "REQUIRES", "Control"):
            return frozenset(item.requires.controls)
        case _:
            return frozenset()


def _edges(mappings: MappingSet) -> list[EdgeWrite]:
    edges: list[EdgeWrite] = []
    for label, items in mappings.by_label.items():
        for item in items:
            node = NodeRef(label, item.id)
            if isinstance(item, Grounded):
                edges += [
                    EdgeWrite("DERIVED_FROM", node, NodeRef("Clause", c)) for c in item.clause_ids
                ]
            for source_label, edge_type, target_label in STRUCTURE:
                if source_label == label:
                    edges += [
                        EdgeWrite(edge_type, node, NodeRef(target_label, target))
                        for target in sorted(_structure(item, edge_type, target_label))
                    ]
            if item.supersedes is not None:
                edges.append(EdgeWrite("SUPERSEDES", node, NodeRef(label, item.supersedes)))
    for crosswalk in mappings.crosswalks:
        edges.append(
            EdgeWrite(
                "CROSSWALKS_TO",
                NodeRef(mappings.label_of(crosswalk.source), crosswalk.source),
                NodeRef(mappings.label_of(crosswalk.target), crosswalk.target),
                {"kind": crosswalk.kind.value, "note": _strip(crosswalk.note)},
            )
        )
    return edges


def _last_visible(item: Dated, today: dt.date) -> dt.date:
    """The last day of the item we can see: its end, or today."""
    return min(item.valid_until - dt.timedelta(days=1), max(today, item.valid_from))


async def _ids_in_force(graph: GraphAdmin, label: str, as_of: dt.date) -> dict[str, GraphRow]:
    rows = await graph.read(GraphQuery(as_of=as_of, max_depth=1).start(label).build())
    return {str(row.node["stable_id"]): row for row in rows}


async def _check_grounding(graph: GraphAdmin, mappings: MappingSet, today: dt.date) -> None:
    """Every clause an item derives from is in the graph, in force at some
    point of the item's interval we can see — its start or its last visible
    day — and on that last day at least one of them is: an item may not
    outlive the text it was read from (ADR-0028). One read per distinct date."""
    grounded = [
        (label, item)
        for label, items in mappings.by_label.items()
        for item in items
        if isinstance(item, Grounded)
    ]
    dates = {d for _, item in grounded for d in (item.valid_from, _last_visible(item, today))}
    in_force = {d: set(await _ids_in_force(graph, "Clause", d)) for d in sorted(dates)}
    for label, item in grounded:
        assert isinstance(item, Grounded)
        first, last = item.valid_from, _last_visible(item, today)
        for clause_id in item.clause_ids:
            if clause_id not in in_force[first] | in_force[last]:
                raise MappingError(
                    f"{item.id}: clause {clause_id} is not in the graph in force on {first} "
                    f"or {last} — load the corpus first"
                )
        if not in_force[last] & set(item.clause_ids):
            raise MappingError(
                f"{item.id}: none of its clauses is in force on {last} — end the "
                f"{label.lower()} with its clause, or derive it from the compilation now in force"
            )


async def _check_unchanged(graph: GraphAdmin, mappings: MappingSet) -> None:
    """An item already loaded is unchanged — its properties, its valid_from,
    any end already recorded, and its structural edges (`STRUCTURE`). A
    correction in place would mutate a node (D-05), refused here, before any
    write, rather than halfway through. Retiring an open node is the one
    change allowed. One read per label, edge shape and distinct date."""
    for label, items in mappings.by_label.items():
        for valid_from in sorted({i.valid_from for i in items}):
            dated = [i for i in items if i.valid_from == valid_from]
            loaded = await _ids_in_force(graph, label, valid_from)
            for item in dated:
                row = loaded.get(item.id)
                if row is None:
                    continue
                wanted: dict[str, object] = {**_node_properties(item), "valid_from": valid_from}
                stored = {k: v for k, v in row.node.items() if k not in WRITER_KEYS}
                changed = sorted(
                    k
                    for k in set(wanted) | set(stored)
                    if stored.get(k) != _comparable(wanted.get(k))
                )
                ended = row.node.get("valid_to") not in (NO_END_DATE, item.valid_until)
                if ended:
                    changed.append("valid_to")
                if changed:
                    raise MappingError(
                        f"{item.id} is already loaded with different {changed}: a loaded "
                        f"{label.lower()} is immutable (D-05) — supersede it with a new id"
                    )
            for source_label, edge_type, target_label in STRUCTURE:
                if source_label != label:
                    continue
                query = (
                    GraphQuery(as_of=valid_from, max_depth=1)
                    .start(label)
                    .follow((edge_type,), target_label, direction=Direction.OUT)
                )
                stored_targets: dict[str, set[str]] = {}
                for row in await graph.read(query.build()):
                    stored_targets.setdefault(row.path[0], set()).add(row.path[1])
                for item in dated:
                    if item.id not in loaded:
                        continue
                    if frozenset(stored_targets.get(item.id, set())) != _structure(
                        item, edge_type, target_label
                    ):
                        raise MappingError(
                            f"{item.id} is already loaded with different {edge_type} "
                            f"{target_label} edges: a loaded {label.lower()} is immutable "
                            "(D-05) — supersede it with a new id"
                        )


WRITER_KEYS = frozenset({"stable_id", "recorded_at", "valid_to", "end_reason"})


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
                NodeWrite(NodeRef(label, item.id), _bitemporal(item), _node_properties(item))
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
