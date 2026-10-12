"""The graph's closed vocabulary and value types (PHASES.md Phase 2): the
labels a node may carry, the edge types it may have, and the small types the
writer, the query builder and `GraphAdmin` share. Labels and edge types are interpolated
into Cypher — they can't be parameters — so both the writer (`neo4j_graph`)
and the query builder (`graph_query`) take them only from these sets.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from compliance_rag.corpus.models import NO_END_DATE

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
    {"PART_OF", "VERSION_OF", "SUPERSEDES", "APPLIES_TO", "REQUIRES", "CROSSWALKS_TO"}
)


Scalar = str | int | float | bool | dt.date | tuple[str, ...]
"""What a node or edge property may hold: Neo4j stores these natively."""


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


class Status(StrEnum):
    IN_FORCE = "in_force"
    NOT_YET_COMMENCED = "not_yet_commenced"
    REPEALED = "repealed"


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
    here: the writer stamps it, so no caller can backdate what we learned."""

    valid_from: dt.date
    status: Status
    valid_to: dt.date = NO_END_DATE

    def __post_init__(self) -> None:
        if self.valid_to <= self.valid_from:
            raise GraphWriteRefused(
                f"valid_to {self.valid_to} is not after valid_from {self.valid_from}"
            )


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
