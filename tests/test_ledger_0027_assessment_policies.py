"""Ledger 0027 (#300, ADR-0053 F7, ADR-0034): `assessment.policies`, the policy versions a
findings run sent, recorded with the assessment. Needs a Postgres server (`DATABASE_URL`);
CI sets it and `REQUIRE_LEDGER_DB`.

An assessment recorded before the migration has none (NULL, which a report says it predates);
a recorded one is a JSON array; nothing else is accepted.
"""

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


def _assessment(
    scratch: psycopg.Connection[Any], assessment_id: str, policies: Any = None
) -> None:  # Any: JSON
    scratch.execute(
        "INSERT INTO assessment (tenant_id, assessment_id, user_id, corpus_version, corpus_sha, "
        "mapping_version, mapping_sha, as_of, trace_id, policies) "
        "VALUES ('acme', %s, 'a@x', NULL, %s, NULL, %s, '2026-11-22', %s, %s)",
        (
            assessment_id,
            "a" * 40,
            "b" * 40,
            "a" * 32,
            None if policies is None else Jsonb(policies),
        ),
    )


@needs_ledger_db
def test_0027_adds_policies_idempotently_and_keeps_an_older_assessment_as_it_was() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0027"))
    before = [f for f in files if f.name < migration.name]
    assert before[-1].name.startswith("0026")
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
        _assessment_before = (
            "INSERT INTO assessment (tenant_id, assessment_id, user_id, corpus_version, "
            "corpus_sha, mapping_version, mapping_sha, as_of, trace_id) VALUES ('acme', 'old', "
            "'a@x', NULL, %s, NULL, %s, '2026-11-15', %s)"
        )
        scratch.execute(_assessment_before, ("a" * 40, "b" * 40, "a" * 32))
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))
        # an assessment recorded before the migration has no policy versions, and says so
        assert scratch.execute(
            "SELECT policies FROM assessment WHERE assessment_id = 'old'"
        ).fetchone() == (None,)
        # a recorded one is an array, as written
        recorded = [
            {"instrument_id": "acme/privacy-policy", "version_id": "v1", "title": "Privacy Policy"}
        ]
        _assessment(scratch, "new", recorded)
        assert scratch.execute(
            "SELECT policies FROM assessment WHERE assessment_id = 'new'"
        ).fetchone() == (recorded,)
        _assessment(scratch, "none-sent", [])  # a run that sent no policy: an empty array
        # nothing but an array
        with pytest.raises(psycopg.errors.CheckViolation, match="assessment_policies"):
            _assessment(scratch, "bad", {"instrument_id": "acme/privacy-policy"})
