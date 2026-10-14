"""The sparse channel encodes the question as a question (issue #190).

`fastembed`'s `Qdrant/bm25` has two encoders. `.embed` is for documents: each
term's weight is its frequency in the text, softened by the text's length.
`.query_embed` is for questions: each distinct term weighs 1, which is how BM25
scores a query (a word the question repeats is not a second reason to match it).
The adapter used `.embed` for both, so a question that repeated a word weighed
that word up and a long question weighed every term differently from a short one.

The first group pins what each encoder returns for this `fastembed` version, so
the claim is checked rather than assumed, and needs no server. The second builds
a pair on a real Qdrant server (one chunk holding the rare term the question asks
about, two holding only the word it repeats) and proves the question reaches the
sparse channel as a question: the rare-term chunk ranks first, and it does not
when the question is encoded as a document, which a third test pins so the pair
can't quietly stop catching the bug.

The server tests skip without a reachable `QDRANT_URL`, like
`test_qdrant_sparse_idf.py`.
"""

from __future__ import annotations

import datetime as dt
import math
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from qdrant_client import AsyncQdrantClient, models

from compliance_rag.adapters.fakes import FakeEmbedder, FakeLedgerStore
from compliance_rag.adapters.interfaces import Channel, RetrievalResult
from compliance_rag.adapters.qdrant_retrieval_store import (
    SPARSE_VECTOR_NAME,
    QdrantRetrievalStore,
    _encode_documents,  # pyright: ignore[reportPrivateUsage]
    _encode_query,  # pyright: ignore[reportPrivateUsage]
    _sparse_encoder,  # pyright: ignore[reportPrivateUsage]
)
from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.eval.golden import load_golden_set
from tests.conftest import ALL_LAW, qdrant_reachable

_QDRANT_URL = os.environ.get("QDRANT_URL", "")

needs_qdrant = pytest.mark.skipif(
    not qdrant_reachable(_QDRANT_URL),
    reason="QDRANT_URL not set or not reachable — this test needs a real Qdrant server",
)

QUESTION = "records records records records records xylophone"
"""Repeats `records`, a word two of the clauses use, and asks about `xylophone`,
which one clause uses."""

RARE_TEXT = "xylophone custodians keep a quarterly register"
COMMON_TEXTS = (
    "records custodians keep a quarterly register",
    "records auditors review the annual ledger",
)
"""Same length as `RARE_TEXT` once stop words are gone, so the documents weigh
their one query term alike and only the question's encoding can decide the order."""

_FILLER_SUBJECTS = ("members", "trustees", "directors", "officers", "agents", "partners", "staff")
_FILLER_VERBS = ("approve", "publish", "escalate", "reconcile", "retain", "notify")
_FILLER_OBJECTS = ("budgets", "notices", "minutes", "invoices", "returns")
FILLER_COUNT = 37


def _filler_texts() -> tuple[str, ...]:
    """Clauses that share no term with the question."""
    return tuple(
        f"{_FILLER_SUBJECTS[n % 7]} {_FILLER_VERBS[n % 6]} {_FILLER_OBJECTS[n % 5]} "
        f"within {n + 3} days"
        for n in range(FILLER_COUNT)
    )


def _chunk(chunk_id: str, number: int, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        clause_id=f"privacy-act-1988:TEST-FIXTURE-001:sch1/app{number}/1",
        text=text,
    )


def pair_chunks() -> tuple[Chunk, ...]:
    """One chunk holding the rare term, two holding the repeated word, and
    filler: 40 chunks, so `xylophone` is in 1 of them and `records` in 2."""
    texts = (RARE_TEXT, *COMMON_TEXTS, *_filler_texts())
    ids = ("rare", "common-1", "common-2", *(f"filler-{n}" for n in range(FILLER_COUNT)))
    return tuple(_chunk(i, n, t) for n, (i, t) in enumerate(zip(ids, texts, strict=True), 1))


def _weights(vector: models.SparseVector) -> dict[int, float]:
    return dict(zip(vector.indices, vector.values, strict=True))


# ---- what each encoder returns, without a server ----------------------------


@pytest.mark.parametrize(
    "question",
    [
        "entity information xylophone",
        "entity entity entity information xylophone",
        QUESTION,
        "What must the Entity do about information, and about information again?",
        "x" + " entity information" * 40 + " xylophone",
    ],
    ids=["distinct", "repeated", "repeating-a-common-word", "punctuated", "long"],
)
def test_a_question_gives_each_distinct_term_a_weight_of_one(question: str) -> None:
    vector = _encode_query(question)

    assert vector.values, "a question with words has terms"
    assert len(set(vector.indices)) == len(vector.indices)  # one entry per term
    assert set(vector.values) == {1.0}


def test_a_repeated_word_is_still_one_term_in_the_question() -> None:
    once = _encode_query("entity information xylophone")
    repeated = _encode_query("entity entity entity information xylophone")

    assert _weights(repeated) == _weights(once)


def test_a_question_is_encoded_onto_the_dimensions_its_chunks_are_stored_under() -> None:
    """The two encoders share a tokeniser, stemmer and stop list, so the same
    text lands on the same terms: a question can only match a chunk through
    the dimensions the chunk was stored under."""
    text = "The Entity's records, recorded by entities: xylophone!"

    assert set(_encode_query(text).indices) == set(_encode_documents([text])[0].indices)


def test_a_chunk_is_weighted_by_how_often_a_term_occurs_in_it() -> None:
    """Why there are two encoders: the document weight is frequency, softened by
    length, so a repeated word weighs more in a chunk and no word weighs 1."""
    (vector,) = _encode_documents(["entity entity entity information xylophone"])
    weights = sorted(_weights(vector).values())

    assert len(weights) == 3
    assert weights[0] == weights[1] < weights[2]  # entity, repeated, is the heaviest
    assert 1.0 not in weights


def test_encoding_documents_gives_one_vector_per_text_in_order() -> None:
    texts = ["entity information", "xylophone", "records records"]

    vectors = _encode_documents(texts)

    assert [len(v.indices) for v in vectors] == [2, 1, 1]
    assert vectors[1].indices == _encode_query("xylophone").indices


def test_fastembed_itself_gives_a_question_one_entry_per_distinct_term_weighing_one() -> None:
    """The library's own contract for `Qdrant/bm25`, read from it directly
    rather than through the adapter: if a `fastembed` upgrade changes what
    `query_embed` returns, this names it before the ranking test does."""
    (raw,) = _sparse_encoder().query_embed("entity entity entity information xylophone")

    assert sorted(raw.indices.tolist()) == sorted(set(raw.indices.tolist()))
    assert len(raw.indices) == 3
    assert set(raw.values.tolist()) == {1}


def test_a_question_of_stop_words_alone_has_no_terms_under_either_encoder() -> None:
    """Nothing to match on the sparse side, as before the split: the dense
    channel is what answers such a question (`..._still_searches`, below)."""
    (as_document,) = _encode_documents(["the of and"])

    assert _encode_query("the of and") == as_document
    assert _encode_query("the of and").indices == []


def test_every_golden_question_is_encoded_as_a_question_onto_its_chunks_dimensions() -> None:
    """Every real question in the golden set, not a representative few: one
    weight of 1 per distinct term, on the terms the document encoder finds."""
    for golden in load_golden_set().queries:
        question = _encode_query(golden.query)
        (as_document,) = _encode_documents([golden.query])

        assert set(question.values) <= {1.0}, golden.id
        assert len(set(question.indices)) == len(question.indices), golden.id
        assert set(question.indices) == set(as_document.indices), golden.id


# ---- on a real server -------------------------------------------------------


@pytest.fixture
async def client() -> AsyncIterator[AsyncQdrantClient]:
    qdrant = AsyncQdrantClient(url=_QDRANT_URL)
    try:
        yield qdrant
    finally:
        await qdrant.close()


@pytest.fixture
async def store() -> AsyncIterator[QdrantRetrievalStore]:
    """The pair, stored on a throwaway collection (with the IDF modifier, #179)."""
    async with QdrantRetrievalStore.ephemeral(
        _QDRANT_URL, embedder=FakeEmbedder(dimensions=8), ledger=FakeLedgerStore(), prefix="qenc"
    ) as ready:
        await ready.upsert_chunks(pair_chunks())
        yield ready


def _name(store: QdrantRetrievalStore) -> str:
    return store._collection_name  # pyright: ignore[reportPrivateUsage]


async def _search(store: QdrantRetrievalStore, question: str) -> RetrievalResult:
    return await store.search(
        question,
        law=ALL_LAW,
        tenant_id="_public",
        scopes=(Scope.PUBLIC,),
        as_of=dt.date(2026, 1, 1),
        limit=50,
    )


async def _sparse_ranking(store: QdrantRetrievalStore, question: str) -> list[str]:
    """The sparse channel's own list for `question`, best first, every match."""
    result = await _search(store, question)
    return [c.chunk_id for c in result.per_channel[Channel.SPARSE]]


async def _ranking_of_vector(
    client: AsyncQdrantClient, store: QdrantRetrievalStore, vector: models.SparseVector
) -> list[str]:
    hits = await client.query_points(
        _name(store), query=vector, using=SPARSE_VECTOR_NAME, limit=50, with_payload=True
    )
    return [str((p.payload or {})["chunk_id"]) for p in hits.points]


@needs_qdrant
async def test_a_question_repeating_a_common_word_does_not_outrank_the_rare_term_match(
    store: QdrantRetrievalStore,
) -> None:
    ranking = await _sparse_ranking(store, QUESTION)

    assert ranking[0] == "rare", ranking
    assert set(ranking) == {"rare", "common-1", "common-2"}  # nothing else matches


@needs_qdrant
async def test_a_question_of_stop_words_alone_still_searches(
    store: QdrantRetrievalStore,
) -> None:
    result = await _search(store, "the of and")

    assert result.per_channel[Channel.SPARSE] == ()
    assert result.fused  # the dense channel carries it, as before


@needs_qdrant
async def test_the_same_question_encoded_as_a_document_ranks_the_common_word_first(
    client: AsyncQdrantClient, store: QdrantRetrievalStore
) -> None:
    """The pair catches the bug it is for: with the question's repeated word
    weighed by its frequency, as `.embed` does, a clause that only has that word
    outranks the one that has the rare term. If this stops holding, the test
    above proves nothing."""
    (as_document,) = _encode_documents([QUESTION])

    ranking = await _ranking_of_vector(client, store, as_document)

    assert ranking[0] != "rare", ranking


@needs_qdrant
async def test_the_question_reaches_the_sparse_channel_with_every_term_weighing_one(
    store: QdrantRetrievalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[models.SparseVector] = []
    client = store._client  # pyright: ignore[reportPrivateUsage]
    query_points = client.query_points

    async def recording(*args: Any, **kwargs: Any) -> Any:  # Any: forwarded untouched
        query = kwargs.get("query")
        if kwargs.get("using") == SPARSE_VECTOR_NAME and isinstance(query, models.SparseVector):
            sent.append(query)
        return await query_points(*args, **kwargs)

    monkeypatch.setattr(client, "query_points", recording)

    await _sparse_ranking(store, QUESTION)

    assert len(sent) == 1  # the sparse channel's own query; the fused one prefetches
    assert set(sent[0].values) == {1.0}


@needs_qdrant
async def test_chunks_are_still_stored_with_term_frequency_weights(
    client: AsyncQdrantClient, store: QdrantRetrievalStore
) -> None:
    """Only the question changed: a chunk is stored as before, as `.embed`
    weighs it, which is the part of BM25 Qdrant's IDF modifier is multiplied into."""
    records, _next = await client.scroll(
        _name(store), with_vectors=True, with_payload=["chunk_id"], limit=100
    )
    stored = {
        str((r.payload or {})["chunk_id"]): r.vector for r in records if isinstance(r.vector, dict)
    }
    rare = stored["rare"][SPARSE_VECTOR_NAME]
    assert isinstance(rare, models.SparseVector)

    (expected,) = _encode_documents([RARE_TEXT])
    stored_weights, expected_weights = _weights(rare), _weights(expected)
    assert stored_weights.keys() == expected_weights.keys()
    # Qdrant keeps sparse values as 32-bit floats.
    assert all(
        math.isclose(stored_weights[term], weight, rel_tol=1e-6)
        for term, weight in expected_weights.items()
    )
    assert 1.0 not in rare.values
