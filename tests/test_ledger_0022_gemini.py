"""Ledger 0022 (#326, ADR-0057): `gemini` joins `claude` and `demo` as a
generator an answer turn can record, and nothing else changes. Needs a
Postgres server (`DATABASE_URL`); CI sets it and `REQUIRE_LEDGER_DB`."""

from __future__ import annotations

import pathlib
from typing import Any, LiteralString, cast

import psycopg
import pytest

from tests.ledger_db import needs_ledger_db, scratch_database

MIGRATIONS = pathlib.Path(__file__).parents[1] / "db" / "ledger"


def _sql(text: str) -> LiteralString:
    """A migration file this repository ships: never input."""
    return cast("LiteralString", text)


def _turn(
    scratch: psycopg.Connection[Any], chat_id: str, generator: str | None, role: str = "assistant"
) -> None:
    """An exchange: its question, then its answer holding `generator`; or, for
    `role="user"`, a question holding it (which the column refuses)."""
    insert = (
        "INSERT INTO chat (tenant_id, chat_id, turn_index, user_id, role, content, outcome, "
        "generator, as_of, trace_id) VALUES ('acme', %s, %s, 'uid-1', %s, %s, %s, %s, "
        "'2026-11-11', 't')"
    )
    if role == "user":
        scratch.execute(insert, (chat_id, 0, "user", "Q?", None, generator))
        return
    scratch.execute(insert, (chat_id, 0, "user", "Q?", None, None))
    scratch.execute(insert, (chat_id, 1, "assistant", None, "internal", generator))


@needs_ledger_db
def test_0022_admits_gemini_keeps_claude_and_demo_and_changes_no_row() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0022"))
    before = [f for f in files if f.name < migration.name]
    assert before[-1].name.startswith("0021")
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
        _turn(scratch, "old-claude", "claude")
        # until 0022 the column doesn't know gemini
        with pytest.raises(psycopg.errors.CheckViolation, match="chat_generator"):
            _turn(scratch, "early-gemini", "gemini")
        snapshot = "SELECT row_to_json(c)::jsonb FROM chat c ORDER BY chat_id, turn_index"
        rows_before = scratch.execute(_sql(snapshot)).fetchall()
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))
            assert scratch.execute(_sql(snapshot)).fetchall() == rows_before
        for chat_id, generator in (
            ("new-gemini", "gemini"),
            ("new-claude", "claude"),
            ("d", "demo"),
        ):
            _turn(scratch, chat_id, generator)
        _turn(scratch, "none", None)
        # still only the three names, and only on an answer turn
        for chat_id, generator, role in (
            ("bad-name", "gpt", "assistant"),
            ("bad-bedrock", "bedrock", "assistant"),
            ("bad-role", "gemini", "user"),
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="chat_generator"):
                _turn(scratch, chat_id, generator, role)
