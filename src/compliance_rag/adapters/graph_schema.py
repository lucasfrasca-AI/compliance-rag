"""The graph's closed vocabulary and value types (PHASES.md Phase 2): the
labels a node may carry, the edge types it may have, and the small types the
writer, the query builder and `GraphAdmin` share. Labels and edge types are interpolated
into Cypher — they can't be parameters — so both the writer (`neo4j_graph`)
and the query builder (`graph_query`) take them only from these sets.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field

from compliance_rag.corpus.models import NO_END_DATE, check_end
from compliance_rag.corpus.models import EndReason as EndReason  # re-exported for the graph

PERSISTED_LABELS = frozenset(
    {
        "Instrument",
        "InstrumentVersion",
        "Clause",
        "Obligation",
        "Condition",
        "Actor",
        "Control",
        "Jurisdiction",
        "Sector",
    }
)
"""PHASES.md Phase 2's schema. Tenant-subgraph labels (`Organisation`, ...)
are projected per request and never persisted (CONTEXT.md), so they are not
writable here."""

EDGE_TYPES = frozenset(
    {
        "PART_OF",
        "VERSION_OF",
        "SUPERSEDES",
        "APPLIES_TO",
        "REQUIRES",
        "CROSSWALKS_TO",
        "DERIVED_FROM",
        "QUALIFIED_BY",
        "HAS_OPERAND",
    }
)
"""PHASES.md Phase 2's edges, plus the #93 design session's (ADR-0027,
ADR-0028): `DERIVED_FROM` (Obligation, Condition, Actor, Control -> Clause),
`QUALIFIED_BY` (Actor -> its condition tree's root) and `HAS_OPERAND`
(Condition -> Condition; the operator is on the parent node)."""


Scalar = str | int | float | bool | dt.date | tuple[str, ...] | tuple[int | float, ...]
"""What a node or edge property may hold: Neo4j stores these natively (a list
is homogeneous)."""


class GraphRefused(ValueError):
    """Anything the graph refuses, before or during a query."""


class GraphSchemaRefused(GraphRefused):
    """A label or edge type outside the schema — refused for reads and
    writes alike, before any Cypher is built."""


class GraphWriteRefused(GraphRefused):
    """A write that would mutate an immutable node or edge, or that names
    something the graph doesn't hold."""


def checked_label(label: str) -> str:
    if label not in PERSISTED_LABELS:
        raise GraphSchemaRefused(f"{label!r} is not a persisted graph label")
    return label


def checked_edge_type(edge_type: str) -> str:
    if edge_type not in EDGE_TYPES:
        raise GraphSchemaRefused(f"{edge_type!r} is not a graph edge type")
    return edge_type


@dataclass(frozen=True, slots=True)
class NodeRef:
    """One node, by label and stable ID — the label checked against the schema."""

    label: str
    stable_id: str

    def __post_init__(self) -> None:
        checked_label(self.label)


@dataclass(frozen=True, slots=True)
class Bitemporal:
    """Valid time for one node. `recorded_at` (transaction time) is not
    here: the writer stamps it, so no caller can backdate what we learned.
    Nor is a status: it is derived from these on each date (ADR-0029,
    `corpus.models.status_on`). An ended node says why (`end_reason`)."""

    valid_from: dt.date
    valid_to: dt.date = NO_END_DATE
    end_reason: EndReason | None = None

    def __post_init__(self) -> None:
        if self.valid_to <= self.valid_from:
            raise GraphWriteRefused(
                f"valid_to {self.valid_to} is not after valid_from {self.valid_from}"
            )
        try:
            check_end(self.valid_to, self.end_reason)
        except ValueError as exc:
            raise GraphWriteRefused(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class GraphRow:
    """One result of a builder-built read: the path's last node, with its
    properties as stored, and the stable IDs along the path."""

    node: Mapping[str, object]
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphCounts:
    nodes: int
    edges: int


WRITER_KEYS = frozenset({"stable_id", "recorded_at", "valid_to", "end_reason"})
"""Stored properties a loader doesn't author: the stable ID, transaction time,
and the end of valid time, which only a retirement sets (D-05, ADR-0029)."""


@dataclass(frozen=True, slots=True)
class StoredEdge:
    """One outgoing edge as stored: its type, its target, its properties."""

    edge_type: str
    target: NodeRef
    properties: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class StoredNode:
    """A node exactly as stored, at no date: every property (valid time
    included) and every outgoing edge. What a loader compares an authored
    item with before it writes (`GraphAdmin.stored`); never an answer about
    the law, which is always a builder-built read on `as_of` (D-05)."""

    node: NodeRef
    properties: Mapping[str, object]
    edges: tuple[StoredEdge, ...]

    def targets(self, edge_type: str) -> frozenset[NodeRef]:
        return frozenset(e.target for e in self.edges if e.edge_type == edge_type)


@dataclass(frozen=True, slots=True)
class NodeWrite:
    """One node for a batched write (`GraphAdmin.put_nodes`)."""

    node: NodeRef
    bitemporal: Bitemporal
    properties: Mapping[str, Scalar] = field(default_factory=dict[str, Scalar])


@dataclass(frozen=True, slots=True)
class EdgeWrite:
    """One edge for a batched write (`GraphAdmin.put_edges`)."""

    edge_type: str
    source: NodeRef
    target: NodeRef
    properties: Mapping[str, Scalar] = field(default_factory=dict[str, Scalar])


def refuse_duplicates(kind: str, keys: Sequence[Hashable]) -> None:
    """A batch naming one node or edge twice is ambiguous — which content
    wins? — so it is refused, never collapsed to the last one (issue #108)."""
    if len(keys) != len(set(keys)):
        raise GraphWriteRefused(f"a batch names the same {kind} twice")
