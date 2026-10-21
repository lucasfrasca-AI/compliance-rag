"""The answer event and a chat read back carry the named-instrument flag
(#252, ADR-0051), whatever the model wrote and however the answer ended.

Acme holds its six synthetic policies and subscribes to the Privacy Act, as
in the human's case. The generator is a fake that answers the Spam Act
question confidently, citing Acme's marketing policy verbatim, so the answer
verifies: the reader must still be told the Spam Act isn't in the
environment.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import Profile, TurnOutcome
from compliance_rag.api.app import create_app
from compliance_rag.assessment.environment import NotInEnvironment
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.eval.golden_corpus import POLICY_TENANT_ID, golden_chunks, golden_clauses
from compliance_rag.eval.harness import golden_clause_store, golden_ledger
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import api_settings, bearer, fake_api_services, mint

SPAM_QUESTION = (
    "under the spam act, can i send emails to customers without consent from a personal "
    "email instead of business one"
)
AS_OF = "2026-10-12"
MARKETING = "acme/marketing-policy:v1:1/1"
FLAG = {"named": "spam act", "known_as": "Spam Act 2003", "part_of": None}
"""With the register signed (2026-10-21), the flag names the instrument by its
register name."""


def _reply(quote: str) -> str:
    return json.dumps(
        {
            "answer": "Under the Spam Act you may email customers from any address.",
            "citations": [{"clause_id": MARKETING, "quote": quote}],
        }
    )


def _marketing_quote() -> str:
    return next(c.canonical_text for c in golden_clauses() if c.clause_id == MARKETING)[:60]


async def _ledger() -> FakeLedgerStore:
    ledger = await golden_ledger()
    await ledger.put_profile(Profile(POLICY_TENANT_ID))
    await ledger.subscribe(
        tenant_id=POLICY_TENANT_ID, instrument_id="privacy-act-1988", subscribed_by="setup"
    )
    return ledger


def _client(
    generator: FakeGenerator, ledger: FakeLedgerStore, clauses: FakeClauseStore
) -> httpx.AsyncClient:
    settings = api_settings()
    services = fake_api_services(
        settings=settings,
        ledger=ledger,
        clauses=clauses,
        pipeline=AssessmentPipeline(
            RetrievalPipeline(FakeRetrievalStore(golden_chunks(), ledger=ledger), FakeReranker()),
            generator,
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
            tenant_text=TenantTextGate.from_settings(settings),
        ),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)),
        base_url="http://api",
        headers=bearer(mint()),
    )


def _events(stream: str) -> dict[str, dict[str, Any]]:  # Any: JSON
    events: dict[str, dict[str, Any]] = {}  # Any: JSON
    for block in stream.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events[lines["event"]] = json.loads(lines["data"])
    return events


async def _ask_and_read_back(
    reply: str, question: str = SPAM_QUESTION
) -> tuple[dict[str, Any], dict[str, Any], FakeLedgerStore]:  # Any: JSON
    ledger = await _ledger()
    async with _client(FakeGenerator(reply), ledger, await golden_clause_store()) as client:
        response = await client.post("/v1/answers", json={"question": question, "as_of": AS_OF})
        events = _events(response.text)
        chat_id = next(iter(events.values()))["chat_id"]
        chat = (await client.get(f"/v1/chats/{chat_id}")).json()
    return events, chat, ledger


async def test_a_verified_confident_spam_act_answer_reaches_the_reader_flagged() -> None:
    events, chat, ledger = await _ask_and_read_back(_reply(_marketing_quote()))
    answer = events["answer"]
    assert answer["verified"] is True and answer["text"].startswith("Under the Spam Act")
    assert answer["not_in_environment"] == [FLAG]
    # Recorded with the turn, and read back as the member was shown it.
    _, recorded = await ledger.chat_turns(POLICY_TENANT_ID, "uid-123", answer["chat_id"])
    assert recorded.not_in_environment == (NotInEnvironment("spam act", "Spam Act 2003"),)
    question, shown = chat["turns"]
    assert "not_in_environment" not in question
    assert shown["verified"] is True and shown["not_in_environment"] == [FLAG]


async def test_a_withheld_answer_is_flagged_and_reads_back_so() -> None:
    events, chat, _ = await _ask_and_read_back(_reply("a quote the policy never says"))
    answer = events["answer"]
    assert answer["verified"] is False and answer["text"] is None
    assert answer["not_in_environment"] == [FLAG]
    assert chat["turns"][1]["withheld"] and chat["turns"][1]["not_in_environment"] == [FLAG]


async def test_a_failed_request_is_a_marker_only() -> None:
    events, chat, _ = await _ask_and_read_back("not json")
    assert events["error"]["error"] == "malformed_generation"
    assert "not_in_environment" not in events["error"]
    shown = chat["turns"][1]
    assert shown["error"] == TurnOutcome.MALFORMED_GENERATION.value
    assert shown["not_in_environment"] == []


async def test_a_question_naming_only_what_is_held_is_not_flagged() -> None:
    events, chat, _ = await _ask_and_read_back(
        _reply(_marketing_quote()), "Under the Privacy Act, can Acme email its customers?"
    )
    assert events["answer"]["not_in_environment"] == []
    assert chat["turns"][1]["not_in_environment"] == []
