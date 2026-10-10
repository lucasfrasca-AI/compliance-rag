"""The Neo4j graph adapter and schema (issue #87), on a real server.

Against a local Neo4j Community server (CI: a service container), never Aura:
`NEO4J_TEST_URI` is a test-only variable, and the fixture refuses any host but
localhost because it wipes the database. Skipped without it locally; CI sets
`REQUIRE_NEO4J` so a missing server fails instead of skipping.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import LiteralString, cast
from urllib.parse import urlparse

import pytest
from neo4j import AsyncDriver
from neo4j.exceptions import ConstraintError

from compliance_rag.adapters.neo4j_graph import (
    PERSISTED_LABELS,
    Bitemporal,
    GraphWriteRefused,
    Neo4jGraph,
    Status,
    schema_statements,
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


def _show(kind: str) -> LiteralString:
    return cast("LiteralString", f"SHOW {kind} YIELD name, type RETURN name, type")


async def _wipe(graph: Neo4jGraph) -> None:
    driver = graph._driver  # pyright: ignore[reportPrivateUsage] — test teardown only
    await driver.execute_query("MATCH (n) DETACH DELETE n")
    for kind in ("CONSTRAINTS", "INDEXES"):
        rows, _, _ = await driver.execute_query(_show(kind))
        for row in rows:
            if row["type"] != "LOOKUP":
                drop = "CONSTRAINT" if kind == "CONSTRAINTS" else "INDEX"
                await driver.execute_query(
                    cast("LiteralString", f"DROP {drop} {row['name']} IF EXISTS")
                )


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


async def _schema(graph: Neo4jGraph) -> set[str]:
    driver = graph._driver  # pyright: ignore[reportPrivateUsage]
    names: set[str] = set()
    for kind in ("CONSTRAINTS", "INDEXES"):
        rows, _, _ = await driver.execute_query(_show(kind))
        names |= {str(r["name"]) for r in rows if r["type"] != "LOOKUP"}
    return names


# ---- schema, on a real server ---------------------------------------------


@needs_neo4j
async def test_the_schema_applies_idempotently(graph: Neo4jGraph) -> None:
    first = await graph.apply_schema()
    after_first = await _schema(graph)
    second = await graph.apply_schema()
    assert await _schema(graph) == after_first
    assert (first.applied, first.unsupported) == (second.applied, second.unsupported)
    assert set(first.applied) <= after_first


@needs_neo4j
async def test_every_persisted_label_has_a_unique_stable_id(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    driver = graph._driver  # pyright: ignore[reportPrivateUsage]
    rows, _, _ = await driver.execute_query(
        "SHOW CONSTRAINTS YIELD type, labelsOrTypes, properties "
        "WHERE type = 'UNIQUENESS' RETURN labelsOrTypes[0] AS label, properties AS props"
    )
    unique = {str(r["label"]) for r in rows if list(r["props"]) == ["stable_id"]}
    assert unique == PERSISTED_LABELS


@needs_neo4j
async def test_the_database_refuses_a_duplicate_stable_id(graph: Neo4jGraph) -> None:
    """The constraint itself, not only the writer's MERGE: a raw second
    CREATE with the same stable_id is refused by the server."""
    await graph.apply_schema()
    driver = graph._driver  # pyright: ignore[reportPrivateUsage]
    await driver.execute_query("CREATE (:Clause {stable_id: 'x:v1:1'})")
    with pytest.raises(ConstraintError):
        await driver.execute_query("CREATE (:Clause {stable_id: 'x:v1:1'})")


@needs_neo4j
async def test_statements_the_edition_refuses_are_named_not_hidden(graph: Neo4jGraph) -> None:
    """Community refuses property-existence constraints. Every refused
    statement is one of the file's existence constraints, reported by name;
    nothing else may fail. (On Aura/Enterprise the list is empty.)"""
    report = await graph.apply_schema()
    existence = {
        re.match(r"CREATE CONSTRAINT (\w+)", s).group(1)  # type: ignore[union-attr]
        for s in schema_statements()
        if "IS NOT NULL" in s
    }
    assert set(report.unsupported) <= existence
    assert set(report.applied) | set(report.unsupported) == {
        re.match(r"CREATE (?:CONSTRAINT|INDEX) (\w+)", s).group(1)  # type: ignore[union-attr]
        for s in schema_statements()
    }


# ---- the writer: bitemporal on every node, immutable after ---------------


@needs_neo4j
async def test_every_written_node_carries_all_four_bitemporal_properties(
    graph: Neo4jGraph,
) -> None:
    """The invariant the Community edition can't hold in schema, held by the
    writer on every edition."""
    await graph.apply_schema()
    for label in sorted(PERSISTED_LABELS):
        await graph.put_node(label, f"{label.lower()}-1", OPEN)
    driver = graph._driver  # pyright: ignore[reportPrivateUsage]
    rows, _, _ = await driver.execute_query(
        "MATCH (n) WHERE n.valid_from IS NULL OR n.valid_to IS NULL "
        "OR n.status IS NULL OR n.recorded_at IS NULL RETURN count(n) AS missing"
    )
    assert rows[0]["missing"] == 0
    rows, _, _ = await driver.execute_query(
        "MATCH (n:Clause) RETURN n.valid_to AS valid_to, n.recorded_at AS recorded_at"
    )
    assert rows[0]["valid_to"].to_native() == NO_END_DATE  # a date, never null
    assert rows[0]["recorded_at"] is not None


@needs_neo4j
async def test_rewriting_identical_content_is_a_no_op(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    assert await graph.put_node("Clause", "c1", OPEN, {"text_sha256": "ab"}) is True
    assert await graph.put_node("Clause", "c1", OPEN, {"text_sha256": "ab"}) is False
    assert await graph.counts() == (1, 0)


@needs_neo4j
async def test_a_node_is_immutable(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node("Clause", "c1", OPEN, {"text_sha256": "ab"})
    with pytest.raises(GraphWriteRefused, match="immutable"):
        await graph.put_node("Clause", "c1", OPEN, {"text_sha256": "cd"})
    with pytest.raises(GraphWriteRefused, match="immutable"):
        await graph.put_node(
            "Clause",
            "c1",
            Bitemporal(valid_from=JUN, status=Status.IN_FORCE),
            {"text_sha256": "ab"},
        )


@needs_neo4j
async def test_retiring_sets_valid_to_once(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node("InstrumentVersion", "act:v1", OPEN)
    await graph.retire_node("InstrumentVersion", "act:v1", valid_to=JUN)
    await graph.retire_node("InstrumentVersion", "act:v1", valid_to=JUN)  # same date: no-op
    with pytest.raises(GraphWriteRefused, match="already retired"):
        await graph.retire_node("InstrumentVersion", "act:v1", valid_to=DEC)
    with pytest.raises(GraphWriteRefused, match="no InstrumentVersion"):
        await graph.retire_node("InstrumentVersion", "act:v9", valid_to=JUN)
    # A loader re-running with the retired content is idempotent.
    retired = Bitemporal(valid_from=JAN, status=Status.IN_FORCE, valid_to=JUN)
    assert await graph.put_node("InstrumentVersion", "act:v1", retired) is False


@needs_neo4j
async def test_a_failed_write_changes_nothing(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node("Clause", "c1", OPEN, {"text_sha256": "ab"})
    with pytest.raises(GraphWriteRefused):
        await graph.put_node("Clause", "c1", OPEN, {"text_sha256": "cd"})
    driver = graph._driver  # pyright: ignore[reportPrivateUsage]
    rows, _, _ = await driver.execute_query("MATCH (n:Clause) RETURN n.text_sha256 AS h")
    assert [r["h"] for r in rows] == ["ab"]


@needs_neo4j
async def test_edges_join_existing_nodes_only(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node("InstrumentVersion", "act:v1", OPEN)
    await graph.put_node("Instrument", "act", OPEN)
    edge = ("InstrumentVersion", "act:v1"), ("Instrument", "act")
    assert await graph.put_edge("VERSION_OF", *edge) is True
    assert await graph.put_edge("VERSION_OF", *edge) is False  # idempotent
    with pytest.raises(GraphWriteRefused, match="does not exist"):
        await graph.put_edge("VERSION_OF", ("InstrumentVersion", "act:v2"), ("Instrument", "act"))
    assert await graph.counts() == (2, 1)


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
            await _offline().put_node(bad, "x", OPEN)
    with pytest.raises(GraphWriteRefused):
        await _offline().put_edge("HANDLES", ("Clause", "a"), ("Clause", "b"))


async def test_the_writer_owns_the_bitemporal_and_id_properties() -> None:
    for key in ("valid_from", "valid_to", "status", "recorded_at", "stable_id", "Bad-Key"):
        with pytest.raises(GraphWriteRefused):
            await _offline().put_node("Clause", "x", OPEN, {key: "x"})


def test_valid_to_must_follow_valid_from() -> None:
    with pytest.raises(ValueError, match="not after"):
        Bitemporal(valid_from=JUN, status=Status.IN_FORCE, valid_to=JAN)


def test_ci_never_holds_aura_credentials() -> None:
    """CI's graph is the service container. No workflow references the
    deployed graph's URI or credentials (Aura holds public law only, D-09,
    and nothing in CI needs it)."""
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for workflow in workflows.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        assert "NEO4J_URI" not in text and "NEO4J_PASSWORD:" not in text, workflow.name
        assert "neo4j.io" not in text, workflow.name


def test_no_cypher_outside_adapters() -> None:
    """CLAUDE.md: no Cypher outside `adapters/`."""
    src = Path(__file__).resolve().parents[1] / "src" / "compliance_rag"
    cypher = re.compile(r"\b(MATCH|MERGE)\s*\(")
    offenders = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if "adapters" not in p.parts and cypher.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
