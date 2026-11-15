"""Ledger 0025 (#298, ADR-0053 F2, ADR-0059 Q2 and Q4): `check_extraction` (one
row per extracted check) and `check_claim` (one per accepted claim, 25 at most,
always `inferred`). Needs a Postgres server (`DATABASE_URL`); CI sets it and
`REQUIRE_LEDGER_DB`."""

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


def _extraction(scratch: psycopg.Connection[Any], check_id: str = "c" * 32) -> None:
    scratch.execute(
        "INSERT INTO check_extraction (tenant_id, check_id, as_of, trace_id) "
        "VALUES ('acme', %s, '2026-11-15', %s)",
        (check_id, "a" * 32),
    )


def _claim(scratch: psycopg.Connection[Any], **changes: Any) -> None:  # Any: column values
    values: dict[str, Any] = {  # Any: column values
        "check_id": "c" * 32,
        "claim_index": 1,
        "passage": "p3",
        "quote_start": 10,
        "quote_end": 40,
        "quote_sha256": "ab" * 32,
        "summary": "Profiles are kept for 7 years.",
        "obligation_ids": ["OB-PA-APP11-1"],
        "policy_clause_ids": [],
        "provenance": "inferred",
        "trace_id": "a" * 32,
        **changes,
    }
    scratch.execute(
        "INSERT INTO check_claim (tenant_id, check_id, claim_index, passage, quote_start, "
        "quote_end, quote_sha256, summary, obligation_ids, policy_clause_ids, provenance, "
        "trace_id) VALUES ('acme', %(check_id)s, %(claim_index)s, %(passage)s, %(quote_start)s, "
        "%(quote_end)s, %(quote_sha256)s, %(summary)s, %(obligation_ids)s, "
        "%(policy_clause_ids)s, %(provenance)s, %(trace_id)s)",
        values,
    )


def _check(scratch: psycopg.Connection[Any], check_id: str, instrument_id: str) -> None:
    scratch.execute(
        "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction, scope, "
        "document_class) VALUES ('acme', %s, 'T', 'AU', 'tenant', 'submitted_document')",
        (instrument_id,),
    )
    scratch.execute(
        "INSERT INTO instrument_version (tenant_id, instrument_id, version_id, title, valid_from) "
        "VALUES ('acme', %s, 'v1', 'T', '2026-11-15')",
        (instrument_id,),
    )
    scratch.execute(
        "INSERT INTO submitted_check (tenant_id, check_id, user_id, instrument_id, version_id, "
        "filename, document_format, trace_id) VALUES ('acme', %s, 'uid-1', %s, 'v1', 'p.md', "
        "'markdown', %s)",
        (check_id, instrument_id, "a" * 32),
    )


@needs_ledger_db
def test_0025_adds_the_tables_idempotently_and_they_refuse_what_a_claim_must_not_be() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0025"))
    before = [f for f in files if f.name < migration.name]
    assert before[-1].name.startswith("0024")
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))
        _check(scratch, "c" * 32, "acme/check-1")

        # a claim belongs to an extraction, and an extraction to a check the ledger holds
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _extraction(scratch, "d" * 32)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _claim(scratch)  # no extraction yet
        _extraction(scratch)
        _claim(scratch)
        # one extraction per check, and one claim per index
        with pytest.raises(psycopg.errors.UniqueViolation):
            _extraction(scratch)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _claim(scratch)
        _claim(scratch, claim_index=2, passage="p4", quote_start=50, quote_end=60)

        # 25 at most, numbered from 1
        for index in (0, 26):
            with pytest.raises(psycopg.errors.CheckViolation, match="check_claim_index"):
                _claim(scratch, claim_index=index)
        # inferred and nothing else: a claim is never promoted
        for provenance in ("asserted", "parsed", "external", ""):
            with pytest.raises(psycopg.errors.CheckViolation, match="check_claim_provenance"):
                _claim(scratch, claim_index=3, provenance=provenance)
        # it names a passage, a span in the document, the hash of its text, and one line
        for column, value, constraint in (
            ("passage", "3", "check_claim_passage"),
            ("passage", "p0", "check_claim_passage"),
            ("quote_end", 10, "check_claim_span"),
            ("quote_start", -1, "check_claim_span"),
            ("quote_sha256", "ab" * 31, "check_claim_sha256"),
            ("quote_sha256", "AB" * 32, "check_claim_sha256"),
            ("summary", "  ", "check_claim_summary"),
            ("summary", "two\nlines", "check_claim_summary"),
            ("trace_id", " ", "check_claim_trace_id"),
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
                _claim(scratch, claim_index=3, **{column: value})
        # an extraction is pending, done or failed, and done exactly when it has a time
        for status, completed in (("later", "NULL"), ("done", "NULL"), ("pending", "now()")):
            with pytest.raises(psycopg.errors.CheckViolation):
                scratch.execute(
                    _sql(
                        "INSERT INTO check_extraction (tenant_id, check_id, as_of, trace_id, "
                        f"status, completed_at) VALUES ('acme', '{'1' * 32}', '2026-11-15', "
                        f"'{'a' * 32}', '{status}', {completed})"
                    )
                )
        # the extraction names its request
        _check(scratch, "e" * 32, "acme/check-2")
        with pytest.raises(psycopg.errors.CheckViolation, match="check_extraction_trace_id"):
            scratch.execute(
                "INSERT INTO check_extraction (tenant_id, check_id, as_of, trace_id) "
                "VALUES ('acme', %s, '2026-11-15', ' ')",
                ("e" * 32,),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="check_extraction_dropped"):
            scratch.execute(
                "INSERT INTO check_extraction (tenant_id, check_id, as_of, trace_id, "
                "claims_dropped) VALUES ('acme', %s, '2026-11-15', %s, -1)",
                ("e" * 32, "a" * 32),
            )
        (count,) = scratch.execute("SELECT count(*) FROM check_claim").fetchone() or (0,)
        assert count == 2
