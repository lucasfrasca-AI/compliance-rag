"""The CAG prefix is capped at 30,000 tokens, and a prefix over the cap fails
loudly before any generator call (#279, ADR-0054).

The unit tests bound the local estimate on every Acme fixture. The pipeline
tests prove the refusal happens before the generator is asked, and the API
tests that it reaches the stream and the chat record as an error turn.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import TurnOutcome
from compliance_rag.assessment.pipeline import LIBRARY_QUESTION, AssessmentPipeline
from compliance_rag.assessment.prefix_cap import (
    CHARS_PER_TOKEN,
    PREFIX_CAP_ADR,
    PREFIX_CAP_TOKENS,
    PolicyCorpusTooLarge,
    estimate_tokens,
)
from compliance_rag.assessment.prompt import SYSTEM_PROMPT, SourceClause, policy_prefix
from compliance_rag.corpus.display_form import cite
from compliance_rag.corpus.policy_parser import parse_policy_markdown
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.eval.golden_corpus import (
    POLICY_INSTRUMENT_IDS,
    POLICY_TENANT_ID,
    POLICY_VERSION_ID,
    golden_clauses,
    policy_source,
    policy_titles,
)
from compliance_rag.eval.harness import golden_clause_store
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import api_settings
from tests.test_api_answers import (
    APP11,
    _ask,  # pyright: ignore[reportPrivateUsage]
    _ledger,  # pyright: ignore[reportPrivateUsage]
    _reply,  # pyright: ignore[reportPrivateUsage]
)

AS_OF = dt.date(2026, 10, 12)
EVERY_POLICY = tuple(POLICY_INSTRUMENT_IDS)
"""All six Acme fixtures (`CLAUDE.md`: every fixture, not a representative one)."""
LOWER_BOUND = 1.15
UPPER_BOUND = 3.0
"""The stated bound (`prefix_cap`'s docstring): the estimate is at least 1.15
times an independent real subword tokenizer's count, and at most 3 times it."""


def _prefix_of(filename: str) -> str:
    """One Acme policy as the cached prefix carries it: fenced, each clause
    marked with the form it is cited by."""
    instrument_id = POLICY_INSTRUMENT_IDS[filename]
    titles = {(instrument_id, POLICY_VERSION_ID): policy_titles()[instrument_id]}
    clauses = parse_policy_markdown(
        policy_source(filename),
        instrument_id=instrument_id,
        version_id=POLICY_VERSION_ID,
        tenant_id=POLICY_TENANT_ID,
    )
    prefix = policy_prefix(
        [
            SourceClause(c.clause_id, c.canonical_text, cited_as=cite(c.clause_id, titles))
            for c in clauses
        ]
    )
    assert prefix is not None
    return prefix


# ---- the estimate ----------------------------------------------------------------------


@pytest.mark.parametrize("filename", EVERY_POLICY)
def test_the_estimate_never_under_counts_a_fixture_against_the_stated_bound(
    filename: str,
) -> None:
    prefix = _prefix_of(filename)
    real = count_reranker_tokens(prefix)
    estimated = estimate_tokens(prefix)
    assert estimated >= LOWER_BOUND * real, (filename, estimated, real)
    assert estimated <= UPPER_BOUND * real, (filename, estimated, real)  # not uselessly loose


def test_the_estimate_holds_the_bound_on_the_whole_acme_prefix_and_its_system_prompt() -> None:
    prefix = "\n".join(_prefix_of(f) for f in EVERY_POLICY)
    real = count_reranker_tokens(SYSTEM_PROMPT) + count_reranker_tokens(prefix)
    estimated = estimate_tokens(SYSTEM_PROMPT, prefix)
    assert LOWER_BOUND * real <= estimated <= UPPER_BOUND * real


def _fenced(clauses: list[tuple[str, str]]) -> str:
    """Clauses as the prefix fences them, ids and 24-hex boundary included."""
    prefix = policy_prefix(
        [SourceClause(f"acme/big-policy:v1:{path}", text, cited_as=path) for path, text in clauses]
    )
    assert prefix is not None
    return prefix


ADVERSARIAL = {
    "many short clauses": _fenced([(f"1/{n}", "Staff must lock screens.") for n in range(1, 301)]),
    "numeric clauses": _fenced(
        [
            (f"{n}/{m}", f"Keep records {n * m} for {m + 7} years, ref 2026-{n:04d}.")
            for n in range(1, 21)
            for m in range(1, 16)
        ]
    ),
    "hex-heavy markers": _fenced(
        [(f"{n}/1", "deadbeefcafe0123456789abcdef" * 4) for n in range(1, 101)]
    ),
    "all-letter hex": "deadbeef" * 400 + " " + "abcdefabcdef" * 300,
    "punctuation-heavy": _fenced(
        [("1/1", "(a)[b]{c}; <d>, !e? #f* @g% &h| ~i^ -j_ +k= /l\\ " * 300)]
    ),
    "newline-heavy": _fenced([(f"1/{n}", "a\n\n\nb") for n in range(1, 301)]),
    "non-ascii": _fenced([("1/1", "法律に従う義務 — “quoted” é ü ñ 🙂 " * 300)]),
}
"""Shapes the six prose-heavy Acme fixtures can't show: each is a prefix the
pipeline could really build (a policy of many short clauses, of numbers, of
ids), measured against the same proxy."""


@pytest.mark.parametrize("shape", ADVERSARIAL)
def test_the_estimate_never_under_counts_a_structure_heavy_prefix(shape: str) -> None:
    prefix = ADVERSARIAL[shape]
    real = count_reranker_tokens(prefix)
    estimated = estimate_tokens(prefix)
    assert estimated >= LOWER_BOUND * real, (shape, estimated, real)
    assert estimated <= 6 * real, (shape, estimated, real)  # still a measurement


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("", 0),
        ("   \t ", 0),
        ("g" * 25, 10),  # letters only: ceil(25 / 2.5)
        ("g", 1),
        ("0123456789", 10),  # a run with a digit: a token a character
        ("c0ffee", 6),
        ("deadbeef", 8),  # hex with no digit, long enough
        ("deadbee", 3),  # too short to be taken for hex: an ordinary word
        ("(a);", 5),  # three symbols at 1.25 and a letter at 1, rounded up
        ("a\nb", 4),  # a newline counts as a symbol: 1.25
        ("—é", 5),  # a token per UTF-8 byte
    ],
)
def test_the_estimate_counts_each_character_class(text: str, tokens: int) -> None:
    assert estimate_tokens(text) == tokens


def test_the_estimate_sums_the_parts_and_ignores_an_absent_one() -> None:
    assert estimate_tokens("g" * 25, None, "h" * 25) == 20
    assert estimate_tokens(None) == 0
    assert CHARS_PER_TOKEN == 2.5


def test_the_cap_is_a_constant_naming_its_adr_not_a_setting() -> None:
    assert PREFIX_CAP_TOKENS == 30_000 and PREFIX_CAP_ADR == "ADR-0054"
    assert not [name for name in Settings.model_fields if "prefix" in name or "cap" in name]
    decisions = Path(__file__).resolve().parents[1] / "DECISIONS.md"
    assert "≤30k tokens since ADR-0054" in decisions.read_text(encoding="utf-8")


# ---- the pipeline: refused before any generator call -------------------------------------


def _policies_estimating(tokens: int) -> list[SourceClause]:
    """A one-clause corpus whose whole prefix (system prompt included) the
    estimate puts at exactly `tokens`."""

    def corpus(n: int) -> list[SourceClause]:
        return [SourceClause("acme/big-policy:v1:1/1", "a" * n, cited_as="1.1")]

    def estimate(n: int) -> int:
        return estimate_tokens(SYSTEM_PROMPT, policy_prefix(corpus(n)))

    size = bisect.bisect_left(range(PREFIX_CAP_TOKENS * 4), tokens, key=estimate)
    assert estimate(size) == tokens
    return corpus(size)


def _pipeline(generator: FakeGenerator) -> AssessmentPipeline:
    ledger = FakeLedgerStore()
    return AssessmentPipeline(
        RetrievalPipeline(FakeRetrievalStore(ledger=ledger), FakeReranker()),
        generator,
        Verifier(FakeClauseStore(), ledger, CanonicalCitationVerifier()),
        ledger,
        tenant_text=TenantTextGate.from_settings(api_settings()),
    )


async def test_a_corpus_just_over_the_cap_fails_before_the_generator_is_called(
    span_exporter: InMemorySpanExporter,
) -> None:
    generator = FakeGenerator()
    pipeline = _pipeline(generator)
    with pytest.raises(PolicyCorpusTooLarge) as refused:
        await pipeline.answer(
            "What must we do?",
            POLICY_TENANT_ID,
            AS_OF,
            law=LIBRARY_QUESTION,
            policies=_policies_estimating(PREFIX_CAP_TOKENS + 1),
        )
    assert generator.calls == []  # no generator call, repair included
    assert refused.value.estimated_tokens == PREFIX_CAP_TOKENS + 1
    assert refused.value.cap_tokens == PREFIX_CAP_TOKENS
    assert "30,000" in str(refused.value) and "30,001" in str(refused.value)
    assert "ADR-0054" in str(refused.value)
    (answer_span,) = (s for s in span_exporter.get_finished_spans() if s.name == "answer")
    assert answer_span.attributes is not None
    assert answer_span.attributes["prefix.estimated_tokens"] == PREFIX_CAP_TOKENS + 1
    assert answer_span.attributes["prefix.cap_tokens"] == PREFIX_CAP_TOKENS


async def test_a_corpus_exactly_at_the_cap_is_read_in_full_and_the_span_records_it(
    span_exporter: InMemorySpanExporter,
) -> None:
    generator = FakeGenerator()
    policies = _policies_estimating(PREFIX_CAP_TOKENS)
    await _pipeline(generator).answer(
        "What must we do?",
        POLICY_TENANT_ID,
        AS_OF,
        law=LIBRARY_QUESTION,
        policies=policies,
    )
    (call,) = generator.calls
    assert call["cached_prefix"] == policy_prefix(policies)  # all of it, never a subset
    (answer_span,) = (s for s in span_exporter.get_finished_spans() if s.name == "answer")
    assert answer_span.attributes is not None
    assert answer_span.attributes["prefix.estimated_tokens"] == PREFIX_CAP_TOKENS


async def test_acmes_corpus_passes_with_room_to_spare(
    span_exporter: InMemorySpanExporter,
) -> None:
    quote = next(c for c in golden_clauses() if c.clause_id == APP11).canonical_text[:60]
    generator = FakeGenerator(_reply((APP11, quote)))
    _, events = await _ask(generator)
    assert [name for name, _ in events] == ["coverage", "retrieval", "answer"]
    assert len(generator.calls) == 1 and generator.calls[0]["cached_prefix"]
    (answer_span,) = (s for s in span_exporter.get_finished_spans() if s.name == "answer")
    assert answer_span.attributes is not None
    estimate = answer_span.attributes["prefix.estimated_tokens"]
    assert isinstance(estimate, int)
    assert (
        3_200 < estimate < PREFIX_CAP_TOKENS * 0.4
    )  # about 3.2k by word count, over-counted but far under


# ---- the API: the stream and the chat record ----------------------------------------------


async def test_the_error_reaches_the_stream_and_the_chat_as_an_error_turn() -> None:
    ledger = await _ledger()
    clauses = await golden_clause_store()
    edited = next(c for c in golden_clauses() if c.instrument_id == "acme/privacy-policy")
    huge = "Acme keeps every record it holds, for as long as it can. " * 2_000
    await clauses.put_clause(edited.model_copy(update={"canonical_text": huge}))
    generator = FakeGenerator(_reply())
    response, events = await _ask(generator, ledger=ledger, clauses=clauses)
    assert response.status_code == 200
    assert [name for name, _ in events] == ["coverage", "retrieval", "error"]
    error = events[-1][1]
    assert error["error"] == "policy_corpus_too_large"
    assert error["cap_tokens"] == PREFIX_CAP_TOKENS
    assert error["estimated_tokens"] > PREFIX_CAP_TOKENS
    assert "too large" in error["message"] and "ADR-0054" in error["message"]
    assert "Acme keeps every record" not in response.text  # the policy's words stay out
    assert generator.calls == []  # before any generator call
    # Recorded as the chat's error turn, as a model that refused for quota is
    # (#251): the member's question, then a marker, and nothing else.
    turns = await ledger.chat_turns(POLICY_TENANT_ID, "uid-123", error["chat_id"])
    assert [t.outcome for t in turns] == [None, TurnOutcome.INTERNAL]
    assert turns[1].content is None and not turns[1].citations and not turns[1].blocked
    assert json.loads(json.dumps(error)) == error
