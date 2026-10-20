"""A question naming an instrument outside the tenant's environment (#252,
ADR-0051): detected from the question alone, and flagged in every layer.

The human's case: a tenant holding only the Privacy Act asked "under the spam
act, can i send emails to customers without consent…". Retrieval ranked APP
7.8(b) first ("the Spam Act 2003;", the Privacy Act's own carve-out), then APP
7's direct-marketing clauses, and the verifier can't tell an answer that
presents APP 7 as Spam Act law from a good one: every quote matches.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from scripts.run_answer_eval import INJECTION_QUESTIONS

from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import Profile
from compliance_rag.assessment import environment
from compliance_rag.assessment.environment import (
    InstrumentRegister,
    NotInEnvironment,
    UnsignedInstrumentRegister,
    committed_instrument_register,
    load_instrument_register,
    named_instruments,
    not_in_environment,
    part_selected,
)
from compliance_rag.assessment.pipeline import LIBRARY_QUESTION, AssessmentPipeline
from compliance_rag.assessment.prompt import (
    SYSTEM_PROMPT,
    SourceClause,
    environment_note,
    fence,
    user_prompt,
)
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import ClauseRecord, DocumentClass
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.eval.golden import load_golden_set
from compliance_rag.eval.golden_corpus import LAW_TITLE, gate_law_clauses, policy_titles
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import api_settings

SPAM_QUESTION = (
    "under the spam act, can i send emails to customers without consent from a personal "
    "email instead of business one"
)
LAW = "privacy-act-1988"
LAW_VERSION = "C2026C00227"
APP7_2 = f"{LAW}:{LAW_VERSION}:sch1/app7/2"
APP7_8_B = f"{LAW}:{LAW_VERSION}:sch1/app7/8/b"
HELD = {LAW: frozenset({LAW_TITLE})}
"""A tenant subscribed to the Act and nothing else, as in the human's case."""
NO_REGISTER = InstrumentRegister()
AS_OF = dt.date(2026, 10, 21)


def absent(question: str, held: dict[str, frozenset[str]] | None = None) -> list[str]:
    return [
        a.named for a in not_in_environment(question, HELD if held is None else held, NO_REGISTER)
    ]


# ---- detection: the pattern alone ---------------------------------------------------


def test_the_humans_lowercase_spam_act_question_is_flagged_by_the_pattern_alone() -> None:
    assert not_in_environment(SPAM_QUESTION, HELD, NO_REGISTER) == (NotInEnvironment("spam act"),)


@pytest.mark.parametrize(
    ("question", "named"),
    [
        ("Under the Spam Act 2003, can we email customers?", ["Spam Act 2003"]),
        ("Does the Spam Act apply to SMS?", ["Spam Act"]),
        ("does the spam act apply to SMS?", ["spam act"]),
        ("is the spam act relevant here", ["spam act"]),
        ("spam act 2003 consent rules", ["spam act 2003"]),
        # The pattern stops at a function word, so it names less than the
        # register does ("Do Not Call Register Act 2006", below), but it flags.
        ("Under the Do Not Call Register Act 2006", ["Call Register Act 2006"]),
        (
            "What does the Telecommunications (Interception and Access) Act 1979 say?",
            ["Telecommunications (Interception and Access) Act 1979"],
        ),
        (
            "the Australian Securities and Investments Commission Act 2001",
            ["Australian Securities and Investments Commission Act 2001"],
        ),
        ("Under the Privacy Act 2020, what changed?", ["Privacy Act 2020"]),
        ("Both the Spam Act and the SPAM ACT, under the spam act", ["Spam Act"]),
        ("(see the Spam Act)", ["Spam Act"]),
        # The verb after a title-case name reads as a verb, unless a statute
        # is introduced ("under the").
        ("Under the Spam Act on 1 July, what changed?", ["Spam Act"]),
    ],
)
def test_the_pattern_flags_an_act_the_environment_doesnt_hold(
    question: str, named: list[str]
) -> None:
    assert absent(question) == named


@pytest.mark.parametrize(
    "question",
    [
        "What does the Privacy Act say about direct marketing?",
        "under the privacy act, what is personal information?",
        "Is Acme covered by the Privacy Act 1988?",
        "What makes a business exempt from counting as an organisation under the Act?",
        "How should the privacy officer act on a complaint?",
        "how should the officer act?",
        "The staff act responsibly when they handle data",
        "members of the board act on advice",
        "We must act quickly if there is a breach",
        "How do I act on a request to delete data?",
        "Is it different in the ACT?",
        "an act or practice that breaches an APP",
        "Can Acme Act on the Board decision?",
        "Should Legal Act On It",
        "Our Marketing Team Act fast",
    ],
)
def test_the_pattern_flags_nothing_held_and_never_the_verb_or_the_territory(
    question: str,
) -> None:
    assert absent(question) == []


def test_an_instrument_held_as_a_tenants_own_document_is_held_by_its_recorded_title() -> None:
    supplied = {**HELD, "acme/spam-act": frozenset({"Spam Act 2003"})}
    assert absent(SPAM_QUESTION, supplied) == []
    assert absent("under the spam act 2003", supplied) == []
    # A different year is a different instrument.
    assert absent("under the spam act 2004", supplied) == ["spam act 2004"]


def test_a_tenant_holding_nothing_has_the_privacy_act_flagged_too() -> None:
    assert absent("What does the Privacy Act say?", {}) == ["Privacy Act"]


def test_retrieved_text_never_reaches_detection() -> None:
    """Only the question is read: APP 7.8(b)'s own words name the Spam Act,
    and a question that doesn't name it gets no flag whatever is retrieved."""
    app7_8_b = next(c for c in gate_law_clauses() if c.clause_id == APP7_8_B)
    assert app7_8_b.canonical_text == "the Spam Act 2003;"
    assert absent("Can we email customers without consent?") == []


# ---- false positives, measured on everything we hold ---------------------------------


def _golden_questions() -> list[str]:
    return [q.query for q in load_golden_set().queries] + list(INJECTION_QUESTIONS)


def test_no_golden_question_names_any_instrument() -> None:
    """Every golden query and every live injection probe: none names an
    instrument at all, held or not, with the committed register and without."""
    questions = _golden_questions()
    assert len(questions) == 28
    for register in (NO_REGISTER, committed_instrument_register()):
        named = {q: [n.named for n in named_instruments(q, register)] for q in questions}
        assert {q: n for q, n in named.items() if n} == {}


def test_no_synthetic_policy_title_triggers_it() -> None:
    """Each synthetic policy's recorded title, and the Act's: none is flagged,
    whatever else the tenant holds."""
    titles = [*policy_titles().values(), LAW_TITLE]
    assert len(titles) == 7
    for register in (NO_REGISTER, committed_instrument_register()):
        for held in (HELD, {}):
            for title in titles:
                if title == LAW_TITLE and not held:
                    continue  # the Act, asked by a tenant that doesn't hold it
                assert not_in_environment(title, held, register) == (), title


# ---- the register ---------------------------------------------------------------------


REGISTER = """\
author: "model: Claude Opus 5.5 — a test fixture"
reviewed_by: "Lucas Frasca"
date: "2026-10-21"
instruments:
  - known_as: "Privacy Act 1988"
    library_id: privacy-act-1988
    aliases: ["Privacy Act"]
  - known_as: "Spam Act 2003"
    aliases: ["Spam Act"]
  - known_as: "Do Not Call Register Act 2006"
    aliases: ["Do Not Call Register Act", "DNCR Act"]
  - known_as: "General Data Protection Regulation (EU) 2016/679"
    aliases: ["GDPR", "General Data Protection Regulation"]
  - known_as: "Notifiable Data Breaches scheme"
    aliases: ["NDB scheme", "notifiable data breaches"]
    part_of: privacy-act-1988
    paths: ["s26w"]
"""


def _register(tmp_path: Path, text: str = REGISTER, root: Path | None = None) -> InstrumentRegister:
    path = tmp_path / "instruments.yaml"
    path.write_text(text, encoding="utf-8")
    return load_instrument_register(path, root or environment.INSTRUMENTS_ROOT)


def test_the_register_names_what_the_pattern_finds_and_what_it_cant(tmp_path: Path) -> None:
    register = _register(tmp_path)
    assert not_in_environment(SPAM_QUESTION, HELD, register) == (
        NotInEnvironment("spam act", known_as="Spam Act 2003"),
    )
    assert not_in_environment("Does the GDPR apply to us?", HELD, register) == (
        NotInEnvironment("GDPR", known_as="General Data Protection Regulation (EU) 2016/679"),
    )
    assert not_in_environment("What does the Privacy Act say?", HELD, register) == ()
    assert not_in_environment("What does the Privacy Act say?", {}, register) == (
        NotInEnvironment("Privacy Act", known_as="Privacy Act 1988"),
    )
    # Lowercase, after a function word, where the pattern alone reads nothing.
    question = "Can we breach the do not call register act?"
    assert absent(question) == []
    assert not_in_environment(question, HELD, register) == (
        NotInEnvironment("do not call register act", known_as="Do Not Call Register Act 2006"),
    )
    # A name the register doesn't hold is still the pattern's.
    assert not_in_environment("Under the Fair Work Act", HELD, register) == (
        NotInEnvironment("Fair Work Act"),
    )


def test_a_part_the_selection_doesnt_hold_is_flagged_though_its_instrument_is(
    tmp_path: Path,
) -> None:
    """Part IIIC is inside the Privacy Act, and the committed selection holds
    none of it: a tenant holding the Act doesn't hold the scheme."""
    register = _register(tmp_path)
    (ndb,) = (e for e in register.entries if e.part_of is not None)
    assert not part_selected(ndb)
    assert not_in_environment("Do we need to report under the NDB scheme?", HELD, register) == (
        NotInEnvironment(
            "NDB scheme", known_as="Notifiable Data Breaches scheme", part_of="Privacy Act 1988"
        ),
    )
    # Selected into, it is held wherever the Act is, and only there.
    root = tmp_path / "instruments"
    (root / LAW / "v9").mkdir(parents=True)
    (root / LAW / "v9" / "manifest.yaml").write_text(
        'author: "Lucas Frasca"\nreviewed_by: "Lucas Frasca"\ndate: 2026-10-21\n'
        "prefixes:\n  - sch1/\n  - s26we/\n",
        encoding="utf-8",
    )
    selected = _register(tmp_path, root=root)
    assert not_in_environment("the notifiable data breaches rules", HELD, selected) == ()
    assert [a.known_as for a in not_in_environment("the NDB scheme", {}, selected)] == [
        "Notifiable Data Breaches scheme"
    ]
    # A second committed version that leaves Part IIIC out: held on no date,
    # since which version is in force isn't known when the register loads.
    (root / LAW / "v10").mkdir()
    (root / LAW / "v10" / "manifest.yaml").write_text(
        'author: "Lucas Frasca"\nreviewed_by: "Lucas Frasca"\ndate: 2026-10-21\n'
        "prefixes:\n  - sch1/\n",
        encoding="utf-8",
    )
    assert _register(tmp_path, root=root).selected_parts == frozenset()


def test_an_absent_register_is_empty_and_a_pending_one_is_refused(tmp_path: Path) -> None:
    assert load_instrument_register(tmp_path / "none.yaml") == InstrumentRegister()
    pending = REGISTER.replace('"Lucas Frasca"', '"PENDING — Lucas Frasca"')
    with pytest.raises(UnsignedInstrumentRegister, match="awaiting review"):
        _register(tmp_path, pending)
    undated = REGISTER.replace('date: "2026-10-21"', 'date: "PENDING"')
    with pytest.raises(UnsignedInstrumentRegister):
        _register(tmp_path, undated)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (('reviewed_by: "Lucas Frasca"', 'reviewed_by: "model: Claude"'), "never a model"),
        (('aliases: ["Spam Act"]', 'aliases: ["Privacy Act"]'), "names both"),
        (('paths: ["s26w"]', "paths: []"), "names its instrument and its paths"),
        (('aliases: ["Spam Act"]', 'aliases: ["Spam \\"Act\\""]'), "one line of letters"),
        (('paths: ["s26w"]', 'paths: ["S26W"]'), "clause-path prefix"),
    ],
)
def test_a_malformed_register_is_refused(tmp_path: Path, change: tuple[str, str], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        _register(tmp_path, REGISTER.replace(*change))


def test_the_committed_register_loads() -> None:
    """Refused while pending, so an unsigned register stops the build."""
    assert isinstance(committed_instrument_register(), InstrumentRegister)


# ---- the prompt -------------------------------------------------------------------------


def test_the_note_is_fenced_and_every_value_json_quoted() -> None:
    flags = (
        NotInEnvironment('spam "act"', known_as="Spam Act 2003"),
        NotInEnvironment(
            "NDB scheme", known_as="Notifiable Data Breaches scheme", part_of="Privacy Act 1988"
        ),
    )
    note = environment_note(flags)
    assert note is not None
    opening, first, second, closing = note.split("\n")
    boundary = opening.removeprefix("<not_in_environment boundary=").removesuffix(">")
    assert len(boundary) == 24 and closing == f"</not_in_environment boundary={boundary}>"
    assert first == f'[{boundary} instrument named "spam \\"act\\"" known as "Spam Act 2003"]'
    assert second == (
        f'[{boundary} instrument named "NDB scheme" known as "Notifiable Data Breaches scheme" '
        'part of "Privacy Act 1988"]'
    )
    # The boundary is hashed from the markers: other words, another boundary.
    other = environment_note((NotInEnvironment("spam act"),))
    assert other is not None and boundary not in other


def test_a_prompt_with_nothing_absent_keeps_its_bytes() -> None:
    clauses = [SourceClause(APP7_2, "text")]
    assert environment_note(()) is None
    assert user_prompt("q?", clauses) == f"{fence('retrieved_clauses', clauses)}\n\nQuestion: q?"
    flagged = user_prompt("q?", clauses, (NotInEnvironment("spam act"),))
    note = environment_note((NotInEnvironment("spam act"),))
    assert flagged == f"{fence('retrieved_clauses', clauses)}\n\n{note}\n\nQuestion: q?"


def test_the_system_prompt_says_what_an_answer_must_and_must_never_say() -> None:
    assert "<not_in_environment boundary=B>" in SYSTEM_PROMPT
    assert "say plainly, before anything else, that it is not in this environment" in SYSTEM_PROMPT
    assert "never state, summarise, guess or imply what it requires" in SYSTEM_PROMPT
    assert "name the instrument each clause it relies on is from" in SYSTEM_PROMPT


# ---- the pipeline -----------------------------------------------------------------------


def _law(*clause_ids: str) -> tuple[ClauseRecord, ...]:
    return tuple(c for c in gate_law_clauses() if c.clause_id in clause_ids)


APP7 = (
    f"{LAW}:{LAW_VERSION}:sch1/app7/1",
    APP7_2,
    f"{LAW}:{LAW_VERSION}:sch1/app7/8",
    APP7_8_B,
)
CONFIDENT = "Under the Spam Act you can email customers without consent if you offer an opt-out."


async def spam_pipeline(reply: str) -> tuple[AssessmentPipeline, FakeGenerator]:
    ledger = FakeLedgerStore()
    await ledger.put_profile(Profile("acme"))
    await ledger.record_version(
        tenant_id="_public",
        instrument_id=LAW,
        version_id=LAW_VERSION,
        valid_from=dt.date(2026, 1, 1),
        supersedes=None,
        document_class=DocumentClass.LIBRARY_LAW,
        title=LAW_TITLE,
    )
    await ledger.subscribe(tenant_id="acme", instrument_id=LAW, subscribed_by="setup")
    clauses = FakeClauseStore()
    for clause in _law(*APP7):
        await clauses.put_clause(clause)
    chunks = chunk_clauses(_law(*APP7), count_tokens=count_reranker_tokens)
    generator = FakeGenerator(reply)
    return (
        AssessmentPipeline(
            RetrievalPipeline(FakeRetrievalStore(chunks, ledger=ledger), FakeReranker()),
            generator,
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
            tenant_text=TenantTextGate.from_settings(api_settings()),
            register=NO_REGISTER,
        ),
        generator,
    )


def confident_spam_act_answer(quote_from: str = APP7_2) -> str:
    """The failure the human named: a confident Spam Act answer whose every
    quote is APP 7 text, so the verifier passes it."""
    quote = next(c.canonical_text for c in gate_law_clauses() if c.clause_id == quote_from)
    return json.dumps(
        {"answer": CONFIDENT, "citations": [{"clause_id": APP7_2, "quote": quote[:80]}]}
    )


async def test_a_confident_spam_act_answer_from_app7_text_verifies_and_still_carries_the_flag(
    span_exporter: InMemorySpanExporter,
) -> None:
    pipeline, generator = await spam_pipeline(confident_spam_act_answer())
    answer = await pipeline.answer(SPAM_QUESTION, "acme", AS_OF, law=LIBRARY_QUESTION)
    # The verifier can't see it: the quote is APP 7.2's own text.
    assert answer.verified and answer.unverified_draft.answer == CONFIDENT
    # The flag is the pipeline's, from the question, whatever the model said.
    assert answer.not_in_environment == (NotInEnvironment("spam act"),)
    # The model was told, in the fenced note, before it wrote a word.
    (call,) = generator.calls
    note = environment_note(answer.not_in_environment)
    assert note is not None and note in str(call["user"])
    assert call["system"] == SYSTEM_PROMPT
    (recorded,) = [s for s in span_exporter.get_finished_spans() if s.name == "answer"]
    attributes = recorded.attributes or {}
    assert attributes["answer.not_in_environment"] == ("spam act",)
    assert attributes["answer.environment"] == (LAW,)


async def test_a_withheld_answer_carries_the_flag_too() -> None:
    blocked = json.dumps(
        {"answer": CONFIDENT, "citations": [{"clause_id": APP7_2, "quote": "made up"}]}
    )
    pipeline, _ = await spam_pipeline(blocked)
    answer = await pipeline.answer(SPAM_QUESTION, "acme", AS_OF, law=LIBRARY_QUESTION)
    assert not answer.verified and answer.blocked
    assert answer.not_in_environment == (NotInEnvironment("spam act"),)


async def test_a_question_naming_only_held_law_gets_no_flag_and_no_note() -> None:
    pipeline, generator = await spam_pipeline(confident_spam_act_answer())
    answer = await pipeline.answer(
        "Under the Privacy Act, can we email customers?", "acme", AS_OF, law=LIBRARY_QUESTION
    )
    assert answer.not_in_environment == ()
    assert "not_in_environment" not in str(generator.calls[0]["user"])
