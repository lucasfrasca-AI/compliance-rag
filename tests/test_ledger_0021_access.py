"""Ledger 0021 (#322): `access` joins `quota` as a reason a
`generation_unavailable` turn can hold, and nothing else changes. Needs a
Postgres server (`DATABASE_URL`); CI sets it and `REQUIRE_LEDGER_DB`."""

from __future__ import annotations

import pathlib
from typing import Any, LiteralString, cast

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.ledger_db import needs_ledger_db, scratch_database

MIGRATIONS = pathlib.Path(__file__).parents[1] / "db" / "ledger"


def _sql(text: str) -> LiteralString:
    """A migration file this repository ships: never input."""
    return cast("LiteralString", text)


def _turn(scratch: psycopg.Connection[Any], chat_id: str, outcome: str, detail: object) -> None:
    scratch.execute(
        "INSERT INTO chat (tenant_id, chat_id, turn_index, user_id, role, content, as_of, "
        "trace_id) VALUES ('acme', %s, 0, 'uid-1', 'user', 'Q?', '2026-11-10', 't')",
        (chat_id,),
    )
    scratch.execute(
        "INSERT INTO chat (tenant_id, chat_id, turn_index, user_id, role, outcome, "
        "outcome_detail, as_of, trace_id) VALUES ('acme', %s, 1, 'uid-1', 'assistant', %s, %s, "
        "'2026-11-10', 't')",
        (chat_id, outcome, Jsonb(detail)),
    )


@needs_ledger_db
def test_0021_admits_access_as_a_reason_and_nothing_else_and_changes_no_row() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0021"))
    before = [f for f in files if f.name < migration.name]
    assert before[-1].name.startswith("0020")
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
        _turn(scratch, "old-quota", "generation_unavailable", {"reason": "quota"})
        # until 0021 the one reason is quota
        with pytest.raises(psycopg.errors.CheckViolation):
            _turn(scratch, "early-access", "generation_unavailable", {"reason": "access"})
        snapshot = "SELECT row_to_json(c)::jsonb FROM chat c ORDER BY chat_id, turn_index"
        rows_before = scratch.execute(_sql(snapshot)).fetchall()
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))
            assert scratch.execute(_sql(snapshot)).fetchall() == rows_before
        _turn(scratch, "new-access", "generation_unavailable", {"reason": "access"})
        _turn(scratch, "new-quota", "generation_unavailable", {"reason": "quota"})
        # still only our words: another reason, extra keys, free text and a
        # missing reason are refused, as is a detail on any other outcome
        for chat_id, outcome, detail in (
            ("bad-reason", "generation_unavailable", {"reason": "denied"}),
            ("bad-extra", "generation_unavailable", {"reason": "access", "message": "x"}),
            ("bad-empty", "generation_unavailable", {}),
            ("bad-other", "internal", {"reason": "access"}),
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                _turn(scratch, chat_id, outcome, detail)
