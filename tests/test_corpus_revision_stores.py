"""`CorpusRevision` across the stores (#178 part 2, ADR-0042): the correction
applied to the bucket (`GCSClauseStore` over the versioned in-memory bucket),
a real local Neo4j (`NEO4J_TEST_URI`) and a real local Qdrant (`QDRANT_URL`)
or the fake; its audit in the bucket and on the graph; that a re-run finds
nothing to change; that a correction stopped part-way is detected and
finished; that a refusal writes nothing; and the amendment, as far as it is
built, on the same fixture.

The deployed state is `tests/test_corpus_revision.py`'s: the constructs
fixture's `s6/` and `sch1/app7/` as the parser before #187 stored them."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.corpus_revision import CorpusRevision, Planned
from compliance_rag.adapters.fakes import FakeEmbedder, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.graph_schema import (
    Bitemporal,
    GraphWriteRefused,
    NodeRef,
    NodeWrite,
)
from compliance_rag.adapters.interfaces import (
    RetrievalAdmin,
    RetrievalStore,
    SourceKind,
    SourceVersion,
)
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.adapters.qdrant_retrieval_store import (
    QdrantRetrievalStore,
    _chunk_point_id,  # pyright: ignore[reportPrivateUsage] — reads one point directly
)
from compliance_rag.adapters.revision_authorization import (
    RevisionAuthorization,
    _issue,  # pyright: ignore[reportPrivateUsage] — the graph's half of the contract
)
from compliance_rag.corpus.ingest import ingest_compilation
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    Chunk,
    ClauseRecord,
    DocumentClass,
    Scope,
    canonical_sha256,
    canonicalise,
    sha256_hex,
)
from compliance_rag.corpus.revision import RevisionDrift, RevisionMode, RevisionRefused
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.corpus.vector_load import load_version_vectors
from compliance_rag.graph.loader import (
    check_instrument_version,
    recorded_load,
    write_instrument_version,
)
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION
from tests.conftest import ALL_LAW, qdrant_reachable
from tests.test_corpus_revision import (
    BY,
    CHANGED,
    DEPLOYED,
    INSTRUMENT,
    PIN,
    SELECTION,
    VERSION,
    WHY,
    ack,
    corrected_clauses,
    deploy_bucket,
    deployed_clauses,
    source,
    words,
)
from tests.test_gcs_clause_store import _FakeBucket  # pyright: ignore[reportPrivateUsage]
from tests.test_neo4j_graph import cypher, needs_neo4j
from tests.test_neo4j_graph import (
    graph as plain_graph,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

SINCE, EFFECTIVE, NOV = dt.date(1989, 1, 1), dt.date(2026, 6, 4), dt.date(2026, 11, 1)
DEPLOYED_CODE = SourceVersion(SourceKind.CORPUS, sha="a" * 40, tag="corpus-2026.10.11")
FIXED_CODE = SourceVersion(SourceKind.CORPUS, sha="b" * 40)
AT = dt.datetime(2026, 10, 15, 9, 0, tzinfo=dt.UTC)
QDRANT_URL = os.environ.get("QDRANT_URL", "")


@pytest.fixture
async def graph(plain_graph: Neo4jGraph) -> Neo4jGraph:  # noqa: F811
    await plain_graph.apply_schema()
    return plain_graph


@dataclass
class Stores:
    bucket: _FakeBucket
    clauses: GCSClauseStore
    graph: Neo4jGraph
    retrieval: RetrievalAdmin
    ledger: FakeLedgerStore
    qdrant: AsyncQdrantClient | None
    collection: str

    def lifecycle(self) -> CorpusLifecycle:
        return CorpusLifecycle(
            ledger=self.ledger,
            retrieval=self.retrieval,
            clauses=self.clauses,
            generation_region=BUILD_PHASE_OFFSHORE_REGION,
            synthetic_registry=SyntheticRegistry(keys=frozenset(), source="none: public law"),
        )

    def revision(
        self, *, graph: Neo4jGraph | None = None, retrieval: RetrievalAdmin | None = None
    ) -> CorpusRevision:
        ids = iter(f"rev-{n}" for n in range(1, 100))
        return CorpusRevision(
            clauses=self.clauses,
            graph=graph or self.graph,
            retrieval=retrieval or self.retrieval,
            ledger=self.ledger,
            count_tokens=words,
            clock=lambda: AT,
            new_revision_id=lambda: next(ids),
        )

    async def texts(self, version: str = VERSION) -> dict[str, str]:
        return await self.retrieval.stored_chunk_texts(
            instrument_id=INSTRUMENT, tenant_id=PUBLIC_TENANT_ID, version_id=version
        )


@pytest.fixture(params=["fake", "qdrant"])
async def stores(request: pytest.FixtureRequest, graph: Neo4jGraph) -> AsyncIterator[Stores]:
    ledger = FakeLedgerStore()
    client: AsyncQdrantClient | None = None
    collection = f"revision-test-{uuid.uuid4().hex[:8]}"
    retrieval: RetrievalAdmin
    if request.param == "qdrant":
        if not qdrant_reachable(QDRANT_URL):
            pytest.skip("QDRANT_URL not set or not reachable")
        client = AsyncQdrantClient(url=QDRANT_URL)
        qdrant = QdrantRetrievalStore(
            client, collection_name=collection, embedder=FakeEmbedder(dimensions=8), ledger=ledger
        )
        await qdrant.ensure_ready()
        retrieval = qdrant
    else:
        retrieval = FakeRetrievalStore(ledger=ledger)
    bucket = _FakeBucket()
    found = Stores(bucket, GCSClauseStore(bucket), graph, retrieval, ledger, client, collection)
    yield found
    if client is not None:
        await client.delete_collection(collection)
        await client.close()


async def deploy(stores: Stores, clauses: tuple[ClauseRecord, ...] | None = None) -> None:
    """The deployed state: the bucket ingest (#99), the graph structure load
    (Phase 2) and the vector load (#144), all of the old parse."""
    clauses = clauses or deployed_clauses()
    if clauses == deployed_clauses():
        await deploy_bucket(stores.clauses)
    check = partial(
        check_instrument_version, stores.graph, clauses, effective=EFFECTIVE, supersedes=None
    )
    async with recorded_load(stores.graph, DEPLOYED_CODE, check=check) as checked:
        await write_instrument_version(
            stores.graph,
            clauses,
            checked,
            instrument_since=SINCE,
            effective=EFFECTIVE,
            supersedes=None,
        )
    await load_version_vectors(
        clauses,
        count_tokens=words,
        lifecycle=stores.lifecycle(),
        retrieval=stores.retrieval,
        ledger=stores.ledger,
        instrument_id=INSTRUMENT,
        version_id=clauses[0].version_id,
        tenant_id=PUBLIC_TENANT_ID,
        effective_from=EFFECTIVE,
        document_class=DocumentClass.LIBRARY_LAW,
    )


async def plan(stores: Stores, revision: CorpusRevision | None = None) -> Planned:
    return await (revision or stores.revision()).plan(
        source(), instrument_id=INSTRUMENT, version_id=VERSION, selection=SELECTION
    )


async def correct(
    stores: Stores, revision: CorpusRevision | None = None, **kwargs: Any
) -> Any:  # Any: the report, or what a test passes through
    revision = revision or stores.revision()
    return await revision.correct(
        await plan(stores, revision),
        kwargs.pop("acknowledgement", ack()),
        pinned_source_sha256=kwargs.pop("pin", PIN),
        corpus_version=FIXED_CODE,
        instrument_since=SINCE,
        effective=EFFECTIVE,
    )


async def finish(
    stores: Stores, revision: CorpusRevision | None = None
) -> Any:  # Any: report or None
    revision = revision or stores.revision()
    return await revision.finish(
        await plan(stores, revision),
        corpus_version=FIXED_CODE,
        instrument_since=SINCE,
        effective=EFFECTIVE,
    )


async def graph_hashes(graph: Neo4jGraph) -> dict[str, tuple[str, list[str]]]:
    rows = await cypher(
        graph,
        "MATCH (c:Clause) RETURN c.stable_id AS id, c.canonical_sha256 AS sha, "
        "coalesce(c.corrections, []) AS trail",
    )
    return {str(r["id"]): (str(r["sha"]), [str(e) for e in r["trail"]]) for r in rows}


async def snapshot(stores: Stores) -> tuple[object, ...]:
    return (
        dict(stores.bucket.objects),
        list(stores.bucket.noncurrent),
        await graph_hashes(stores.graph),
        await stores.graph.loaded_sources(),
        await stores.texts(),
    )


# ---- the correction ---------------------------------------------------------


@needs_neo4j
async def test_a_correction_republishes_the_clauses_in_every_store_with_its_audit(
    stores: Stores,
) -> None:
    await deploy(stores)
    revision = stores.revision()
    planned = await plan(stores, revision)
    assert {c.new_clause_id for c in planned.plan.changed} == CHANGED
    before = await revision.status(planned)
    assert before.record is None and before.graph_holds_version
    assert before.retrieval_holds_version and not before.unfinished

    report = await correct(stores, revision)

    assert (report.graph_nodes_corrected, report.graph_loaded, report.chunks_revised) == (
        7,
        True,
        7,
    )
    record = report.record
    assert (record.revision_id, record.mode, record.by, record.reason) == (
        "rev-1",
        RevisionMode.CORRECT,
        BY,
        WHY,
    )
    assert (record.at, record.source_sha256, record.corpus_sha) == (
        AT.isoformat(),
        PIN,
        FIXED_CODE.sha,
    )
    fresh = {c.clause_id: c for c in corrected_clauses()}
    # The bucket: the manifest's hashes and the record, and the objects.
    manifest = json.loads(stores.bucket.objects[f"{INSTRUMENT}/{VERSION}/manifest.json"])
    assert manifest["clause_paths"] == {
        c.clause_path: canonical_sha256(c.canonical_text) for c in fresh.values()
    }
    (on_record,) = manifest["revisions"]
    assert on_record["clauses"]["s6/1/record/d"] == {
        "previous_sha256": canonical_sha256(canonicalise(DEPLOYED["s6/1/record/d"])),
        "sha256": canonical_sha256(fresh[f"{INSTRUMENT}:{VERSION}:s6/1/record/d"].canonical_text),
    }
    assert (on_record["by"], on_record["reason"], on_record["at"]) == (BY, WHY, AT.isoformat())
    assert on_record["source_sha256"] == PIN
    for clause in fresh.values():
        assert (
            await stores.clauses.get_clause(clause.clause_id, tenant_id=PUBLIC_TENANT_ID) == clause
        )
    noncurrent = {
        json.loads(data)["canonical_text"]
        for name, _, data in stores.bucket.noncurrent
        if "/clauses/" in name
    }
    assert noncurrent == {c.canonical_text for c in deployed_clauses() if c.clause_id in CHANGED}
    # The graph: the new hash and one trail entry on each corrected node only.
    hashes = await graph_hashes(stores.graph)
    for clause_id, clause in fresh.items():
        sha, trail = hashes[clause_id]
        assert sha == canonical_sha256(clause.canonical_text)
        if clause_id in CHANGED:
            (entry,) = [json.loads(e) for e in trail]
            assert (entry["revision_id"], entry["by"], entry["reason"]) == ("rev-1", BY, WHY)
            path = clause.clause_path
            assert entry["previous"] == {
                "canonical_sha256": canonical_sha256(canonicalise(DEPLOYED[path]))
            }
            assert entry["source_sha256"] == PIN and entry["at"] == AT.isoformat()
        else:
            assert trail == []
    loaded = (await stores.graph.loaded_sources()).corpus
    assert loaded.version == FIXED_CODE and not loaded.loading and not loaded.blocked_by_abandoned
    # Qdrant: the new text, under the same ids.
    assert await stores.texts() == {i: c.canonical_text for i, c in fresh.items()}
    assert not (await revision.status(planned)).unfinished


@needs_neo4j
@pytest.mark.parametrize("stores", ["qdrant"], indirect=True)
async def test_on_qdrant_only_the_text_and_vectors_of_a_revised_point_change(
    stores: Stores,
) -> None:
    assert stores.qdrant is not None
    await deploy(stores)
    point = _chunk_point_id(f"{INSTRUMENT}:{VERSION}:s6/1/credit-reporting-body")
    (before,) = await stores.qdrant.retrieve(
        stores.collection, ids=[point], with_payload=True, with_vectors=True
    )
    await correct(stores)
    (after,) = await stores.qdrant.retrieve(
        stores.collection, ids=[point], with_payload=True, with_vectors=True
    )
    assert before.payload is not None and after.payload is not None
    assert {k: v for k, v in after.payload.items() if k != "text"} == {
        k: v for k, v in before.payload.items() if k != "text"
    }
    assert (
        after.payload["text"]
        == "credit reporting body means: that carries on a credit reporting business."
    )
    assert after.vector != before.vector
    # What retrieval returns for the clause is the corrected text.
    assert isinstance(stores.retrieval, RetrievalStore)
    found = await stores.retrieval.lookup_clause(
        f"{INSTRUMENT}:{VERSION}:s6/1/credit-reporting-body",
        law=ALL_LAW,
        tenant_id=PUBLIC_TENANT_ID,
        scopes=(Scope.PUBLIC,),
        as_of=dt.date(2026, 10, 15),
    )
    assert found is not None and found.text == after.payload["text"]


@needs_neo4j
async def test_a_rerun_after_a_correction_finds_nothing_to_change(stores: Stores) -> None:
    await deploy(stores)
    await correct(stores)
    after = await snapshot(stores)
    planned = await plan(stores)
    assert planned.plan.empty
    with pytest.raises(RevisionRefused, match="changes nothing"):
        await correct(stores)
    assert await finish(stores) is None
    assert await snapshot(stores) == after


# ---- refusals write nothing --------------------------------------------------


@needs_neo4j
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"pin": "0" * 64}, "not the version's pinned source"),
        ({"acknowledgement": ack(CHANGED - {f"{INSTRUMENT}:{VERSION}:s6/1"})}, "missing"),
        (
            {"acknowledgement": ack(CHANGED | {f"{INSTRUMENT}:{VERSION}:s6/1/record"})},
            "not in the plan",
        ),
        ({"acknowledgement": ack(by=" ")}, "who authorised it"),
        ({"acknowledgement": ack(findings=1)}, "0 finding"),
    ],
)
async def test_a_refused_correction_writes_nothing_anywhere(
    stores: Stores,
    change: dict[str, Any],
    message: str,  # Any: keyword overrides
) -> None:
    await deploy(stores)
    before = await snapshot(stores)
    with pytest.raises(RevisionRefused, match=message):
        await correct(stores, **change)
    assert await snapshot(stores) == before


@needs_neo4j
async def test_the_findings_counted_are_every_tenants_citing_a_changed_clause(
    stores: Stores,
) -> None:
    await deploy(stores)
    stores.ledger.seed_finding("acme", f"{INSTRUMENT}:{VERSION}:s6/1")
    stores.ledger.seed_finding("beta", f"{INSTRUMENT}:{VERSION}:s6/1/record/d")
    stores.ledger.seed_finding("beta", f"{INSTRUMENT}:{VERSION}:s6/1/record/a")  # unchanged
    planned = await plan(stores)
    assert await stores.revision().findings_citing(planned.plan) == 2
    with pytest.raises(RevisionRefused, match="2 finding"):
        await correct(stores)
    await correct(stores, acknowledgement=ack(findings=2))


@needs_neo4j
async def test_a_graph_that_disagrees_with_the_bucket_is_refused_before_any_write(
    stores: Stores,
) -> None:
    await deploy(stores)
    await cypher(
        stores.graph,
        f"MATCH (c:Clause {{stable_id: '{INSTRUMENT}:{VERSION}:s6/1/record/a'}}) "
        "SET c.canonical_sha256 = 'drifted'",
    )
    before = await snapshot(stores)
    with pytest.raises(RevisionDrift, match=r"graph holds .*s6/1/record/a"):
        await correct(stores)
    assert await snapshot(stores) == before


@needs_neo4j
async def test_qdrant_that_disagrees_with_the_bucket_is_refused_before_any_write(
    stores: Stores,
) -> None:
    await deploy(stores)
    drifted = Chunk(
        chunk_id=f"{INSTRUMENT}:{VERSION}:s6/1/record/a",
        clause_id=f"{INSTRUMENT}:{VERSION}:s6/1/record/a",
        text="drifted",
        valid_from=EFFECTIVE,
    )
    await stores.retrieval.upsert_chunks((drifted,))
    before = await snapshot(stores)
    with pytest.raises(RevisionDrift, match=r"Qdrant holds .*s6/1/record/a"):
        await correct(stores)
    assert await snapshot(stores) == before


@needs_neo4j
async def test_a_version_not_loaded_into_the_graph_or_qdrant_is_corrected_in_the_bucket(
    graph: Neo4jGraph,
) -> None:
    bucket = _FakeBucket()
    clauses = GCSClauseStore(bucket)
    await deploy_bucket(clauses)
    ledger = FakeLedgerStore()
    revision = CorpusRevision(
        clauses=clauses,
        graph=graph,
        retrieval=FakeRetrievalStore(ledger=ledger),
        ledger=ledger,
        count_tokens=words,
    )
    planned = await revision.plan(
        source(), instrument_id=INSTRUMENT, version_id=VERSION, selection=SELECTION
    )
    report = await revision.correct(
        planned,
        ack(),
        pinned_source_sha256=PIN,
        corpus_version=FIXED_CODE,
        instrument_since=SINCE,
        effective=EFFECTIVE,
    )
    assert (report.graph_nodes_corrected, report.graph_loaded, report.chunks_revised) == (
        0,
        False,
        0,
    )
    assert (await graph.counts()).nodes == 0  # the graph loader's to load, from the corrected text


# ---- a correction stopped part-way -------------------------------------------


class _GraphStepDies(Neo4jGraph):
    async def revise_clauses(self, authorization: RevisionAuthorization) -> int:
        raise RuntimeError("the connection to Aura dropped")


@needs_neo4j
async def test_a_correction_whose_graph_step_died_is_detected_and_finished(
    stores: Stores,
) -> None:
    await deploy(stores)
    dying = _GraphStepDies(stores.graph._driver, database="neo4j")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError, match="dropped"):
        await correct(stores, stores.revision(graph=dying))

    revision = stores.revision()
    planned = await plan(stores, revision)
    assert planned.plan.empty  # the bucket committed the revision
    status = await revision.status(planned)
    assert status.unfinished and status.record is not None
    assert status.objects_behind == ()
    assert set(status.graph_behind) == set(status.chunks_behind) == CHANGED
    assert status.corpus_load_open  # the graph step's load never finished (#153)
    with pytest.raises(RevisionRefused, match="unfinished"):
        await correct(stores)
    with pytest.raises(RevisionRefused, match="corpus load is open"):
        await finish(stores)

    (open_load,) = await cypher(
        stores.graph,
        "MATCH (l:Load) WHERE l.finished_at IS NULL AND l.abandoned_at IS NULL "
        "RETURN l.load_id AS id",
    )
    await stores.graph.abandon_load(str(open_load["id"]), "Piero Frasca", "graph step died")
    report = await finish(stores)
    assert report.record == status.record  # the same revision, not a new one
    assert (report.graph_nodes_corrected, report.graph_loaded, report.chunks_revised) == (
        7,
        True,
        7,
    )
    finished = await revision.status(planned)
    assert not finished.unfinished
    loaded = (await stores.graph.loaded_sources()).corpus
    assert loaded.version == FIXED_CODE and not loaded.blocked_by_abandoned
    # One trail entry per node, naming the revision the bucket recorded.
    hashes = await graph_hashes(stores.graph)
    for clause_id in CHANGED:
        (entry,) = [json.loads(e) for e in hashes[clause_id][1]]
        assert entry["revision_id"] == status.record.revision_id
    assert await finish(stores) is None


class _QdrantStepDies(FakeRetrievalStore):
    async def revise_chunks(
        self, authorization: RevisionAuthorization, chunks: tuple[Chunk, ...]
    ) -> None:
        raise RuntimeError("Qdrant Cloud answered 503")


@needs_neo4j
@pytest.mark.parametrize("stores", ["fake"], indirect=True)
async def test_a_correction_whose_qdrant_step_died_is_detected_and_finished(
    stores: Stores,
) -> None:
    await deploy(stores)
    assert isinstance(stores.retrieval, FakeRetrievalStore)
    dying = _QdrantStepDies(ledger=stores.ledger)
    dying._chunks = stores.retrieval._chunks  # pyright: ignore[reportPrivateUsage] — one store, two faces
    with pytest.raises(RuntimeError, match="503"):
        await correct(stores, stores.revision(retrieval=dying))
    revision = stores.revision()
    planned = await plan(stores, revision)
    status = await revision.status(planned)
    assert (status.objects_behind, status.graph_behind) == ((), ())
    assert set(status.chunks_behind) == CHANGED and not status.corpus_load_open
    report = await finish(stores)
    assert (report.graph_nodes_corrected, report.graph_loaded, report.chunks_revised) == (
        0,
        False,
        7,
    )
    assert not (await revision.status(planned)).unfinished


@needs_neo4j
async def test_a_correction_whose_bucket_objects_stopped_part_way_is_detected_and_finished(
    stores: Stores,
) -> None:
    await deploy(stores)
    original = stores.bucket.write
    objects = 0

    def failing(name: str, data: str) -> None:
        nonlocal objects
        if "/clauses/" in name:
            objects += 1
            if objects == 3:
                raise RuntimeError("429 Too Many Requests")
        original(name, data)

    stores.bucket.write = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="429"):
        await correct(stores)
    stores.bucket.write = original  # type: ignore[method-assign]
    revision = stores.revision()
    planned = await plan(stores, revision)
    status = await revision.status(planned)
    assert len(status.objects_behind) == 5
    assert set(status.graph_behind) == set(status.chunks_behind) == CHANGED
    assert not status.corpus_load_open  # the graph step never began
    report = await finish(stores)
    assert (report.graph_nodes_corrected, report.chunks_revised) == (7, 7)
    assert not (await revision.status(planned)).unfinished
    for clause in corrected_clauses():
        assert (
            await stores.clauses.get_clause(clause.clause_id, tenant_id=PUBLIC_TENANT_ID) == clause
        )


@needs_neo4j
async def test_finishing_needs_a_parse_that_reproduces_the_recorded_text(stores: Stores) -> None:
    await deploy(stores)
    dying = _GraphStepDies(stores.graph._driver, database="neo4j")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError):
        await correct(stores, stores.revision(graph=dying))
    revision = stores.revision()
    planned = await plan(stores, revision)
    other = tuple(
        c.model_copy(update={"canonical_text": "a different parse"})
        if c.clause_path == "s6/1"
        else c
        for c in planned.clauses
    )
    with pytest.raises(RevisionRefused, match=r"doesn't give .*s6/1 the text"):
        await revision.finish(
            replace(planned, clauses=other),
            corpus_version=FIXED_CODE,
            instrument_since=SINCE,
            effective=EFFECTIVE,
        )


# ---- the amendment: a new compilation, version and retire -------------------

AMENDED_VERSION = "C2026C00999"
OLD_WORDS = "request not to receive direct marketing communications from the first organisation"


def amended_source() -> str:
    """An invented later compilation: one paragraph of APP 7.6 reworded."""
    original = source()
    assert original.count(OLD_WORDS) == 1
    return original.replace(OLD_WORDS, f"{OLD_WORDS} or any related body corporate")


@needs_neo4j
async def test_an_amendment_publishes_the_new_version_and_retires_the_old(stores: Stores) -> None:
    current = corrected_clauses()
    await ingest_compilation(
        source(),
        instrument_id=INSTRUMENT,
        version_id=VERSION,
        tenant_id=PUBLIC_TENANT_ID,
        scope=Scope.PUBLIC,
        selection=SELECTION,
        store=stores.clauses,
    )
    await deploy(stores, current)
    revision = stores.revision()
    planned = await revision.plan(
        amended_source(),
        instrument_id=INSTRUMENT,
        version_id=AMENDED_VERSION,
        selection=SELECTION,
        stored_version_id=VERSION,
    )
    plan_ = planned.plan
    assert plan_.mode is RevisionMode.AMEND
    reworded = f"{INSTRUMENT}:{AMENDED_VERSION}:sch1/app7/6/c"
    assert [c.new_clause_id for c in plan_.changed] == [reworded]
    assert [c.old_clause_id for c in plan_.changed] == [f"{INSTRUMENT}:{VERSION}:sch1/app7/6/c"]
    assert (plan_.added, plan_.removed) == ((), ())

    async def amend() -> Any:  # Any: the report
        return await revision.amend(
            planned,
            ack(frozenset({reworded})),
            pinned_source_sha256=sha256_hex(amended_source()),
            corpus_version=FIXED_CODE,
            lifecycle=stores.lifecycle(),
            instrument_since=SINCE,
            effective=NOV,
        )

    with pytest.raises(RevisionRefused, match="not the one pinned"):
        await revision.amend(
            planned,
            ack(frozenset({reworded})),
            pinned_source_sha256=PIN,
            corpus_version=FIXED_CODE,
            lifecycle=stores.lifecycle(),
            instrument_since=SINCE,
            effective=NOV,
        )
    report = await amend()
    # The bucket: the new version, with the amendment on its manifest; the old untouched.
    (record,) = await stores.clauses.revisions(
        INSTRUMENT, AMENDED_VERSION, tenant_id=PUBLIC_TENANT_ID
    )
    assert record == report.record
    assert (record.mode, record.supersedes, record.by) == (RevisionMode.AMEND, VERSION, BY)
    assert await stores.clauses.revisions(INSTRUMENT, VERSION, tenant_id=PUBLIC_TENANT_ID) == ()
    new_manifest = await stores.clauses.get_manifest(
        INSTRUMENT, AMENDED_VERSION, tenant_id=PUBLIC_TENANT_ID
    )
    assert len(new_manifest) == len(current)
    # The graph: the new version supersedes the old, which is retired with its clauses.
    (old_version,) = await cypher(
        stores.graph,
        f"MATCH (n:InstrumentVersion {{stable_id: '{INSTRUMENT}:{VERSION}'}})"
        f"<-[:SUPERSEDES]-(m:InstrumentVersion {{stable_id: '{INSTRUMENT}:{AMENDED_VERSION}'}}) "
        "RETURN n.valid_to AS valid_to, n.end_reason AS why",
    )
    assert (old_version["valid_to"].to_native(), old_version["why"]) == (NOV, "superseded")
    (retired,) = await cypher(
        stores.graph,
        f"MATCH (c:Clause {{stable_id: '{INSTRUMENT}:{VERSION}:sch1/app7/6/c'}}) "
        "RETURN c.valid_to AS valid_to",
    )
    assert retired["valid_to"].to_native() == NOV
    # Qdrant and the ledger: the new version from NOV, the old one until then.
    assert len(await stores.texts(AMENDED_VERSION)) == len(current)
    assert await stores.ledger.version_validity(
        tenant_id=PUBLIC_TENANT_ID, instrument_id=INSTRUMENT, version_id=VERSION
    ) == (EFFECTIVE, NOV)
    assert await stores.ledger.version_validity(
        tenant_id=PUBLIC_TENANT_ID, instrument_id=INSTRUMENT, version_id=AMENDED_VERSION
    ) == (NOV, None)
    assert isinstance(stores.retrieval, RetrievalStore)
    for as_of, version in ((dt.date(2026, 10, 31), VERSION), (NOV, AMENDED_VERSION)):
        found = await stores.retrieval.lookup_clause(
            f"{INSTRUMENT}:{version}:sch1/app7/6/c",
            law=ALL_LAW,
            tenant_id=PUBLIC_TENANT_ID,
            scopes=(Scope.PUBLIC,),
            as_of=as_of,
        )
        assert found is not None
    gone = await stores.retrieval.lookup_clause(
        f"{INSTRUMENT}:{VERSION}:sch1/app7/6/c",
        law=ALL_LAW,
        tenant_id=PUBLIC_TENANT_ID,
        scopes=(Scope.PUBLIC,),
        as_of=NOV,
    )
    assert gone is None
    # Re-running it finishes nothing new and records nothing twice.
    again = await amend()
    assert again.record == record and again.chunks_revised == 0
    assert (
        len(await stores.clauses.revisions(INSTRUMENT, AMENDED_VERSION, tenant_id=PUBLIC_TENANT_ID))
        == 1
    )


# ---- the graph's half --------------------------------------------------------


def _correction(revision_id: str = "g1") -> RevisionAuthorization:
    from tests.test_corpus_revision import authorization

    return authorization(revision_id)


@needs_neo4j
async def test_the_graph_moves_a_hash_only_from_the_previous_one_all_or_nothing(
    stores: Stores,
) -> None:
    await deploy(stores)
    granted = _correction()
    wrong = replace(
        granted.record,
        clauses=(
            *granted.record.clauses[:-1],
            replace(granted.record.clauses[-1], previous_sha256="0" * 64),
        ),
    )
    before = await graph_hashes(stores.graph)
    with pytest.raises(GraphWriteRefused, match="neither the previous"):
        await stores.graph.revise_clauses(_issue(wrong))
    assert await graph_hashes(stores.graph) == before  # one transaction: none landed
    assert await stores.graph.revise_clauses(granted) == 7
    assert await stores.graph.revise_clauses(granted) == 0  # already corrected: a no-op
    after = await graph_hashes(stores.graph)
    assert all(len(after[c][1]) == 1 for c in CHANGED)
    # A loader writing the corrected parse finds every node as it would write it.
    created = await stores.graph.put_nodes(
        [
            NodeWrite(
                NodeRef("Clause", c.clause_id),
                Bitemporal(valid_from=EFFECTIVE),
                {
                    "instrument_id": INSTRUMENT,
                    "version_id": VERSION,
                    "clause_path": c.clause_path,
                    "canonical_sha256": canonical_sha256(c.canonical_text),
                    "canonicalisation_version": c.canonicalisation_version,
                },
            )
            for c in corrected_clauses()
        ]
    )
    assert created == 0


@needs_neo4j
async def test_the_graph_refuses_a_missing_node_and_an_amendment(graph: Neo4jGraph) -> None:
    with pytest.raises(GraphWriteRefused, match="no Clause"):
        await graph.revise_clauses(_correction())
    amendment = replace(
        _correction().record,
        mode=RevisionMode.AMEND,
        supersedes="C2026C00111",
    )
    with pytest.raises(GraphWriteRefused, match="only a correction"):
        await graph.revise_clauses(_issue(amendment))
