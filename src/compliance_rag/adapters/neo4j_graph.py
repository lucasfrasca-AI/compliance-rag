"""The Neo4j graph (issue #87, Phase 2): schema, and the only writes.

D-05's bitemporal rules are enforced here, on every edition, not left to the
database: every node is written with `valid_from`, `valid_to`, `status` and
`recorded_at`; `valid_to` is a date, never null (the `NO_END_DATE` sentinel,
CONTEXT.md: Bitemporal); a node, once written, is immutable except for one
transition — `valid_to` from `NO_END_DATE` to a real date, which is how a
superseded node is retired. Re-writing a node with identical content is a
no-op, so loaders are idempotent on stable ID.

Labels and relationship types can't be Cypher parameters, so they are taken
only from the closed sets below; every value is a bound parameter.

Reads go through the query builder (issue #88), which requires `as_of` and a
depth cap — this module has no unscoped read.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import LiteralString, cast

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncManagedTransaction
from neo4j.exceptions import Neo4jError
from neo4j.time import Date, DateTime

from compliance_rag import telemetry
from compliance_rag.corpus.models import NO_END_DATE
from compliance_rag.settings import Settings

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "db" / "neo4j" / "schema.cypher"

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

BITEMPORAL_KEYS = frozenset({"valid_from", "valid_to", "status", "recorded_at"})

_PROPERTY_KEY = re.compile(r"^[a-z][a-z0-9_]*$")

Scalar = str | int | float | bool | dt.date | tuple[str, ...]
"""What a node or edge property may hold: Neo4j stores these natively."""


class Status(StrEnum):
    IN_FORCE = "in_force"
    NOT_YET_COMMENCED = "not_yet_commenced"
    REPEALED = "repealed"


@dataclass(frozen=True, slots=True)
class Bitemporal:
    """Valid time for one node. `recorded_at` (transaction time) is not
    here: the writer stamps it, so no caller can backdate what we learned."""

    valid_from: dt.date
    status: Status
    valid_to: dt.date = NO_END_DATE

    def __post_init__(self) -> None:
        if self.valid_to <= self.valid_from:
            raise ValueError(f"valid_to {self.valid_to} is not after valid_from {self.valid_from}")


class GraphWriteRefused(ValueError):
    """A write that would mutate an immutable node, or that names something
    the graph doesn't hold."""


@dataclass(frozen=True, slots=True)
class SchemaReport:
    applied: tuple[str, ...]
    """Every statement the server accepted (idempotent: `IF NOT EXISTS`)."""
    unsupported: tuple[str, ...]
    """Constraint names this edition refuses (property existence on
    Community). The writer enforces the same invariant instead."""


def schema_statements(path: Path = SCHEMA_PATH) -> tuple[str, ...]:
    """The schema file's statements, comments stripped, in file order."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()]
    body = "\n".join(line for line in lines if not line.lstrip().startswith("//"))
    return tuple(s.strip() for s in body.split(";") if s.strip())


def _statement_name(statement: str) -> str:
    match = re.match(r"CREATE (?:CONSTRAINT|INDEX) (\w+)", statement)
    if match is None:
        raise ValueError(f"schema statement without a name: {statement[:60]!r}")
    return match.group(1)


def _is_existence_constraint(statement: str) -> bool:
    return statement.startswith("CREATE CONSTRAINT") and "IS NOT NULL" in statement


def _trusted(query: str) -> LiteralString:
    """The driver types queries as `LiteralString` to keep values out of
    Cypher text. The only non-literal pieces interpolated in this module are
    labels and edge types that passed `_label`/`_edge_type` (closed sets) and
    statements read from the checked-in schema file; every value is a bound
    parameter."""
    return cast("LiteralString", query)


def _label(label: str) -> str:
    if label not in PERSISTED_LABELS:
        raise GraphWriteRefused(f"{label!r} is not a persisted graph label")
    return label


def _edge_type(edge_type: str) -> str:
    if edge_type not in EDGE_TYPES:
        raise GraphWriteRefused(f"{edge_type!r} is not a graph edge type")
    return edge_type


def _properties(properties: Mapping[str, Scalar]) -> dict[str, Scalar]:
    out: dict[str, Scalar] = {}
    for key, value in properties.items():
        if not _PROPERTY_KEY.match(key):
            raise GraphWriteRefused(f"property key {key!r} is not snake_case")
        if key in BITEMPORAL_KEYS or key == "stable_id":
            raise GraphWriteRefused(f"{key!r} is set by the writer, not passed as a property")
        out[key] = list(value) if isinstance(value, tuple) else value  # type: ignore[assignment] — Neo4j lists
    return out


def _to_python(value: object) -> object:
    """Neo4j temporal types back to the standard library, for comparison."""
    if isinstance(value, Date):
        return value.to_native()
    if isinstance(value, DateTime):
        return value.to_native()
    if isinstance(value, list):
        return tuple(_to_python(v) for v in cast("list[object]", value))
    return value


class Neo4jGraph:
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
        cls, uri: str, *, username: str, password: str, database: str = "neo4j"
    ) -> AsyncGenerator[Neo4jGraph]:
        driver = AsyncGraphDatabase.driver(uri, auth=(username, password))
        try:
            await driver.verify_connectivity()
            yield cls(driver, database=database)
        finally:
            await driver.close()

    @classmethod
    def from_settings(cls, settings: Settings) -> AbstractAsyncContextManager[Neo4jGraph]:
        """The deployed graph (Aura Free: public law only, D-09). Tests never
        use this: they connect to a local server by URI."""
        return cls.connect(
            settings.neo4j_uri,
            username=settings.neo4j_username,
            password=settings.neo4j_password.get_secret_value(),
            database=settings.neo4j_database,
        )

    # ---- schema -------------------------------------------------------------

    async def apply_schema(self, path: Path = SCHEMA_PATH) -> SchemaReport:
        applied: list[str] = []
        unsupported: list[str] = []
        with telemetry.span("neo4j_graph.apply_schema", **{"graph.schema": str(path.name)}) as s:
            for statement in schema_statements(path):
                name = _statement_name(statement)
                try:
                    await self._driver.execute_query(_trusted(statement), database_=self._database)
                except Neo4jError as exc:
                    if _is_existence_constraint(statement) and "Enterprise" in str(exc):
                        unsupported.append(name)
                        continue
                    raise
                applied.append(name)
            s.set_attribute("graph.schema_applied", len(applied))
            s.set_attribute("graph.schema_unsupported", unsupported)
        return SchemaReport(applied=tuple(applied), unsupported=tuple(unsupported))

    # ---- writes -------------------------------------------------------------

    async def put_node(
        self,
        label: str,
        stable_id: str,
        bitemporal: Bitemporal,
        properties: Mapping[str, Scalar] | None = None,
    ) -> bool:
        """Write one immutable node. Returns True if created, False if an
        identical node already existed. Different content under the same
        stable ID is refused, except retiring it (`valid_to` from
        `NO_END_DATE` to a date), which is applied."""
        props: dict[str, Scalar] = {
            **_properties(properties or {}),
            "valid_from": bitemporal.valid_from,
            "valid_to": bitemporal.valid_to,
            "status": bitemporal.status.value,
        }
        query = (
            f"MERGE (n:{_label(label)} {{stable_id: $stable_id}}) "
            "ON CREATE SET n += $props, n.recorded_at = $now, n._created = true "
            "WITH n, coalesce(n._created, false) AS created REMOVE n._created "
            "RETURN created, properties(n) AS stored"
        )

        async def write(tx: AsyncManagedTransaction) -> bool:
            result = await tx.run(
                _trusted(query), stable_id=stable_id, props=props, now=self._clock()
            )
            record = await result.single(strict=True)
            if record["created"]:
                return True
            stored = {
                k: _to_python(v) for k, v in cast("dict[str, object]", record["stored"]).items()
            }
            wanted = {k: _to_python(v) for k, v in props.items()}
            differs = {
                k
                for k in set(stored) | set(wanted)
                if k not in {"stable_id", "recorded_at", "valid_to"}
                and stored.get(k) != wanted.get(k)
            }
            if differs:
                raise GraphWriteRefused(
                    f"{label} {stable_id!r} is immutable: {sorted(differs)} differ from what "
                    "is stored. An amendment is a new node with SUPERSEDES (D-05)"
                )
            if stored["valid_to"] != bitemporal.valid_to:
                await self._retire(tx, label, stable_id, stored, bitemporal.valid_to)
            return False

        with telemetry.span(
            "neo4j_graph.put_node", **{"graph.label": label, "graph.stable_id": stable_id}
        ) as s:
            async with self._driver.session(database=self._database) as session:
                created = await session.execute_write(write)
            s.set_attribute("graph.created", created)
            return created

    async def retire_node(self, label: str, stable_id: str, *, valid_to: dt.date) -> None:
        """Set `valid_to` on a node still open (`NO_END_DATE`) — the one
        permitted change. Retiring again to the same date is a no-op."""
        query = (
            f"MATCH (n:{_label(label)} {{stable_id: $stable_id}}) RETURN properties(n) AS stored"
        )

        async def write(tx: AsyncManagedTransaction) -> None:
            record = await (await tx.run(_trusted(query), stable_id=stable_id)).single()
            if record is None:
                raise GraphWriteRefused(f"no {label} {stable_id!r} to retire")
            stored = {
                k: _to_python(v) for k, v in cast("dict[str, object]", record["stored"]).items()
            }
            if stored["valid_to"] != valid_to:
                await self._retire(tx, label, stable_id, stored, valid_to)

        with telemetry.span(
            "neo4j_graph.retire_node",
            **{"graph.label": label, "graph.stable_id": stable_id, "graph.valid_to": str(valid_to)},
        ):
            async with self._driver.session(database=self._database) as session:
                await session.execute_write(write)

    async def _retire(
        self,
        tx: AsyncManagedTransaction,
        label: str,
        stable_id: str,
        stored: Mapping[str, object],
        valid_to: dt.date,
    ) -> None:
        if stored["valid_to"] != NO_END_DATE:
            raise GraphWriteRefused(
                f"{label} {stable_id!r} was already retired on {stored['valid_to']}; "
                "valid_to is set once (D-05)"
            )
        valid_from = cast("dt.date", stored["valid_from"])
        if valid_to <= valid_from:
            raise GraphWriteRefused(f"valid_to {valid_to} is not after valid_from {valid_from}")
        await tx.run(
            _trusted(
                f"MATCH (n:{_label(label)} {{stable_id: $stable_id}}) SET n.valid_to = $valid_to"
            ),
            stable_id=stable_id,
            valid_to=valid_to,
        )

    async def put_edge(
        self,
        edge_type: str,
        source: tuple[str, str],
        target: tuple[str, str],
        properties: Mapping[str, Scalar] | None = None,
    ) -> bool:
        """MERGE one edge between two existing nodes, each given as
        `(label, stable_id)`. Returns True if created. Both ends must exist:
        an edge to nothing is a loader bug, not a node to invent."""
        (source_label, source_id), (target_label, target_id) = source, target
        query = (
            f"OPTIONAL MATCH (a:{_label(source_label)} {{stable_id: $source}}) "
            f"OPTIONAL MATCH (b:{_label(target_label)} {{stable_id: $target}}) "
            "WITH a, b WHERE a IS NOT NULL AND b IS NOT NULL "
            f"MERGE (a)-[r:{_edge_type(edge_type)}]->(b) "
            "ON CREATE SET r += $props, r._created = true "
            "WITH r, coalesce(r._created, false) AS created REMOVE r._created "
            "RETURN created"
        )

        async def write(tx: AsyncManagedTransaction) -> bool:
            result = await tx.run(
                _trusted(query),
                source=source_id,
                target=target_id,
                props=_properties(properties or {}),
            )
            record = await result.single()
            if record is None:
                raise GraphWriteRefused(
                    f"{edge_type}: {source_label} {source_id!r} or {target_label} "
                    f"{target_id!r} does not exist"
                )
            return bool(record["created"])

        with telemetry.span(
            "neo4j_graph.put_edge",
            **{"graph.edge_type": edge_type, "graph.source": source_id, "graph.target": target_id},
        ):
            async with self._driver.session(database=self._database) as session:
                return await session.execute_write(write)

    # ---- counts (Phase 2 exit: Aura usage under 10% of its limits) ----------

    async def counts(self) -> tuple[int, int]:
        with telemetry.span("neo4j_graph.counts") as s:
            nodes, _, _ = await self._driver.execute_query(
                "MATCH (n) RETURN count(n) AS c", database_=self._database
            )
            edges, _, _ = await self._driver.execute_query(
                "MATCH ()-[r]->() RETURN count(r) AS c", database_=self._database
            )
            counted = (int(nodes[0]["c"]), int(edges[0]["c"]))
            s.set_attribute("graph.nodes", counted[0])
            s.set_attribute("graph.edges", counted[1])
            return counted
