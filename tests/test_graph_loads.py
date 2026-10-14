"""The graph records each load (#153, ADR-0034), on a real Neo4j server.

A `Load` says which corpus or mapping version was written, from which commit,
and whether the loader finished. `loaded_sources()` reads it back as the latest
finished load of each kind, and whether one is still open: that is what lets
the API refuse to record an assessment against a graph a loader left
part-written. Loads of a kind are serialised, so there is one answer; a failed
load stays open until someone abandons it. The loaders run their checks, then
`recorded_load`, then their writes, so a load that was refused leaves no record.

Local Neo4j only, like the rest of the graph tests (`tests/test_neo4j_graph.py`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from neo4j.exceptions import ConstraintError
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from scripts import load_graph_structure, load_mappings

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
from compliance_rag.graph.loader import (
    check_instrument_version,
    check_mappings,
    load_instrument_version,
    recorded_load,
    write_instrument_version,
    write_mappings,
)
from compliance_rag.graph.mappings import MappingError, load_mapping_set
from compliance_rag.graph.track_a import GraphTrackA
from tests.test_graph_loader import FIXTURE, JAN, SEP, SINCE, V1, load_test_corpus
from tests.test_neo4j_graph import cypher, needs_neo4j
from tests.test_neo4j_graph import (
    graph as plain_graph,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

CORPUS, MAPPING = SourceKind.CORPUS, SourceKind.MAPPING


def version(kind: SourceKind, n: int, tag: str | None = None) -> SourceVersion:
    return SourceVersion(kind, sha=f"{n:040x}", tag=tag)


M1 = version(MAPPING, 1, "mappings-2026.10")
M2 = version(MAPPING, 2, "mappings-2026.11")
M3 = version(MAPPING, 3)  # untagged
C1 = version(CORPUS, 10, "corpus-2026.10")


@pytest.fixture
async def graph(plain_graph: Neo4jGraph) -> Neo4jGraph:  # noqa: F811
    """The test graph with the schema applied: the uniqueness constraint that
    lets only one load of a kind be open is what makes beginning atomic."""
    await plain_graph.apply_schema()
    return plain_graph


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
    assert stored["started_at"] is not None
    assert "finished_at" not in stored and "abandoned_at" not in stored
    assert stored["open_kind"] == "mapping"  # it holds its kind until it ends
    # Begun is not loaded: a load that hasn't finished is the graph part-written.
    assert await graph.loaded_sources() == LoadedSources(mapping=LoadedSource(None, loading=True))


@needs_neo4j
async def test_a_finished_load_is_what_the_graph_holds(graph: Neo4jGraph) -> None:
    await graph.finish_load(await graph.begin_load(M1))
    held = await graph.loaded_sources()
    assert held.mapping == LoadedSource(M1, loading=False)
    assert held.corpus == LoadedSource()  # the other kind is untouched
    assert held.mapping.version is not None and held.mapping.version.tag == "mappings-2026.10"
    (row,) = await cypher(graph, "MATCH (l:Load) RETURN keys(l) AS keys")
    assert "finished_at" in row["keys"] and "open_kind" not in row["keys"]  # the hold is released


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
async def test_times_come_from_the_servers_clock_not_the_callers(graph: Neo4jGraph) -> None:
    """A client whose clock is years out records the same times a right one
    would: ordering loads by the client's clock would let it rewrite history."""
    driver = graph._driver  # pyright: ignore[reportPrivateUsage]
    skewed = Neo4jGraph(
        driver, database="neo4j", clock=lambda: dt.datetime(2001, 1, 1, tzinfo=dt.UTC)
    )
    await skewed.finish_load(await skewed.begin_load(M1))
    (row,) = await cypher(graph, "MATCH (l:Load) RETURN l.started_at AS s, l.finished_at AS f")
    started, finished = row["s"].to_native(), row["f"].to_native()
    assert abs(dt.datetime.now(dt.UTC) - started) < dt.timedelta(minutes=5)
    assert started <= finished


# ---- serialised: one open load per kind ------------------------------------------------


@needs_neo4j
async def test_a_second_load_of_a_kind_cannot_begin_while_one_is_open(graph: Neo4jGraph) -> None:
    first = await graph.begin_load(M1)
    with pytest.raises(GraphWriteRefused, match=rf"mapping load \({first}.*still open.*--abandon"):
        await graph.begin_load(M2)
    assert len(await cypher(graph, "MATCH (l:Load) RETURN l")) == 1  # the refused one left nothing
    await graph.finish_load(first)
    await graph.begin_load(M2)  # the first ended: the next may begin


@needs_neo4j
async def test_the_two_kinds_are_loaded_independently(graph: Neo4jGraph) -> None:
    await graph.begin_load(C1)
    await graph.begin_load(M1)  # a corpus load being open doesn't hold the mappings back
    held = await graph.loaded_sources()
    assert held.corpus == LoadedSource(None, loading=True)
    assert held.mapping == LoadedSource(None, loading=True)


@needs_neo4j
async def test_loads_that_begin_together_cannot_both_begin(graph: Neo4jGraph) -> None:
    """Both read 'none open' and both write: only the uniqueness constraint on
    the hold a load takes on its kind stops the second."""
    for attempt in range(5):
        results = await asyncio.gather(
            graph.begin_load(version(MAPPING, 100 + attempt)),
            graph.begin_load(version(MAPPING, 200 + attempt)),
            return_exceptions=True,
        )
        winners = [r for r in results if isinstance(r, str)]
        refused = [r for r in results if isinstance(r, GraphWriteRefused)]
        assert len(winners) == 1 and len(refused) == 1, results
        await graph.finish_load(winners[0])
    assert len(await cypher(graph, "MATCH (l:Load) RETURN l")) == 5


@needs_neo4j
async def test_the_hold_on_a_kind_is_unique_in_the_schema(graph: Neo4jGraph) -> None:
    await cypher(graph, "CREATE (:Load {load_id: 'a', open_kind: 'mapping'})")
    with pytest.raises(ConstraintError):
        await cypher(graph, "CREATE (:Load {load_id: 'b', open_kind: 'mapping'})")
    await cypher(graph, "CREATE (:Load {load_id: 'c', open_kind: 'corpus'})")


@needs_neo4j
async def test_a_newer_load_that_hasnt_finished_is_loading_over_the_last_finished(
    graph: Neo4jGraph,
) -> None:
    await graph.finish_load(await graph.begin_load(M1))
    await graph.begin_load(M2)
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=True)


# ---- abandoning a load that failed -----------------------------------------------------


@needs_neo4j
async def test_a_failed_load_blocks_until_it_is_abandoned_and_another_finishes(
    graph: Neo4jGraph,
) -> None:
    await graph.finish_load(await graph.begin_load(M1))
    failed = await graph.begin_load(M2)  # never finished: it failed
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=True)
    with pytest.raises(GraphWriteRefused, match="still open"):
        await graph.begin_load(M3)  # nothing clears it by itself, however long it has been
    await graph.abandon_load(failed, "lead@acme.example", "the connection dropped halfway")
    # Abandoned is not finished: the graph holds what it held, and isn't loading.
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=False)
    await graph.finish_load(await graph.begin_load(M3))
    assert (await graph.loaded_sources()).mapping == LoadedSource(M3, loading=False)


@needs_neo4j
async def test_abandoning_the_only_load_leaves_nothing_loaded_and_not_loading(
    graph: Neo4jGraph,
) -> None:
    await graph.abandon_load(await graph.begin_load(M1), "lead", "never mind")
    assert await graph.loaded_sources() == LoadedSources()


@needs_neo4j
async def test_an_abandonment_records_who_when_and_why_and_releases_the_hold(
    graph: Neo4jGraph,
) -> None:
    load_id = await graph.begin_load(M1)
    await graph.abandon_load(load_id, "lead@acme.example", "the connection dropped")
    (row,) = await cypher(graph, "MATCH (l:Load) RETURN properties(l) AS p")
    stored = row["p"]
    assert stored["abandoned_by"] == "lead@acme.example"
    assert stored["abandon_reason"] == "the connection dropped"
    assert stored["abandoned_at"] is not None
    assert "finished_at" not in stored  # it never finished
    assert "open_kind" not in stored


@needs_neo4j
async def test_an_abandonment_names_who_and_why(graph: Neo4jGraph) -> None:
    load_id = await graph.begin_load(M1)
    for by, reason in (("", "why"), ("who", ""), (" ", "why"), ("who", "  ")):
        with pytest.raises(GraphWriteRefused, match="who did it and why"):
            await graph.abandon_load(load_id, by, reason)
    assert (await graph.loaded_sources()).mapping.loading  # still open


@needs_neo4j
async def test_a_load_ends_once(graph: Neo4jGraph) -> None:
    """Finished or abandoned: either way it can't be finished or abandoned again."""
    finished = await graph.begin_load(M1)
    await graph.finish_load(finished)
    abandoned = await graph.begin_load(M2)
    await graph.abandon_load(abandoned, "lead", "failed")
    (before,) = await cypher(
        graph, f"MATCH (l:Load {{load_id: '{finished}'}}) RETURN l.finished_at AS at"
    )
    for load_id in (finished, abandoned):
        with pytest.raises(GraphWriteRefused, match="ends once"):
            await graph.finish_load(load_id)
        with pytest.raises(GraphWriteRefused, match="ends once"):
            await graph.abandon_load(load_id, "lead", "again")
    with pytest.raises(GraphWriteRefused, match="already finished"):
        await graph.finish_load(finished)
    with pytest.raises(GraphWriteRefused, match="was abandoned"):
        await graph.finish_load(abandoned)
    (after,) = await cypher(
        graph, f"MATCH (l:Load {{load_id: '{finished}'}}) RETURN l.finished_at AS at"
    )
    assert after["at"] == before["at"]
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=False)


@needs_neo4j
async def test_an_unknown_load_can_be_neither_finished_nor_abandoned(graph: Neo4jGraph) -> None:
    with pytest.raises(GraphWriteRefused, match="no load"):
        await graph.finish_load("0" * 32)
    with pytest.raises(GraphWriteRefused, match="no load"):
        await graph.abandon_load("0" * 32, "lead", "why")
    assert await cypher(graph, "MATCH (l:Load) RETURN l") == []


@needs_neo4j
async def test_concurrent_closes_of_one_load_cannot_both_win(graph: Neo4jGraph) -> None:
    for attempt in range(5):
        load_id = await graph.begin_load(version(MAPPING, 300 + attempt))
        results = await asyncio.gather(
            graph.finish_load(load_id),
            graph.finish_load(load_id),
            graph.abandon_load(load_id, "lead", "raced"),
            return_exceptions=True,
        )
        refused = [r for r in results if isinstance(r, GraphWriteRefused)]
        assert len(refused) == 2, results


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
async def test_each_load_call_is_a_span(
    graph: Neo4jGraph, span_exporter: InMemorySpanExporter
) -> None:
    load_id = await graph.begin_load(M1)
    await graph.finish_load(load_id)
    other = await graph.begin_load(M2)
    await graph.abandon_load(other, "lead", "failed")
    await graph.loaded_sources()
    spans = {s.name: dict(s.attributes or {}) for s in span_exporter.get_finished_spans()}
    assert spans["neo4j_graph.begin_load"]["graph.load_kind"] == "mapping"
    assert spans["neo4j_graph.begin_load"]["graph.load_version"] in {M1.described, M2.described}
    assert spans["neo4j_graph.finish_load"]["graph.load_id"] == load_id
    assert spans["neo4j_graph.abandon_load"]["graph.load_id"] == other
    assert spans["neo4j_graph.loaded_sources"]["graph.mapping_version"] == M1.described
    assert spans["neo4j_graph.loaded_sources"]["graph.corpus_version"] == "none"
    assert spans["neo4j_graph.loaded_sources"]["graph.mapping_loading"] is False


# ---- the loaders' wrapper ----------------------------------------------------------------


@needs_neo4j
async def test_a_load_that_returns_is_finished(graph: Neo4jGraph) -> None:
    async with recorded_load(graph, M1):
        # Mid-load, the graph says so.
        assert (await graph.loaded_sources()).mapping == LoadedSource(None, loading=True)
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=False)


@needs_neo4j
async def test_a_load_that_raises_is_left_open(graph: Neo4jGraph) -> None:
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
    with pytest.raises(GraphWriteRefused, match="still open"):
        async with recorded_load(graph, M3):
            raise AssertionError("a second load began over an open one")


@needs_neo4j
async def test_a_load_refused_by_its_checks_leaves_no_record_at_all(graph: Neo4jGraph) -> None:
    """The loaders check, then record, then write: what the checks refuse (the
    routine D-05 refusals) must not look like a load that failed, or each one
    would stop assessments until someone abandoned it."""
    with pytest.raises(ValueError, match="a version with no clauses"):
        await check_instrument_version(graph, (), effective=JAN, supersedes=None)
    with pytest.raises(ValueError, match="nothing to supersede"):
        await check_instrument_version(graph, V1, effective=JAN, supersedes="v0")
    with pytest.raises(MappingError, match="not in the graph"):
        await check_mappings(graph, load_mapping_set([FIXTURE]), today=SEP)  # no corpus loaded
    assert await cypher(graph, "MATCH (l:Load) RETURN l") == []
    assert await graph.loaded_sources() == LoadedSources()


@needs_neo4j
async def test_the_split_loaders_do_what_the_whole_one_does(graph: Neo4jGraph) -> None:
    checked = await check_instrument_version(graph, V1, effective=JAN, supersedes=None)
    async with recorded_load(graph, C1):
        report = await write_instrument_version(
            graph, V1, checked, instrument_since=SINCE, effective=JAN, supersedes=None
        )
    assert report.nodes_created == 4 and report.counts.nodes == 4 + 1  # and the Load record
    again = await load_instrument_version(
        graph, V1, instrument_since=SINCE, effective=JAN, supersedes=None
    )
    assert again.nodes_created == 0  # the whole loader, idempotent over the split one's work
    mappings = load_mapping_set([FIXTURE])
    found = await check_mappings(graph, mappings, today=SEP)
    async with recorded_load(graph, M1):
        await write_mappings(graph, mappings, found, None)
    held = await graph.loaded_sources()
    assert held == LoadedSources(corpus=LoadedSource(C1), mapping=LoadedSource(M1))


@needs_neo4j
async def test_track_a_reports_what_its_reader_holds(graph: Neo4jGraph) -> None:
    await graph.finish_load(await graph.begin_load(C1))
    await graph.begin_load(M1)
    assert await GraphTrackA(graph).loaded_sources() == await graph.loaded_sources()
    assert await GraphTrackA(graph).loaded_sources() == LoadedSources(
        corpus=LoadedSource(C1), mapping=LoadedSource(None, loading=True)
    )


# ---- the loader scripts, end to end on the test graph ---------------------------------------


def _use(graph: Neo4jGraph, script: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # Any: a module
    """Point a loader script at the test graph (which it must not close)."""

    @asynccontextmanager
    async def connect(_: object) -> AsyncGenerator[Neo4jGraph]:
        yield graph

    monkeypatch.setattr(script.Neo4jGraph, "from_settings", connect)


def _resolves_to(fixed: SourceVersion) -> Callable[..., SourceVersion]:
    """A stand-in for `resolve_source_version`: the repository's own state
    (clean or not, tagged or not) is not what these tests are about."""

    def resolve(kind: SourceKind, *, repo: Path) -> SourceVersion:
        return fixed

    return resolve


def _mappings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mapping script reads `MAPPINGS`: the invented fixture, not the law."""
    shutil.copy(FIXTURE, tmp_path / "test-obligations.yaml")
    monkeypatch.setattr(load_mappings, "MAPPINGS", tmp_path)
    monkeypatch.setattr(load_mappings, "resolve_source_version", _resolves_to(M1))


@needs_neo4j
async def test_the_mapping_script_records_a_load_that_returned(
    graph: Neo4jGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await load_test_corpus(graph)
    _use(graph, load_mappings, monkeypatch)
    _mappings_dir(tmp_path, monkeypatch)
    await load_mappings.main(SEP, None)
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=False)


@needs_neo4j
async def test_the_mapping_script_refused_by_its_checks_leaves_no_record(
    graph: Neo4jGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(graph, load_mappings, monkeypatch)
    _mappings_dir(tmp_path, monkeypatch)
    with pytest.raises(MappingError, match="not in the graph"):  # the corpus isn't loaded
        await load_mappings.main(SEP, None)
    assert await cypher(graph, "MATCH (l:Load) RETURN l") == []
    assert await graph.loaded_sources() == LoadedSources()  # assessments are not blocked


@needs_neo4j
async def test_a_failed_mapping_load_blocks_until_the_operator_abandons_it(
    graph: Neo4jGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await load_test_corpus(graph)
    _use(graph, load_mappings, monkeypatch)
    _mappings_dir(tmp_path, monkeypatch)
    real_write = load_mappings.write_mappings

    async def dies(*_: object) -> None:
        raise ConnectionError("Aura went away halfway")

    monkeypatch.setattr(load_mappings, "write_mappings", dies)
    with pytest.raises(ConnectionError):
        await load_mappings.main(SEP, None)
    assert (await graph.loaded_sources()).mapping == LoadedSource(None, loading=True)

    # Running it again is refused, naming the load and how to clear it.
    monkeypatch.setattr(load_mappings, "write_mappings", real_write)
    (row,) = await cypher(graph, "MATCH (l:Load) RETURN l.load_id AS id")
    with pytest.raises(GraphWriteRefused, match=rf"--abandon {row['id']}"):
        await load_mappings.main(SEP, None)

    await load_mappings.abandon(row["id"], "lead@acme.example", "Aura went away")
    assert (await graph.loaded_sources()).mapping == LoadedSource(None, loading=False)
    await load_mappings.main(SEP, None)
    assert (await graph.loaded_sources()).mapping == LoadedSource(M1, loading=False)


def _no_source(html: Path | None) -> str:
    return ""


def _parsed(*args: object, **kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(clauses=V1)


def _manifest(text: str) -> SimpleNamespace:
    def select(clauses: object) -> object:
        return clauses

    return SimpleNamespace(select=select)


@needs_neo4j
async def test_the_corpus_script_records_a_load_that_returned(
    graph: Neo4jGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source isn't fetched and the parser doesn't run: V1 stands in for
    the clauses they would have made."""
    _use(graph, load_graph_structure, monkeypatch)
    monkeypatch.setattr(load_graph_structure, "resolve_source_version", _resolves_to(C1))
    monkeypatch.setattr(load_graph_structure, "pinned_source", _no_source)
    monkeypatch.setattr(load_graph_structure, "parse_compilation", _parsed)
    monkeypatch.setattr(load_graph_structure, "parse_selection_manifest", _manifest)
    await load_graph_structure.main(None)
    assert (await graph.loaded_sources()).corpus == LoadedSource(C1, loading=False)
    await load_graph_structure.main(None)  # idempotent, and recorded each time
    assert len(await cypher(graph, "MATCH (l:Load) RETURN l")) == 2


@needs_neo4j
async def test_the_corpus_script_can_abandon_a_failed_load(
    graph: Neo4jGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(graph, load_graph_structure, monkeypatch)
    failed = await graph.begin_load(C1)
    await load_graph_structure.abandon(failed, "lead@acme.example", "the parser crashed")
    (row,) = await cypher(graph, "MATCH (l:Load) RETURN l.abandoned_by AS by")
    assert row["by"] == "lead@acme.example"
    assert (await graph.loaded_sources()).corpus == LoadedSource()
