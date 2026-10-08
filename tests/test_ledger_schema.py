"""Ledger schema DDL, proven against a real Postgres (issues #3, #2).

`db/ledger/*.sql` is applied once, in filename order, before this test runs —
by CI's own step (`.github/workflows/ci.yml`), or by docker-compose's
`initdb.d` mount on a container's first start. This file only ever proves
what the DDL does; it never applies it, so the same migration path is
exercised whichever way a developer or CI got there.

Connects with a plain `psycopg` connection, deliberately outside any adapter:
there is no `LedgerStore` method for "insert a raw tenant row", and inventing
one just to exercise a `CHECK` constraint would test the adapter instead of
the schema. Reading `DATABASE_URL` straight from the environment, rather than
through `Settings`, is the same reasoning: this is test infrastructure
locating its own database, not the application reading its runtime config.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from typing import LiteralString

import psycopg
import pytest
from psycopg.types.json import Jsonb

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — needs a migrated ledger Postgres "
    "(see CI, or docker-compose up postgres)",
)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """One transaction per test, always rolled back.

    Never committed, so tests never accumulate rows in a shared database and
    a CHECK violation (which aborts the transaction) needs no special
    handling — rollback always succeeds, even on an aborted transaction.
    """
    assert DATABASE_URL is not None
    connection = psycopg.connect(DATABASE_URL)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _insert_tenant(conn: psycopg.Connection[tuple[object, ...]], tenant_id: str) -> None:
    conn.execute("INSERT INTO tenant (tenant_id, name) VALUES (%s, %s)", (tenant_id, "test"))


@pytest.mark.parametrize(
    "bad_tenant_id",
    ["acme:corp", "acme/corp", "", "_public"],
    ids=["colon", "slash", "empty", "reserved"],
)
def test_check_constraint_rejects_invalid_tenant_ids(
    conn: psycopg.Connection[tuple[object, ...]], bad_tenant_id: str
) -> None:
    """db/ledger/0002_tenant_id_constraints.sql, proven — not inferred from reading it."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_tenant(conn, bad_tenant_id)


def test_check_constraint_allows_a_real_tenant_id(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _insert_tenant(conn, "acme")  # must not raise


# --------------------------------------------------------------------------
# db/ledger/0003_canonicalisation_version.sql (issue #2)
# --------------------------------------------------------------------------


def _seed_clause(
    conn: psycopg.Connection[tuple[object, ...]],
    tenant_id: str,
    instrument_id: str,
    version_id: str,
    clause_path: str,
) -> str:
    """Everything clause_version's foreign keys require, so each test only
    inserts the clause_version row it's actually testing."""
    _insert_tenant(conn, tenant_id)
    conn.execute(
        "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction) "
        "VALUES (%s, %s, %s, %s)",
        (tenant_id, instrument_id, "Test Instrument", "AU"),
    )
    conn.execute(
        "INSERT INTO instrument_version "
        "(tenant_id, instrument_id, version_id, title, status, valid_from) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (tenant_id, instrument_id, version_id, "v1", "in_force", dt.date(2020, 1, 1)),
    )
    conn.execute(
        "INSERT INTO clause (tenant_id, instrument_id, clause_path) VALUES (%s, %s, %s)",
        (tenant_id, instrument_id, clause_path),
    )
    return f"{instrument_id}:{version_id}:{clause_path}"


@pytest.fixture
def clause_id(conn: psycopg.Connection[tuple[object, ...]]) -> str:
    return _seed_clause(conn, "acme", "acme-hr-policy", "v1", "sec1")


def test_clause_version_canonicalisation_version_defaults_to_one(
    conn: psycopg.Connection[tuple[object, ...]], clause_id: str
) -> None:
    """db/ledger/0003_canonicalisation_version.sql, proven — not inferred from reading it."""
    conn.execute(
        "INSERT INTO clause_version "
        "(tenant_id, clause_id, instrument_id, version_id, clause_path, "
        "canonical_sha256, storage_uri) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ("acme", clause_id, "acme-hr-policy", "v1", "sec1", "deadbeef", "gs://bucket/clause"),
    )
    row = conn.execute(
        "SELECT canonicalisation_version FROM clause_version "
        "WHERE tenant_id = %s AND clause_id = %s",
        ("acme", clause_id),
    ).fetchone()
    assert row == (1,)


def test_clause_version_canonicalisation_version_is_not_null(
    conn: psycopg.Connection[tuple[object, ...]], clause_id: str
) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute(
            "INSERT INTO clause_version "
            "(tenant_id, clause_id, instrument_id, version_id, clause_path, "
            "canonical_sha256, storage_uri, canonicalisation_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)",
            ("acme", clause_id, "acme-hr-policy", "v1", "sec1", "deadbeef", "gs://bucket/clause"),
        )


def _insert_assessment(
    conn: psycopg.Connection[tuple[object, ...]], tenant_id: str, assessment_id: str
) -> None:
    conn.execute(
        "INSERT INTO assessment "
        "(tenant_id, assessment_id, corpus_version, mapping_version, as_of, trace_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (tenant_id, assessment_id, "corpus-v1", "mapping-v1", dt.date(2026, 1, 1), "trace-1"),
    )


def _insert_finding(
    conn: psycopg.Connection[tuple[object, ...]],
    tenant_id: str,
    finding_id: str,
    assessment_id: str,
    citations: list[dict[str, object]],
) -> None:
    conn.execute(
        "INSERT INTO finding "
        "(tenant_id, finding_id, assessment_id, claim_text, verdict, provenance_tier, "
        "citations, trace_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            tenant_id,
            finding_id,
            assessment_id,
            "Data is retained for 7 years.",
            "consistent",
            "parsed",
            Jsonb(citations),
            "trace-1",
        ),
    )


@pytest.fixture
def assessment_id(conn: psycopg.Connection[tuple[object, ...]]) -> str:
    _insert_tenant(conn, "acme")
    _insert_assessment(conn, "acme", "assess-1")
    return "assess-1"


@pytest.mark.parametrize(
    "citations",
    [
        [{"clause_id": "acme-hr-policy:v1:sec1", "quoted_text": "Text."}],
        [
            {
                "clause_id": "acme-hr-policy:v1:sec1",
                "quoted_text": "Text.",
                "canonicalisation_version": "1",
            }
        ],
        [
            {
                "clause_id": "acme-hr-policy:v1:sec1",
                "quoted_text": "Text.",
                "canonicalisation_version": 0,
            }
        ],
    ],
    ids=["missing", "non_numeric", "not_positive"],
)
def test_finding_citations_check_rejects_an_invalid_version(
    conn: psycopg.Connection[tuple[object, ...]],
    assessment_id: str,
    citations: list[dict[str, object]],
) -> None:
    """db/ledger/0003_canonicalisation_version.sql, proven — not inferred from reading it."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_finding(conn, "acme", "finding-1", assessment_id, citations)


def _subscribe(
    conn: psycopg.Connection[tuple[object, ...]], tenant_id: str, instrument_id: str
) -> None:
    conn.execute(
        "INSERT INTO tenant_instrument (tenant_id, instrument_id, subscribed_by) "
        "VALUES (%s, %s, %s)",
        (tenant_id, instrument_id, "user-1"),
    )


def test_tenant_instrument_records_a_subscription(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """db/ledger/0004_tenant_instrument.sql, proven — not inferred from reading it."""
    _insert_tenant(conn, "acme")
    _subscribe(conn, "acme", "privacy-act-1988")
    row = conn.execute(
        "SELECT subscribed_by, subscribed_at IS NOT NULL FROM tenant_instrument "
        "WHERE tenant_id = %s AND instrument_id = %s",
        ("acme", "privacy-act-1988"),
    ).fetchone()
    assert row == ("user-1", True)


@pytest.mark.parametrize(
    "instrument_id",
    ["acme/privacy-policy", "privacy-act-1988:C2026C00227", ""],
    ids=["tenant-prefixed", "stable-id-not-instrument-id", "empty"],
)
def test_tenant_instrument_accepts_library_ids_only(
    conn: psycopg.Connection[tuple[object, ...]], instrument_id: str
) -> None:
    _insert_tenant(conn, "acme")
    with pytest.raises(psycopg.errors.CheckViolation):
        _subscribe(conn, "acme", instrument_id)


def test_tenant_instrument_requires_a_real_tenant(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """`_public` can't be a `tenant` row (0002), so it can never hold a
    subscription row either — it sees the library by ownership instead."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _subscribe(conn, "_public", "privacy-act-1988")


def test_tenant_instrument_is_one_row_per_tenant_and_instrument(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _insert_tenant(conn, "acme")
    _subscribe(conn, "acme", "privacy-act-1988")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _subscribe(conn, "acme", "privacy-act-1988")


def test_finding_citations_check_allows_a_citation_carrying_the_version(
    conn: psycopg.Connection[tuple[object, ...]], assessment_id: str
) -> None:
    _insert_finding(
        conn,
        "acme",
        "finding-1",
        assessment_id,
        [
            {
                "clause_id": "acme-hr-policy:v1:sec1",
                "quoted_text": "Text.",
                "canonicalisation_version": 1,
            }
        ],
    )  # must not raise


# --------------------------------------------------------------------------
# db/ledger/0005_version_removal.sql (ADR-0021)
# --------------------------------------------------------------------------


def _seed_version(
    conn: psycopg.Connection[tuple[object, ...]], tenant_id: str, instrument_id: str
) -> None:
    _insert_tenant(conn, tenant_id)
    conn.execute(
        "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction, scope) "
        "VALUES (%s, %s, %s, %s, 'tenant')",
        (tenant_id, instrument_id, "Retention policy", "AU"),
    )
    conn.execute(
        "INSERT INTO instrument_version "
        "(tenant_id, instrument_id, version_id, title, status, valid_from) "
        "VALUES (%s, %s, 'v1', 'v1', 'in_force', %s)",
        (tenant_id, instrument_id, dt.date(2025, 1, 1)),
    )


def test_withdrawn_and_purged_markers_keep_the_version_row(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _seed_version(conn, "acme", "acme/retention-policy")
    conn.execute(
        "UPDATE instrument_version SET withdrawn_at = now(), withdrawn_by = 'u1', "
        "purged_at = now(), purged_by = 'u1', purge_reason = 'legal hold lifted' "
        "WHERE tenant_id = 'acme' AND instrument_id = 'acme/retention-policy'"
    )
    row = conn.execute(
        "SELECT withdrawn_by, purged_by, purge_reason FROM instrument_version "
        "WHERE tenant_id = 'acme' AND instrument_id = 'acme/retention-policy'"
    ).fetchone()
    assert row == ("u1", "u1", "legal hold lifted")


def test_a_removal_marker_records_who_when_and_why_together(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _seed_version(conn, "acme", "acme/retention-policy")
    incomplete: tuple[LiteralString, ...] = (
        "UPDATE instrument_version SET withdrawn_at = now() WHERE tenant_id = 'acme'",
        "UPDATE instrument_version SET purged_at = now(), purged_by = 'u1' "
        "WHERE tenant_id = 'acme'",
        "UPDATE instrument_version SET purged_by = 'u1', purge_reason = 'r' "
        "WHERE tenant_id = 'acme'",
    )
    for statement in incomplete:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(statement)


def test_removal_audit_records_a_refusal_for_a_non_tenant(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """No FK to `tenant`: a refused request naming `_public` must still be auditable."""
    conn.execute(
        "INSERT INTO corpus_removal_audit "
        "(audit_id, tenant_id, instrument_id, action, outcome, actor, reason) "
        "VALUES ('a1', '_public', 'privacy-act-1988', 'purge', 'refused', 'admin', 'typo')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO corpus_removal_audit "
            "(audit_id, tenant_id, instrument_id, action, outcome, actor, reason) "
            "VALUES ('a2', 'acme', 'acme/x', 'delete', 'completed', 'u1', 'r')"
        )


def test_count_findings_citing_matches_by_stable_id_prefix(
    conn: psycopg.Connection[tuple[object, ...]], assessment_id: str
) -> None:
    def cite(finding_id: str, *clause_ids: str) -> None:
        _insert_finding(
            conn,
            "acme",
            finding_id,
            assessment_id,
            [
                {"clause_id": c, "quoted_text": "t", "canonicalisation_version": 2}
                for c in clause_ids
            ],
        )

    cite("f1", "acme/retention_policy:v1:3/1")
    cite("f2", "acme/retention_policy:v1:3/2", "acme/retention_policy:v2:3/1")
    cite("f3", "acme/retention_policy:v2:3/1")
    cite("f4", "acme/retentionXpolicy:v1:3/1")  # '_' must not act as a LIKE wildcard

    def count(prefix: str) -> object:
        row = conn.execute("SELECT count_findings_citing('acme', %s)", (prefix,)).fetchone()
        assert row is not None
        return row[0]

    assert count("acme/retention_policy:v1:") == 2
    assert count("acme/retention_policy:v2:") == 2
    assert count("acme/retention_policy:") == 3
    assert conn.execute(
        "SELECT count_findings_citing('other-corp', 'acme/retention_policy:')"
    ).fetchone() == (0,)
