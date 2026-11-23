"""Ledger 0029 (#303, ADR-0053 U4, ADR-0021): `instrument_version.backdate_reason`, and the
removal audit's `publish` action and `trace_id`. Needs a Postgres server (`DATABASE_URL`); CI sets
it and `REQUIRE_LEDGER_DB`.

A version recorded before the migration has no reason, and an audit row before it no trace (NULL);
a reason is never blank; the audit admits a publish and still refuses any other action.
"""

from __future__ import annotations

import pathlib
from typing import LiteralString, cast

import psycopg
import pytest

from tests.ledger_db import needs_ledger_db, scratch_database

MIGRATIONS = pathlib.Path(__file__).parents[1] / "db" / "ledger"


def _sql(text: str) -> LiteralString:
    """A migration file this repository ships: never input."""
    return cast("LiteralString", text)


_VERSION = (
    "INSERT INTO instrument_version (tenant_id, instrument_id, version_id, title, valid_from, "
    "backdate_reason) VALUES ('acme', 'acme/privacy-policy', %s, 'Privacy Policy', "
    "'2026-01-01', %s)"
)
_AUDIT = (
    "INSERT INTO corpus_removal_audit (audit_id, tenant_id, instrument_id, action, outcome, "
    "actor, reason, trace_id) VALUES (%s, 'acme', 'acme/privacy-policy', %s, 'completed', "
    "'a@acme.example', 'why', %s)"
)


@needs_ledger_db
def test_0029_adds_its_columns_idempotently_and_keeps_older_rows_as_they_were() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0029"))
    before = [f for f in files if f.name < migration.name]
    assert before[-1].name.startswith("0028")
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
        scratch.execute(
            "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction, scope, "
            "document_class) VALUES ('acme', 'acme/privacy-policy', 'Privacy Policy', 'AU', "
            "'tenant', 'internal_policy')"
        )
        scratch.execute(
            "INSERT INTO instrument_version (tenant_id, instrument_id, version_id, title, "
            "valid_from) VALUES ('acme', 'acme/privacy-policy', 'v1', 'Privacy Policy', "
            "'2026-01-01')"
        )
        scratch.execute(
            "INSERT INTO corpus_removal_audit (audit_id, tenant_id, instrument_id, action, "
            "outcome, actor, reason) VALUES ('old', 'acme', 'acme/privacy-policy', 'withdraw', "
            "'completed', 'a@acme.example', 'why')"
        )
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))
        # a row written before has no reason and no trace, and says so
        assert scratch.execute(
            "SELECT backdate_reason FROM instrument_version WHERE version_id = 'v1'"
        ).fetchone() == (None,)
        assert scratch.execute(
            "SELECT trace_id FROM corpus_removal_audit WHERE audit_id = 'old'"
        ).fetchone() == (None,)
        # a recorded reason is kept as it was written
        scratch.execute(_VERSION, ("v2", "the board approved it in January"))
        assert scratch.execute(
            "SELECT backdate_reason FROM instrument_version WHERE version_id = 'v2'"
        ).fetchone() == ("the board approved it in January",)
        # never blank
        for blank in ("", "   "):
            with pytest.raises(psycopg.errors.CheckViolation, match="backdate_reason"):
                scratch.execute(_VERSION, ("v3", blank))
        # the audit admits a publish with its trace, and still no other action
        scratch.execute(_AUDIT, ("new", "publish", "a" * 32))
        assert scratch.execute(
            "SELECT action, trace_id FROM corpus_removal_audit WHERE audit_id = 'new'"
        ).fetchone() == ("publish", "a" * 32)
        for action in ("withdraw", "purge", "replace_in_place"):
            scratch.execute(_AUDIT, (f"kept-{action}", action, None))
        with pytest.raises(psycopg.errors.CheckViolation, match="corpus_removal_audit_action"):
            scratch.execute(_AUDIT, ("bad", "delete", None))
