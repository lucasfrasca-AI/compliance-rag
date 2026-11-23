"""Ledger 0032 (#306, ADR-0053 F6): a policy mapping is decided once, by one UPDATE of exactly the
decision's columns, and a derived finding points at its mapping. Needs a Postgres server
(`DATABASE_URL`); CI sets it and `REQUIRE_LEDGER_DB`.

The trigger refuses, whatever the caller: a delete; any update of a mapping that is not
`proposed`; an update that leaves it `proposed`; and an update that changes a column the model's
run wrote. The table's CHECKs refuse a decision that names nobody or no request, and a reason on
anything but a rejection. Applying the file twice changes nothing.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from tests.ledger_db import needs_ledger_db, scratch_database
from tests.test_ledger_0031_policy_mapping import (
    CLAUSE,
    MIGRATIONS,
    POLICY,
    _mapping,  # pyright: ignore[reportPrivateUsage]
    _seed,  # pyright: ignore[reportPrivateUsage]
    _sql,  # pyright: ignore[reportPrivateUsage]
)

DECIDE = (
    "UPDATE policy_mapping SET status = %s, approver = 'uid-lead', "
    "approver_email = 'lead@acme.example', decided_at = now(), decision_trace_id = %s"
)


def _migrated_through_0032(scratch: psycopg.Connection[Any]) -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0032"))
    for earlier in (f for f in files if f.name < migration.name):
        scratch.execute(_sql(earlier.read_text()))
    _seed(scratch)
    for _ in range(2):  # and again: idempotent
        scratch.execute(_sql(migration.read_text()))


@needs_ledger_db
def test_a_mapping_is_decided_once_by_updating_the_decisions_columns_alone() -> None:
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        _migrated_through_0032(scratch)
        for obligation in ("OB-A", "OB-B", "OB-C", "OB-D"):
            _mapping(scratch, obligation=obligation)
        ids = [r[0] for r in scratch.execute("SELECT mapping_id FROM policy_mapping ORDER BY 1")]
        before = scratch.execute(
            "SELECT policy_clause_id, obligation_id, kind, rationale, proposer, trace_id, "
            "proposed_at, mapping_id FROM policy_mapping WHERE mapping_id = %s",
            (ids[0],),
        ).fetchone()

        # a decision: the status and the decision's columns, together
        scratch.execute(_sql(DECIDE + " WHERE mapping_id = %s"), ("approved", "e" * 32, ids[0]))
        row = scratch.execute(
            "SELECT status, approver, approver_email, decided_at IS NOT NULL, decision_trace_id, "
            "trace_id FROM policy_mapping WHERE mapping_id = %s",
            (ids[0],),
        ).fetchone()
        assert row == ("approved", "uid-lead", "lead@acme.example", True, "e" * 32, "a" * 32)
        after = scratch.execute(
            "SELECT policy_clause_id, obligation_id, kind, rationale, proposer, trace_id, "
            "proposed_at, mapping_id FROM policy_mapping WHERE mapping_id = %s",
            (ids[0],),
        ).fetchone()
        assert after == before  # nothing the model's run wrote moved

        # a rejection carries its optional reason, which only a rejection may
        scratch.execute(
            _sql(DECIDE + ", rejection_reason = 'No.' WHERE mapping_id = %s"),
            ("rejected", "f" * 32, ids[1]),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_reason"):
            scratch.execute(
                _sql(DECIDE + ", rejection_reason = 'Fine.' WHERE mapping_id = %s"),
                ("approved", "f" * 32, ids[2]),
            )
        for reason in (" ", "two\nlines", "x" * 301):
            with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_reason"):
                scratch.execute(
                    _sql(DECIDE + ", rejection_reason = %s WHERE mapping_id = %s"),
                    ("rejected", "f" * 32, reason, ids[2]),
                )

        # a decision names who, when and which request, or it is not one
        for email, at, trace in (
            (None, "now()", "e" * 32),
            (" ", "now()", "e" * 32),
            ("lead@acme.example", None, "e" * 32),
            ("lead@acme.example", "now()", None),
            ("lead@acme.example", "now()", " "),
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_decided"):
                scratch.execute(
                    _sql(
                        "UPDATE policy_mapping SET status = 'approved', approver = 'uid-lead', "
                        "approver_email = %s, decided_at = "
                        + ("NULL" if at is None else at)
                        + ", decision_trace_id = %s WHERE mapping_id = %s"
                    ),
                    (email, trace, ids[2]),
                )
        # and a proposal has none of them: not written with one, not updated to have one
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_decided"):
            scratch.execute(
                "INSERT INTO policy_mapping (tenant_id, policy_instrument_id, policy_version_id, "
                "policy_clause_id, obligation_id, kind, rationale, proposer, trace_id, decided_at) "
                "VALUES ('acme', %s, 'v1', %s, 'OB-E', 'implements', 'r', 'm', 't', now())",
                (POLICY, CLAUSE),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_guard"):
            scratch.execute(
                "UPDATE policy_mapping SET decided_at = now() WHERE mapping_id = %s", (ids[2],)
            )

        # the trigger: a decided mapping never changes, even to the same status
        for status in ("approved", "rejected"):
            for decided in ids[:2]:
                with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_guard"):
                    scratch.execute(
                        _sql(DECIDE + " WHERE mapping_id = %s"), (status, "9" * 32, decided)
                    )
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_guard"):
            scratch.execute(
                "UPDATE policy_mapping SET rejection_reason = NULL WHERE mapping_id = %s", (ids[1],)
            )

        # an update that decides nothing, or that changes what the model's run wrote
        with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_guard"):
            scratch.execute(
                "UPDATE policy_mapping SET rationale = 'Reworded.' WHERE mapping_id = %s", (ids[2],)
            )
        for change in (
            "obligation_id = 'OB-Z'",
            "policy_clause_id = '" + POLICY + ":v1:3/9'",
            "kind = 'partially_implements'",
            "rationale = 'Reworded.'",
            "proposer = 'someone'",
            "trace_id = '" + "b" * 32 + "'",
            "proposed_at = now()",
            "policy_version_id = 'v2'",
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_guard"):
                scratch.execute(
                    _sql(DECIDE + ", " + change + " WHERE mapping_id = %s"),
                    ("approved", "e" * 32, ids[2]),
                )
        # still proposed: all of that wrote nothing
        assert scratch.execute(
            "SELECT status, approver FROM policy_mapping WHERE mapping_id = %s", (ids[2],)
        ).fetchone() == ("proposed", None)

        # and none is ever deleted, decided or not
        for any_id in (ids[0], ids[2]):
            with pytest.raises(psycopg.errors.CheckViolation, match="policy_mapping_guard"):
                scratch.execute("DELETE FROM policy_mapping WHERE mapping_id = %s", (any_id,))
        assert scratch.execute("SELECT count(*) FROM policy_mapping").fetchone() == (4,)
        assert CLAUSE  # the clause the rows are of


@needs_ledger_db
def test_a_finding_names_a_derived_finding_and_its_mapping_together_or_neither() -> None:
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        _migrated_through_0032(scratch)
        columns = {
            str(r[0]): r
            for r in scratch.execute(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'finding'"
            ).fetchall()
        }
        assert {"derived_from", "mapping_id"} <= set(columns)
        assert columns["mapping_id"][1] == "YES" and columns["derived_from"][1] == "YES"
        names = {
            str(r[0])
            for r in scratch.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'finding'::regclass"
            ).fetchall()
        }
        assert {
            "finding_mapping_fk",
            "finding_derived_together",
            "finding_derived_is_a_verdict",
        } <= names
        indexes = {
            str(r[0])
            for r in scratch.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'finding'"
            ).fetchall()
        }
        # the key now includes the source, so the model's own finding and a derived one
        # for the same claim and obligation coexist
        assert "finding_one_per_claim_target_and_source" in indexes
        assert "finding_one_per_claim_and_target" not in indexes
