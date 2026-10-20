"""An obligation names its exceptions, and they travel with it (#164, ADR-0037).

The human's decision: "APP 7.2, 7.8, 8.2 and 11.3 say when an obligation does
NOT apply, so an answer citing APP 11.1 without 11.3 is incomplete in a way
the reader can't detect. Treat it as a correctness bug, not a modelling
nicety." (On its parsed text APP 11.3 says what the steps include, not when
they don't apply: ADR-0037 records it as no exception.) So:

- the mapping model names an obligation's exceptions, in reviewed mapping
  content kept beside the signed file, loaded as `EXCEPTED_BY` edges;
- Track A's scope includes the exceptions of every obligation that applies or
  might (on the fake and on Neo4j), and the clauses inside them (ADR-0035);
- `answer()` gives the model an obligation's exceptions whenever it gives it
  the obligation's clause, fetched by id if the ranking left them out.

Every fixture is invented (`test-act`): the drafted exceptions of the real Act
wait for the human's signature and never reach these tests.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from compliance_rag.adapters.fakes import (
    FakeCitationVerifier,
    FakeClauseStore,
    FakeGenerator,
    FakeGraphStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import NO_PREFIX, Candidate, LawScope, Owed, PolicyCorpus
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.assessment.coverage import Coverage, ExceptionOf, Outcome
from compliance_rag.assessment.evidence import Evidence, Facts
from compliance_rag.assessment.pipeline import AssessmentPipeline, OwedClauseUndelivered
from compliance_rag.assessment.prompt import SourceClause, fence, policy_prefix
from compliance_rag.corpus.models import (
    NO_END_DATE,
    PUBLIC_TENANT_ID,
    Chunk,
    ClauseRecord,
    DocumentClass,
    Scope,
)
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.graph.loader import load_instrument_version, load_mappings
from compliance_rag.graph.mappings import MappingError, MappingSet, load_mapping_set
from compliance_rag.graph.track_a import GraphTrackA
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import REGION
from compliance_rag.verifier.citations import Verifier
from tests.test_applicability import AS_OF, CLAUSES, FIXTURE, LAST_FY, fact
from tests.test_graph_loader import SINCE, clause_records, write_yaml
from tests.test_neo4j_graph import cypher, needs_neo4j
from tests.test_neo4j_graph import graph as graph

ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS = ROOT / "tests" / "fixtures" / "mappings" / "test-exceptions.yaml"

KEEP, KEEP_EXCEPTION = "test-act:v1:s1/1", "test-act:v1:s1/2"
HEALTH, HEALTH_EXCEPTION = "test-act:v1:s2/1", "test-act:v1:s3/1"
OPEN = CLAUSES[KEEP]
VALIDITY = {**CLAUSES, KEEP_EXCEPTION: OPEN, HEALTH_EXCEPTION: OPEN}
TEXTS = {
    "s1/1": "The keeper must keep the invented records safe and secure.",
    "s1/2": "Subclause 1 does not apply to anything lent to a museum for display.",
    "s2/1": "Health files stay apart from other files.",
    "s3/1": "Subclause 2.1 does not apply to anonymous health files.",
}
QUESTION = "How must the keeper keep the invented records safe and secure?"
"""Shares words with s1/1 and none with its exception, so the fake's ranking
(word overlap) never returns the exception."""

OVER = [fact("annual_turnover_aud", 4000, LAST_FY), fact("provides_health_service", False)]
"""Over the threshold: OB-T-KEEP applies. No health service: OB-T-HEALTH doesn't."""


def with_exceptions() -> MappingSet:
    """The applicability fixture's obligations, with the exceptions fixture
    beside it."""
    return load_mapping_set([FIXTURE, EXCEPTIONS])


def exceptions_document() -> dict[str, Any]:  # Any: YAML
    return yaml.safe_load(EXCEPTIONS.read_text(encoding="utf-8"))


async def coverage_of(evidence: list[Evidence] | None = None) -> Coverage:
    graph = FakeGraphStore(with_exceptions(), clause_validity=VALIDITY)
    return await graph.coverage(Facts.of(evidence or OVER), tenant_id="acme", as_of=AS_OF)


# ---- the mapping model ------------------------------------------------------------


def test_an_exceptions_file_names_the_obligations_of_another_file() -> None:
    """The signed file is never edited: its obligations gain their exceptions
    from a file beside it, validated as one set with it."""
    mappings = with_exceptions()
    assert mappings.exceptions_by_obligation == {
        "OB-T-KEEP": (KEEP_EXCEPTION,),
        "OB-T-HEALTH": (HEALTH_EXCEPTION,),
    }


@pytest.mark.parametrize(
    ("obligation", "clause", "message"),
    [
        ("OB-T-NOPE", KEEP_EXCEPTION, "exceptions of unknown obligation OB-T-NOPE"),
        ("OB-T-KEEP", KEEP, "is its own clause, not an exception"),
        ("OB-T-KEEP", "test-act:v1:s1", "contains its own clause test-act:v1:s1/1"),
    ],
    ids=["unknown-obligation", "its-own-clause", "the-provision-around-it"],
)
def test_an_exception_that_isnt_another_clause_of_a_known_obligation_is_refused(
    tmp_path: Path, obligation: str, clause: str, message: str
) -> None:
    document = exceptions_document()
    document["exceptions"][0] = {"obligation_id": obligation, "clause_ids": [clause]}
    document["source_clause_ids"].append(clause)
    with pytest.raises(MappingError, match=message):
        load_mapping_set([FIXTURE, write_yaml(tmp_path, document)])


def test_an_exception_outside_the_files_sources_is_refused(tmp_path: Path) -> None:
    document = exceptions_document()
    document["exceptions"][0]["clause_ids"] = ["test-act:v1:s9/9"]
    with pytest.raises(MappingError, match="s9/9 is not in source_clause_ids"):
        load_mapping_set([FIXTURE, write_yaml(tmp_path, document)])


def test_an_obligations_exceptions_are_listed_once(tmp_path: Path) -> None:
    """One entry per obligation, so a reviewer reads all of them in one place."""
    second = exceptions_document()
    second["exceptions"] = [second["exceptions"][0]]
    with pytest.raises(MappingError, match="exceptions of OB-T-KEEP are listed twice"):
        load_mapping_set([FIXTURE, EXCEPTIONS, write_yaml(tmp_path, second, "again.yaml")])


async def test_a_pending_exceptions_file_is_refused_before_the_graph_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-01: the loader refuses any file awaiting review — an exceptions file
    beside a signed one included — before it opens the graph."""
    from scripts import load_mappings

    shutil.copy(FIXTURE, tmp_path / "test-applicability.yaml")
    pending = exceptions_document() | {"reviewed_by": "PENDING — Lucas Frasca"}
    write_yaml(tmp_path, pending, "test-exceptions.yaml")

    def opened(*_: object, **__: object) -> None:
        raise AssertionError("the graph was opened")

    monkeypatch.setattr(load_mappings, "MAPPINGS", tmp_path)
    monkeypatch.setattr(load_mappings.Neo4jGraph, "from_settings", opened)
    with pytest.raises(
        SystemExit, match=r"awaiting review, not loaded: \['test-exceptions.yaml'\]"
    ):
        await load_mappings.main(AS_OF, None)


def test_no_corpus_mapping_awaits_review() -> None:
    """What the loader refuses, CI refuses too: the corpus-mapping tests (the
    applicability gate, Acme's scope) would otherwise run on unsigned
    content. A model-drafted file is corpus content only once signed (D-01)."""
    from scripts import load_mappings

    files = sorted((ROOT / "corpus" / "mappings").glob("*.yaml"))
    assert load_mappings.unreviewed(files) == []


# ---- Track A's scope, on the fake --------------------------------------------------


async def test_an_obligation_that_applies_brings_its_exceptions_into_scope() -> None:
    coverage = await coverage_of()
    keep, health = coverage.obligation("OB-T-KEEP"), coverage.obligation("OB-T-HEALTH")
    assert keep.outcome is Outcome.APPLICABLE and keep.exception_clause_ids == (KEEP_EXCEPTION,)
    assert KEEP_EXCEPTION not in keep.citations  # it decides nothing about applying
    law = LawScope.of(coverage)
    assert law.admits(Scope.PUBLIC, KEEP_EXCEPTION)
    assert law.admits(Scope.PUBLIC, f"{KEEP_EXCEPTION}/a")  # and what's inside it (ADR-0035)
    assert law.exceptions == {(KEEP, KEEP_EXCEPTION)}
    # Not applicable: reported, never retrieved — its exception neither.
    assert health.outcome is Outcome.NOT_APPLICABLE
    assert health.exception_clause_ids == (HEALTH_EXCEPTION,)
    assert not law.admits(Scope.PUBLIC, HEALTH_EXCEPTION)


async def test_an_obligation_that_might_apply_brings_its_exceptions_too() -> None:
    """Unassessable is "possibly": the answer still cites the law that would
    apply, so the exceptions to it as well."""
    coverage = await coverage_of([fact("annual_turnover_aud", 900, LAST_FY)])
    assert coverage.obligation("OB-T-KEEP").outcome is Outcome.UNASSESSABLE
    assert LawScope.of(coverage).admits(Scope.PUBLIC, KEEP_EXCEPTION)


async def test_an_exception_not_in_force_is_not_in_scope() -> None:
    later = dt.date(2027, 1, 1)
    graph = FakeGraphStore(
        with_exceptions(), clause_validity={**VALIDITY, KEEP_EXCEPTION: (later, NO_END_DATE)}
    )
    coverage = await graph.coverage(Facts.of(OVER), tenant_id="acme", as_of=AS_OF)
    assert coverage.obligation("OB-T-KEEP").exception_clause_ids == ()
    assert not LawScope.of(coverage).admits(Scope.PUBLIC, KEEP_EXCEPTION)


async def test_an_exception_is_owed_for_its_clause_or_any_clause_inside_it() -> None:
    law = LawScope.of(await coverage_of())
    owed = {KEEP_EXCEPTION: Owed(excepts=(KEEP,))}
    assert law.owed([KEEP]) == owed
    assert law.owed([f"{KEEP}/a"]) == owed  # a paragraph is its clause's text
    assert law.owed(["test-act:v1:s1/10", "test-act:v1:s1"]) == {}  # not inside s1/1
    assert LawScope.unscoped("test").owed([KEEP]) == {}  # no Track A, no exceptions


def test_a_scope_cannot_owe_a_clause_it_does_not_cite() -> None:
    with pytest.raises(ValueError, match="doesn't cite"):
        LawScope(frozenset({KEEP}), "test", frozenset({ExceptionOf(KEEP, KEEP_EXCEPTION)}))


# ---- on Neo4j ---------------------------------------------------------------------

CORPUS = clause_records("v1", TEXTS)


async def load_with_exceptions(graph: Neo4jGraph, mappings: MappingSet | None = None) -> None:
    await graph.apply_schema()
    await load_instrument_version(
        graph, CORPUS, instrument_since=SINCE, effective=OPEN[0], supersedes=None
    )
    await load_mappings(graph, mappings or with_exceptions(), today=AS_OF)


async def excepted_by(graph: Neo4jGraph) -> set[tuple[str, str]]:
    rows = await cypher(
        graph,
        "MATCH (o:Obligation)-[:EXCEPTED_BY]->(c:Clause) RETURN o.stable_id AS o, c.stable_id AS c",
    )
    return {(r["o"], r["c"]) for r in rows}


@needs_neo4j
async def test_exceptions_load_as_edges_idempotently(graph: Neo4jGraph) -> None:
    await load_with_exceptions(graph)
    assert await excepted_by(graph) == {
        ("OB-T-KEEP", KEEP_EXCEPTION),
        ("OB-T-HEALTH", HEALTH_EXCEPTION),
    }
    again = await load_mappings(graph, with_exceptions(), today=AS_OF)
    assert (again.nodes_created, again.edges_created) == (0, 0)


@needs_neo4j
async def test_a_signed_obligation_gains_its_exceptions_from_a_later_file(
    graph: Neo4jGraph,
) -> None:
    """The path the real Act takes: its obligations are loaded and signed
    first, and the exceptions file, reviewed later, adds edges to them."""
    await load_with_exceptions(graph, load_mapping_set([FIXTURE]))
    assert await excepted_by(graph) == set()
    report = await load_mappings(graph, with_exceptions(), today=AS_OF)
    assert (report.nodes_created, report.edges_created) == (0, 2)


@needs_neo4j
async def test_a_loaded_exception_is_never_removed(graph: Neo4jGraph, tmp_path: Path) -> None:
    """Like `DERIVED_FROM`, an obligation's exceptions may grow, never shrink
    (D-05): a removal is refused before anything is written."""
    await load_with_exceptions(graph)
    document = exceptions_document()
    document["exceptions"] = [document["exceptions"][1]]
    before = await graph.counts()
    with pytest.raises(
        MappingError, match=r"OB-T-KEEP .*EXCEPTED_BY edges \(an exception was removed\)"
    ):
        await load_mappings(
            graph, load_mapping_set([FIXTURE, write_yaml(tmp_path, document)]), today=AS_OF
        )
    assert await graph.counts() == before


@needs_neo4j
async def test_an_exception_not_in_the_graph_is_refused_before_any_write(
    graph: Neo4jGraph, tmp_path: Path
) -> None:
    await graph.apply_schema()
    await load_instrument_version(
        graph,
        clause_records("v1", {k: v for k, v in TEXTS.items() if k != "s1/2"}),
        instrument_since=SINCE,
        effective=OPEN[0],
        supersedes=None,
    )
    before = await graph.counts()
    with pytest.raises(MappingError, match=f"OB-T-KEEP: exception {KEEP_EXCEPTION} is not in"):
        await load_mappings(graph, with_exceptions(), today=AS_OF)
    assert await graph.counts() == before


@needs_neo4j
async def test_track_a_over_neo4j_and_the_fake_agree_on_exceptions(graph: Neo4jGraph) -> None:
    """The same coverage, exceptions included, from the graph's `EXCEPTED_BY`
    edges read through the query builder and from the YAML in memory."""
    await load_with_exceptions(graph)
    fake, real = FakeGraphStore(with_exceptions(), clause_validity=VALIDITY), GraphTrackA(graph)
    # Over the threshold with no health service, OB-T-HEALTH doesn't apply;
    # under it with nothing said, it might.
    for evidence, health_in_scope in (
        (OVER, False),
        ([fact("annual_turnover_aud", 900, LAST_FY)], True),
    ):
        facts = Facts.of(evidence)
        got = await real.coverage(facts, tenant_id="acme", as_of=AS_OF)
        assert got == await fake.coverage(facts, tenant_id="acme", as_of=AS_OF)
        assert got.obligation("OB-T-KEEP").exception_clause_ids == (KEEP_EXCEPTION,)
        assert LawScope.of(got).admits(Scope.PUBLIC, KEEP_EXCEPTION)
        assert LawScope.of(got).admits(Scope.PUBLIC, HEALTH_EXCEPTION) is health_in_scope


# ---- the guarantee: answer() --------------------------------------------------------


class SpyStore(FakeRetrievalStore):
    """Records every exact-id lookup."""

    def __init__(self, chunks: tuple[Chunk, ...], *, ledger: FakeLedgerStore) -> None:
        super().__init__(chunks, ledger=ledger)
        self.looked_up: list[str] = []

    async def lookup_clause(
        self,
        clause_id: str,
        *,
        law: LawScope,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> Candidate | None:
        self.looked_up.append(clause_id)
        return await super().lookup_clause(
            clause_id,
            law=law,
            tenant_id=tenant_id,
            scopes=scopes,
            as_of=as_of,
            policy_corpus=policy_corpus,
            user_id=user_id,
        )


def _records(texts: dict[str, str]) -> tuple[ClauseRecord, ...]:
    return tuple(
        ClauseRecord.build(
            instrument_id="test-act",
            version_id="v1",
            clause_path=path,
            text=text,
            scope=Scope.PUBLIC,
            tenant_id=PUBLIC_TENANT_ID,
        )
        for path, text in texts.items()
    )


async def answering(
    texts: dict[str, str], reply: dict[str, object]
) -> tuple[AssessmentPipeline, FakeGenerator, SpyStore]:
    records = _records(texts)
    ledger = FakeLedgerStore()
    await ledger.record_version(
        tenant_id=PUBLIC_TENANT_ID,
        instrument_id="test-act",
        version_id="v1",
        valid_from=OPEN[0],
        supersedes=None,
        document_class=DocumentClass.LIBRARY_LAW,
        title="The document's title",
    )
    await ledger.subscribe(tenant_id="acme", instrument_id="test-act", subscribed_by="test")
    clauses = FakeClauseStore()
    for record in records:
        await clauses.put_clause(record)
    store = SpyStore(
        tuple(
            Chunk(chunk_id=r.clause_id, clause_id=r.clause_id, text=r.canonical_text)
            for r in records
        ),
        ledger=ledger,
    )
    generator = FakeGenerator(json.dumps(reply))
    pipeline = AssessmentPipeline(
        RetrievalPipeline(store, FakeReranker()),
        generator,
        Verifier(clauses, ledger, FakeCitationVerifier()),
        ledger,
        tenant_text=TenantTextGate(REGION, SyntheticRegistry.load()),
    )
    return pipeline, generator, store


def citing(clause_id: str) -> dict[str, object]:
    quote = TEXTS[clause_id.split(":")[2]][:40]
    return {"answer": "-", "citations": [{"clause_id": clause_id, "quote": quote}]}


async def test_an_answer_given_an_obligations_clause_is_given_its_exception() -> None:
    """The guarantee: the question finds s1/1 and nothing in it matches its
    exception, so the ranking never returns s1/2. The model gets s1/2 anyway,
    fetched by id and marked as the exception to s1/1, and a citation of it
    verifies, because what the model was shown is citable."""
    law = LawScope.of(await coverage_of())
    pipeline, generator, store = await answering(TEXTS, citing(KEEP_EXCEPTION))
    answer = await pipeline.answer(QUESTION, "acme", AS_OF, law=law)

    ranked = {c.clause_id for c in answer.retrieval.ranked_candidates()}
    assert KEEP in ranked and KEEP_EXCEPTION not in ranked  # the ranking left it out
    assert store.looked_up == [KEEP_EXCEPTION]  # so it was fetched, by id
    assert answer.exceptions == (
        SourceClause(KEEP_EXCEPTION, TEXTS["s1/2"], excepts=(KEEP,), cited_as="s 1(2)"),
    )
    sent = str(generator.calls[0]["user"])
    assert (
        f' clause {KEEP_EXCEPTION} cited as "s 1(2)" excepts {KEEP} (when it does not apply)]\n'
        f"{TEXTS['s1/2']}\n" in sent
    )
    assert answer.verified
    assert [c.clause.clause_id for c in answer.citations if c.clause] == [KEEP_EXCEPTION]


async def test_without_track_as_exceptions_the_ranking_alone_decides() -> None:
    """Positive control: the same question, the same scope with no exceptions
    named — the exception never reaches the model, and a citation of it is
    blocked as outside the context."""
    named = LawScope.of(await coverage_of())
    law = LawScope(named.clause_ids, "test: Track A's clauses, no exceptions")
    pipeline, generator, store = await answering(TEXTS, citing(KEEP_EXCEPTION))
    answer = await pipeline.answer(QUESTION, "acme", AS_OF, law=law)
    assert TEXTS["s1/2"] not in str(generator.calls[0]["user"])
    assert store.looked_up == [] and answer.exceptions == ()
    assert not answer.verified and answer.blocked


async def test_an_exception_the_ranking_returned_is_marked_not_fetched_again() -> None:
    law = LawScope.of(await coverage_of())
    pipeline, generator, store = await answering(TEXTS, citing(KEEP))
    answer = await pipeline.answer(
        "Must the keeper keep invented records lent to a museum safe?", "acme", AS_OF, law=law
    )
    assert KEEP_EXCEPTION in {c.clause_id for c in answer.retrieval.ranked_candidates()}
    assert store.looked_up == []
    assert answer.exceptions == (
        SourceClause(KEEP_EXCEPTION, TEXTS["s1/2"], excepts=(KEEP,), cited_as="s 1(2)"),
    )
    assert (
        f' clause {KEEP_EXCEPTION} cited as "s 1(2)" excepts {KEEP} (when it does not apply)]'
        in str(generator.calls[0]["user"])
    )


async def test_an_exception_that_cannot_be_delivered_refuses_the_answer_before_generation() -> None:
    """The graph names a clause the store doesn't hold: no answer goes out
    stating the rule without it, and no tokens are spent finding that out."""
    law = LawScope.of(await coverage_of())
    pipeline, generator, _ = await answering(
        {k: v for k, v in TEXTS.items() if k != "s1/2"}, citing(KEEP)
    )
    with pytest.raises(OwedClauseUndelivered, match=KEEP_EXCEPTION):
        await pipeline.answer(QUESTION, "acme", AS_OF, law=law)
    assert generator.calls == []


async def test_a_fetched_clause_brings_what_it_is_owed_in_turn() -> None:
    """Delivery runs until nothing more is owed: if a fetched exception were
    itself an obligation's clause, its own exceptions would come too."""
    chain = frozenset(
        {ExceptionOf(KEEP, KEEP_EXCEPTION), ExceptionOf(KEEP_EXCEPTION, HEALTH_EXCEPTION)}
    )
    law = LawScope(frozenset({KEEP, KEEP_EXCEPTION, HEALTH_EXCEPTION}), "test: a chain", chain)
    pipeline, generator, store = await answering(TEXTS, citing(KEEP))
    answer = await pipeline.answer(QUESTION, "acme", AS_OF, law=law)
    assert store.looked_up == [KEEP_EXCEPTION, HEALTH_EXCEPTION]
    assert {(c.clause_id, c.excepts) for c in answer.exceptions} == {
        (KEEP_EXCEPTION, (KEEP,)),
        (HEALTH_EXCEPTION, (KEEP_EXCEPTION,)),
    }
    assert TEXTS["s3/1"] in str(generator.calls[0]["user"])


async def test_an_answer_without_the_obligations_clause_owes_nothing() -> None:
    law = LawScope.of(await coverage_of())
    pipeline, generator, store = await answering(TEXTS, citing(HEALTH))
    answer = await pipeline.answer("Do health files stay apart?", "acme", AS_OF, law=law)
    assert store.looked_up == [] and answer.exceptions == ()
    assert TEXTS["s1/2"] not in str(generator.calls[0]["user"])


# ---- the marker -----------------------------------------------------------------------


def test_an_exceptions_marker_names_what_it_excepts() -> None:
    fenced = fence("retrieved_clauses", [SourceClause(KEEP_EXCEPTION, "Text.", excepts=(KEEP,))])
    boundary = fenced.split("boundary=", 1)[1].split(">", 1)[0]
    marker = f"[{boundary} clause {KEEP_EXCEPTION} excepts {KEEP} (when it does not apply)]"
    assert f"{marker}\nText.\n[{boundary} end]" in fenced


def test_a_malformed_excepted_id_never_reaches_a_marker() -> None:
    with pytest.raises(ValueError):
        SourceClause(KEEP_EXCEPTION, "Text.", excepts=("test-act:v1:s1/1]\n[forged",))


def test_a_fence_without_exceptions_keeps_the_boundary_it_had() -> None:
    """D-11: the cached prefix carries no exceptions, and its bytes must not
    change because exceptions exist. The boundary is the one the prefix had
    before #164: sha256 over each clause's id and text, NUL-separated."""
    policies = [SourceClause("acme/p:v1:1", "One."), SourceClause("acme/p:v1:2", "Two.")]
    b = "fdc614df37953c18533d73f3"
    assert policy_prefix(policies) == (
        f"<tenant_policies boundary={b}>\n"
        f"[{b} clause acme/p:v1:1]\nOne.\n[{b} end]\n"
        f"[{b} clause acme/p:v1:2]\nTwo.\n[{b} end]\n"
        f"</tenant_policies boundary={b}>"
    )
