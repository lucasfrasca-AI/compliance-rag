"""Ledger 0023 (#296, ADR-0053 U1): `submitted_document` joins the three
document classes an instrument can have, and nothing else changes. Needs a
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


def _instrument(
    scratch: psycopg.Connection[Any], tenant: str, instrument_id: str, document_class: str | None
) -> None:
    scratch.execute(
        "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction, scope, "
        "document_class) VALUES (%s, %s, 'T', 'AU', 'tenant', %s)",
        (tenant, instrument_id, document_class),
    )


@needs_ledger_db
def test_0023_admits_a_submitted_document_keeps_the_rest_and_changes_no_row() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0023"))
    before = [f for f in files if f.name < migration.name]
    assert before[-1].name.startswith("0022")
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
        _instrument(scratch, "acme", "acme/policy", "internal_policy")
        _instrument(scratch, "acme", "acme/supplied", "external_upload")
        # until 0023 the column doesn't know the class
        with pytest.raises(psycopg.errors.CheckViolation, match="document_class_check"):
            _instrument(scratch, "acme", "acme/early-proposal", "submitted_document")
        snapshot = "SELECT row_to_json(i)::jsonb FROM instrument i ORDER BY instrument_id"
        rows_before = scratch.execute(_sql(snapshot)).fetchall()
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))
            assert scratch.execute(_sql(snapshot)).fetchall() == rows_before
        _instrument(scratch, "acme", "acme/proposal", "submitted_document")
        _instrument(scratch, "acme", "acme/unclassified", None)  # before 0011's rows: still NULL
        # still only the four, and the library stays `_public`'s alone
        with pytest.raises(psycopg.errors.CheckViolation, match="document_class_check"):
            _instrument(scratch, "acme", "acme/bad", "user_note")
        with pytest.raises(psycopg.errors.CheckViolation, match="library_law_is_public"):
            _instrument(scratch, "acme", "acme/law", "library_law")
        with pytest.raises(psycopg.errors.CheckViolation, match="library_law_is_public"):
            _instrument(scratch, "_public", "a-proposal", "submitted_document")
