"""A refused generation and an over-cap policy corpus are their own chat
outcomes (#289, ledger 0019), so a chat read back says what the member was shown.

- `generation_unavailable` stores its reason (`quota`) and nothing from the
  provider's message;
- `policy_corpus_too_large` stores the estimate and the cap, as the stream
  event carries them;
- both are read back through `GET /v1/chats/{id}` on the fake ledger and on
  Postgres, and a row recorded before 0019 (`internal`) reads as it was.

The contract (`tests/contracts/test_ledger.py`, run on both ledgers) and the
schema's constraints (`tests/test_ledger_schema.py`) hold the rest. The
Postgres tests here need `DATABASE_URL` and run in CI.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import uuid
from dataclasses import replace
from typing import Any, LiteralString, cast

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from compliance_rag.adapters.fakes import FakeGenerator, FakeRetrievalStore
from compliance_rag.adapters.interfaces import (
    ChatAnswer,
    ChatExchange,
    LedgerStore,
    PrefixMeasure,
    Profile,
    TurnOutcome,
    Unavailability,
)
from compliance_rag.assessment.prefix_cap import PREFIX_CAP_TOKENS
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, DocumentClass
from compliance_rag.eval.golden_corpus import POLICY_TENANT_ID, golden_chunks, golden_clauses
from compliance_rag.eval.harness import GOLDEN_VALID_FROM, golden_clause_store
from tests.api_support import MEMBER, api_settings
from tests.test_api_answers import GA05, OFFSHORE, QUESTION
from tests.test_api_answers import _client as answers_client  # pyright: ignore[reportPrivateUsage]
from tests.test_api_answers import _events as events_of  # pyright: ignore[reportPrivateUsage]
from tests.test_api_answers import _ledger as acme_ledger  # pyright: ignore[reportPrivateUsage]
from tests.test_api_answers import _reply as reply_of  # pyright: ignore[reportPrivateUsage]
from tests.vertex_quota import quota_exhausted_generator

DATABASE_URL = os.environ.get("DATABASE_URL", "")
needs_postgres = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set: needs a migrated ledger"
)
AS_OF = "2026-10-12"
PROVIDER_TOKEN = "ya29.TOKEN-LIKE-0123456789abcdefghij"
"""What a provider's error message can carry that must never be stored."""
PROVIDER_MESSAGE = f"Quota exceeded for aiplatform.googleapis.com, credential {PROVIDER_TOKEN}"
HUGE_POLICY = "Acme keeps every record it holds, for as long as it can. " * 2_000
MIGRATIONS = pathlib.Path(__file__).parents[1] / "db" / "ledger"


def _sql(text: str) -> LiteralString:
    """A migration file or a name this test generated: never input."""
    return cast("LiteralString", text)


async def _ask(
    client: httpx.AsyncClient, **body: Any
) -> list[tuple[str, dict[str, Any]]]:  # Any: JSON
    response = await client.post("/v1/answers", json={"question": QUESTION, "as_of": AS_OF, **body})
    assert response.status_code == 200
    return events_of(response.text)


async def _refused_for_quota(ledger: LedgerStore) -> dict[str, Any]:  # Any: JSON
    """A tester asks while the model's quota is zero and the provider's message
    carries a token-like string; returns the chat as the API reads it back."""
    requests: list[httpx.Request] = []
    generator = quota_exhausted_generator(
        FakeRetrievalStore(golden_chunks(), ledger=ledger), requests, message=PROVIDER_MESSAGE
    )
    settings = api_settings(**OFFSHORE, offshore_generation_testers=MEMBER)
    async with await answers_client(
        generator, ledger, await golden_clause_store(), settings=settings
    ) as client:
        events = await _ask(client)
        assert len(requests) == 1 and events[-1][1]["error"] == "generation_unavailable"
        read = await client.get(f"/v1/chats/{events[-1][1]['chat_id']}")
    assert read.status_code == 200
    assert PROVIDER_TOKEN not in read.text and "Quota exceeded" not in read.text
    return read.json()


async def _refused_for_size(
    ledger: LedgerStore,
) -> tuple[dict[str, Any], dict[str, Any]]:  # Any: JSON
    """A question over a policy corpus past the cap: the error event, and the
    chat as the API reads it back."""
    clauses = await golden_clause_store()
    edited = next(c for c in golden_clauses() if c.instrument_id == "acme/privacy-policy")
    await clauses.put_clause(edited.model_copy(update={"canonical_text": HUGE_POLICY}))
    async with await answers_client(FakeGenerator(reply_of()), ledger, clauses) as client:
        events = await _ask(client)
        error = events[-1][1]
        assert error["error"] == "policy_corpus_too_large"
        read = await client.get(f"/v1/chats/{error['chat_id']}")
    assert read.status_code == 200
    return error, read.json()


def _check_quota_read_back(chat: dict[str, Any]) -> None:  # Any: JSON
    question, answer = chat["turns"]
    assert question["text"] == QUESTION
    assert answer["error"] == "generation_unavailable"
    assert answer["reason"] == "quota"
    assert answer["verified"] is False and answer["text"] is None
    assert answer["citations"] == [] and answer["blocked"] == []
    assert answer["withheld"] is None and answer["not_in_environment"] == []


def _check_size_read_back(error: dict[str, Any], chat: dict[str, Any]) -> None:  # Any: JSON
    _, answer = chat["turns"]
    assert answer["error"] == "policy_corpus_too_large"
    assert answer["estimated_tokens"] == error["estimated_tokens"] > PREFIX_CAP_TOKENS
    assert answer["cap_tokens"] == error["cap_tokens"] == PREFIX_CAP_TOKENS
    assert answer["text"] is None and answer["citations"] == []
    assert "Acme keeps every record" not in str(chat)  # the policy's words stay out


# ---- the fake ledger -------------------------------------------------------------------


async def test_a_refused_generation_reads_back_with_its_reason_and_no_provider_text() -> None:
    ledger = await acme_ledger()
    chat = await _refused_for_quota(ledger)
    _check_quota_read_back(chat)
    stored = await ledger.chat_turns(POLICY_TENANT_ID, "uid-123", chat["chat_id"])
    assert [t.outcome for t in stored] == [None, TurnOutcome.GENERATION_UNAVAILABLE]
    assert stored[1].unavailable is Unavailability.QUOTA
    # nothing of the provider's message is anywhere in what was recorded
    assert PROVIDER_TOKEN not in repr(stored) and "Quota exceeded" not in repr(stored)


async def test_an_over_cap_corpus_reads_back_with_its_estimate_and_cap() -> None:
    ledger = await acme_ledger()
    error, chat = await _refused_for_size(ledger)
    _check_size_read_back(error, chat)
    stored = await ledger.chat_turns(POLICY_TENANT_ID, "uid-123", error["chat_id"])
    assert [t.outcome for t in stored] == [None, TurnOutcome.POLICY_CORPUS_TOO_LARGE]
    assert stored[1].prefix is not None
    assert (stored[1].prefix.estimated_tokens, stored[1].prefix.cap_tokens) == (
        error["estimated_tokens"],
        error["cap_tokens"],
    )


async def test_a_turn_recorded_before_0019_still_reads_back_as_internal() -> None:
    ledger = await acme_ledger()
    chat_id = uuid.uuid4().hex
    await ledger.record_chat_exchange(
        ChatExchange(
            tenant_id=POLICY_TENANT_ID,
            chat_id=chat_id,
            user_id="uid-123",
            question=QUESTION,
            answer=ChatAnswer(TurnOutcome.INTERNAL),
            as_of=dt.date.fromisoformat(AS_OF),
            trace_id="9" * 32,
        )
    )
    async with await answers_client(FakeGenerator(), ledger, await golden_clause_store()) as client:
        answer = (await client.get(f"/v1/chats/{chat_id}")).json()["turns"][1]
    assert answer["error"] == "internal"
    assert "reason" not in answer and "estimated_tokens" not in answer


# ---- Postgres ---------------------------------------------------------------------------


async def _postgres_world() -> Any:  # Any: PostgresLedgerStore, imported late
    from tests.test_postgres_ledger_store import empty_ledger

    postgres = await empty_ledger()
    await postgres.put_profile(Profile(POLICY_TENANT_ID))
    for tenant_id, instrument_id, version_id in sorted(
        {(c.tenant_id, c.instrument_id, c.version_id) for c in golden_clauses()}
    ):
        await postgres.record_version(
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            valid_from=GOLDEN_VALID_FROM,
            supersedes=None,
            document_class=DocumentClass.LIBRARY_LAW
            if tenant_id == PUBLIC_TENANT_ID
            else DocumentClass.INTERNAL_POLICY,
            title="The document's title",
        )
    await postgres.subscribe(
        tenant_id=POLICY_TENANT_ID, instrument_id="privacy-act-1988", subscribed_by="setup"
    )
    for evidence in GA05.evidence():
        await postgres.record_evidence(replace(evidence, trace_id="5e7" + "0" * 29))
    return postgres


@needs_postgres
async def test_both_refusals_are_recorded_and_read_back_through_the_api_on_postgres() -> None:
    from tests.test_postgres_ledger_store import empty_ledger

    postgres = await _postgres_world()
    try:
        chat = await _refused_for_quota(postgres)
        _check_quota_read_back(chat)
        error, sized = await _refused_for_size(postgres)
        _check_size_read_back(error, sized)
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            rows = await (
                await conn.execute(
                    "SELECT chat_id, role, outcome, outcome_detail, content FROM chat "
                    "ORDER BY created_at, turn_index"
                )
            ).fetchall()
            # No column of any row holds a word of the provider's message.
            leaked = await (
                await conn.execute(
                    "SELECT count(*) FROM chat WHERE chat::text LIKE %s OR chat::text LIKE %s",
                    (f"%{PROVIDER_TOKEN}%", "%Quota exceeded%"),
                )
            ).fetchone()
        assert leaked == (0,)
        assert [(r[1], r[2], r[3]) for r in rows] == [
            ("user", None, None),
            ("assistant", "generation_unavailable", {"reason": "quota"}),
            ("user", None, None),
            (
                "assistant",
                "policy_corpus_too_large",
                {"estimated_tokens": error["estimated_tokens"], "cap_tokens": PREFIX_CAP_TOKENS},
            ),
        ]
        assert all(r[4] is None for r in rows[1::2])
    finally:
        await empty_ledger()


@needs_postgres
async def test_a_stored_detail_that_cant_be_read_costs_the_turn_its_detail_and_not_the_chat() -> (
    None
):
    """The constraint admits nothing else, so this is a row from before it was
    enforced: the chat is still returned, the turn keeps its outcome."""
    from tests.test_postgres_ledger_store import empty_ledger

    postgres = await empty_ledger()
    try:
        await postgres.put_profile(Profile("acme"))
        for number, shown in enumerate(
            (
                ChatAnswer(TurnOutcome.GENERATION_UNAVAILABLE, unavailable=Unavailability.QUOTA),
                ChatAnswer(
                    TurnOutcome.POLICY_CORPUS_TOO_LARGE,
                    prefix=PrefixMeasure(5, 3),
                ),
            )
        ):
            await postgres.record_chat_exchange(
                ChatExchange(
                    tenant_id="acme",
                    chat_id=f"c{number}",
                    user_id="uid-1",
                    question="Q?",
                    answer=shown,
                    as_of=dt.date(2026, 10, 12),
                    trace_id="1" * 32,
                )
            )
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute("ALTER TABLE chat DROP CONSTRAINT chat_outcome_detail")
            await conn.execute(
                "UPDATE chat SET outcome_detail = %s WHERE chat_id = 'c0' AND turn_index = 1",
                (Jsonb({"reason": "Quota exceeded: ya29.SECRET"}),),
            )
            await conn.execute(
                "UPDATE chat SET outcome_detail = %s WHERE chat_id = 'c1' AND turn_index = 1",
                (Jsonb({"estimated_tokens": "lots", "cap_tokens": None}),),
            )
            await conn.commit()
            try:
                _, unavailable = await postgres.chat_turns("acme", "uid-1", "c0")
                _, too_large = await postgres.chat_turns("acme", "uid-1", "c1")
            finally:
                await conn.execute("DELETE FROM chat")
                # the migration is idempotent: it puts the constraint back
                await conn.commit()
                with psycopg.connect(DATABASE_URL, autocommit=True) as restore:
                    restore.execute(
                        _sql((MIGRATIONS / "0019_chat_refusal_outcomes.sql").read_text())
                    )
                await conn.commit()
        assert (unavailable.outcome, unavailable.unavailable) == (
            TurnOutcome.GENERATION_UNAVAILABLE,
            None,
        )
        assert (too_large.outcome, too_large.prefix) == (TurnOutcome.POLICY_CORPUS_TOO_LARGE, None)
    finally:
        await empty_ledger()


# ---- the migration ---------------------------------------------------------------------


@needs_postgres
def test_0019_applies_to_a_ledger_at_0018_and_leaves_its_rows_as_they_were() -> None:
    """A scratch database brought to 0018, holding turns of every outcome that
    existed, then 0019 applied: each row is unchanged, takes no detail, the new
    outcomes are accepted, and applying it again changes nothing."""
    name = f"ledger_0018_{uuid.uuid4().hex[:12]}"
    admin = psycopg.connect(DATABASE_URL, autocommit=True)
    try:
        try:
            admin.execute(_sql(f'CREATE DATABASE "{name}"'))
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("this role can't create a scratch database")
        scratch = psycopg.connect(make_conninfo(DATABASE_URL, dbname=name), autocommit=True)
        try:
            files = sorted(MIGRATIONS.glob("*.sql"))
            before = [f for f in files if f.name < "0019"]
            assert files[-1].name.startswith("0019") and before[-1].name.startswith("0018")
            for migration in before:
                scratch.execute(_sql(migration.read_text()))
            scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
            old = (
                "verified",
                "blocked",
                "uncited",
                "residency",
                "malformed_generation",
                "internal",
            )
            clause = "privacy-act-1988:C2026C00227:sch1/app11/1"
            for number, outcome in enumerate(old):
                chat_id = f"c{number}"
                shared: dict[str, Any] = {  # Any: row values
                    "t": "acme",
                    "c": chat_id,
                    "u": "uid-1",
                    "d": dt.date(2026, 10, 14),
                    "tr": "t1",
                }
                scratch.execute(
                    "INSERT INTO chat (tenant_id, chat_id, turn_index, user_id, role, content, "
                    "as_of, trace_id) VALUES (%(t)s, %(c)s, 0, %(u)s, 'user', 'Q?', %(d)s, %(tr)s)",
                    shared,
                )
                scratch.execute(
                    "INSERT INTO chat (tenant_id, chat_id, turn_index, user_id, role, content, "
                    "outcome, citations, blocked, as_of, trace_id) VALUES "
                    "(%(t)s, %(c)s, 1, %(u)s, 'assistant', %(content)s, %(o)s, %(cit)s, "
                    "%(blk)s, %(d)s, %(tr)s)",
                    {
                        **shared,
                        "o": outcome,
                        "content": "Text." if outcome == "verified" else None,
                        "cit": Jsonb(
                            [{"clause_id": clause, "version_id": "C2026C00227"}]
                            if outcome == "verified"
                            else []
                        ),
                        "blk": Jsonb([clause] if outcome == "blocked" else []),
                    },
                )
            snapshot = (
                "SELECT row_to_json(c)::jsonb - 'outcome_detail' FROM chat c "
                "ORDER BY c.chat_id, c.turn_index"
            )
            rows_before = scratch.execute(_sql(snapshot)).fetchall()
            assert len(rows_before) == 12

            for _ in range(2):  # and again: idempotent
                scratch.execute(_sql(files[-1].read_text()))
                assert scratch.execute(_sql(snapshot)).fetchall() == rows_before
                assert scratch.execute(
                    "SELECT count(*) FROM chat WHERE outcome_detail IS NOT NULL"
                ).fetchone() == (0,)
                assert scratch.execute("SELECT count(*) FROM chat").fetchone() == (12,)

            # (a chat's first turn is its question, so each new outcome follows one)
            for outcome, detail in (
                ("generation_unavailable", {"reason": "quota"}),
                ("policy_corpus_too_large", {"estimated_tokens": 31204, "cap_tokens": 30000}),
            ):
                chat_id = f"new-{outcome}"
                scratch.execute(
                    "INSERT INTO chat (tenant_id, chat_id, turn_index, user_id, role, content, "
                    "as_of, trace_id) VALUES ('acme', %s, 0, 'uid-1', 'user', 'Q?', "
                    "'2026-10-14', 't2')",
                    (chat_id,),
                )
                scratch.execute(
                    "INSERT INTO chat (tenant_id, chat_id, turn_index, user_id, role, outcome, "
                    "outcome_detail, as_of, trace_id) VALUES ('acme', %s, 1, 'uid-1', "
                    "'assistant', %s, %s, '2026-10-14', 't2')",
                    (chat_id, outcome, Jsonb(detail)),
                )
            assert scratch.execute("SELECT count(*) FROM chat").fetchone() == (16,)
        finally:
            scratch.close()
    finally:
        admin.execute(_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.close()
