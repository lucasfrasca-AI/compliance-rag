"""The Neo4j graph (issue #87, Phase 2): schema, and the only writes.

D-05's bitemporal rules are enforced here, on every edition, not left to the
database: every node is written with `valid_from`, `valid_to` and
`recorded_at`; `valid_to` is a date, never null (the `NO_END_DATE` sentinel,
CONTEXT.md: Bitemporal); a node, once written, is immutable except for one
transition — `valid_to` from `NO_END_DATE` to a real date, with the
`end_reason` that says why (superseded, repealed, expired), which is how a
node is retired. That transition takes the node's write lock before reading
`valid_to`, so two concurrent retires serialise and can't both win.
Re-writing a node or edge with identical content is a no-op, so loaders are
idempotent on stable ID.

No status is stored (ADR-0029): whether a node is in force, not yet
commenced or ended *on a date* is derived from `valid_from`, `valid_to` and
`end_reason` (`corpus.models.status_on`). `migrate_to_derived_status` moves
nodes written before ADR-0029 onto that model.

Labels and relationship types can't be Cypher parameters, so they are taken
only from the closed sets below; every value is a bound parameter.

Reads go through the query builder (issue #88), which requires `as_of` and a
depth cap. `counts()` is the one exception, and it reads no node: whole-graph
totals for the Aura capacity check (PHASES.md Phase 2 exit). `loaded_sources()`
is the other: it reads the `Load` records (#153, `graph_schema.LOAD_LABEL`),
which say what a loader wrote, not what the law said.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import LiteralString, cast

from neo4j import (
    AsyncDriver,
    AsyncGraphDatabase,
    AsyncManagedTransaction,
    NotificationDisabledCategory,
    Record,
    RoutingControl,
)
from neo4j.exceptions import ConstraintError, Neo4jError
from neo4j.time import Date, DateTime

from compliance_rag import telemetry
from compliance_rag.adapters.graph_query import BuiltQuery
from compliance_rag.adapters.graph_schema import (
    CORRECTABLE_EDGES,
    CORRECTIONS,
    COSMETIC,
    EDGE_TYPES,
    INCOMPLETE_DETAILS,
    INCOMPLETE_REASONS,
    PERSISTED_LABELS,
    WRITER_KEYS,
    Bitemporal,
    CorrectionWrite,
    EdgeWrite,
    EndReason,
    GraphCounts,
    GraphRow,
    GraphWriteRefused,
    NodeRef,
    NodeWrite,
    Scalar,
    StoredEdge,
    StoredNode,
    checked_edge_type,
    checked_label,
    incomplete_of_properties,
    incomplete_properties,
    refuse_duplicates,
)
from compliance_rag.adapters.interfaces import (
    GraphAdmin,
    LoadedSource,
    LoadedSources,
    SourceKind,
    SourceVersion,
)
from compliance_rag.adapters.revision_authorization import RevisionAuthorization
from compliance_rag.corpus.models import NO_END_DATE
from compliance_rag.corpus.revision import described
from compliance_rag.settings import Settings

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "db" / "neo4j" / "schema.cypher"

BITEMPORAL_KEYS = frozenset({"valid_from", "valid_to", "end_reason", "recorded_at"})
RESERVED_KEYS = BITEMPORAL_KEYS | {"stable_id", "status", CORRECTIONS}
"""Never passed as a property: the writer sets the first five, and `status`
is derived, never stored (ADR-0029), so no caller can write one back."""

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
    match = re.match(r"(?:CREATE|DROP) (?:CONSTRAINT|INDEX) (\w+)", statement)
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
    (closed sets), statements read from the checked-in schema file, and the
    `SET` clause `_close_load` picks from its own closed dictionary; every
    value is a bound parameter."""
    return cast("LiteralString", query)


def _properties(properties: Mapping[str, Scalar]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in properties.items():
        if not _PROPERTY_KEY.match(key):
            raise GraphWriteRefused(f"property key {key!r} is not snake_case")
        if key in RESERVED_KEYS:
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


def _bitemporal(bitemporal: Bitemporal) -> dict[str, object]:
    """The stored form: `end_reason` only once ended (Neo4j stores no null)."""
    stored: dict[str, object] = {
        "valid_from": bitemporal.valid_from,
        "valid_to": bitemporal.valid_to,
    }
    if bitemporal.end_reason is not None:
        stored["end_reason"] = bitemporal.end_reason.value
    return stored


_RETIREMENT = WRITER_KEYS
_EDGE_AUDIT = frozenset({CORRECTIONS})
"""An edge's only writer-kept property: its correction trail (ADR-0031)."""


def _refuse_if_changed(
    node: NodeRef, stored: Mapping[str, object], wanted: Mapping[str, object]
) -> None:
    """A node is immutable except `valid_to` and `end_reason`, which only
    `_retire` changes."""
    differs = _differing(stored, wanted, ignore=_RETIREMENT)
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

    @staticmethod
    def _new_driver(uri: str, *, username: str, password: str, database: str) -> AsyncDriver:
        if not (uri and username and database):
            raise ValueError("Neo4j uri, username and database are all required")
        # Every label and relationship type a query names comes from the closed
        # vocabularies in graph_schema.py, so one the server doesn't recognise
        # means no data of that kind yet (no REQUIRES edge until an obligation
        # has a condition of its own), never a typo: left on, it would warn on
        # every Track A read. The category also covers property keys, which are
        # literals in this module; a misspelt one is caught by the tests that
        # assert what queries return, not by a server warning. Every other
        # notification category stays on (#141).
        return AsyncGraphDatabase.driver(
            uri,
            auth=(username, password),
            notifications_disabled_categories=[NotificationDisabledCategory.UNRECOGNIZED],
        )

    @classmethod
    @asynccontextmanager
    async def connect(
        cls, uri: str, *, username: str, password: str, database: str
    ) -> AsyncGenerator[Neo4jGraph]:
        """`database` has no default: on Aura Free it is the instance ID, not
        "neo4j", and every query names it explicitly."""
        driver = cls._new_driver(uri, username=username, password=password, database=database)
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

    @classmethod
    def open(cls, settings: Settings) -> Neo4jGraph:
        """The deployed graph for a long-lived process (the API, #147), which
        closes it with `aclose()` on shutdown; scripts use `from_settings`,
        which closes on exit. The driver connects on first use."""
        return cls(
            cls._new_driver(
                settings.neo4j_uri,
                username=settings.neo4j_username,
                password=settings.neo4j_password.get_secret_value(),
                database=settings.neo4j_database,
            ),
            database=settings.neo4j_database,
        )

    async def verify(self) -> None:
        """Reach the server now, so a bad URI or credential fails at startup."""
        await self._driver.verify_connectivity()

    async def aclose(self) -> None:
        await self._driver.close()

    async def _write[T](
        self,
        span: str,
        attributes: Mapping[str, str | int],
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
        props: dict[str, object] = {**_properties(properties or {}), **_bitemporal(bitemporal)}

        async def work(tx: AsyncManagedTransaction) -> bool:
            created, stored = await self._merge_node(tx, node, props)
            if created:
                return True
            _refuse_if_changed(node, stored, props)
            await self._retire_if_ending(tx, node, stored, bitemporal)
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
        goes through `_retire`'s lock-then-read. Returns the number created.

        Each batch commits on its own: a refusal in one leaves the batches
        before it committed. Every write is idempotent, so re-running the same
        call after fixing the refused row completes it."""
        refuse_duplicates("node", [(w.node.label, w.node.stable_id) for w in writes])
        created = 0
        for label in sorted({w.node.label for w in writes}):
            same_label = [w for w in writes if w.node.label == label]
            for start in range(0, len(same_label), BATCH_SIZE):
                created += await self._put_node_batch(label, same_label[start : start + BATCH_SIZE])
        return created

    async def _put_node_batch(self, label: str, batch: Sequence[NodeWrite]) -> int:
        props: list[dict[str, object]] = [
            {**_properties(w.properties), **_bitemporal(w.bitemporal)} for w in batch
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
                await self._retire_if_ending(tx, write.node, stored, write.bitemporal)
            return created

        attributes: dict[str, str | int] = {"graph.label": label, "graph.batch": len(batch)}
        return await self._write("neo4j_graph.put_nodes", attributes, work)

    async def put_edges(self, writes: Sequence[EdgeWrite]) -> int:
        """`put_edge` for many edges in `BATCH_SIZE`-row transactions. Both
        ends of every edge in a batch are checked before any is merged, so a
        missing end refuses the batch with nothing written. Returns the
        number created. Batches commit on their own, as for `put_nodes`."""
        refuse_duplicates(
            "edge", [(w.edge_type, w.source.stable_id, w.target.stable_id) for w in writes]
        )
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
                elif _differing(_stored(record), wanted[key], ignore=_EDGE_AUDIT):
                    raise GraphWriteRefused(f"{edge_type} {key[0]!r}->{key[1]!r} is immutable")
            return created

        attributes: dict[str, str | int] = {"graph.edge_type": edge_type, "graph.batch": len(batch)}
        return await self._write("neo4j_graph.put_edges", attributes, work)

    async def retire_node(self, node: NodeRef, *, valid_to: dt.date, end_reason: EndReason) -> None:
        """Set `valid_to` and `end_reason` on a node still open
        (`NO_END_DATE`) — the one permitted change. Retiring again to the same
        date for the same reason is a no-op."""

        async def work(tx: AsyncManagedTransaction) -> None:
            await self._retire(tx, node, valid_to, end_reason)

        attributes = {
            "graph.label": node.label,
            "graph.stable_id": node.stable_id,
            "graph.valid_to": valid_to.isoformat(),
            "graph.end_reason": end_reason.value,
        }
        await self._write("neo4j_graph.retire_node", attributes, work)

    async def _retire_if_ending(
        self,
        tx: AsyncManagedTransaction,
        node: NodeRef,
        stored: Mapping[str, object],
        wanted: Bitemporal,
    ) -> None:
        """A re-write that ends a stored open node retires it; one that
        reopens or re-dates it is refused by `_retire`."""
        same_end = all(stored.get(k) == v for k, v in _bitemporal(wanted).items()) and (
            "end_reason" in stored
        ) == (wanted.end_reason is not None)
        if same_end:
            return
        if wanted.end_reason is None:
            raise GraphWriteRefused(
                f"{node.label} {node.stable_id!r} ended on {stored['valid_to']}; "
                "a node is never reopened (D-05)"
            )
        await self._retire(tx, node, wanted.valid_to, wanted.end_reason)

    async def _retire(
        self, tx: AsyncManagedTransaction, node: NodeRef, valid_to: dt.date, end_reason: EndReason
    ) -> None:
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
            _trusted(
                match + "REMOVE n._lock RETURN n.valid_to AS was, n.end_reason AS why, "
                "n.valid_from AS valid_from"
            ),
            stable_id=node.stable_id,
        )
        record = await result.single(strict=True)
        was = to_python(record["was"])
        valid_from = cast("dt.date", to_python(record["valid_from"]))
        if was == valid_to and record["why"] == end_reason.value:
            return  # already retired on this date for this reason: idempotent
        if was != NO_END_DATE:
            raise GraphWriteRefused(
                f"{node.label} {node.stable_id!r} was already retired on {was} "
                f"({record['why']}); valid_to and end_reason are set once (D-05)"
            )
        if valid_to <= valid_from:
            raise GraphWriteRefused(f"valid_to {valid_to} is not after valid_from {valid_from}")
        await tx.run(
            _trusted(match + "SET n.valid_to = $valid_to, n.end_reason = $end_reason"),
            stable_id=node.stable_id,
            valid_to=valid_to,
            end_reason=end_reason.value,
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
        if not record["created"] and _differing(_stored(record), props, ignore=_EDGE_AUDIT):
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
        properties: Mapping[str, Scalar] | None = None,
        retire_with: tuple[NodeRef, ...] = (),
    ) -> None:
        """D-05's amendment, in one transaction: `new` is written in force
        from `effective`, `new -SUPERSEDES-> old`, and `old` is retired on
        `effective` as superseded. All of it lands or none of it does.
        Re-running the same amendment is a no-op."""
        if old.label != new.label:
            raise GraphWriteRefused(f"{new.label} cannot supersede a {old.label}")
        props: dict[str, object] = {
            **_properties(properties or {}),
            **_bitemporal(Bitemporal(valid_from=effective)),
        }

        async def work(tx: AsyncManagedTransaction) -> None:
            await self._retire(tx, old, effective, EndReason.SUPERSEDED)
            for node in retire_with:
                await self._retire(tx, node, effective, EndReason.SUPERSEDED)
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

    # ---- cosmetic corrections (ADR-0031) -------------------------------------

    async def correct(
        self, corrections: Sequence[CorrectionWrite], *, by: str, reason: str
    ) -> None:
        """Every correction in one transaction (all land or none does), each
        lock, read, check, set — as a retirement does. Refused unless every
        key is `COSMETIC` and already present, the edge (if any) is a
        crosswalk, and the change names who made it and why; the previous
        wording goes on the `corrections` trail, so the node or edge still
        says what it said before."""
        if not corrections:
            return
        if not (by.strip() and reason.strip()):
            raise GraphWriteRefused("a correction names who made it and why (ADR-0031)")
        for correction in corrections:
            meaning = set(correction.changes) - COSMETIC
            if meaning:
                raise GraphWriteRefused(
                    f"{sorted(meaning)} are meaning, not wording: a new id (ADR-0031)"
                )
            if correction.edge is not None and correction.edge[0] not in CORRECTABLE_EDGES:
                raise GraphWriteRefused(f"{correction.edge[0]} carries no wording to correct")
        now = self._clock().isoformat()

        async def work(tx: AsyncManagedTransaction) -> None:
            for correction in corrections:
                await self._correct(tx, correction, by=by, reason=reason, now=now)

        attributes = {
            "graph.corrected": ",".join(sorted({c.node.stable_id for c in corrections})),
            "graph.corrected_by": by,
        }
        await self._write("neo4j_graph.correct", attributes, work)

    async def _correct(
        self,
        tx: AsyncManagedTransaction,
        correction: CorrectionWrite,
        *,
        by: str,
        reason: str,
        now: str,
    ) -> None:
        node, edge = correction.node, correction.edge
        parameters: dict[str, object] = {"stable_id": node.stable_id}
        if edge is None:
            match, subject = f"MATCH (n:{node.label} {{stable_id: $stable_id}}) ", "n"
        else:
            edge_type, other = edge
            match = (
                f"MATCH (a:{node.label} {{stable_id: $stable_id}})"
                f"-[r:{checked_edge_type(edge_type)}]->(b:{other.label} {{stable_id: $other}}) "
            )
            subject = "r"
            parameters["other"] = other.stable_id
        locked = await tx.run(
            _trusted(match + f"SET {subject}._lock = true RETURN properties({subject}) AS p"),
            parameters,
        )
        record = await locked.single()
        if record is None:
            raise GraphWriteRefused(
                f"nothing to correct: {node.label} {node.stable_id!r} {edge or ''}"
            )
        before = {k: to_python(v) for k, v in cast("dict[str, object]", record["p"]).items()}
        props = _properties(correction.changes)
        absent = sorted(k for k in props if k not in before)
        if absent:
            raise GraphWriteRefused(
                f"{node.stable_id}: {absent} not present — a correction rewords, it never adds"
            )
        previous = {k: before[k] for k in props if before[k] != props[k]}
        if not previous:
            await tx.run(_trusted(match + f"REMOVE {subject}._lock"), parameters)
            return  # already says this: idempotent
        entry = json.dumps(
            {
                "at": now,
                "by": by,
                "reason": reason,
                "previous": {k: str(v) for k, v in previous.items()},
            },
            sort_keys=True,
        )
        await tx.run(
            _trusted(
                match + f"SET {subject} += $props, "
                f"{subject}.{CORRECTIONS} = coalesce({subject}.{CORRECTIONS}, []) + $entry "
                f"REMOVE {subject}._lock"
            ),
            {**parameters, "props": props, "entry": entry},
        )

    # ---- parse corrections of library law (ADR-0042) ------------------------

    async def revise_clauses(self, authorization: RevisionAuthorization) -> int:
        """Every clause of the authorised correction in one transaction, each
        lock, read, check, set, as `correct` does: the node's form from the
        recorded previous one to the new one — `canonical_sha256`, and its
        incompleteness (ADR-0047) — and an entry on the node's `corrections`
        trail (the revision's when, who, why, id and source, and the previous
        form). A node already in the new form is left alone; a missing node,
        or one in any other form, refuses the whole revision."""
        record = authorization.record
        if not record.corrected:
            raise GraphWriteRefused(
                "only a correction rewrites a Clause node in place; an amendment is a new "
                "version node with SUPERSEDES (D-05)"
            )

        async def work(tx: AsyncManagedTransaction) -> int:
            changed = 0
            for entry in record.corrected:
                clause_id = record.clause_id(entry.clause_path)
                locked = await tx.run(
                    "MATCH (n:Clause {stable_id: $stable_id}) SET n._lock = true "
                    "RETURN properties(n) AS p",
                    stable_id=clause_id,
                )
                row = await locked.single()
                if row is None:
                    raise GraphWriteRefused(f"no Clause {clause_id!r} to correct")
                stored = {k: to_python(v) for k, v in cast("dict[str, object]", row["p"]).items()}
                have = (stored.get("canonical_sha256"), incomplete_of_properties(stored))
                if have == (entry.sha256, entry.incomplete):
                    await tx.run(
                        "MATCH (n:Clause {stable_id: $stable_id}) REMOVE n._lock",
                        stable_id=clause_id,
                    )
                    continue  # already corrected: a re-run
                if (
                    have != (entry.previous_sha256, entry.previous_incomplete)
                    or entry.previous_sha256 is None
                ):
                    raise GraphWriteRefused(
                        f"Clause {clause_id!r} has canonical_sha256 {have[0]} and is "
                        f"{described(have[1])}: neither the previous ({entry.previous_sha256}, "
                        f"{described(entry.previous_incomplete)}) nor the new ({entry.sha256}, "
                        f"{described(entry.incomplete)}) form revision {record.revision_id} "
                        "records"
                    )
                incomplete = incomplete_properties(entry.incomplete)
                await tx.run(
                    "MATCH (n:Clause {stable_id: $stable_id}) "
                    "SET n.canonical_sha256 = $sha, "
                    f"n.{INCOMPLETE_REASONS} = $reasons, n.{INCOMPLETE_DETAILS} = $details, "
                    "n.corrections = coalesce(n.corrections, []) + $entry REMOVE n._lock",
                    stable_id=clause_id,
                    sha=entry.sha256,
                    # A null removes the property: a complete clause has neither.
                    reasons=incomplete.get(INCOMPLETE_REASONS),
                    details=incomplete.get(INCOMPLETE_DETAILS),
                    entry=record.trail_entry(entry),
                )
                changed += 1
            return changed

        attributes: dict[str, str | int] = {
            "graph.revision_id": record.revision_id,
            "graph.revised": ",".join(
                sorted(record.clause_id(c.clause_path) for c in record.corrected)
            ),
            "graph.revised_by": record.by,
        }
        return await self._write("neo4j_graph.revise_clauses", attributes, work)

    # ---- migration (ADR-0029) ---------------------------------------------

    async def migrate_to_derived_status(self) -> int:
        """Move nodes written before ADR-0029 onto derived status, in one
        transaction; idempotent. Returns how many nodes changed.

        - A node whose stored status contradicts its dates — `repealed` while
          still open — is refused, naming how many: which one is right is a
          human's call, not the migration's.
        - An ended node gets the `end_reason` its stored status implies:
          `repealed` if it said so, otherwise `superseded` (amendment was the
          only path that ended a node before this ADR).
        - Then `status` is removed.

        Run after `apply_schema`, which drops the constraints that required
        `status` (on Aura they would refuse the removal)."""

        async def work(tx: AsyncManagedTransaction) -> int:
            contradictory = await tx.run(
                "MATCH (n) WHERE n.status = 'repealed' AND n.valid_to = $open RETURN count(n) AS c",
                open=NO_END_DATE,
            )
            bad = int((await contradictory.single(strict=True))["c"])
            if bad:
                raise GraphWriteRefused(
                    f"{bad} node(s) are stored as repealed but still open; resolve them by "
                    "hand before migrating (ADR-0029)"
                )
            changed = await tx.run(
                "MATCH (n) WHERE n.status IS NOT NULL "
                "OR (n.valid_to <> $open AND n.end_reason IS NULL) "
                "SET n.end_reason = CASE "
                "  WHEN n.valid_to = $open THEN null "
                "  WHEN n.end_reason IS NOT NULL THEN n.end_reason "
                "  WHEN n.status = 'repealed' THEN 'repealed' "
                "  ELSE 'superseded' END "
                "REMOVE n.status RETURN count(n) AS c",
                open=NO_END_DATE,
            )
            return int((await changed.single(strict=True))["c"])

        return await self._write("neo4j_graph.migrate_to_derived_status", {}, work)

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

    # ---- what a loader compares with (not a traversal) ----------------------

    async def stored(self, nodes: Sequence[NodeRef]) -> dict[NodeRef, StoredNode]:
        """Named nodes as stored, with their outgoing edges — one statement
        per label. Like `counts()`, outside the query builder because it
        answers "what did we write", not "what was the law on a date": a
        loader's integrity check needs every edge, whatever its dates."""
        found: dict[NodeRef, StoredNode] = {}
        with telemetry.span("neo4j_graph.stored", **{"graph.nodes": len(nodes)}):
            for label in sorted({n.label for n in nodes}):
                ids = sorted({n.stable_id for n in nodes if n.label == label})
                query = (
                    f"UNWIND $ids AS id MATCH (n:{checked_label(label)} {{stable_id: id}}) "
                    "OPTIONAL MATCH (n)-[r]->(m) "
                    "RETURN n.stable_id AS id, properties(n) AS props, "
                    "collect(CASE WHEN r IS NULL THEN null ELSE {type: type(r), "
                    "label: labels(m)[0], target: m.stable_id, props: properties(r)} END) AS edges"
                )
                records, _, _ = await self._driver.execute_query(
                    _trusted(query), ids=ids, database_=self._database, routing_=RoutingControl.READ
                )
                for record in records:
                    node = NodeRef(label, str(record["id"]))
                    edges = tuple(
                        StoredEdge(
                            edge_type=str(e["type"]),
                            target=NodeRef(str(e["label"]), str(e["target"])),
                            properties={
                                k: to_python(v)
                                for k, v in cast("dict[str, object]", e["props"]).items()
                            },
                        )
                        for e in cast("list[dict[str, object]]", record["edges"])
                    )
                    found[node] = StoredNode(node, _stored_from(record, "props"), edges)
        return found

    # ---- loads: which version a loader wrote (#153, ADR-0034) ----------------

    async def begin_load(self, version: SourceVersion) -> str:
        """Record that a load started: a `Load` node (`graph_schema.LOAD_LABEL`)
        with its kind, tag, commit and start. Written once and given its end
        once, by `finish_load` or `abandon_load`; never rewritten or removed.

        Loads of a kind are serialised. An open one (begun, neither finished
        nor abandoned) refuses this, naming it; the check and the write are
        one transaction, and an open load carries `open_kind`, which a
        uniqueness constraint (`schema.cypher`) lets only one load per kind
        hold, so two that begin together can't both succeed. Times come from
        the server's clock, never the caller's."""
        load_id = uuid.uuid4().hex
        kind = version.kind.value
        props: dict[str, object] = {
            "load_id": load_id,
            "kind": kind,
            "sha": version.sha,
            "open_kind": kind,
        }
        if version.tag is not None:  # Neo4j stores no null: untagged has no tag
            props["tag"] = version.tag

        async def work(tx: AsyncManagedTransaction) -> str:
            open_load = await (
                await tx.run(
                    "MATCH (l:Load {kind: $kind}) "
                    "WHERE l.finished_at IS NULL AND l.abandoned_at IS NULL "
                    "RETURN l.load_id AS id, toString(l.started_at) AS started LIMIT 1",
                    kind=kind,
                )
            ).single()
            if open_load is not None:
                raise GraphWriteRefused(
                    f"a {kind} load ({open_load['id']}, begun {open_load['started']}) is still "
                    "open: wait for it to finish, or, if it failed, abandon it by re-running the "
                    f"loader with --abandon {open_load['id']} --by NAME --reason WHY (#153)"
                )
            await tx.run("CREATE (l:Load) SET l = $props, l.started_at = datetime()", props=props)
            return load_id

        attributes = {"graph.load_kind": kind, "graph.load_version": version.described}
        try:
            return await self._write("neo4j_graph.begin_load", attributes, work)
        except ConstraintError as raced:
            raise GraphWriteRefused(
                f"another {kind} load began at the same moment; the first to begin is the one "
                "that runs (#153)"
            ) from raced

    async def finish_load(self, load_id: str) -> None:
        """Set `finished_at` on an open load, once, and close it. An unknown
        load, or one already finished or abandoned, is refused."""
        await self._close_load("finish", load_id, {})

    async def abandon_load(self, load_id: str, by: str, reason: str, *, wrote: bool = True) -> None:
        """Set `abandoned_at`, with who, why and whether it may have written,
        on an open load, once, and close it. It stays what it was: begun, and
        never finished, so the version the graph holds is unchanged; unless it
        may have written (`wrote`, the default), it blocks assessments until a
        later load finishes (`loaded_sources`). An unknown load, or one
        already finished or abandoned, is refused, as is an abandonment that
        names nobody or no reason."""
        if not (by.strip() and reason.strip()):
            raise GraphWriteRefused("abandoning a load names who did it and why (#153)")
        await self._close_load("abandon", load_id, {"by": by, "reason": reason, "wrote": wrote})

    async def _close_load(
        self, action: str, load_id: str, parameters: Mapping[str, str | bool]
    ) -> None:
        """Lock, then read, then set, as a retirement does, so two concurrent
        closes of one load can't both win; and release the load's hold on its
        kind (`open_kind`), which lets the next load begin."""
        sets = {
            "finish": "l.finished_at = datetime()",
            "abandon": "l.abandoned_at = datetime(), l.abandoned_by = $by, "
            "l.abandon_reason = $reason, l.wrote = $wrote",
        }[action]

        async def work(tx: AsyncManagedTransaction) -> None:
            locked = await tx.run(
                "MATCH (l:Load {load_id: $load_id}) SET l._lock = true RETURN count(l) AS found",
                load_id=load_id,
            )
            if (await locked.single(strict=True))["found"] == 0:
                raise GraphWriteRefused(f"no load {load_id!r} to {action}")
            result = await tx.run(
                "MATCH (l:Load {load_id: $load_id}) REMOVE l._lock "
                "RETURN l.finished_at IS NOT NULL AS finished, "
                "l.abandoned_at IS NOT NULL AS abandoned",
                load_id=load_id,
            )
            state = await result.single(strict=True)
            if state["finished"]:
                raise GraphWriteRefused(
                    f"load {load_id!r} already finished; a load ends once, so it can't be "
                    f"{action}ed"
                )
            if state["abandoned"]:
                raise GraphWriteRefused(
                    f"load {load_id!r} was abandoned; a load ends once, so it can't be {action}ed"
                )
            await tx.run(
                _trusted(f"MATCH (l:Load {{load_id: $load_id}}) SET {sets} REMOVE l.open_kind"),
                {"load_id": load_id, **parameters},
            )

        attributes: dict[str, str | int] = {"graph.load_id": load_id}
        if "wrote" in parameters:
            attributes["graph.load_wrote"] = int(parameters["wrote"])
        await self._write(f"neo4j_graph.{action}_load", attributes, work)

    async def loaded_sources(self) -> LoadedSources:
        """The latest finished load of each kind, and what stands in the way of
        assessing against it: `loading` while a load is open (running, or
        failed and not yet abandoned), and `blocked_by_abandoned` while a load
        abandoned after it may have written is newer than the latest finished
        one. Only a later finished load clears that: abandoning releases the
        hold on the kind but leaves the graph as the failed load left it.
        Latest is by start, on the server's clock; since loads of a kind are
        serialised that is also the order they ended in. Like `counts()`, a
        read outside the query builder that returns no node of the law."""
        with telemetry.span("neo4j_graph.loaded_sources") as s:
            found = {kind: await self._loaded(kind) for kind in SourceKind}
            for kind, state in found.items():
                version = state.version.described if state.version else "none"
                s.set_attribute(f"graph.{kind.value}_version", version)
                s.set_attribute(f"graph.{kind.value}_loading", state.loading)
                s.set_attribute(
                    f"graph.{kind.value}_blocked_by_abandoned", state.blocked_by_abandoned
                )
            return LoadedSources(corpus=found[SourceKind.CORPUS], mapping=found[SourceKind.MAPPING])

    async def _loaded(self, kind: SourceKind) -> LoadedSource:
        records, _, _ = await self._driver.execute_query(
            "OPTIONAL MATCH (f:Load {kind: $kind}) WHERE f.finished_at IS NOT NULL "
            "WITH f ORDER BY f.started_at DESC, f.load_id DESC LIMIT 1 "
            "OPTIONAL MATCH (u:Load {kind: $kind}) "
            "WHERE u.finished_at IS NULL AND u.abandoned_at IS NULL "
            "WITH f, count(u) AS open "
            "OPTIONAL MATCH (a:Load {kind: $kind}) "
            "WHERE a.abandoned_at IS NOT NULL AND coalesce(a.wrote, true) "
            "AND (f IS NULL OR a.started_at > f.started_at) "
            "RETURN f.tag AS tag, f.sha AS sha, open, count(a) AS abandoned",
            kind=kind.value,
            database_=self._database,
            routing_=RoutingControl.READ,
        )
        (record,) = records
        version = None
        if record["sha"] is not None:
            version = SourceVersion(kind, sha=str(record["sha"]), tag=record["tag"])
        return LoadedSource(
            version=version,
            loading=int(record["open"]) > 0,
            blocked_by_abandoned=int(record["abandoned"]) > 0,
        )

    # ---- counts (Phase 2 exit: Aura usage under 10% of its limits) ----------

    async def counts(self) -> GraphCounts:
        """Whole-graph totals, `Load` records included. Not a traversal and
        returns no node, so it is the one read outside the query builder
        (module docstring)."""
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
    "checked_edge_type",
    "checked_label",
]
