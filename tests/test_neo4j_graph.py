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
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, LiteralString, cast
from urllib.parse import urlparse

import pytest
from neo4j import AsyncDriver
from neo4j.exceptions import ConstraintError

from compliance_rag.adapters.graph_schema import EdgeWrite, EndReason, GraphSchemaRefused, NodeWrite
from compliance_rag.adapters.neo4j_graph import (
    PERSISTED_LABELS,
    Bitemporal,
    GraphCounts,
    GraphWriteRefused,
    Neo4jGraph,
    NodeRef,
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
OPEN = Bitemporal(valid_from=JAN)
C1 = NodeRef("Clause", "c1")
V1 = NodeRef("InstrumentVersion", "act:v1")
ACT = NodeRef("Instrument", "act")


async def cypher(graph: Neo4jGraph, query: str) -> list[dict[str, Any]]:  # Any: raw rows
    """Inspect the database directly — tests only."""
    driver: AsyncDriver = graph._driver  # pyright: ignore[reportPrivateUsage]
    rows, _, _ = await driver.execute_query(cast("LiteralString", query), database_="neo4j")
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
        NEO4J_TEST_URI, username="neo4j", password=NEO4J_TEST_PASSWORD, database="neo4j"
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
    statements = schema_statements()
    created = {statement_name(s) for s in statements if s.startswith("CREATE")}
    dropped = {statement_name(s) for s in statements if s.startswith("DROP")}
    present = {name for _, name in after_first}
    assert created - set(first.unsupported) <= present
    assert not dropped & present  # retired by ADR-0027 / ADR-0029


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
        "OR n.recorded_at IS NULL OR n.status IS NOT NULL RETURN count(n) AS missing",
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
        await graph.put_node(C1, Bitemporal(valid_from=JUN), {"text_sha256": "ab"})


@needs_neo4j
async def test_retiring_sets_valid_to_once(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node(V1, OPEN)
    await graph.retire_node(V1, valid_to=JUN, end_reason=EndReason.SUPERSEDED)
    await graph.retire_node(V1, valid_to=JUN, end_reason=EndReason.SUPERSEDED)  # same date: no-op
    with pytest.raises(GraphWriteRefused, match="already retired"):
        await graph.retire_node(V1, valid_to=DEC, end_reason=EndReason.SUPERSEDED)
    with pytest.raises(GraphWriteRefused, match="no InstrumentVersion"):
        await graph.retire_node(
            NodeRef("InstrumentVersion", "act:v9"), valid_to=JUN, end_reason=EndReason.SUPERSEDED
        )
    # A loader re-running with the retired content is idempotent...
    retired = Bitemporal(valid_from=JAN, valid_to=JUN, end_reason=EndReason.SUPERSEDED)
    assert await graph.put_node(V1, retired) is False
    # ...and can't move the date again.
    with pytest.raises(GraphWriteRefused, match="already retired"):
        await graph.put_node(
            V1, Bitemporal(valid_from=JAN, valid_to=DEC, end_reason=EndReason.SUPERSEDED)
        )


@needs_neo4j
async def test_a_retirement_records_why_and_cannot_change_it(graph: Neo4jGraph) -> None:
    """ADR-0029: `end_reason` is set with `valid_to`, once."""
    await graph.apply_schema()
    await graph.put_node(V1, OPEN)
    await graph.retire_node(V1, valid_to=JUN, end_reason=EndReason.REPEALED)
    (row,) = await cypher(graph, "MATCH (n:InstrumentVersion) RETURN n.end_reason AS why")
    assert row["why"] == "repealed"
    with pytest.raises(GraphWriteRefused, match="already retired"):
        await graph.retire_node(V1, valid_to=JUN, end_reason=EndReason.SUPERSEDED)


@needs_neo4j
async def test_an_open_node_stores_no_end_reason_and_no_status(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node(V1, OPEN)
    (row,) = await cypher(graph, "MATCH (n:InstrumentVersion) RETURN keys(n) AS keys")
    assert "end_reason" not in row["keys"]
    assert "status" not in row["keys"]


@needs_neo4j
async def test_an_ended_node_is_never_reopened(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node(V1, Bitemporal(valid_from=JAN, valid_to=JUN, end_reason=EndReason.EXPIRED))
    with pytest.raises(GraphWriteRefused, match="never reopened"):
        await graph.put_node(V1, OPEN)


@needs_neo4j
async def test_migration_moves_pre_adr_0029_nodes_to_derived_status(graph: Neo4jGraph) -> None:
    """Nodes written before ADR-0029: an open one loses `status` and gains
    nothing; a retired one keeps its dates and gains the `end_reason` its old
    status implies; nothing else changes. Idempotent."""
    await graph.apply_schema()
    await graph.put_node(V1, OPEN)
    await cypher(
        graph,
        "CREATE (:InstrumentVersion {stable_id: 'act:v0', valid_from: date('2025-01-01'), "
        "valid_to: date('2026-01-01'), status: 'in_force', recorded_at: datetime()}), "
        "(:Obligation {stable_id: 'OB-OLD', valid_from: date('2025-01-01'), "
        "valid_to: date('2026-01-01'), status: 'repealed', recorded_at: datetime()})",
    )
    await cypher(
        graph, "MATCH (n:InstrumentVersion {stable_id: 'act:v1'}) SET n.status = 'in_force'"
    )
    (before,) = await cypher(graph, "MATCH (n {stable_id: 'act:v1'}) RETURN properties(n) AS p")
    assert await graph.migrate_to_derived_status() == 3
    assert await graph.migrate_to_derived_status() == 0
    rows = await cypher(
        graph, "MATCH (n) RETURN n.stable_id AS id, n.end_reason AS why, keys(n) AS keys"
    )
    assert {r["id"]: r["why"] for r in rows} == {
        "act:v1": None,
        "act:v0": "superseded",
        "OB-OLD": "repealed",
    }
    assert not any("status" in r["keys"] for r in rows)
    (after,) = await cypher(graph, "MATCH (n {stable_id: 'act:v1'}) RETURN properties(n) AS p")
    assert dict(after["p"]) == {k: v for k, v in dict(before["p"]).items() if k != "status"}
    # A migrated retirement re-runs as a no-op, as any other does.
    retired = Bitemporal(
        valid_from=dt.date(2025, 1, 1), valid_to=JAN, end_reason=EndReason.SUPERSEDED
    )
    assert await graph.put_node(NodeRef("InstrumentVersion", "act:v0"), retired) is False


@needs_neo4j
async def test_migration_refuses_a_status_that_contradicts_the_dates(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_node(V1, OPEN)
    await cypher(graph, "MATCH (n:InstrumentVersion) SET n.status = 'repealed'")
    with pytest.raises(GraphWriteRefused, match="repealed but still open"):
        await graph.migrate_to_derived_status()
    (row,) = await cypher(graph, "MATCH (n:InstrumentVersion) RETURN n.status AS status")
    assert row["status"] == "repealed"  # nothing changed


@needs_neo4j
async def test_concurrent_retires_cannot_both_win(graph: Neo4jGraph) -> None:
    """D-05: valid_to is set once. Two retires racing on one open node: one
    succeeds, the other is refused, and the stored date is the winner's."""
    await graph.apply_schema()
    for attempt in range(5):
        node = NodeRef("InstrumentVersion", f"act:race{attempt}")
        await graph.put_node(node, OPEN)
        results = await asyncio.gather(
            graph.retire_node(node, valid_to=JUN, end_reason=EndReason.SUPERSEDED),
            graph.retire_node(node, valid_to=DEC, end_reason=EndReason.SUPERSEDED),
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
        with pytest.raises(GraphSchemaRefused):
            NodeRef(bad, "x")
    with pytest.raises(GraphSchemaRefused):
        await _offline().put_edge("HANDLES", C1, C1)


async def test_the_writer_owns_the_bitemporal_and_id_properties() -> None:
    for key in ("valid_from", "valid_to", "status", "recorded_at", "stable_id", "Bad-Key"):
        with pytest.raises(GraphWriteRefused):
            await _offline().put_node(C1, OPEN, {key: "x"})


def test_valid_to_must_follow_valid_from() -> None:
    with pytest.raises(GraphWriteRefused, match="not after"):
        Bitemporal(valid_from=JUN, valid_to=JAN, end_reason=EndReason.SUPERSEDED)


KEEPALIVE = "aura-keepalive.yml"


def test_ci_never_holds_aura_credentials() -> None:
    """CI's graph is the service container. No workflow but the keep-alive
    references the deployed graph's URI, credentials or any secret named for
    it (Aura holds public law only, D-09, and nothing in CI needs it)."""
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    deployed = re.compile(
        r"NEO4J_(URI|USERNAME|PASSWORD|INSTANCE)|secrets\.\w*(NEO4J|AURA)|neo4j\.io", re.IGNORECASE
    )
    for workflow in workflows.glob("*.yml"):
        if workflow.name != KEEPALIVE:
            assert not deployed.search(workflow.read_text(encoding="utf-8")), workflow.name


def test_the_keepalive_runs_no_repository_code_and_leaves_nothing_behind() -> None:
    """The one workflow holding Aura credentials checks nothing out, installs
    nothing, has no token permissions, and sends a single statement that
    writes (so it counts as activity however Aura measures it) and deletes
    what it wrote, in the same statement, touching only :KeepAlive."""
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / KEEPALIVE).read_text(
        encoding="utf-8"
    )
    assert "actions/checkout" not in text and "uv " not in text and "pip " not in text
    assert "permissions: {}" in text
    (statement,) = re.findall(r'"statement":"(.*?)"}\'', text)
    assert statement.startswith("MERGE (k:KeepAlive ")
    assert "DELETE k" in statement and "DETACH" not in statement
    assert re.findall(r"\((\w+):(\w+)", statement) == [("k", "KeepAlive")]  # nothing else matched
    assert '"queryType" *: *"rw"' in text  # fails unless Aura counted a write


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


# ---- the database is always named (Aura Free: it is the instance ID) -------


class _RecordingDriver:
    """Stands in for the driver: records the database every call names."""

    def __init__(self) -> None:
        self.databases: list[object] = []

    async def verify_connectivity(self) -> None: ...

    async def close(self) -> None: ...

    async def execute_query(self, query: object, **kwargs: object) -> tuple[list[Any], None, None]:
        self.databases.append(kwargs.get("database_"))
        return [{"c": 0}], None, None

    def session(self, **kwargs: object) -> _RecordingSession:
        self.databases.append(kwargs.get("database"))
        return _RecordingSession()


class _RecordingSession:
    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc: object) -> None: ...

    async def execute_write(self, work: object) -> bool:
        return True


async def test_every_query_names_the_configured_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Aura Free the database is the instance ID (`5b790f49`), not "neo4j":
    a call that relied on the driver's default database would address one
    that doesn't exist. `from_settings` carries NEO4J_DATABASE through, and
    every query names it."""
    from neo4j import AsyncGraphDatabase

    from compliance_rag.settings import Settings

    driver = _RecordingDriver()

    def fake_driver(*args: object, **kwargs: object) -> _RecordingDriver:
        return driver

    monkeypatch.setattr(AsyncGraphDatabase, "driver", fake_driver)
    settings = Settings(
        neo4j_uri="neo4j+s://5b790f49.databases.neo4j.io",
        neo4j_username="5b790f49",
        neo4j_database="5b790f49",
    )
    async with Neo4jGraph.from_settings(settings) as graph:
        await graph.counts()  # reads: execute_query
        await graph.put_node(C1, OPEN)  # writes: a session
    assert driver.databases == ["5b790f49", "5b790f49", "5b790f49"]


@pytest.mark.parametrize("missing", ["neo4j_uri", "neo4j_username", "neo4j_database"])
async def test_a_missing_connection_value_fails_loudly(missing: str) -> None:
    from compliance_rag.settings import Settings

    complete = Settings(
        neo4j_uri="neo4j+s://x.databases.neo4j.io", neo4j_username="x", neo4j_database="x"
    )
    settings = complete.model_copy(update={missing: ""})
    with pytest.raises(ValueError, match="all required"):
        async with Neo4jGraph.from_settings(settings):
            pass


def test_the_settings_database_has_no_neo4j_default() -> None:
    from compliance_rag.settings import Settings

    assert Settings.model_fields["neo4j_database"].default == ""


# ---- batched writes (issue #108) ---------------------------------------------


def _clauses_batch(n: int) -> list[NodeWrite]:
    return [NodeWrite(NodeRef("Clause", f"c{i}"), OPEN, {"text_sha256": f"h{i}"}) for i in range(n)]


@needs_neo4j
async def test_a_batch_spanning_several_transactions_writes_every_node_once(
    graph: Neo4jGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    import compliance_rag.adapters.neo4j_graph as module

    monkeypatch.setattr(module, "BATCH_SIZE", 7)
    await graph.apply_schema()
    assert await graph.put_nodes(_clauses_batch(30)) == 30
    assert await graph.put_nodes(_clauses_batch(30)) == 0  # idempotent
    rows = await cypher(
        graph,
        "MATCH (n:Clause) WHERE n.valid_from IS NULL OR n.valid_to IS NULL "
        "OR n.recorded_at IS NULL OR n.status IS NOT NULL RETURN count(n) AS missing",
    )
    assert rows[0]["missing"] == 0
    assert await graph.counts() == GraphCounts(nodes=30, edges=0)


@needs_neo4j
async def test_a_changed_node_refuses_its_whole_batch(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_nodes(_clauses_batch(3))
    batch = [*_clauses_batch(3), NodeWrite(NodeRef("Clause", "new"), OPEN)]
    batch[1] = NodeWrite(NodeRef("Clause", "c1"), OPEN, {"text_sha256": "changed"})
    with pytest.raises(GraphWriteRefused, match="immutable"):
        await graph.put_nodes(batch)
    assert await graph.counts() == GraphCounts(nodes=3, edges=0)  # "new" rolled back too


@needs_neo4j
async def test_a_batch_can_retire_and_cannot_move_a_date(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_nodes([NodeWrite(V1, OPEN)])
    retired = Bitemporal(valid_from=JAN, valid_to=JUN, end_reason=EndReason.SUPERSEDED)
    assert await graph.put_nodes([NodeWrite(V1, retired)]) == 0
    (row,) = await cypher(graph, "MATCH (n:InstrumentVersion) RETURN n.valid_to AS v")
    assert row["v"].to_native() == JUN
    with pytest.raises(GraphWriteRefused, match="already retired"):
        await graph.put_nodes(
            [
                NodeWrite(
                    V1, Bitemporal(valid_from=JAN, valid_to=DEC, end_reason=EndReason.SUPERSEDED)
                )
            ]
        )


async def test_a_batch_naming_one_node_twice_is_refused() -> None:
    with pytest.raises(GraphWriteRefused, match="twice"):
        await _offline().put_nodes([NodeWrite(C1, OPEN), NodeWrite(C1, OPEN)])


@needs_neo4j
async def test_an_edge_batch_with_a_missing_end_writes_nothing(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await graph.put_nodes([NodeWrite(V1, OPEN), *_clauses_batch(3)])
    edges = [EdgeWrite("PART_OF", NodeRef("Clause", f"c{i}"), V1) for i in range(3)]
    with pytest.raises(GraphWriteRefused, match="does not exist"):
        await graph.put_edges([*edges, EdgeWrite("PART_OF", NodeRef("Clause", "ghost"), V1)])
    assert (await graph.counts()).edges == 0
    assert await graph.put_edges(edges) == 3
    assert await graph.put_edges(edges) == 0  # idempotent
    with pytest.raises(GraphWriteRefused, match="immutable"):
        await graph.put_edges([EdgeWrite("PART_OF", edges[0].source, V1, {"kind": "x"})])


async def test_a_batch_naming_one_edge_twice_is_refused_not_collapsed() -> None:
    """Two PART_OF c0->v1 with different properties: which wins? Neither — the
    batch is refused (the review found it silently kept the last)."""
    source = NodeRef("Clause", "c0")
    with pytest.raises(GraphWriteRefused, match="same edge twice"):
        await _offline().put_edges(
            [
                EdgeWrite("PART_OF", source, V1, {"kind": "a"}),
                EdgeWrite("PART_OF", source, V1, {"kind": "b"}),
            ]
        )


@needs_neo4j
async def test_stored_returns_named_nodes_with_their_edges_at_no_date(graph: Neo4jGraph) -> None:
    """The loader's integrity read: properties and outgoing edges whatever
    their dates; a node that isn't there is simply absent."""
    await graph.apply_schema()
    await graph.put_node(
        V1, Bitemporal(valid_from=JAN, valid_to=JUN, end_reason=EndReason.SUPERSEDED)
    )
    await graph.put_node(ACT, OPEN, {"title": "Act"})
    await graph.put_edge("VERSION_OF", V1, ACT, {"note": "n"})
    found = await graph.stored([V1, ACT, NodeRef("Clause", "missing")])
    assert set(found) == {V1, ACT}
    assert found[V1].properties["end_reason"] == "superseded"
    assert found[V1].targets("VERSION_OF") == {ACT}
    assert dict(found[V1].edges[0].properties) == {"note": "n"}
    assert found[ACT].edges == ()


# ---- server notifications (#141) ------------------------------------------


@needs_neo4j
async def test_reading_a_kind_of_data_not_yet_loaded_logs_no_warning(
    graph: Neo4jGraph, caplog: pytest.LogCaptureFixture
) -> None:
    """A label or relationship type with no data yet is "unrecognised" to the
    server, which warned on every Track A read on Aura (no `REQUIRES` edge).
    Neo4j keeps a type's token once any data has used it, even after the data
    is gone, so a long-lived test database already knows every type the
    builder names: the query uses names it has never seen, through the same
    driver."""
    caplog.set_level(logging.DEBUG, logger="neo4j.notifications")
    unseen = uuid.uuid4().hex[:8].upper()
    await cypher(graph, f"MATCH (a:UNSEEN_{unseen})-[:UNSEEN_{unseen}]->(b) RETURN b LIMIT 1")
    assert [r.getMessage() for r in caplog.records if r.name == "neo4j.notifications"] == []


@needs_neo4j
async def test_other_notifications_still_reach_the_log(
    graph: Neo4jGraph, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="neo4j.notifications")
    await cypher(graph, "MATCH (n) RETURN id(n) LIMIT 1")  # id() is deprecated in 5.x
    logged = [r.getMessage() for r in caplog.records if r.name == "neo4j.notifications"]
    assert any("Deprecat" in m for m in logged), logged
