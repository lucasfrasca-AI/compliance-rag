"""The Neo4j graph (issue #87, Phase 2): schema, and the only writes.

D-05's bitemporal rules are enforced here, on every edition, not left to the
database: every node is written with `valid_from`, `valid_to`, `status` and
`recorded_at`; `valid_to` is a date, never null (the `NO_END_DATE` sentinel,
CONTEXT.md: Bitemporal); a node, once written, is immutable except for one
transition — `valid_to` from `NO_END_DATE` to a real date, which is how a
superseded node is retired. That transition takes the node's write lock
before reading `valid_to`, so two concurrent retires serialise and can't both
win. Re-writing a node or edge with identical
content is a no-op, so loaders are idempotent on stable ID.

`status` is the node's own content (in force, not yet commenced, repealed as
enacted) and is immutable like the rest; whether a node is in force *on a
date* is always the `valid_from <= as_of < valid_to` range, which retiring
changes.

Labels and relationship types can't be Cypher parameters, so they are taken
only from the closed sets below; every value is a bound parameter.

Reads go through the query builder (issue #88), which requires `as_of` and a
depth cap. `counts()` is the one exception, and it reads no node: whole-graph
totals for the Aura capacity check (PHASES.md Phase 2 exit).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import LiteralString, cast

from neo4j import (
    AsyncDriver,
    AsyncGraphDatabase,
    AsyncManagedTransaction,
    Record,
    RoutingControl,
)
from neo4j.exceptions import Neo4jError
from neo4j.time import Date, DateTime

from compliance_rag import telemetry
from compliance_rag.adapters.graph_query import BuiltQuery
from compliance_rag.adapters.graph_schema import (
    EDGE_TYPES,
    PERSISTED_LABELS,
    Bitemporal,
    EdgeWrite,
    GraphCounts,
    GraphRow,
    GraphWriteRefused,
    NodeRef,
    NodeWrite,
    Scalar,
    Status,
    checked_edge_type,
    checked_label,
)
from compliance_rag.adapters.interfaces import GraphAdmin
from compliance_rag.corpus.models import NO_END_DATE
from compliance_rag.settings import Settings

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "db" / "neo4j" / "schema.cypher"

BITEMPORAL_KEYS = frozenset({"valid_from", "valid_to", "status", "recorded_at"})

BATCH_SIZE = 500
"""Rows per batched write transaction: large enough that loading a
compilation is a handful of round trips to Aura, small enough that a refused
row rolls back a bounded amount of work."""

EDITION_REFUSAL = "Neo.DatabaseError.Schema.ConstraintCreationFailed"
"""The code Community returns for a property-existence constraint, with a
message naming Enterprise Edition. Both must match for a refusal to be
reported as an edition limit rather than raised."""

_PROPERTY_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class SchemaReport:
    applied: tuple[str, ...]
    """Every statement the server accepted (idempotent: `IF NOT EXISTS`)."""
    unsupported: tuple[str, ...]
    """Constraint names this edition refuses (property existence on
    Community). The writer enforces the same invariant instead."""


def schema_statements(path: Path = SCHEMA_PATH) -> tuple[str, ...]:
    """The schema file's statements, comments stripped, in file order."""
    lines = path.read_text(encoding="utf-8").splitlines()
    body = "\n".join(line for line in lines if not line.lstrip().startswith("//"))
    return tuple(s.strip() for s in body.split(";") if s.strip())


def statement_name(statement: str) -> str:
    match = re.match(r"CREATE (?:CONSTRAINT|INDEX) (\w+)", statement)
    if match is None:
        raise ValueError(f"schema statement without a name: {statement[:60]!r}")
    return match.group(1)


def _is_edition_refusal(statement: str, exc: Neo4jError) -> bool:
    return (
        statement.startswith("CREATE CONSTRAINT")
        and "IS NOT NULL" in statement
        and exc.code == EDITION_REFUSAL
        and "Enterprise Edition" in (exc.message or "")
    )


def _trusted(query: str) -> LiteralString:
    """The driver types queries as `LiteralString` to keep values out of
    Cypher text. The only non-literal pieces interpolated in this module are
    labels and edge types that passed `checked_label`/`checked_edge_type`
    (closed sets) and statements read from the checked-in schema file; every
    value is a bound parameter."""
    return cast("LiteralString", query)


def _properties(properties: Mapping[str, Scalar]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in properties.items():
        if not _PROPERTY_KEY.match(key):
            raise GraphWriteRefused(f"property key {key!r} is not snake_case")
        if key in BITEMPORAL_KEYS or key == "stable_id":
            raise GraphWriteRefused(f"{key!r} is set by the writer, not passed as a property")
        out[key] = list(value) if isinstance(value, tuple) else value
    return out


def to_python(value: object) -> object:
    """Neo4j temporal types and lists back to the standard library."""
    if isinstance(value, Date | DateTime):
        return value.to_native()
    if isinstance(value, list):
        return tuple(to_python(v) for v in cast("list[object]", value))
    return value


def _stored_from(record: Record, key: str) -> dict[str, object]:
    return {k: to_python(v) for k, v in cast("dict[str, object]", record[key]).items()}


def _stored(record: Record) -> dict[str, object]:
    return _stored_from(record, "stored")


def _differing(
    stored: Mapping[str, object], wanted: Mapping[str, object], *, ignore: frozenset[str]
) -> set[str]:
    return {
        k
        for k in set(stored) | set(wanted)
        if k not in ignore and stored.get(k) != to_python(wanted.get(k))
    }


def _refuse_if_changed(
    node: NodeRef, stored: Mapping[str, object], wanted: Mapping[str, object]
) -> None:
    """A node is immutable except `valid_to`, which only `_retire` changes."""
    differs = _differing(stored, wanted, ignore=frozenset({"stable_id", "recorded_at", "valid_to"}))
    if differs:
        raise GraphWriteRefused(
            f"{node.label} {node.stable_id!r} is immutable: {sorted(differs)} differ from what "
            "is stored. An amendment is a new node with SUPERSEDES (D-05)"
        )


class Neo4jGraph(GraphAdmin):
    def __init__(
        self,
        driver: AsyncDriver,
        *,
        database: str,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self._driver = driver
        self._database = database
        self._clock = clock

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, uri: str, *, username: str, password: str, database: str
    ) -> AsyncGenerator[Neo4jGraph]:
        """`database` has no default: on Aura Free it is the instance ID, not
        "neo4j", and every query names it explicitly."""
        if not (uri and username and database):
            raise ValueError("Neo4j uri, username and database are all required")
        driver = AsyncGraphDatabase.driver(uri, auth=(username, password))
        try:
            await driver.verify_connectivity()
            yield cls(driver, database=database)
        finally:
            await driver.close()

    @classmethod
    def from_settings(cls, settings: Settings) -> AbstractAsyncContextManager[Neo4jGraph]:
        """The deployed graph (Aura Free: public law only, D-09). Tests never
        use this: they connect to a local server by URI. All five values come
        from the credentials file Aura downloads; on Aura Free the username
        and database are both the instance ID."""
        return cls.connect(
            settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password.get_secret_value(),
            database=settings.neo4j_database,
        )

    async def _write[T](
        self,
        span: str,
        attributes: Mapping[str, str],
        work: Callable[[AsyncManagedTransaction], Awaitable[T]],
    ) -> T:
        """One write transaction in one span: committed whole or not at all."""
        with telemetry.span(span, **attributes) as current:
            async with self._driver.session(database=self._database) as session:
                result = await session.execute_write(work)
            current.set_attribute("graph.outcome", str(result))
            return result

    # ---- schema -------------------------------------------------------------

    async def apply_schema(self, path: Path = SCHEMA_PATH) -> SchemaReport:
        applied: list[str] = []
        unsupported: list[str] = []
        with telemetry.span("neo4j_graph.apply_schema", **{"graph.schema": path.name}) as s:
            for statement in schema_statements(path):
                name = statement_name(statement)
                try:
                    await self._driver.execute_query(_trusted(statement), database_=self._database)
                except Neo4jError as exc:
                    if _is_edition_refusal(statement, exc):
                        unsupported.append(name)
                        continue
                    raise
                applied.append(name)
            s.set_attribute("graph.schema_applied", len(applied))
            s.set_attribute("graph.schema_unsupported", unsupported)
        return SchemaReport(applied=tuple(applied), unsupported=tuple(unsupported))

    # ---- writes -------------------------------------------------------------

    async def put_node(
        self, node: NodeRef, bitemporal: Bitemporal, properties: Mapping[str, Scalar] | None = None
    ) -> bool:
        """Write one immutable node. True if created, False if an identical
        node already existed. Different content under the same stable ID is
        refused, except retiring it (`valid_to` from `NO_END_DATE` to a date),
        which is applied."""
        props: dict[str, object] = {
            **_properties(properties or {}),
            "valid_from": bitemporal.valid_from,
            "valid_to": bitemporal.valid_to,
            "status": bitemporal.status.value,
        }

        async def work(tx: AsyncManagedTransaction) -> bool:
            created, stored = await self._merge_node(tx, node, props)
            if created:
                return True
            _refuse_if_changed(node, stored, props)
            if stored["valid_to"] != bitemporal.valid_to:
                await self._retire(tx, node, bitemporal.valid_to)
            return False

        attributes = {"graph.label": node.label, "graph.stable_id": node.stable_id}
        return await self._write("neo4j_graph.put_node", attributes, work)

    async def _merge_node(
        self, tx: AsyncManagedTransaction, node: NodeRef, props: Mapping[str, object]
    ) -> tuple[bool, dict[str, object]]:
        query = (
            f"MERGE (n:{node.label} {{stable_id: $stable_id}}) "
            "ON CREATE SET n += $props, n.recorded_at = $now, n._created = true "
            "WITH n, coalesce(n._created, false) AS created REMOVE n._created "
            "RETURN created, properties(n) AS stored"
        )
        result = await tx.run(
            _trusted(query), stable_id=node.stable_id, props=props, now=self._clock()
        )
        record = await result.single(strict=True)
        return bool(record["created"]), _stored(record)

    async def put_nodes(self, writes: Sequence[NodeWrite]) -> int:
        """`put_node` for many nodes in `BATCH_SIZE`-row transactions, one
        `UNWIND` statement each (issue #108: 780 single-node transactions to
        offshore Aura took 14.5 minutes). Every guarantee holds per row: a
        changed node is refused and its whole batch rolls back; a retirement
        goes through `_retire`'s lock-then-read. Returns the number created."""
        ids = [(w.node.label, w.node.stable_id) for w in writes]
        if len(ids) != len(set(ids)):
            raise GraphWriteRefused("a batch names the same node twice")
        created = 0
        for label in sorted({w.node.label for w in writes}):
            same_label = [w for w in writes if w.node.label == label]
            for start in range(0, len(same_label), BATCH_SIZE):
                created += await self._put_node_batch(label, same_label[start : start + BATCH_SIZE])
        return created

    async def _put_node_batch(self, label: str, batch: Sequence[NodeWrite]) -> int:
        props: list[dict[str, object]] = [
            {
                **_properties(w.properties),
                "valid_from": w.bitemporal.valid_from,
                "valid_to": w.bitemporal.valid_to,
                "status": w.bitemporal.status.value,
            }
            for w in batch
        ]
        rows = [
            {"stable_id": w.node.stable_id, "props": p} for w, p in zip(batch, props, strict=True)
        ]
        query = (
            f"UNWIND $rows AS row MERGE (n:{checked_label(label)} {{stable_id: row.stable_id}}) "
            "ON CREATE SET n += row.props, n.recorded_at = $now, n._created = true "
            "WITH n, coalesce(n._created, false) AS created REMOVE n._created "
            "RETURN n.stable_id AS stable_id, created, properties(n) AS stored"
        )
        wanted = {w.node.stable_id: (w, p) for w, p in zip(batch, props, strict=True)}

        async def work(tx: AsyncManagedTransaction) -> int:
            result = await tx.run(_trusted(query), rows=rows, now=self._clock())
            created = 0
            async for record in result:
                write, wanted_props = wanted[str(record["stable_id"])]
                if record["created"]:
                    created += 1
                    continue
                stored = _stored(record)
                _refuse_if_changed(write.node, stored, wanted_props)
                if stored["valid_to"] != write.bitemporal.valid_to:
                    await self._retire(tx, write.node, write.bitemporal.valid_to)
            return created

        attributes = {"graph.label": label, "graph.batch": str(len(batch))}
        return await self._write("neo4j_graph.put_nodes", attributes, work)

    async def put_edges(self, writes: Sequence[EdgeWrite]) -> int:
        """`put_edge` for many edges in `BATCH_SIZE`-row transactions. Both
        ends of every edge in a batch are checked before any is merged, so a
        missing end refuses the batch with nothing written. Returns the
        number created."""
        created = 0
        shapes = sorted({(w.edge_type, w.source.label, w.target.label) for w in writes})
        for edge_type, source_label, target_label in shapes:
            same = [
                w
                for w in writes
                if (w.edge_type, w.source.label, w.target.label)
                == (edge_type, source_label, target_label)
            ]
            for start in range(0, len(same), BATCH_SIZE):
                created += await self._put_edge_batch(
                    edge_type, source_label, target_label, same[start : start + BATCH_SIZE]
                )
        return created

    async def _put_edge_batch(
        self, edge_type: str, source_label: str, target_label: str, batch: Sequence[EdgeWrite]
    ) -> int:
        wanted: dict[tuple[str, str], dict[str, object]] = {
            (w.source.stable_id, w.target.stable_id): _properties(w.properties) for w in batch
        }
        rows = [{"source": s, "target": t, "props": p} for (s, t), p in wanted.items()]
        ends = (
            f"UNWIND $rows AS row "
            f"OPTIONAL MATCH (a:{checked_label(source_label)} {{stable_id: row.source}}) "
            f"OPTIONAL MATCH (b:{checked_label(target_label)} {{stable_id: row.target}}) "
        )
        missing_query = ends + (
            "WITH row, a, b WHERE a IS NULL OR b IS NULL "
            "RETURN row.source AS source, row.target AS target"
        )
        merge_query = (
            ends + f"MERGE (a)-[r:{checked_edge_type(edge_type)}]->(b) "
            "ON CREATE SET r += row.props, r._created = true "
            "WITH row, r, coalesce(r._created, false) AS created REMOVE r._created "
            "RETURN row.source AS source, row.target AS target, created, properties(r) AS stored"
        )

        async def work(tx: AsyncManagedTransaction) -> int:
            missing = [record async for record in await tx.run(_trusted(missing_query), rows=rows)]
            if missing:
                first = missing[0]
                raise GraphWriteRefused(
                    f"{edge_type}: {len(missing)} edge(s) name a node that does not exist "
                    f"(first: {first['source']!r} -> {first['target']!r})"
                )
            created = 0
            async for record in await tx.run(_trusted(merge_query), rows=rows):
                key = (str(record["source"]), str(record["target"]))
                if record["created"]:
                    created += 1
                elif _differing(_stored(record), wanted[key], ignore=frozenset()):
                    raise GraphWriteRefused(f"{edge_type} {key[0]!r}->{key[1]!r} is immutable")
            return created

        attributes = {"graph.edge_type": edge_type, "graph.batch": str(len(batch))}
        return await self._write("neo4j_graph.put_edges", attributes, work)

    async def retire_node(self, node: NodeRef, *, valid_to: dt.date) -> None:
        """Set `valid_to` on a node still open (`NO_END_DATE`) — the one
        permitted change. Retiring again to the same date is a no-op."""

        async def work(tx: AsyncManagedTransaction) -> None:
            await self._retire(tx, node, valid_to)

        attributes = {
            "graph.label": node.label,
            "graph.stable_id": node.stable_id,
            "graph.valid_to": valid_to.isoformat(),
        }
        await self._write("neo4j_graph.retire_node", attributes, work)

    async def _retire(self, tx: AsyncManagedTransaction, node: NodeRef, valid_to: dt.date) -> None:
        """Lock, then read, then set — in one transaction. The first statement
        is a real write, so it takes the node's write lock; a concurrent retire
        blocks there until this one commits, and its read then sees this
        one's date and is refused. (A no-op `SET n.p = n.p` takes no lock, and
        a read-then-conditional-SET in one statement loses the race.)"""
        match = f"MATCH (n:{node.label} {{stable_id: $stable_id}}) "
        locked = await tx.run(
            _trusted(match + "SET n._lock = true RETURN count(n) AS found"),
            stable_id=node.stable_id,
        )
        if (await locked.single(strict=True))["found"] == 0:
            raise GraphWriteRefused(f"no {node.label} {node.stable_id!r} to retire")
        result = await tx.run(
            _trusted(match + "REMOVE n._lock RETURN n.valid_to AS was, n.valid_from AS valid_from"),
            stable_id=node.stable_id,
        )
        record = await result.single(strict=True)
        was = to_python(record["was"])
        valid_from = cast("dt.date", to_python(record["valid_from"]))
        if was == valid_to:
            return  # already retired on this date: idempotent
        if was != NO_END_DATE:
            raise GraphWriteRefused(
                f"{node.label} {node.stable_id!r} was already retired on {was}; "
                "valid_to is set once (D-05)"
            )
        if valid_to <= valid_from:
            raise GraphWriteRefused(f"valid_to {valid_to} is not after valid_from {valid_from}")
        await tx.run(
            _trusted(match + "SET n.valid_to = $valid_to"),
            stable_id=node.stable_id,
            valid_to=valid_to,
        )

    async def put_edge(
        self,
        edge_type: str,
        source: NodeRef,
        target: NodeRef,
        properties: Mapping[str, Scalar] | None = None,
    ) -> bool:
        """MERGE one edge between two existing nodes. True if created, False
        if an identical edge existed; different properties are refused, as
        for nodes. Both ends must exist: an edge to nothing is a loader bug,
        not a node to invent."""
        props = _properties(properties or {})
        checked_edge_type(edge_type)

        async def work(tx: AsyncManagedTransaction) -> bool:
            return await self._merge_edge(tx, edge_type, source, target, props)

        attributes = {
            "graph.edge_type": edge_type,
            "graph.source": source.stable_id,
            "graph.target": target.stable_id,
        }
        return await self._write("neo4j_graph.put_edge", attributes, work)

    async def _merge_edge(
        self,
        tx: AsyncManagedTransaction,
        edge_type: str,
        source: NodeRef,
        target: NodeRef,
        props: Mapping[str, object],
    ) -> bool:
        query = (
            f"OPTIONAL MATCH (a:{source.label} {{stable_id: $source}}) "
            f"OPTIONAL MATCH (b:{target.label} {{stable_id: $target}}) "
            "WITH a, b WHERE a IS NOT NULL AND b IS NOT NULL "
            f"MERGE (a)-[r:{checked_edge_type(edge_type)}]->(b) "
            "ON CREATE SET r += $props, r._created = true "
            "WITH r, coalesce(r._created, false) AS created REMOVE r._created "
            "RETURN created, properties(r) AS stored"
        )
        result = await tx.run(
            _trusted(query), source=source.stable_id, target=target.stable_id, props=props
        )
        record = await result.single()
        if record is None:
            raise GraphWriteRefused(
                f"{edge_type}: {source.label} {source.stable_id!r} or {target.label} "
                f"{target.stable_id!r} does not exist"
            )
        if not record["created"] and _differing(_stored(record), props, ignore=frozenset()):
            raise GraphWriteRefused(
                f"{edge_type} {source.stable_id!r}->{target.stable_id!r} is immutable"
            )
        return bool(record["created"])

    async def amend(
        self,
        *,
        old: NodeRef,
        new: NodeRef,
        effective: dt.date,
        status: Status,
        properties: Mapping[str, Scalar] | None = None,
        retire_with: tuple[NodeRef, ...] = (),
    ) -> None:
        """D-05's amendment, in one transaction: `new` is written in force
        from `effective`, `new -SUPERSEDES-> old`, and `old` is retired on
        `effective`. All of it lands or none of it does. Re-running the same
        amendment is a no-op."""
        if old.label != new.label:
            raise GraphWriteRefused(f"{new.label} cannot supersede a {old.label}")
        props: dict[str, object] = {
            **_properties(properties or {}),
            "valid_from": effective,
            "valid_to": NO_END_DATE,
            "status": status.value,
        }

        async def work(tx: AsyncManagedTransaction) -> None:
            await self._retire(tx, old, effective)
            for node in retire_with:
                await self._retire(tx, node, effective)
            created, stored = await self._merge_node(tx, new, props)
            if not created:
                _refuse_if_changed(new, stored, props)
            await self._merge_edge(tx, "SUPERSEDES", new, old, {})

        attributes = {
            "graph.label": old.label,
            "graph.old": old.stable_id,
            "graph.new": new.stable_id,
            "graph.effective": effective.isoformat(),
        }
        await self._write("neo4j_graph.amend", attributes, work)

    # ---- reads: only what the query builder built (D-05) --------------------

    async def read(self, query: BuiltQuery) -> tuple[GraphRow, ...]:
        """Run one builder-built query. A `BuiltQuery` can only come from
        `GraphQuery.build`, so every read here carries `as_of` and a cap."""
        if not isinstance(query, BuiltQuery):  # pyright: ignore[reportUnnecessaryIsInstance] — runtime guard
            raise TypeError("Neo4jGraph.read runs only a BuiltQuery (D-05)")
        query.verify()  # refuses one changed after building
        with telemetry.span(
            "neo4j_graph.read",
            **{
                "graph.as_of": query.as_of.isoformat(),
                "graph.max_depth": query.max_depth,
                "graph.query": query.text,
            },
        ) as current:
            records, _, _ = await self._driver.execute_query(
                query.text,
                parameters_=dict(query.parameters),
                database_=self._database,
                routing_=RoutingControl.READ,
            )
            rows = tuple(
                GraphRow(
                    node=_stored_from(record, "node"),
                    path=tuple(str(s) for s in cast("list[object]", record["path"])),
                )
                for record in records
            )
            current.set_attribute("graph.rows", len(rows))
            current.set_attribute(
                "graph.result_ids", [str(r.node.get("stable_id", "")) for r in rows]
            )
            return rows

    # ---- counts (Phase 2 exit: Aura usage under 10% of its limits) ----------

    async def counts(self) -> GraphCounts:
        """Whole-graph totals. Not a traversal and returns no node, so it is
        the one read outside the query builder (module docstring)."""
        with telemetry.span("neo4j_graph.counts") as s:
            nodes, _, _ = await self._driver.execute_query(
                "MATCH (n) RETURN count(n) AS c", database_=self._database
            )
            edges, _, _ = await self._driver.execute_query(
                "MATCH ()-[r]->() RETURN count(r) AS c", database_=self._database
            )
            counted = GraphCounts(nodes=int(nodes[0]["c"]), edges=int(edges[0]["c"]))
            s.set_attribute("graph.nodes", counted.nodes)
            s.set_attribute("graph.edges", counted.edges)
            return counted


__all__ = [
    "EDGE_TYPES",
    "PERSISTED_LABELS",
    "Bitemporal",
    "GraphCounts",
    "GraphRow",
    "GraphWriteRefused",
    "Neo4jGraph",
    "NodeRef",
    "SchemaReport",
    "Status",
    "checked_edge_type",
    "checked_label",
]
