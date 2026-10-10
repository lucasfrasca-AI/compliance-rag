"""The graph's closed vocabulary (PHASES.md Phase 2): the labels a node may
carry and the edge types it may have. Labels and edge types are interpolated
into Cypher — they can't be parameters — so both the writer (`neo4j_graph`)
and the query builder (`graph_query`) take them only from these sets.
"""

from __future__ import annotations

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
