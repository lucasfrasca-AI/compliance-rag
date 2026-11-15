"""Ledger 0024 (#297, ADR-0059): `submitted_check`, one member's submission of a
document to be checked. A table of its own, because `assessment` needs a corpus
and mapping commit that a check does not have until a model has run. Needs a
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


def _check(scratch: psycopg.Connection[Any], **changes: str) -> None:
    values = {
        "tenant_id": "acme",
        "check_id": "c" * 32,
        "user_id": "uid-1",
        "instrument_id": "acme/check-1",
        "version_id": "v1",
        "filename": "proposal.md",
        "document_format": "markdown",
        "trace_id": "a" * 32,
        **changes,
    }
    scratch.execute(
        "INSERT INTO submitted_check (tenant_id, check_id, user_id, instrument_id, version_id, "
        "filename, document_format, trace_id) VALUES (%(tenant_id)s, %(check_id)s, %(user_id)s, "
        "%(instrument_id)s, %(version_id)s, %(filename)s, %(document_format)s, %(trace_id)s)",
        values,
    )


def _document(scratch: psycopg.Connection[Any], instrument_id: str, version_id: str = "v1") -> None:
    scratch.execute(
        "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction, scope, "
        "document_class) VALUES ('acme', %s, 'T', 'AU', 'tenant', 'submitted_document') "
        "ON CONFLICT DO NOTHING",
        (instrument_id,),
    )
    scratch.execute(
        "INSERT INTO instrument_version (tenant_id, instrument_id, version_id, title, valid_from) "
        "VALUES ('acme', %s, %s, 'T', '2026-11-15')",
        (instrument_id, version_id),
    )


@needs_ledger_db
def test_0024_adds_the_table_idempotently_and_it_refuses_what_a_check_must_not_be() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0024"))
    before = [f for f in files if f.name < migration.name]
    assert before[-1].name.startswith("0023")
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))
        _document(scratch, "acme/check-1")
        _check(scratch)

        # one check per id, and per version
        with pytest.raises(psycopg.errors.UniqueViolation):
            _check(scratch, instrument_id="acme/check-1")
        with pytest.raises(psycopg.errors.UniqueViolation):
            _check(scratch, check_id="d" * 32)
        # it points at a version the ledger holds, of a tenant it holds
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _check(scratch, check_id="e" * 32, instrument_id="acme/never-stored")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _check(scratch, check_id="f" * 32, tenant_id="ghost")
        # and names who, what and in which request
        _document(scratch, "acme/check-2")
        for column, blank in (
            ("user_id", " "),
            ("trace_id", ""),
            ("filename", "  "),
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match=f"submitted_check_{column}"):
                _check(scratch, check_id="1" * 32, instrument_id="acme/check-2", **{column: blank})
        with pytest.raises(psycopg.errors.CheckViolation, match="submitted_check_format"):
            _check(
                scratch,
                check_id="1" * 32,
                instrument_id="acme/check-2",
                document_format="pdf",
            )
        _check(scratch, check_id="2" * 32, instrument_id="acme/check-2", document_format="text")
        (count,) = scratch.execute("SELECT count(*) FROM submitted_check").fetchone() or (0,)
        assert count == 2
