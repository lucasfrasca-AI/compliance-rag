"""The graph query builder (issue #88): `as_of` and a depth cap are required,
the bitemporal predicate holds on every node of a returned path, and every
variable-length pattern is bounded.

Builder tests need no server. The bitemporal tests run on a real Neo4j
(`NEO4J_TEST_URI`, local only — see `tests/test_neo4j_graph.py`) with
structural nodes only — an instrument, its versions and clauses — never
ontology content (obligations, conditions, applicability).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from neo4j import AsyncDriver

from compliance_rag.adapters.graph_query import (
    MAX_DEPTH_CEILING,
    BuiltQuery,
    Direction,
    GraphQuery,
)
from compliance_rag.adapters.graph_schema import GraphSchemaRefused
from compliance_rag.adapters.neo4j_graph import (
    Bitemporal,
    GraphCounts,
    GraphWriteRefused,
    Neo4jGraph,
    NodeRef,
    Status,
    to_python,
)
from tests.test_neo4j_graph import cypher, needs_neo4j
from tests.test_neo4j_graph import graph as graph


async def _properties(graph: Neo4jGraph, node: NodeRef) -> dict[str, object]:
    (row,) = await cypher(
        graph, f"MATCH (n {{stable_id: '{node.stable_id}'}}) RETURN n {{.*}} AS p"
    )
    return {k: to_python(v) for k, v in row["p"].items()}


JAN, MAR, JUN, SEP = (dt.date(2026, m, 1) for m in (1, 3, 6, 9))


def _q(**kwargs: Any) -> GraphQuery:  # Any: deliberately wrong types under test
    return GraphQuery(**kwargs)


# ---- the builder refuses an unscoped query ---------------------------------


def test_as_of_and_max_depth_have_no_defaults() -> None:
    with pytest.raises(TypeError):
        _q(max_depth=2)
    with pytest.raises(TypeError):
        _q(as_of=JAN)


@pytest.mark.parametrize(
    "as_of", [None, "2026-01-01", dt.datetime(2026, 1, 1, tzinfo=dt.UTC), 20260101]
)
def test_as_of_must_be_a_date(as_of: object) -> None:
    with pytest.raises(ValueError, match="as_of"):
        _q(as_of=as_of, max_depth=2)


@pytest.mark.parametrize("depth", [0, -1, MAX_DEPTH_CEILING + 1, None, True, 2.0])
def test_max_depth_must_be_a_bounded_int(depth: object) -> None:
    with pytest.raises(ValueError, match="max_depth"):
        _q(as_of=JAN, max_depth=depth)


def test_hops_beyond_the_cap_are_refused_across_segments() -> None:
    query = GraphQuery(as_of=JAN, max_depth=3).start("Clause")
    query.follow(("PART_OF",), "InstrumentVersion", direction=Direction.OUT, max_hops=2)
    with pytest.raises(ValueError, match="exceeds max_depth 3"):
        query.follow(("VERSION_OF",), "Instrument", direction=Direction.OUT, max_hops=2)


def test_a_built_query_cannot_be_forged() -> None:
    """`Neo4jGraph.read` runs only a `BuiltQuery`, and only the builder can
    make one: a hand-written one, or a real one altered after building, fails
    its seal."""
    real = GraphQuery(as_of=JAN, max_depth=1).start("Clause").build()
    with pytest.raises(TypeError, match="unchanged"):
        BuiltQuery(text="MATCH (n) RETURN n", parameters={}, as_of=JAN, max_depth=1, seal="x")
    with pytest.raises(TypeError, match="unchanged"):
        dataclasses.replace(real, text="MATCH (n) DETACH DELETE n")
    with pytest.raises(TypeError, match="unchanged"):
        dataclasses.replace(real, max_depth=6)
    tampered = GraphQuery(as_of=JAN, max_depth=1).start("Clause").build()
    object.__setattr__(tampered, "text", "MATCH (n) DETACH DELETE n")
    with pytest.raises(TypeError, match="unchanged"):
        tampered.verify()


async def test_read_refuses_a_query_altered_after_building() -> None:
    tampered = GraphQuery(as_of=JAN, max_depth=1).start("Clause").build()
    object.__setattr__(tampered, "parameters", {"as_of": dt.date.max})
    with pytest.raises(TypeError, match="unchanged"):
        await Neo4jGraph(cast("AsyncDriver", None), database="unused").read(tampered)


@pytest.mark.parametrize("hops", [0, True, 2.0, "2", None])
def test_max_hops_must_be_a_positive_int(hops: object) -> None:
    query = GraphQuery(as_of=JAN, max_depth=3).start("Clause")
    with pytest.raises(ValueError, match=r"max_hops|exceeds"):
        query.follow(("PART_OF",), "InstrumentVersion", direction=Direction.OUT, max_hops=hops)  # pyright: ignore[reportArgumentType]


def test_direction_must_be_a_direction() -> None:
    query = GraphQuery(as_of=JAN, max_depth=1).start("Clause")
    with pytest.raises(ValueError, match="direction"):
        query.follow(("PART_OF",), "InstrumentVersion", direction="sideways")  # pyright: ignore[reportArgumentType]


def test_every_variable_length_pattern_is_bounded_and_every_node_is_scoped() -> None:
    built = (
        GraphQuery(as_of=MAR, max_depth=3)
        .start("Clause", stable_id="act:v1:s1")
        .follow(("PART_OF",), "InstrumentVersion", direction=Direction.OUT)
        .follow(("VERSION_OF",), "Instrument", direction=Direction.OUT, max_hops=2)
        .build()
    )
    bounds = [int(b) for b in re.findall(r"\*1\.\.(\d+)\]", built.text)]
    assert bounds == [1, 2] and sum(bounds) <= built.max_depth
    assert "*]" not in built.text and not re.search(r"\*\d*\.\.\]", built.text)  # no open range
    assert "all(x IN nodes(p) WHERE x.valid_from <= $as_of AND $as_of < x.valid_to)" in built.text
    assert built.parameters == {"as_of": MAR, "start_id": "act:v1:s1"}


def test_values_are_parameters_never_cypher() -> None:
    hostile = "x'}) DETACH DELETE n //"
    built = GraphQuery(as_of=JAN, max_depth=1).start("Clause", stable_id=hostile).build()
    assert hostile not in built.text and built.parameters["start_id"] == hostile


def test_labels_and_edge_types_outside_the_schema_are_refused() -> None:
    with pytest.raises(GraphSchemaRefused):
        GraphQuery(as_of=JAN, max_depth=1).start("Organisation")
    with pytest.raises(GraphSchemaRefused):
        GraphQuery(as_of=JAN, max_depth=1).start("Clause").follow(
            ("HANDLES",), "Clause", direction=Direction.OUT
        )


# ---- bitemporal, on a real server ------------------------------------------


@pytest.fixture
async def amended(graph: Neo4jGraph) -> AsyncIterator[Neo4jGraph]:
    """An instrument amended once: v1 in force Jan-Jun, v2 from Jun, v2
    SUPERSEDES v1; one clause in each version. Loaded the way D-05 says an
    amendment happens: a new node, a SUPERSEDES edge, valid_to on the old."""
    await graph.apply_schema()
    act, v1, v2 = (
        NodeRef("Instrument", "act"),
        NodeRef("InstrumentVersion", "act:v1"),
        NodeRef("InstrumentVersion", "act:v2"),
    )
    c1, c2 = NodeRef("Clause", "act:v1:s1"), NodeRef("Clause", "act:v2:s1")
    await graph.put_node(act, Bitemporal(valid_from=JAN, status=Status.IN_FORCE))
    await graph.put_node(v1, Bitemporal(valid_from=JAN, status=Status.IN_FORCE))
    await graph.put_node(c1, Bitemporal(valid_from=JAN, status=Status.IN_FORCE))
    await graph.put_edge("PART_OF", c1, v1)
    await graph.put_edge("VERSION_OF", v1, act)
    await graph.amend(
        old=v1,
        new=v2,
        effective=JUN,
        status=Status.IN_FORCE,
    )
    await graph.put_node(c2, Bitemporal(valid_from=JUN, status=Status.IN_FORCE))
    await graph.put_edge("PART_OF", c2, v2)
    await graph.put_edge("VERSION_OF", v2, act)
    yield graph


def _versions_of(as_of: dt.date) -> BuiltQuery:
    return (
        GraphQuery(as_of=as_of, max_depth=1)
        .start("Instrument", stable_id="act")
        .follow(("VERSION_OF",), "InstrumentVersion", direction=Direction.IN)
        .build()
    )


@needs_neo4j
async def test_two_as_of_dates_return_different_versions(amended: Neo4jGraph) -> None:
    before = await amended.read(_versions_of(MAR))
    after = await amended.read(_versions_of(SEP))
    assert [r.node["stable_id"] for r in before] == ["act:v1"]
    assert [r.node["stable_id"] for r in after] == ["act:v2"]


@needs_neo4j
async def test_the_change_date_belongs_to_the_new_version(amended: Neo4jGraph) -> None:
    """valid_to is exclusive: on the effective date itself, v2 is in force."""
    assert [r.node["stable_id"] for r in await amended.read(_versions_of(JUN))] == ["act:v2"]


@needs_neo4j
async def test_a_path_through_a_retired_node_is_not_returned(amended: Neo4jGraph) -> None:
    """The clause act:v1:s1 itself is still open-ended, but its version was
    retired in June: after that, the path clause -> version -> instrument is
    not in force, even though both ends are."""

    def query(as_of: dt.date) -> BuiltQuery:
        return (
            GraphQuery(as_of=as_of, max_depth=2)
            .start("Clause", stable_id="act:v1:s1")
            .follow(("PART_OF",), "InstrumentVersion", direction=Direction.OUT)
            .follow(("VERSION_OF",), "Instrument", direction=Direction.OUT)
            .build()
        )

    assert [r.path for r in await amended.read(query(MAR))] == [("act:v1:s1", "act:v1", "act")]
    assert await amended.read(query(SEP)) == ()


@needs_neo4j
async def test_a_path_longer_than_the_cap_is_not_returned(amended: Neo4jGraph) -> None:
    """Clause -> version -> instrument is two hops. Built over one generic
    multi-hop segment, a cap of 1 can't reach the instrument; a cap of 2 can."""

    def query(cap: int) -> BuiltQuery:
        return (
            GraphQuery(as_of=MAR, max_depth=cap)
            .start("Clause", stable_id="act:v1:s1")
            .follow(("PART_OF", "VERSION_OF"), "Instrument", direction=Direction.OUT, max_hops=cap)
            .build()
        )

    assert await amended.read(query(1)) == ()
    assert [r.node["stable_id"] for r in await amended.read(query(2))] == ["act"]


@needs_neo4j
async def test_supersession_is_an_edge_and_nothing_else_changes(graph: Neo4jGraph) -> None:
    """D-05: amending v1 adds v2 and a SUPERSEDES edge and sets v1's valid_to.
    v1's every other property is exactly as it was."""
    await graph.apply_schema()
    v1, v2 = NodeRef("InstrumentVersion", "act:v1"), NodeRef("InstrumentVersion", "act:v2")
    await graph.put_node(v1, Bitemporal(valid_from=JAN, status=Status.IN_FORCE), {"title": "Act"})
    before = await _properties(graph, v1)
    await graph.amend(
        old=v1, new=v2, effective=JUN, status=Status.IN_FORCE, properties={"title": "Act"}
    )
    after = await _properties(graph, v1)
    assert {k for k in before if before[k] != after[k]} == {"valid_to"}
    assert after["valid_to"] == JUN
    supersedes = (
        GraphQuery(as_of=SEP, max_depth=1)
        .start("InstrumentVersion", stable_id="act:v2")
        .follow(("SUPERSEDES",), "InstrumentVersion", direction=Direction.OUT)
        .build()
    )
    # v1 is out of force in September, so the edge is not traversable then...
    assert await graph.read(supersedes) == ()
    assert await graph.counts() == GraphCounts(
        nodes=2, edges=1
    )  # ...but it is there, and nothing was deleted


@needs_neo4j
async def test_an_amendment_is_atomic(graph: Neo4jGraph) -> None:
    """If any part of an amendment is refused, none of it lands."""
    await graph.apply_schema()
    v1, v2 = NodeRef("InstrumentVersion", "act:v1"), NodeRef("InstrumentVersion", "act:v2")
    await graph.put_node(v1, Bitemporal(valid_from=JUN, status=Status.IN_FORCE))
    with pytest.raises(GraphWriteRefused, match="not after"):
        await graph.amend(old=v1, new=v2, effective=JAN, status=Status.IN_FORCE)
    assert await graph.counts() == GraphCounts(nodes=1, edges=0)
    assert (await _properties(graph, v1))["valid_to"] == dt.date.max


@needs_neo4j
async def test_an_amendment_refused_after_the_retire_rolls_the_retire_back(
    graph: Neo4jGraph,
) -> None:
    """The case that matters: the retire succeeds inside the transaction, then
    the new node is refused (it already exists with different content). The
    retire must not survive."""
    await graph.apply_schema()
    v1, v2 = NodeRef("InstrumentVersion", "act:v1"), NodeRef("InstrumentVersion", "act:v2")
    await graph.put_node(v1, Bitemporal(valid_from=JAN, status=Status.IN_FORCE))
    await graph.put_node(v2, Bitemporal(valid_from=JUN, status=Status.IN_FORCE), {"title": "x"})
    with pytest.raises(GraphWriteRefused, match="immutable"):
        await graph.amend(old=v1, new=v2, effective=JUN, status=Status.IN_FORCE)
    stored = await _properties(graph, v1)
    assert stored["valid_to"] == dt.date.max and "_lock" not in stored
    assert await graph.counts() == GraphCounts(nodes=2, edges=0)
