"""Track A (D-03, issue #115): the ontology in force on `as_of`, read through
the query builder, evaluated against the tenant's evidence.

`read_snapshot` issues a fixed set of one-hop reads, every one built by
`GraphQuery` with `as_of` and a depth cap (D-05). Each returns only nodes,
and edges between nodes, in force on that date. Trees are assembled in Python
from those rows rather than walked in Cypher. So no traversal is deeper than
one hop, however deep a condition tree is, and every node a tree reaches has
passed the builder's bitemporal check.

`snapshot_of_mappings` builds the same snapshot from mapping YAML in memory,
for the fake. Both feed one evaluator (`graph/applicability.py`), so the fake
can't drift from the real thing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import defaultdict
from collections.abc import Mapping
from typing import cast

from opentelemetry.trace import Span

from compliance_rag import telemetry
from compliance_rag.adapters.graph_query import Direction, GraphQuery
from compliance_rag.adapters.graph_schema import GraphRow
from compliance_rag.adapters.interfaces import GraphReader, GraphStore, LoadedSources
from compliance_rag.assessment.coverage import Coverage, Outcome
from compliance_rag.assessment.evidence import Facts
from compliance_rag.graph.applicability import (
    ActorSpec,
    ConditionSpec,
    ObligationSpec,
    Snapshot,
    evaluate,
)
from compliance_rag.graph.mappings import Leaf, MappingSet, Operator, lineage, words

_HOPS = (
    ("Obligation", "DERIVED_FROM", "Clause"),
    ("Obligation", "APPLIES_TO", "Actor"),
    ("Obligation", "APPLIES_TO", "Jurisdiction"),
    ("Obligation", "APPLIES_TO", "Sector"),
    ("Obligation", "REQUIRES", "Condition"),
    ("Actor", "DERIVED_FROM", "Clause"),
    ("Actor", "QUALIFIED_BY", "Condition"),
    ("Condition", "DERIVED_FROM", "Clause"),
)


def _query(
    as_of: dt.date, label: str, edge: str | None = None, to: str | None = None
) -> GraphQuery:
    query = GraphQuery(as_of=as_of, max_depth=1).start(label)
    if edge is not None and to is not None:
        query = query.follow((edge,), to, direction=Direction.OUT)
    return query


def _targets(rows: tuple[GraphRow, ...]) -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        found[row.path[0]].append(row.path[1])
    return {source: tuple(sorted(targets)) for source, targets in found.items()}


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, tuple | list):
        return tuple(str(v) for v in cast("tuple[object, ...]", value))
    return ()


def _leaf(node: Mapping[str, object]) -> Leaf | None:
    if node.get("operator") != Operator.LEAF.value:
        return None
    fields = ("field", "period", "test", "value", "since_field", "not_before")
    return Leaf.model_validate({k: node[k] for k in fields if k in node})


async def read_snapshot(reader: GraphReader, as_of: dt.date) -> Snapshot:
    labels = ("Obligation", "Actor", "Condition")
    reads = [reader.read(_query(as_of, label).build()) for label in labels]
    reads += [reader.read(_query(as_of, *hop).build()) for hop in _HOPS]
    results = await asyncio.gather(*reads)
    nodes = {
        label: {str(r.node["stable_id"]): r.node for r in rows}
        for label, rows in zip(labels, results[: len(labels)], strict=True)
    }
    hops = {hop: _targets(rows) for hop, rows in zip(_HOPS, results[len(labels) :], strict=True)}

    def edges(source: str, edge: str, to: str) -> Mapping[str, tuple[str, ...]]:
        return hops[(source, edge, to)]

    conditions = {
        cid: ConditionSpec(
            id=cid,
            operator=Operator(str(node["operator"])),
            operands=_strings(node.get("operands")),
            description=str(node.get("description", "")),
            clause_ids=edges("Condition", "DERIVED_FROM", "Clause").get(cid, ()),
            leaf=_leaf(node),
            question=str(node.get("question", "")),
            unassessable_reason=str(node.get("unassessable_reason", "")),
            lineage=_strings(node.get("lineage")),
        )
        for cid, node in nodes["Condition"].items()
    }
    actors = {
        aid: ActorSpec(
            id=aid,
            name=str(node.get("name", aid)),
            clause_ids=edges("Actor", "DERIVED_FROM", "Clause").get(aid, ()),
            qualified_by=edges("Actor", "QUALIFIED_BY", "Condition")[aid][0],
        )
        for aid, node in nodes["Actor"].items()
        if aid in edges("Actor", "QUALIFIED_BY", "Condition")
    }
    obligations = {
        oid: ObligationSpec(
            id=oid,
            instrument_id=str(node["instrument_id"]),
            summary=str(node.get("summary", "")),
            clause_ids=edges("Obligation", "DERIVED_FROM", "Clause").get(oid, ()),
            actors=edges("Obligation", "APPLIES_TO", "Actor").get(oid, ()),
            jurisdictions=edges("Obligation", "APPLIES_TO", "Jurisdiction").get(oid, ()),
            sectors=edges("Obligation", "APPLIES_TO", "Sector").get(oid, ()),
            conditions=edges("Obligation", "REQUIRES", "Condition").get(oid, ()),
        )
        for oid, node in nodes["Obligation"].items()
    }
    return Snapshot(as_of=as_of, obligations=obligations, actors=actors, conditions=conditions)


def snapshot_of_mappings(
    mappings: MappingSet,
    as_of: dt.date,
    *,
    clause_validity: Mapping[str, tuple[dt.date, dt.date]],
) -> Snapshot:
    """The snapshot `read_snapshot` would return had `mappings` been loaded
    into a graph whose clauses have `clause_validity`."""

    def live(start: dt.date, end: dt.date) -> bool:
        return start <= as_of < end

    def clauses(ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(c for c in ids if c in clause_validity and live(*clause_validity[c])))

    conditions = {
        c.id: ConditionSpec(
            id=c.id,
            operator=c.operator,
            operands=c.operands,
            description=words(c.description),
            clause_ids=clauses(c.clause_ids),
            leaf=c.leaf,
            question=words(c.question or ""),
            unassessable_reason=words(c.unassessable_reason or ""),
            lineage=lineage(mappings.conditions_by_id, c.id),
        )
        for c in mappings.conditions
        if live(c.valid_from, c.valid_until)
    }
    actors = {
        a.id: ActorSpec(a.id, a.name, clauses(a.clause_ids), a.qualified_by)
        for a in mappings.actors
        if live(a.valid_from, a.valid_until) and a.qualified_by in conditions
    }
    in_force = {
        label: {i.id for i in items if live(i.valid_from, i.valid_until)}
        for label, items in mappings.by_label.items()
    }
    obligations = {
        o.id: ObligationSpec(
            id=o.id,
            instrument_id=o.instrument_id,
            summary=words(o.summary),
            clause_ids=clauses(o.clause_ids),
            actors=tuple(sorted(a for a in o.applies_to.actors if a in actors)),
            jurisdictions=tuple(
                sorted(j for j in o.applies_to.jurisdictions if j in in_force["Jurisdiction"])
            ),
            sectors=tuple(sorted(x for x in o.applies_to.sectors if x in in_force["Sector"])),
            conditions=tuple(sorted(c for c in o.requires.conditions if c in conditions)),
        )
        for o in mappings.obligations
        if live(o.valid_from, o.valid_until)
    }
    return Snapshot(as_of=as_of, obligations=obligations, actors=actors, conditions=conditions)


def _record(span: Span, coverage: Coverage) -> None:
    for outcome in Outcome:
        span.set_attribute(
            f"track_a.{outcome.value}", [o.obligation_id for o in coverage.with_outcome(outcome)]
        )
    span.set_attribute("track_a.questions", [q.condition_id for q in coverage.questions])
    span.set_attribute("track_a.clause_ids", sorted(coverage.track_a_clause_ids))


class GraphTrackA(GraphStore):
    """Track A over the persisted graph: reads only `BuiltQuery`s."""

    def __init__(self, reader: GraphReader) -> None:
        self._reader = reader

    async def coverage(self, facts: Facts, *, tenant_id: str, as_of: dt.date) -> Coverage:
        with telemetry.span(
            "track_a.coverage", **{"track_a.tenant_id": tenant_id, "graph.as_of": as_of.isoformat()}
        ) as span:
            coverage = evaluate(
                await read_snapshot(self._reader, as_of), facts, tenant_id=tenant_id
            )
            _record(span, coverage)
            return coverage

    async def loaded_sources(self) -> LoadedSources:
        """What the graph this reads was loaded from (#153)."""
        return await self._reader.loaded_sources()

    async def crosswalks(
        self, obligation_id: str, *, as_of: dt.date, max_depth: int
    ) -> tuple[str, ...]:
        with telemetry.span(
            "track_a.crosswalks",
            **{
                "track_a.obligation_id": obligation_id,
                "graph.as_of": as_of.isoformat(),
                "graph.max_depth": max_depth,
            },
        ) as span:
            found = await self._crosswalks(obligation_id, as_of=as_of, max_depth=max_depth)
            span.set_attribute("track_a.crosswalks", list(found))
            return found

    async def _crosswalks(
        self, obligation_id: str, *, as_of: dt.date, max_depth: int
    ) -> tuple[str, ...]:
        found: set[str] = set()
        for to in ("Obligation", "Control"):
            query = (
                GraphQuery(as_of=as_of, max_depth=max_depth)
                .start("Obligation", stable_id=obligation_id)
                .follow(("CROSSWALKS_TO",), to, direction=Direction.OUT, max_hops=max_depth)
            )
            found |= {str(r.node["stable_id"]) for r in await self._reader.read(query.build())}
        found.discard(obligation_id)
        return tuple(sorted(found))
