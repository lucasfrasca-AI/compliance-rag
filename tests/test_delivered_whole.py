"""A library-law clause is read and delivered in its provision (#200,
ADR-0045). The cross-encoder reads each fused candidate after the clauses that
introduce it (its chapeaux); the ranked top k are delivered whole, each with
its chapeaux and the clauses it introduces (its paragraphs), fetched by
containment and never ranked. What generation is given is `delivered()`, each
clause in its own canonical text.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.fakes import (
    FakeCitationVerifier,
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import Candidate, LawScope, ScoredDocument
from compliance_rag.assessment.pipeline import LIBRARY_QUESTION, AssessmentPipeline
from compliance_rag.corpus.chunker import CHUNK_MAX_TOKENS
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    Chunk,
    ClauseRecord,
    DocumentClass,
    Scope,
    clause_ancestors,
    in_same_provision,
)
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.golden_corpus import LAW_INSTRUMENT_ID, gate_law_clauses
from compliance_rag.eval.recall import NoRerank
from compliance_rag.retrieval.pipeline import RetrievalPipeline, RetrievalTrace, read_in_provision
from compliance_rag.settings import REGION
from compliance_rag.verifier.citations import Verifier
from tests.conftest import ALL_LAW

AS_OF = dt.date(2026, 10, 1)


def law(path: str) -> str:
    return f"privacy-act-1988:V1:{path}"


# Shaped like the Act, worded so the fake's token overlap ranks exactly the
# clauses a test names: a ranked clause is one that shares a word with the
# question, and nothing else is ranked.
TEXTS = {
    "sch1/app11/1": "the entity must take such steps to protect the information:",
    "sch1/app11/1/a": "from misuse interference and loss; and",
    "sch1/app11/1/b": "from unauthorised access modification or disclosure.",
    "sch1/app11/10": "a tenth subclause, which shares a prefix and nothing else.",
    "s6/1": "In this Act, unless the contrary intention appears:",
    "s6/1/agency": "agency means:",
    "s6/1/agency/a": "a Minister; or",
    "s6/1/agency/c": "a body established for a public purpose, not being:",
    "s6/1/agency/c/i": "an incorporated company, society or association; or",
}
CHAPEAU, PARA_A, PARA_B, TENTH = (
    law(p) for p in ("sch1/app11/1", "sch1/app11/1/a", "sch1/app11/1/b", "sch1/app11/10")
)
S6_1, AGENCY, AGENCY_A, AGENCY_C, AGENCY_C_I = (
    law(p) for p in ("s6/1", "s6/1/agency", "s6/1/agency/a", "s6/1/agency/c", "s6/1/agency/c/i")
)

# A tenant's document with nesting the policy parser never produces today:
# delivery is for library law only, so it is delivered as ranked.
POLICY_SECTION = "acme/retention-policy:v1:3"
POLICY_ITEM = "acme/retention-policy:v1:3/1"


def _chunks() -> tuple[Chunk, ...]:
    library = tuple(
        Chunk(chunk_id=law(path), clause_id=law(path), text=text) for path, text in TEXTS.items()
    )
    policy = (
        Chunk(
            chunk_id=POLICY_SECTION,
            clause_id=POLICY_SECTION,
            text="Retention of customer records:",
            scope=Scope.TENANT,
            tenant_id="acme",
            synthetic=True,
        ),
        Chunk(
            chunk_id=POLICY_ITEM,
            clause_id=POLICY_ITEM,
            text="account details are kept seven years.",
            scope=Scope.TENANT,
            tenant_id="acme",
            synthetic=True,
        ),
    )
    return (*library, *policy)


def _pipeline() -> RetrievalPipeline:
    return RetrievalPipeline(FakeRetrievalStore(_chunks(), ledger=FakeLedgerStore()), NoRerank())


async def _retrieve(query: str, *, law_scope: LawScope = ALL_LAW) -> RetrievalTrace:
    return await _pipeline().route(query, PUBLIC_TENANT_ID, AS_OF, law=law_scope)


def _ids(candidates: tuple[Candidate, ...]) -> list[str]:
    return [c.clause_id for c in candidates]


# ---- the provision: containment through paragraphs, never across a Definition


def test_a_paragraph_is_in_its_chapeaus_provision() -> None:
    assert in_same_provision(CHAPEAU, PARA_A)
    assert in_same_provision(AGENCY, AGENCY_C_I)
    assert in_same_provision(AGENCY_C, AGENCY_C_I)


def test_a_provision_is_by_path_segment_and_never_reversed() -> None:
    assert not in_same_provision(CHAPEAU, TENTH)  # sch1/app11/10 is not inside sch1/app11/1
    assert not in_same_provision(PARA_A, CHAPEAU)
    assert not in_same_provision(CHAPEAU, CHAPEAU)


def test_a_definition_is_a_provision_of_its_own() -> None:
    """s 6(1) holds every definition, but none is one of its paragraphs."""
    assert not in_same_provision(S6_1, AGENCY)
    assert not in_same_provision(S6_1, AGENCY_C_I)


def test_a_provision_stays_in_its_version() -> None:
    assert not in_same_provision("privacy-act-1988:V2:sch1/app11/1", PARA_A)


def test_on_the_whole_compilation_only_s6_1_holds_clauses_outside_its_provision() -> None:
    """Every stored clause of C2026C00227 that contains another stored clause
    contains it within one provision, except s 6(1), whose 422 are the
    definitions and their paragraphs (ADR-0045)."""
    ids = {c.clause_id for c in gate_law_clauses()}
    crossing = {
        a for c in ids for a in clause_ancestors(c) if a in ids and not in_same_provision(a, c)
    }
    assert crossing == {f"{LAW_INSTRUMENT_ID}:C2026C00227:s6/1"}


def test_what_a_ranked_clause_can_bring_on_the_whole_compilation_is_pinned() -> None:
    """Delivered recall counts every clause a ranked one brings, so what it
    can bring may not widen unseen (ADR-0045): a looser provision rule would
    raise recall with no better ranking. On C2026C00227, 497 clauses have a
    chapeau (two at most) and 152 have paragraphs (22 at most, "enforcement
    body"): 593 pairs. A change here is a decision, re-recorded deliberately."""
    ids = {c.clause_id for c in gate_law_clauses()}
    pairs = [
        (a, c) for c in ids for a in clause_ancestors(c) if a in ids and in_same_provision(a, c)
    ]
    chapeaux = Counter(c for _, c in pairs)
    paragraphs = Counter(a for a, _ in pairs)
    assert len(pairs) == 593
    assert (len(chapeaux), max(chapeaux.values())) == (497, 2)
    assert (len(paragraphs), max(paragraphs.values())) == (152, 22)
    assert paragraphs.most_common(1) == [
        (f"{LAW_INSTRUMENT_ID}:C2026C00227:s6/1/enforcement-body", 22)
    ]


# ---- the cross-encoder reads a candidate in its provision ----------------------


class Reading(NoRerank):
    """Fusion's order, recording what the cross-encoder would read."""

    def __init__(self) -> None:
        self.read: list[tuple[str, ...]] = []

    async def rerank(
        self, query: str, documents: tuple[str, ...], *, top_k: int
    ) -> tuple[ScoredDocument, ...]:
        self.read.append(documents)
        return await super().rerank(query, documents, top_k=top_k)


async def _read(query: str, tenant_id: str = PUBLIC_TENANT_ID) -> tuple[str, ...]:
    reading = Reading()
    store = FakeRetrievalStore(_chunks(), ledger=FakeLedgerStore())
    await RetrievalPipeline(store, reading).retrieve(query, tenant_id, AS_OF, law=ALL_LAW)
    (documents,) = reading.read
    return documents


async def test_the_cross_encoder_reads_a_paragraph_after_its_chapeau() -> None:
    """GQ-18's (a) and GQ-20's agency paragraphs are fragments read alone:
    their meaning is in the words that introduce them."""
    assert await _read("misuse") == (f"{TEXTS['sch1/app11/1']} {TEXTS['sch1/app11/1/a']}",)
    assert await _read("incorporated") == (
        " ".join(TEXTS[p] for p in ("s6/1/agency", "s6/1/agency/c", "s6/1/agency/c/i")),
    )


async def test_the_cross_encoder_reads_a_clause_with_no_chapeau_as_it_is() -> None:
    """Nothing introduces APP 11.1 here, and a Definition's own clause isn't
    read after s 6(1); a tenant's clause is read as it is."""
    assert await _read("protect") == (TEXTS["sch1/app11/1"],)
    assert await _read("agency") == (TEXTS["s6/1/agency"],)
    assert await _read("seven", tenant_id="acme") == ("account details are kept seven years.",)


async def test_what_is_delivered_is_each_clauses_own_text_not_what_was_read() -> None:
    trace = await _retrieve("misuse")
    assert [(c.clause_id, c.text) for c in trace.delivered()] == [
        (CHAPEAU, TEXTS["sch1/app11/1"]),
        (PARA_A, TEXTS["sch1/app11/1/a"]),
    ]


def test_read_in_provision_takes_only_the_chapeaux_it_is_given() -> None:
    """Out of the law scope or not in force, a chapeau isn't in the lineage
    the store returns, and the candidate is read as it is."""
    candidate = Candidate(chunk_id=PARA_A, clause_id=PARA_A, text="from misuse", score=0.0)
    assert read_in_provision(candidate, ()) == "from misuse"
    chapeau = Candidate(chunk_id=CHAPEAU, clause_id=CHAPEAU, text="to protect it:", score=1.0)
    tenth = Candidate(chunk_id=TENTH, clause_id=TENTH, text="not a chapeau", score=1.0)
    assert read_in_provision(candidate, (tenth, chapeau)) == "to protect it: from misuse"


def test_every_clause_of_the_compilation_read_in_its_provision_fits_the_reranker_window() -> None:
    """The chunk budget (`CHUNK_MAX_TOKENS`, by the reranker's own tokenizer)
    holds for what the cross-encoder reads, a clause after its chapeaux, so the
    service never refuses a candidate for its length (ADR-0045). 115 at most
    on C2026C00227, against 420."""
    clauses = gate_law_clauses()
    as_candidates = tuple(
        Candidate(chunk_id=c.clause_id, clause_id=c.clause_id, text=c.canonical_text, score=0.0)
        for c in clauses
    )
    longest = max(count_reranker_tokens(read_in_provision(c, as_candidates)) for c in as_candidates)
    assert longest <= CHUNK_MAX_TOKENS


# ---- retrieve(): what a ranked clause brings with it ---------------------------


async def test_a_ranked_chapeau_is_delivered_with_its_paragraphs() -> None:
    """GQ-18's shape: APP 11.1 is ranked and its (a) and (b) are not."""
    trace = await _retrieve("protect")
    assert _ids(trace.ranked_candidates()) == [CHAPEAU]
    assert _ids(trace.delivered()) == [CHAPEAU, PARA_A, PARA_B]  # never the 10th subclause
    assert _ids(trace.paragraphs) == [PARA_A, PARA_B] and trace.chapeaux == ()


async def test_a_ranked_paragraph_is_delivered_after_its_chapeaux_outermost_first() -> None:
    """A definition's paragraph comes with the words that say what it
    defines, up to the definition and not into s 6(1); its sibling paragraphs
    don't come with it."""
    trace = await _retrieve("incorporated")
    assert _ids(trace.ranked_candidates()) == [AGENCY_C_I]
    assert _ids(trace.delivered()) == [AGENCY, AGENCY_C, AGENCY_C_I]


async def test_the_subsection_that_holds_the_definitions_brings_none_of_them() -> None:
    trace = await _retrieve("contrary intention")
    assert _ids(trace.delivered()) == [S6_1]


async def test_each_clause_is_delivered_once_and_the_ranking_is_untouched() -> None:
    """The ranker's top k and their order are what they were: delivery adds,
    after ranking, and never re-ranks or cuts."""
    pipeline = _pipeline()
    trace = await pipeline.route("protect misuse", PUBLIC_TENANT_ID, AS_OF, law=ALL_LAW, top_k=1)
    assert _ids(trace.ranked_candidates()) == [CHAPEAU]  # top_k=1: (a) ranked 2nd, cut
    assert _ids(trace.delivered()) == [CHAPEAU, PARA_A, PARA_B]
    both = await pipeline.route("protect misuse", PUBLIC_TENANT_ID, AS_OF, law=ALL_LAW)
    assert _ids(both.ranked_candidates()) == [CHAPEAU, PARA_A]
    assert _ids(both.delivered()) == [CHAPEAU, PARA_A, PARA_B]


async def test_a_tenants_own_clauses_are_delivered_as_ranked() -> None:
    trace = await _pipeline().route("Retention", "acme", AS_OF, law=ALL_LAW)
    assert _ids(trace.delivered()) == [POLICY_SECTION]


async def test_what_comes_with_a_ranked_clause_is_bound_by_the_law_scope() -> None:
    """D-03: nothing comes with a ranked clause that its own channel couldn't
    have returned. A scope citing only (a) admits no chapeau; one citing APP
    11.1 admits its paragraphs (ADR-0035)."""
    only_a = LawScope(frozenset({PARA_A}), "test: Track A cites APP 11.1(a) only")
    trace = await _retrieve("misuse", law_scope=only_a)
    assert _ids(trace.delivered()) == [PARA_A]
    chapeau = LawScope(frozenset({CHAPEAU}), "test: Track A cites APP 11.1")
    trace = await _retrieve("protect", law_scope=chapeau)
    assert _ids(trace.delivered()) == [CHAPEAU, PARA_A, PARA_B]


async def test_the_routers_exact_hit_is_delivered_whole_without_a_rerank() -> None:
    reranker = FakeReranker()
    store = FakeRetrievalStore(_chunks(), ledger=FakeLedgerStore())
    trace = await RetrievalPipeline(store, reranker).route(
        PARA_B, PUBLIC_TENANT_ID, AS_OF, law=ALL_LAW
    )
    assert _ids(trace.delivered()) == [CHAPEAU, PARA_B]
    assert reranker.rerank_calls == 0


async def test_what_was_delivered_whole_is_on_the_retrieve_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    await _retrieve("incorporated")
    (span,) = [s for s in span_exporter.get_finished_spans() if s.name == "retrieve"]
    assert span.attributes is not None
    assert list(span.attributes["retrieval.chapeaux"]) == [AGENCY, AGENCY_C]  # type: ignore[arg-type]
    assert list(span.attributes["retrieval.paragraphs"]) == []  # type: ignore[arg-type]


# ---- answer(): the model is given the delivered list ---------------------------


async def test_the_model_is_given_a_ranked_chapeaus_paragraphs_and_may_cite_them() -> None:
    """What the model was shown is citable (D-02): a paragraph delivered with
    its ranked chapeau is in the prompt and in the verifier's context."""
    records = tuple(
        ClauseRecord.build(
            instrument_id="privacy-act-1988",
            version_id="V1",
            clause_path=path,
            text=text,
            scope=Scope.PUBLIC,
            tenant_id=PUBLIC_TENANT_ID,
        )
        for path, text in TEXTS.items()
    )
    ledger = FakeLedgerStore()
    await ledger.record_version(
        tenant_id=PUBLIC_TENANT_ID,
        instrument_id="privacy-act-1988",
        version_id="V1",
        valid_from=dt.date(2026, 1, 1),
        supersedes=None,
        document_class=DocumentClass.LIBRARY_LAW,
    )
    clauses = FakeClauseStore()
    for record in records:
        await clauses.put_clause(record)
    store = FakeRetrievalStore(
        tuple(
            Chunk(chunk_id=r.clause_id, clause_id=r.clause_id, text=r.canonical_text)
            for r in records
        ),
        ledger=ledger,
    )
    quote = TEXTS["sch1/app11/1/b"][:30]
    generator = FakeGenerator(
        json.dumps({"answer": "-", "citations": [{"clause_id": PARA_B, "quote": quote}]})
    )
    pipeline = AssessmentPipeline(
        RetrievalPipeline(store, NoRerank()),
        generator,
        Verifier(clauses, ledger, FakeCitationVerifier()),
        ledger,
        tenant_text=TenantTextGate(REGION, SyntheticRegistry.load()),
    )
    answer = await pipeline.answer("protect", PUBLIC_TENANT_ID, AS_OF, law=LIBRARY_QUESTION)
    assert _ids(answer.retrieval.ranked_candidates()) == [CHAPEAU]
    sent = str(generator.calls[0]["user"])
    assert all(TEXTS[p] in sent for p in ("sch1/app11/1", "sch1/app11/1/a", "sch1/app11/1/b"))
    assert answer.verified
    assert [c.clause.clause_id for c in answer.citations if c.clause] == [PARA_B]
