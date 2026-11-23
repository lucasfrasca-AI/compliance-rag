"""Ledger 0031 (#305, ADR-0059 Q8 and C2): `policy_mapping`, a tenant's policy-to-law mappings,
and `policy_mapping_run`, the reservation of a policy version's proposing run. Needs a
Postgres server (`DATABASE_URL`); CI sets it and `REQUIRE_LEDGER_DB`.

The database refuses again what `check_mapping_run_write` refuses first, and what the
ticket names: a kind that is not one of two, a status that is not one of three, a
proposal that names an approver (or an approval that names nobody), a blank proposer or
trace id, a rationale that is not one short line, a clause that is not the version's own,
and the same mapping twice. Applying the file twice changes nothing.
"""

from __future__ import annotations

import pathlib
from typing import Any, LiteralString, cast

import psycopg
import pytest

from tests.ledger_db import needs_ledger_db, scratch_database

MIGRATIONS = pathlib.Path(__file__).parents[1] / "db" / "ledger"
POLICY = "acme/data-policy"
CLAUSE = f"{POLICY}:v1:3/1"


def _sql(text: str) -> LiteralString:
    """A migration file this repository ships: never input."""
    return cast("LiteralString", text)


def _seed(scratch: psycopg.Connection[Any]) -> None:
    scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
    scratch.execute(
        "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction, scope, "
        "document_class) VALUES ('acme', %s, 'Data policy', 'AU', 'tenant', 'internal_policy')",
        (POLICY,),
    )
    for version in ("v1", "v2"):
        scratch.execute(
            "INSERT INTO instrument_version (tenant_id, instrument_id, version_id, title, "
            "valid_from) VALUES ('acme', %s, %s, 'Data policy', '2026-11-15')",
            (POLICY, version),
        )


def _mapping(scratch: psycopg.Connection[Any], **changes: Any) -> None:  # Any: column values
    values: dict[str, Any] = {  # Any: column values
        "version": "v1",
        "clause": CLAUSE,
        "obligation": "OB-PA-APP11-1",
        "kind": "implements",
        "rationale": "Section 3.1 requires encryption at rest.",
        "proposer": "gemini-3.5-flash",
        "trace_id": "a" * 32,
        **changes,
    }
    columns = ["status", "approver"]
    given = [changes[c] for c in columns if c in changes]
    extra_columns = "".join(f", {c}" for c in columns if c in changes)
    extra_values = ", %s" * len(given)
    scratch.execute(
        _sql(
            "INSERT INTO policy_mapping (tenant_id, policy_instrument_id, policy_version_id, "
            "policy_clause_id, obligation_id, kind, rationale, proposer, trace_id"
            + extra_columns
            + ") VALUES ('acme', %s, %s, %s, %s, %s, %s, %s, %s"
            + extra_values
            + ")"
        ),
        (
            POLICY,
            values["version"],
            values["clause"],
            values["obligation"],
            values["kind"],
            values["rationale"],
            values["proposer"],
            values["trace_id"],
            *given,
        ),
    )


@needs_ledger_db
def test_0031_is_idempotent_and_refuses_what_a_mapping_must_not_be() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0031"))
    before = [f for f in files if f.name < migration.name]
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        _seed(scratch)
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))

        # a proposal starts `proposed`, with no approver: the column's default
        _mapping(scratch)
        _mapping(scratch, kind="partially_implements")
        _mapping(scratch, obligation="OB-PA-APP11-2")
        row = scratch.execute(
            "SELECT kind, status, approver, proposer, trace_id, proposed_at IS NOT NULL "
            "FROM policy_mapping ORDER BY mapping_id LIMIT 1"
        ).fetchone()
        assert row == ("implements", "proposed", None, "gemini-3.5-flash", "a" * 32, True)

        # the same mapping once: (clause, obligation, kind) is unique, whatever its words
        with pytest.raises(psycopg.errors.UniqueViolation, match="policy_mapping_once"):
            _mapping(scratch, rationale="Different words, same mapping.")

        # kind is one of two, status one of three
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_kind"):
            _mapping(scratch, kind="complies", obligation="OB-X")
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_status"):
            _mapping(scratch, status="pending", approver="lead@acme.example", obligation="OB-X")

        # a proposal has nobody who decided it; an approval or a rejection has somebody
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_approver"):
            _mapping(scratch, approver="lead@acme.example", obligation="OB-Y")
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_approver"):
            _mapping(scratch, status="approved", obligation="OB-Y")
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_approver"):
            _mapping(scratch, status="rejected", approver=" ", obligation="OB-Y")
        _mapping(scratch, status="approved", approver="lead@acme.example", obligation="OB-Y")

        # who proposed and which request, never blank
        for column, constraint in (
            ("proposer", "policy_mapping_proposer"),
            ("trace_id", "policy_mapping_trace_id"),
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
                _mapping(scratch, obligation="OB-Z", **{column: " "})

        # the rationale is one non-blank line of at most 300 characters
        for rationale in ("", "  ", "two\nlines", "carriage\rreturn", "x" * 301):
            with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_rationale"):
                _mapping(scratch, obligation="OB-Z", rationale=rationale)
        _mapping(scratch, obligation="OB-Z", rationale="x" * 300)

        # the obligation id is an id, and the clause one of the version's own
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_obligation_id"):
            _mapping(scratch, obligation="OB bad")
        for clause in (f"{POLICY}:v2:3/1", "acme/other:v1:3/1", f"{POLICY}:v1:"):
            with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_clause_of"):
                _mapping(scratch, clause=clause, obligation="OB-W")
        _mapping(scratch, version="v2", clause=f"{POLICY}:v2:3/1", obligation="OB-W")

        # it names a policy version the ledger holds
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _mapping(scratch, version="v9", clause=f"{POLICY}:v9:3/1", obligation="OB-W")

        # the order is the database's: a caller cannot name it
        with pytest.raises(psycopg.errors.GeneratedAlways):
            scratch.execute(
                "INSERT INTO policy_mapping (mapping_id, tenant_id, policy_instrument_id, "
                "policy_version_id, policy_clause_id, obligation_id, kind, rationale, proposer, "
                "trace_id) VALUES (99, 'acme', %s, 'v1', %s, 'OB-Q', 'implements', 'r', 'm', 't')",
                (POLICY, CLAUSE),
            )

        # the reservation: a status of three, completed with `done`, one per version
        scratch.execute(
            "INSERT INTO policy_mapping_run (tenant_id, policy_instrument_id, policy_version_id, "
            "trace_id) VALUES ('acme', %s, 'v1', %s)",
            (POLICY, "b" * 32),
        )
        assert scratch.execute(
            "SELECT status, completed_at FROM policy_mapping_run"
        ).fetchall() == [("pending", None)]
        with pytest.raises(psycopg.errors.UniqueViolation):
            scratch.execute(
                "INSERT INTO policy_mapping_run (tenant_id, policy_instrument_id, "
                "policy_version_id, trace_id) VALUES ('acme', %s, 'v1', %s)",
                (POLICY, "c" * 32),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_run_status"):
            scratch.execute("UPDATE policy_mapping_run SET status = 'running'")
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_run_completed"):
            scratch.execute("UPDATE policy_mapping_run SET status = 'done'")
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_run_trace_id"):
            scratch.execute("UPDATE policy_mapping_run SET trace_id = ' '")

        # nothing here is evidence, a finding or a graph node: the ledger's other tables are
        # as they were
        assert scratch.execute("SELECT count(*) FROM evidence").fetchone() == (0,)
        assert scratch.execute("SELECT count(*) FROM finding").fetchone() == (0,)
