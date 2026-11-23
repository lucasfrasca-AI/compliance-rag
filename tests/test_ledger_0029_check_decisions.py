"""Ledger 0029 (#301, ADR-0059 Q3): `check_decision`, the append-only table of what a member
decided about a check's claims and findings. Needs a Postgres server (`DATABASE_URL`); CI
sets it and `REQUIRE_LEDGER_DB`.

The database refuses again what `check_decision_write` refuses first: a kind that is not
one of the four, a claim decision that names no claim or names a finding (and the reverse
for a verdict decision), a blank who or trace id, a claim the check does not hold, a
finding that does not exist. Applying the file twice changes nothing.
"""

from __future__ import annotations

import pathlib
from typing import Any, LiteralString, cast

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.ledger_db import needs_ledger_db, scratch_database

MIGRATIONS = pathlib.Path(__file__).parents[1] / "db" / "ledger"
CHECK = "c" * 32


def _sql(text: str) -> LiteralString:
    """A migration file this repository ships: never input."""
    return cast("LiteralString", text)


def _seed(scratch: psycopg.Connection[Any]) -> None:
    """A tenant, a checked document with two claims, and an assessment with one finding."""
    scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
    scratch.execute(
        "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction, scope, "
        "document_class) VALUES ('acme', 'acme/check-1', 'T', 'AU', 'tenant', "
        "'submitted_document')"
    )
    scratch.execute(
        "INSERT INTO instrument_version (tenant_id, instrument_id, version_id, title, valid_from) "
        "VALUES ('acme', 'acme/check-1', 'v1', 'T', '2026-11-15')"
    )
    scratch.execute(
        "INSERT INTO submitted_check (tenant_id, check_id, user_id, instrument_id, version_id, "
        "filename, document_format, trace_id) VALUES ('acme', %s, 'uid-1', 'acme/check-1', 'v1', "
        "'p.md', 'markdown', %s)",
        (CHECK, "a" * 32),
    )
    scratch.execute(
        "INSERT INTO check_extraction (tenant_id, check_id, as_of, trace_id, status, "
        "completed_at) VALUES ('acme', %s, '2026-11-15', %s, 'done', now())",
        (CHECK, "a" * 32),
    )
    for index in (1, 2):
        scratch.execute(
            "INSERT INTO check_claim (tenant_id, check_id, claim_index, passage, quote_start, "
            "quote_end, quote_sha256, summary, obligation_ids, policy_clause_ids, provenance, "
            "trace_id) VALUES ('acme', %s, %s, 'p3', 10, 40, %s, 'A claim.', '{}', '{}', "
            "'inferred', %s)",
            (CHECK, index, "ab" * 32, "a" * 32),
        )
    scratch.execute(
        "INSERT INTO assessment (tenant_id, assessment_id, user_id, corpus_sha, mapping_sha, "
        "as_of, trace_id, check_id, facts, facts_sha256) VALUES ('acme', 'as1', 'a@x', %s, %s, "
        "'2026-11-15', %s, %s, %s, %s)",
        ("a" * 40, "b" * 40, "a" * 32, CHECK, Jsonb([]), "c" * 64),
    )
    scratch.execute(
        "INSERT INTO finding (tenant_id, finding_id, assessment_id, check_id, claim_index, "
        "obligation_id, verdict, provenance_tier, assessable, citations, outcome, trace_id) "
        "VALUES ('acme', 'f1', 'as1', %s, 1, 'OB-PA-APP8-1', 'not_addressed', 'inferred', true, "
        "%s, 'verdict', %s)",
        (
            CHECK,
            Jsonb(
                [
                    {
                        "clause_id": "privacy-act-1988:C2026C00227:sch1/app8/1",
                        "version_id": "C2026C00227",
                        "quoted_text": "reasonable steps",
                        "canonicalisation_version": 1,
                    }
                ]
            ),
            "a" * 32,
        ),
    )


def _decision(scratch: psycopg.Connection[Any], **changes: Any) -> None:  # Any: column values
    values: dict[str, Any] = {  # Any: column values
        "check_id": CHECK,
        "kind": "claim_confirmed",
        "claim_index": 1,
        "finding_id": None,
        "user_id": "uid-1",
        "email": "lead@acme.example",
        "trace_id": "d" * 32,
        **changes,
    }
    scratch.execute(
        "INSERT INTO check_decision (tenant_id, check_id, kind, claim_index, finding_id, "
        "user_id, email, trace_id) VALUES ('acme', %(check_id)s, %(kind)s, %(claim_index)s, "
        "%(finding_id)s, %(user_id)s, %(email)s, %(trace_id)s)",
        values,
    )


@needs_ledger_db
def test_0029_is_idempotent_and_refuses_what_a_decision_must_not_be() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0029"))
    before = [f for f in files if f.name < migration.name]
    assert before[-1].name.startswith("0028")
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        _seed(scratch)
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))

        # a claim decision and a verdict decision, in the order they were made
        _decision(scratch)
        _decision(scratch, kind="claim_rejected", claim_index=2)
        _decision(scratch, kind="verdict_accepted", claim_index=None, finding_id="f1")
        rows = scratch.execute(
            "SELECT decision_id, kind, claim_index, finding_id, decided_at IS NOT NULL "
            "FROM check_decision ORDER BY decision_id"
        ).fetchall()
        assert [r[1:] for r in rows] == [
            ("claim_confirmed", 1, None, True),
            ("claim_rejected", 2, None, True),
            ("verdict_accepted", None, "f1", True),
        ]
        assert [r[0] for r in rows] == sorted(r[0] for r in rows)
        # the order is the database's: a caller cannot name it
        with pytest.raises(psycopg.errors.GeneratedAlways):
            scratch.execute(
                "INSERT INTO check_decision (decision_id, tenant_id, check_id, kind, claim_index, "
                "user_id, email, trace_id) VALUES (99, 'acme', %s, 'claim_confirmed', 1, 'u', "
                "'e', 't')",
                (CHECK,),
            )

        # the kind is one of four
        with pytest.raises(psycopg.errors.CheckViolation, match="check_decision_kind"):
            _decision(scratch, kind="claim_edited")
        # a claim decision names its claim and no finding; a verdict decision the reverse
        for changes in (
            {"claim_index": None},
            {"finding_id": "f1"},
            {"kind": "verdict_accepted", "claim_index": 1, "finding_id": "f1"},
            {"kind": "verdict_accepted", "claim_index": None, "finding_id": None},
            {"kind": "verdict_withdrawn", "claim_index": 1},
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="check_decision_subject"):
                _decision(scratch, **changes)
        # who and which request, never blank
        for column, constraint in (
            ("user_id", "check_decision_user_id"),
            ("email", "check_decision_email"),
            ("trace_id", "check_decision_trace_id"),
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
                _decision(scratch, **{column: " "})
        # it names a claim the check holds, and a finding that exists, and a check that was
        # extracted
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _decision(scratch, claim_index=3)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _decision(scratch, kind="verdict_accepted", claim_index=None, finding_id="ghost")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _decision(scratch, check_id="d" * 32)

        # the claim and the finding are what they were: a decision beside them changes neither
        assert (
            scratch.execute(
                "SELECT provenance, summary, quote_sha256 FROM check_claim ORDER BY claim_index"
            ).fetchall()
            == [("inferred", "A claim.", "ab" * 32)] * 2
        )
        assert scratch.execute("SELECT provenance_tier, outcome FROM finding").fetchall() == [
            ("inferred", "verdict")
        ]
        assert scratch.execute("SELECT count(*) FROM evidence").fetchone() == (0,)
