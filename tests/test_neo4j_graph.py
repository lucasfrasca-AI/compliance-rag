"""The Neo4j graph adapter and schema (issue #87), on a real server.

Against a local Neo4j Community server (CI: a service container), never Aura:
`NEO4J_TEST_URI` is a test-only variable, and the fixture refuses any host but
localhost because it wipes the database. Skipped without it locally; CI sets
`REQUIRE_NEO4J` so a missing server fails instead of skipping.

Raw Cypher appears here only to inspect what the adapter wrote, through
`cypher()`; the adapter's own reads go through the query builder.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, LiteralString, cast
from urllib.parse import urlparse

import pytest
from neo4j import AsyncDriver
from neo4j.exceptions import ConstraintError

from compliance_rag.adapters.neo4j_graph import (
    PERSISTED_LABELS,
    Bitemporal,
    GraphCounts,
    GraphWriteRefused,
    Neo4jGraph,
    NodeRef,
    Status,
    schema_statements,
    statement_name,
)
from compliance_rag.corpus.models import NO_END_DATE

NEO4J_TEST_URI = os.environ.get("NEO4J_TEST_URI", "")
NEO4J_TEST_PASSWORD = os.environ.get("NEO4J_TEST_PASSWORD", "testpassword")
REQUIRED = bool(os.environ.get("REQUIRE_NEO4J"))

needs_neo4j = pytest.mark.skipif(
    not NEO4J_TEST_URI and not REQUIRED,
    reason="NEO4J_TEST_URI not set — needs a local Neo4j server",
)

JAN, JUN, DEC = dt.date(2026, 1, 1), dt.date(2026, 6, 1), dt.date(2026, 12, 1)
OPEN = Bitemporal(valid_from=JAN, status=Status.IN_FORCE)
C1 = NodeRef("Clause", "c1")
V1 = NodeRef("InstrumentVersion", "act:v1")
ACT = NodeRef("Instrument", "act")


async def cypher(graph: Neo4jGraph, query: str) -> list[dict[str, Any]]:  # Any: raw rows
    """Inspect the database directly — tests only."""
    driver: AsyncDriver = graph._driver  # pyright: ignore[reportPrivateUsage]
    rows, _, _ = await driver.execute_query(cast("LiteralString", query))
    return [dict(r) for r in rows]


async def _schema_objects(graph: Neo4jGraph) -> dict[tuple[str, str], str]:
    """(kind, name) -> full definition, for every constraint and index. A
    uniqueness constraint and the index it owns share a name."""
    out: dict[tuple[str, str], str] = {}
    for kind, plural in (("CONSTRAINT", "CONSTRAINTS"), ("INDEX", "INDEXES")):
        for row in await cypher(graph, f"SHOW {plural} YIELD * RETURN *"):
            if row.get("type") != "LOOKUP":
                definition = str(row.get("createStatement"))  # not state/counters
                out[(kind, str(row["name"]))] = definition
    return out


async def _wipe(graph: Neo4jGraph) -> None:
    await cypher(graph, "MATCH (n) DETACH DELETE n")
    # Constraints first: dropping one drops the index it owns.
    for kind in ("CONSTRAINT", "INDEX"):
        for owner_kind, name in await _schema_objects(graph):
            if owner_kind == kind:
                await cypher(graph, f"DROP {kind} {name} IF EXISTS")


@pytest.fixture
async def graph() -> AsyncIterator[Neo4jGraph]:
    if not NEO4J_TEST_URI:
        pytest.fail("REQUIRE_NEO4J is set but NEO4J_TEST_URI is not")
    host = urlparse(NEO4J_TEST_URI).hostname
    assert host in {"localhost", "127.0.0.1"}, f"refusing to wipe a non-local graph ({host})"
    async with Neo4jGraph.connect(
        NEO4J_TEST_URI, username="neo4j", password=NEO4J_TEST_PASSWORD
    ) as g:
        await _wipe(g)
        yield g
        await _wipe(g)


# ---- schema, on a real server ---------------------------------------------


@needs_neo4j
async def test_the_schema_applies_idempotently(graph: Neo4jGraph) -> None:
    first = await graph.apply_schema()
    after_first = await _schema_objects(graph)
    second = await graph.apply_schema()
    assert await _schema_objects(graph) == after_first  # names and definitions
    assert (first.applied, first.unsupported) == (second.applied, second.unsupported)
    assert set(first.applied) <= {name for _, name in after_first}


@needs_neo4j
async def test_every_persisted_label_has_a_unique_stable_id(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    rows = await cypher(
        graph,
        "SHOW CONSTRAINTS YIELD type, labelsOrTypes, properties "
        "WHERE type = 'UNIQUENESS' RETURN labelsOrTypes[0] AS label, properties AS props",
    )
    assert {str(r["label"]) for r in rows if list(r["props"]) == ["stable_id"]} == PERSISTED_LABELS


@needs_neo4j
async def test_the_database_refuses_a_duplicate_stable_id(graph: Neo4jGraph) -> None:
    """The constraint itself, not only the writer's MERGE: a raw second
    CREATE with the same stable_id is refused by the server."""
    await graph.apply_schema()
    await cypher(graph, "CREATE (:Clause {stable_id: 'x:v1:1'})")
    with pytest.raises(ConstraintError):
        await cypher(graph, "CREATE (:Clause {stable_id: 'x:v1:1'})")


@needs_neo4j
async def test_statements_the_edition_refuses_are_named_not_hidden(graph: Neo4jGraph) -> None:
    """Community refuses property-existence constraints. Every refused
    statement is one of the file's existence constraints, reported by name;
    nothing else may fail. (On Aura/Enterprise the list is empty.)"""
    report = await graph.apply_schema()
    statements = schema_statements()
    existence = {statement_name(s) for s in statements if "IS NOT NULL" in s}
    assert set(report.unsupported) <= existence
    assert set(report.applied) | set(report.unsupported) == {statement_name(s) for s in statements}


# ---- the writer: bitemporal on every node, immutable after ---------------


@needs_neo4j
async def test_every_written_node_carries_all_four_bitemporal_properties(
    graph: Neo4jGraph,
) -> None:
    """The invariant the Community edition can't hold in schema, held by the
    writer on every edition."""
    await graph.apply_schema()
    for label in sorted(PERSISTED_LABELS):
        await graph.put_node(NodeRef(label, f"{label.lower()}-1"), OPEN)
    rows = await cypher(
        graph,
        "MATCH (n) WHERE n.valid_from IS NULL OR n.valid_to IS NULL "
        "OR n.status IS NULL OR n.recorded_at IS NULL RETURN count(n) AS missing",
    )
    assert rows[0]["missing"] == 0
    (row,) = await cypher(graph, "MATCH (n:Clause) RETURN n.valid_to AS valid_to")
    assert row["valid_to"].to_native() == NO_END_DATE  # a date, never null


@needs_neo4j
async def test_rewriting_identical_content_is_a_no_op(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    assert await graph.put_node(C1, OPEN, {"text_sha256": "ab"}) is True
    assert await graph.put_node(C1, OPEN, {"text_sha256": "ab"}) is False
    assert await graph.counts() == GraphCounts(nodes=1, edges=0)


@needs_neo4j
async def test_a_node_is_immutable(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node(C1, OPEN, {"text_sha256": "ab"})
    with pytest.raises(GraphWriteRefused, match="immutable"):
        await graph.put_node(C1, OPEN, {"text_sha256": "cd"})
    with pytest.raises(GraphWriteRefused, match="immutable"):
        await graph.put_node(
            C1, Bitemporal(valid_from=JUN, status=Status.IN_FORCE), {"text_sha256": "ab"}
        )


@needs_neo4j
async def test_retiring_sets_valid_to_once(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node(V1, OPEN)
    await graph.retire_node(V1, valid_to=JUN)
    await graph.retire_node(V1, valid_to=JUN)  # same date: no-op
    with pytest.raises(GraphWriteRefused, match="already retired"):
        await graph.retire_node(V1, valid_to=DEC)
    with pytest.raises(GraphWriteRefused, match="no InstrumentVersion"):
        await graph.retire_node(NodeRef("InstrumentVersion", "act:v9"), valid_to=JUN)
    # A loader re-running with the retired content is idempotent...
    retired = Bitemporal(valid_from=JAN, status=Status.IN_FORCE, valid_to=JUN)
    assert await graph.put_node(V1, retired) is False
    # ...and can't move the date again.
    with pytest.raises(GraphWriteRefused, match="already retired"):
        await graph.put_node(V1, Bitemporal(valid_from=JAN, status=Status.IN_FORCE, valid_to=DEC))


@needs_neo4j
async def test_concurrent_retires_cannot_both_win(graph: Neo4jGraph) -> None:
    """D-05: valid_to is set once. Two retires racing on one open node: one
    succeeds, the other is refused, and the stored date is the winner's."""
    await graph.apply_schema()
    for attempt in range(5):
        node = NodeRef("InstrumentVersion", f"act:race{attempt}")
        await graph.put_node(node, OPEN)
        results = await asyncio.gather(
            graph.retire_node(node, valid_to=JUN),
            graph.retire_node(node, valid_to=DEC),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, GraphWriteRefused)]
        assert len(refused) == 1, results
        winner = JUN if results[0] is None else DEC
        (row,) = await cypher(
            graph, f"MATCH (n {{stable_id: 'act:race{attempt}'}}) RETURN n.valid_to AS v"
        )
        assert row["v"].to_native() == winner


@needs_neo4j
async def test_a_failed_write_changes_nothing(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node(C1, OPEN, {"text_sha256": "ab"})
    with pytest.raises(GraphWriteRefused):
        await graph.put_node(C1, OPEN, {"text_sha256": "cd"})
    rows = await cypher(graph, "MATCH (n:Clause) RETURN n.text_sha256 AS h")
    assert [r["h"] for r in rows] == ["ab"]


@needs_neo4j
async def test_edges_join_existing_nodes_only_and_are_immutable(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node(V1, OPEN)
    await graph.put_node(ACT, OPEN)
    assert await graph.put_edge("VERSION_OF", V1, ACT) is True
    assert await graph.put_edge("VERSION_OF", V1, ACT) is False  # idempotent
    with pytest.raises(GraphWriteRefused, match="immutable"):
        await graph.put_edge("VERSION_OF", V1, ACT, {"kind": "partial"})
    with pytest.raises(GraphWriteRefused, match="does not exist"):
        await graph.put_edge("VERSION_OF", NodeRef("InstrumentVersion", "act:v2"), ACT)
    assert await graph.counts() == GraphCounts(nodes=2, edges=1)


# ---- no server needed -------------------------------------------------------


def _offline() -> Neo4jGraph:
    """A graph with no server: these refusals happen before any query runs."""
    return Neo4jGraph(cast("AsyncDriver", None), database="unused")


async def test_labels_and_edge_types_come_only_from_the_closed_sets() -> None:
    """Labels and relationship types are interpolated into Cypher (they can't
    be parameters), so anything outside the schema is refused before a query
    is built."""
    for bad in ("Organisation", "Clause) DETACH DELETE (n", ""):
        with pytest.raises(GraphWriteRefused):
            NodeRef(bad, "x")
    with pytest.raises(GraphWriteRefused):
        await _offline().put_edge("HANDLES", C1, C1)


async def test_the_writer_owns_the_bitemporal_and_id_properties() -> None:
    for key in ("valid_from", "valid_to", "status", "recorded_at", "stable_id", "Bad-Key"):
        with pytest.raises(GraphWriteRefused):
            await _offline().put_node(C1, OPEN, {key: "x"})


def test_valid_to_must_follow_valid_from() -> None:
    with pytest.raises(GraphWriteRefused, match="not after"):
        Bitemporal(valid_from=JUN, status=Status.IN_FORCE, valid_to=JAN)


def test_ci_never_holds_aura_credentials() -> None:
    """CI's graph is the service container. No workflow references the
    deployed graph's URI, credentials or any secret named for it (Aura holds
    public law only, D-09, and nothing in CI needs it)."""
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    deployed = re.compile(
        r"NEO4J_(URI|USERNAME|PASSWORD|INSTANCE)|secrets\.\w*(NEO4J|AURA)|neo4j\.io", re.IGNORECASE
    )
    for workflow in workflows.glob("*.yml"):
        assert not deployed.search(workflow.read_text(encoding="utf-8")), workflow.name


def test_no_cypher_outside_adapters() -> None:
    """CLAUDE.md: no Cypher outside `adapters/`."""
    src = Path(__file__).resolve().parents[1] / "src" / "compliance_rag"
    cypher_text = re.compile(
        r"\b(MATCH|MERGE|CREATE|DETACH DELETE)\s*\(|\bSHOW (CONSTRAINTS|INDEXES)\b"
    )
    offenders = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if "adapters" not in p.parts and cypher_text.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
