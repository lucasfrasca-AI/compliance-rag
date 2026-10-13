"""The graph records each load (#153, ADR-0034), on a real Neo4j server.

A `Load` says which corpus or mapping version was written, from which commit,
and whether the loader finished. `loaded_sources()` reads it back as the
latest finished load of each kind, and whether a newer one hasn't finished:
that is what lets the API refuse to record an assessment against a graph a
loader left part-written. The loaders wrap their writes in `recorded_load`.

Local Neo4j only, like the rest of the graph tests (`tests/test_neo4j_graph.py`).
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from neo4j.exceptions import ConstraintError
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.graph_query import GraphQuery
from compliance_rag.adapters.graph_schema import (
    LOAD_LABEL,
    PERSISTED_LABELS,
    GraphCounts,
    GraphSchemaRefused,
    GraphWriteRefused,
    NodeRef,
)
from compliance_rag.adapters.interfaces import (
    LoadedSource,
    LoadedSources,
    SourceKind,
    SourceVersion,
)
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.graph.loader import load_instrument_version, recorded_load
from compliance_rag.graph.track_a import GraphTrackA
from tests.test_graph_loader import JAN, SINCE, V1
from tests.test_neo4j_graph import cypher, needs_neo4j
from tests.test_neo4j_graph import graph as graph

CORPUS, MAPPING = SourceKind.CORPUS, SourceKind.MAPPING


def version(kind: SourceKind, n: int, tag: str | None = None) -> SourceVersion:
    return SourceVersion(kind, sha=f"{n:040x}", tag=tag)


M1 = version(MAPPING, 1, "mappings-2026.10")
M2 = version(MAPPING, 2, "mappings-2026.11")
M3 = version(MAPPING, 3)  # untagged
C1 = version(CORPUS, 10, "corpus-2026.10")


@needs_neo4j
async def test_nothing_is_loaded_in_a_graph_nothing_was_loaded_into(graph: Neo4jGraph) -> None:
    assert await graph.loaded_sources() == LoadedSources()


@needs_neo4j
async def test_a_load_is_recorded_when_it_begins(graph: Neo4jGraph) -> None:
    load_id = await graph.begin_load(M1)
    (row,) = await cypher(graph, f"MATCH (l:{LOAD_LABEL}) RETURN properties(l) AS p")
    stored = row["p"]
    assert (stored["load_id"], stored["kind"], stored["tag"], stored["sha"]) == (
        load_id,
        "mapping",
        "mappings-2026.10",
        M1.sha,
    )
    assert stored["started_at"] is not None and "finished_at" not in stored
    # Begun is not loaded: a load that hasn't finished is the graph part-written.
    assert await graph.loaded_sources() == LoadedSources(mapping=LoadedSource(None, loading=True))


@needs_neo4j
async def test_a_finished_load_is_what_the_graph_holds(graph: Neo4jGraph) -> None:
    await graph.finish_load(await graph.begin_load(M1))
    held = await graph.loaded_sources()
    assert held.mapping == LoadedSource(M1, loading=False)
    assert held.corpus == LoadedSource()  # the other kind is untouched
    assert held.mapping.version is not None and held.mapping.version.tag == "mappings-2026.10"


@needs_neo4j
async def test_an_untagged_load_is_recorded_by_its_sha_alone(graph: Neo4jGraph) -> None:
    await graph.finish_load(await graph.begin_load(M3))
    (row,) = await cypher(graph, "MATCH (l:Load) RETURN keys(l) AS keys")
    assert "tag" not in row["keys"]
    held = (await graph.loaded_sources()).mapping.version
    assert held is not None and held == M3
    assert held.untagged and held.tag is None


@needs_neo4j
async def test_the_latest_finished_load_wins(graph: Neo4jGraph) -> None:
    for loaded in (M1, M2):
        await graph.finish_load(await graph.begin_load(loaded))
    assert (await graph.loaded_sources()).mapping == LoadedSource(M2, loading=False)


@needs_neo4j
async def test_the_latest_is_by_when_a_load_started_not_when_it_finished(
    graph: Neo4jGraph,
) -> None:
    """A slow earlier load finishing after a later one doesn't take its place."""
    earlier = await graph.begin_load(M1)
    later = await graph.begin_load(M2)
    await graph.finish_load(later)
    await graph.finish_load(earlier)
    assert (await graph.loaded_sources()).mapping == LoadedSource(M2, loading=False)


@needs_neo4j
async def test_a_newer_load_that_hasnt_finished_is_loading_over_the_last_finished(
    graph: Neo4jGraph,
) -> None:
    await graph.finish_load(await graph.begin_load(M1))
    await graph.begin_load(M2)
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=True)


@needs_neo4j
async def test_a_failed_load_followed_by_a_successful_one_is_no_longer_loading(
    graph: Neo4jGraph,
) -> None:
    await graph.finish_load(await graph.begin_load(M1))
    await graph.begin_load(M2)  # never finished: it failed
    assert (await graph.loaded_sources()).mapping.loading
    await graph.finish_load(await graph.begin_load(M3))
    assert (await graph.loaded_sources()).mapping == LoadedSource(M3, loading=False)


@needs_neo4j
async def test_the_two_kinds_are_loaded_independently(graph: Neo4jGraph) -> None:
    await graph.finish_load(await graph.begin_load(C1))
    await graph.begin_load(M1)
    held = await graph.loaded_sources()
    assert held.corpus == LoadedSource(C1, loading=False)
    assert held.mapping == LoadedSource(None, loading=True)


@needs_neo4j
async def test_a_load_finishes_once(graph: Neo4jGraph) -> None:
    load_id = await graph.begin_load(M1)
    await graph.finish_load(load_id)
    (before,) = await cypher(graph, "MATCH (l:Load) RETURN l.finished_at AS at")
    with pytest.raises(GraphWriteRefused, match="already finished"):
        await graph.finish_load(load_id)
    (after,) = await cypher(graph, "MATCH (l:Load) RETURN l.finished_at AS at")
    assert after["at"] == before["at"]


@needs_neo4j
async def test_an_unknown_load_cannot_be_finished(graph: Neo4jGraph) -> None:
    with pytest.raises(GraphWriteRefused, match="no load"):
        await graph.finish_load("0" * 32)
    assert await cypher(graph, "MATCH (l:Load) RETURN l") == []


@needs_neo4j
async def test_concurrent_finishes_cannot_both_win(graph: Neo4jGraph) -> None:
    for _ in range(5):
        load_id = await graph.begin_load(M1)
        results = await asyncio.gather(
            graph.finish_load(load_id), graph.finish_load(load_id), return_exceptions=True
        )
        refused = [r for r in results if isinstance(r, GraphWriteRefused)]
        assert len(refused) == 1, results


@needs_neo4j
async def test_a_load_is_metadata_not_law(graph: Neo4jGraph) -> None:
    """Not a node the ontology can hold or the query builder can reach."""
    assert LOAD_LABEL == "Load" and LOAD_LABEL not in PERSISTED_LABELS
    with pytest.raises(GraphSchemaRefused):
        NodeRef("Load", "x")
    with pytest.raises(GraphSchemaRefused):
        GraphQuery(as_of=dt.date(2026, 10, 14), max_depth=1).start("Load")
    await graph.begin_load(M1)
    assert await graph.counts() == GraphCounts(nodes=1, edges=0)  # it is stored, so it counts


@needs_neo4j
async def test_the_schema_makes_a_load_id_unique(graph: Neo4jGraph) -> None:
    await graph.apply_schema()
    await cypher(graph, "CREATE (:Load {load_id: 'x'})")
    with pytest.raises(ConstraintError):
        await cypher(graph, "CREATE (:Load {load_id: 'x'})")


@needs_neo4j
async def test_each_load_call_is_a_span(
    graph: Neo4jGraph, span_exporter: InMemorySpanExporter
) -> None:
    load_id = await graph.begin_load(M1)
    await graph.finish_load(load_id)
    await graph.loaded_sources()
    spans = {s.name: dict(s.attributes or {}) for s in span_exporter.get_finished_spans()}
    assert spans["neo4j_graph.begin_load"]["graph.load_kind"] == "mapping"
    assert spans["neo4j_graph.begin_load"]["graph.load_version"] == M1.described
    assert spans["neo4j_graph.finish_load"]["graph.load_id"] == load_id
    assert spans["neo4j_graph.loaded_sources"]["graph.mapping_version"] == M1.described
    assert spans["neo4j_graph.loaded_sources"]["graph.corpus_version"] == "none"
    assert spans["neo4j_graph.loaded_sources"]["graph.mapping_loading"] is False


# ---- the loaders' wrapper ----------------------------------------------------------------


@needs_neo4j
async def test_a_load_that_returns_is_finished(graph: Neo4jGraph) -> None:
    async with recorded_load(graph, M1) as load_id:
        # Mid-load, the graph says so.
        assert (await graph.loaded_sources()).mapping == LoadedSource(None, loading=True)
    assert len(load_id) == 32
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=False)


@needs_neo4j
async def test_a_load_that_raises_is_left_unfinished(graph: Neo4jGraph) -> None:
    await graph.finish_load(await graph.begin_load(M1))
    with pytest.raises(RuntimeError, match="the load broke"):
        async with recorded_load(graph, M2):
            raise RuntimeError("the load broke")
    # The earlier version is still the last finished one, and the failure shows.
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=True)
    (row,) = await cypher(
        graph, f"MATCH (l:Load {{sha: '{M2.sha}'}}) RETURN l.finished_at IS NULL AS open"
    )
    assert row["open"] is True


@needs_neo4j
async def test_a_load_that_is_refused_before_any_write_is_left_unfinished_too(
    graph: Neo4jGraph,
) -> None:
    """The wrapper can't tell a load that failed halfway from one refused
    before its first write, so it treats both as failed: running the loader
    again, once the refusal is dealt with, clears it."""
    with pytest.raises(ValueError, match="a version with no clauses"):
        async with recorded_load(graph, C1):
            await load_instrument_version(
                graph, (), instrument_since=SINCE, effective=JAN, supersedes=None
            )
    assert (await graph.loaded_sources()).corpus.loading
    async with recorded_load(graph, C1):
        await graph.apply_schema()
        await load_instrument_version(
            graph, V1, instrument_since=SINCE, effective=JAN, supersedes=None
        )
    assert (await graph.loaded_sources()).corpus == LoadedSource(C1, loading=False)


@needs_neo4j
async def test_track_a_reports_what_its_reader_holds(graph: Neo4jGraph) -> None:
    await graph.finish_load(await graph.begin_load(C1))
    await graph.begin_load(M1)
    assert await GraphTrackA(graph).loaded_sources() == await graph.loaded_sources()
    assert await GraphTrackA(graph).loaded_sources() == LoadedSources(
        corpus=LoadedSource(C1), mapping=LoadedSource(None, loading=True)
    )
