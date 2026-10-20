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
        "(tenant_id, instrument_id, version_id, title, valid_from) "
        "VALUES (%s, %s, %s, %s, %s)",
        (tenant_id, instrument_id, version_id, "v1", dt.date(2020, 1, 1)),
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
        "INSERT INTO assessment (tenant_id, assessment_id, corpus_version, corpus_sha, "
        "mapping_version, mapping_sha, as_of, trace_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            tenant_id,
            assessment_id,
            "corpus-v1",
            "a" * 40,
            "mapping-v1",
            "b" * 40,
            dt.date(2026, 1, 1),
            "trace-1",
        ),
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


def _library_instrument(conn: psycopg.Connection[tuple[object, ...]], instrument_id: str) -> None:
    conn.execute(
        "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction) "
        "VALUES ('_public', %s, %s, 'AU') ON CONFLICT DO NOTHING",
        (instrument_id, instrument_id),
    )


def _subscribe(
    conn: psycopg.Connection[tuple[object, ...]], tenant_id: str, instrument_id: str
) -> None:
    if instrument_id and "/" not in instrument_id and ":" not in instrument_id:
        _library_instrument(conn, instrument_id)  # 0006: a subscription names a held instrument
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


def test_the_library_never_subscribes(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """`_public` is a tenant row since 0006, but it sees the library by
    ownership (ADR-0020) — it can never hold a subscription."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _subscribe(conn, "_public", "privacy-act-1988")


def test_a_subscriber_must_be_a_real_tenant(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _subscribe(conn, "nobody", "privacy-act-1988")


# ---- 0006: the library owner (issue #67, ADR-0025) -------------------------


def test_a_public_law_instrument_and_its_versions_can_be_recorded(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _library_instrument(conn, "privacy-act-1988")
    for version, valid_from, valid_to, end_reason in (
        ("C2025C00100", "2025-01-01", "2026-01-01", "superseded"),
        ("C2026C00227", "2026-01-01", None, None),
    ):
        conn.execute(
            "INSERT INTO instrument_version "
            "(tenant_id, instrument_id, version_id, title, valid_from, valid_to, end_reason) "
            "VALUES ('_public', 'privacy-act-1988', %s, 'Privacy Act 1988', %s, %s, %s)",
            (version, valid_from, valid_to, end_reason),
        )
    count = conn.execute(
        "SELECT count(*) FROM instrument_version WHERE tenant_id = '_public'"
    ).fetchone()
    assert count == (2,)


def test_the_library_owner_is_one_seeded_row(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    rows = conn.execute("SELECT tenant_id FROM tenant WHERE is_library").fetchall()
    assert rows == [("_public",)]


@pytest.mark.parametrize("is_library", [False, True], ids=["plain", "claiming-library"])
def test_no_company_can_register_as_public(
    conn: psycopg.Connection[tuple[object, ...]], is_library: bool
) -> None:
    """Either the CHECK (not flagged) or the seeded row's primary key (flagged)
    refuses it — there is no way in."""
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation)):
        conn.execute(
            "INSERT INTO tenant (tenant_id, name, is_library) VALUES ('_public', 'Evil', %s)",
            (is_library,),
        )


def test_no_other_tenant_can_be_the_library(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO tenant (tenant_id, name, is_library) VALUES ('evil', 'E', true)")


def test_a_subscription_must_name_an_instrument_the_library_holds(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _insert_tenant(conn, "acme")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO tenant_instrument (tenant_id, instrument_id, subscribed_by) "
            "VALUES ('acme', 'spam-act-2003', 'u1')"
        )


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
        "(tenant_id, instrument_id, version_id, title, valid_from) "
        "VALUES (%s, %s, 'v1', 'v1', %s)",
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


# ---- ADR-0029: status is derived, never stored ------------------------------


def test_instrument_version_stores_no_status(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    columns = {
        str(row[0])
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'instrument_version'"
        ).fetchall()
    }
    assert "status" not in columns
    assert {"valid_from", "valid_to", "end_reason"} <= columns


@pytest.mark.parametrize(
    ("valid_to", "end_reason"),
    [("2026-01-01", None), (None, "superseded"), ("2026-01-01", "revoked")],
)
def test_an_end_date_and_its_reason_come_together(
    conn: psycopg.Connection[tuple[object, ...]], valid_to: str | None, end_reason: str | None
) -> None:
    _library_instrument(conn, "privacy-act-1988")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO instrument_version "
            "(tenant_id, instrument_id, version_id, title, valid_from, valid_to, end_reason) "
            "VALUES ('_public', 'privacy-act-1988', 'C1', 't', '2025-01-01', %s, %s)",
            (valid_to, end_reason),
        )


# ---- #114: evidence is a field for a period, append-only ---------------------


def _insert_evidence(
    conn: psycopg.Connection[tuple[object, ...]], evidence_id: str, **override: object
) -> None:
    row: dict[str, object] = {
        "tenant_id": "acme",
        "evidence_id": evidence_id,
        "field": "annual_turnover_aud",
        "value": Jsonb(2500000),
        "period_start": dt.date(2025, 7, 1),
        "period_end": dt.date(2026, 7, 1),
        "provenance_tier": "asserted",
        "source": "CFO",
        "asserted_by": "cfo@acme.example",
        "supersedes": None,
        "trace_id": "t1",
    } | override
    columns = ", ".join(row)
    conn.execute(
        f"INSERT INTO evidence ({columns}) VALUES ({', '.join(['%s'] * len(row))})",  # pyright: ignore[reportArgumentType] — test-built column list
        tuple(row.values()),
    )


@pytest.mark.parametrize(
    "override",
    [
        {"period_end": dt.date(2025, 7, 1)},  # empty period
        {"field": "Annual Turnover"},  # not snake_case
        {"source": "  "},  # no source
        {"asserted_by": None},  # an assertion without who asserted it
        {"supersedes": "e1"},  # supersedes itself
        {"trace_id": None},  # an assertion outside a request
    ],
)
def test_evidence_says_what_for_when_and_on_whose_word(
    conn: psycopg.Connection[tuple[object, ...]], override: dict[str, object]
) -> None:
    _insert_tenant(conn, "acme")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_evidence(conn, "e1", **override)


def test_evidence_has_one_successor_and_supersedes_only_what_exists(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _insert_tenant(conn, "acme")
    _insert_evidence(conn, "e1")
    _insert_evidence(conn, "e2", supersedes="e1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_evidence(conn, "e3", supersedes="e1")


def test_evidence_cannot_supersede_what_was_never_recorded(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _insert_tenant(conn, "acme")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert_evidence(conn, "e1", supersedes="e404")


# ---- #115: coverage, one row per assessment and obligation -----------------------


def _assessment(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    _insert_tenant(conn, "acme")
    conn.execute(
        "INSERT INTO assessment (tenant_id, assessment_id, corpus_version, corpus_sha, "
        "mapping_version, mapping_sha, as_of, trace_id) "
        "VALUES ('acme', 'a1', 'c', %s, 'm', %s, '2026-10-12', 't1')",
        ("a" * 40, "b" * 40),
    )


@pytest.mark.parametrize(
    ("outcome", "citations", "questions"),
    [
        ("probable", ["x:v:1"], []),  # three outcomes, never a score (D-06)
        ("applicable", [], []),  # a decided obligation cites the law
    ],
)
def test_coverage_rows_say_why(
    conn: psycopg.Connection[tuple[object, ...]],
    outcome: str,
    citations: list[str],
    questions: list[object],
) -> None:
    _assessment(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO coverage (tenant_id, assessment_id, obligation_id, outcome, reason, "
            "citations, questions, trace_id) VALUES ('acme', 'a1', 'OB-1', %s, 'r', %s, %s, 't1')",
            (outcome, Jsonb(citations), Jsonb(questions)),
        )


def test_coverage_belongs_to_a_recorded_assessment(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _insert_tenant(conn, "acme")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO coverage (tenant_id, assessment_id, obligation_id, outcome, reason, "
            "trace_id) VALUES ('acme', 'nope', 'OB-1', 'unassessable', 'r', 't1')"
        )


# --------------------------------------------------------------------------
# db/ledger/0011_document_class.sql (#148)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tenant_id", "instrument_id", "scope", "document_class"),
    [
        pytest.param("acme", "acme/handbook", "tenant", "library_law", id="tenant-claims-library"),
        pytest.param("_public", "some-act", "public", "internal_policy", id="library-not-law"),
        pytest.param("acme", "acme/handbook", "tenant", "a-guess", id="not-a-class"),
    ],
)
def test_document_class_is_library_law_exactly_when_the_owner_is_public(
    conn: psycopg.Connection[tuple[object, ...]],
    tenant_id: str,
    instrument_id: str,
    scope: str,
    document_class: str,
) -> None:
    if tenant_id != "_public":
        _insert_tenant(conn, tenant_id)
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO instrument "
            "(tenant_id, instrument_id, title, jurisdiction, scope, document_class) "
            "VALUES (%s, %s, 'x', 'AU', %s, %s)",
            (tenant_id, instrument_id, scope, document_class),
        )


def test_each_class_is_accepted_for_its_owner(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _insert_tenant(conn, "acme")
    for tenant_id, instrument_id, scope, document_class in (
        ("_public", "some-act", "public", "library_law"),
        ("acme", "acme/handbook", "tenant", "internal_policy"),
        ("acme", "acme/iso-extract", "tenant", "external_upload"),
    ):
        conn.execute(
            "INSERT INTO instrument "
            "(tenant_id, instrument_id, title, jurisdiction, scope, document_class) "
            "VALUES (%s, %s, 'x', 'AU', %s, %s)",
            (tenant_id, instrument_id, scope, document_class),
        )


# --------------------------------------------------------------------------
# db/ledger/0013_source_versions.sql (#153)
# --------------------------------------------------------------------------


def _versioned_assessment(
    conn: psycopg.Connection[tuple[object, ...]], override: dict[str, object] | None = None
) -> None:
    row: dict[str, object] = {
        "tenant_id": "acme",
        "assessment_id": "a1",
        "corpus_version": "corpus-2026.10",
        "corpus_sha": "a" * 40,
        "mapping_version": "mappings-2026.10",
        "mapping_sha": "b" * 40,
        "as_of": dt.date(2026, 10, 14),
        "trace_id": "t1",
    } | (override or {})
    columns = ", ".join(row)
    conn.execute(
        f"INSERT INTO assessment ({columns}) VALUES ({', '.join(['%s'] * len(row))})",  # pyright: ignore[reportArgumentType] — test-built column list
        tuple(row.values()),
    )


def test_an_assessment_names_both_shas_and_may_name_no_tag(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """A tag is the name a SHA was known by; without one the SHA alone says
    what was loaded, and the load isn't refused for it."""
    _insert_tenant(conn, "acme")
    _versioned_assessment(conn, {"assessment_id": "tagged"})
    _versioned_assessment(
        conn, {"assessment_id": "untagged", "corpus_version": None, "mapping_version": None}
    )
    rows = conn.execute(
        "SELECT assessment_id, corpus_version, mapping_version FROM assessment ORDER BY 1"
    ).fetchall()
    assert rows == [("tagged", "corpus-2026.10", "mappings-2026.10"), ("untagged", None, None)]


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"corpus_sha": None}, id="no-corpus-sha"),
        pytest.param({"mapping_sha": None}, id="no-mapping-sha"),
        pytest.param({"corpus_sha": "a" * 39}, id="short"),
        pytest.param({"mapping_sha": "b" * 41}, id="long"),
        pytest.param({"corpus_sha": "A" * 40}, id="uppercase"),
        pytest.param({"mapping_sha": "main"}, id="a-name-not-a-sha"),
        pytest.param({"corpus_version": ""}, id="blank-corpus-tag"),
        pytest.param({"mapping_version": "  "}, id="blank-mapping-tag"),
        pytest.param({"trace_id": ""}, id="no-trace-id"),
        pytest.param({"trace_id": "  "}, id="blank-trace-id"),
        pytest.param({"user_id": ""}, id="empty-user"),
        pytest.param({"user_id": " "}, id="blank-user"),
    ],
)
def test_an_assessment_without_full_shas_a_trace_or_a_real_user_is_refused(
    conn: psycopg.Connection[tuple[object, ...]], override: dict[str, object]
) -> None:
    _insert_tenant(conn, "acme")
    with pytest.raises(psycopg.errors.CheckViolation):
        _versioned_assessment(conn, override)


def test_an_assessment_may_name_a_user_or_none(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """Nullable: an assessment not made for a member has nobody to name."""
    _insert_tenant(conn, "acme")
    _versioned_assessment(conn, {"assessment_id": "by-a-member", "user_id": "lead@acme.example"})
    _versioned_assessment(conn, {"assessment_id": "by-nobody", "user_id": None})


@pytest.mark.parametrize(
    "constraint",
    ["assessment_names_both_commits", "assessment_trace_id", "assessment_user_id"],
)
def test_the_rules_for_new_rows_are_enforced_without_validating_old_ones(
    conn: psycopg.Connection[tuple[object, ...]], constraint: str
) -> None:
    """Added NOT VALID: rows from before #153 (tests only) aren't rejected,
    and every new row is."""
    row = conn.execute(
        "SELECT convalidated FROM pg_constraint WHERE conrelid = 'assessment'::regclass "
        "AND conname = %s",
        (constraint,),
    ).fetchone()
    assert row == (False,)


@pytest.mark.parametrize("column", ["corpus_version", "mapping_version"])
def test_a_null_version_is_documented_as_untagged(
    conn: psycopg.Connection[tuple[object, ...]], column: str
) -> None:
    row = conn.execute(
        "SELECT col_description('assessment'::regclass, attnum) FROM pg_attribute "
        "WHERE attrelid = 'assessment'::regclass AND attname = %s",
        (column,),
    ).fetchone()
    assert row is not None and isinstance(row[0], str)
    assert "NULL means untagged" in row[0] and "the SHA alone" in row[0]


# --------------------------------------------------------------------------
# db/ledger/0014_chat_history.sql (#162)
# --------------------------------------------------------------------------

CLAUSE = "privacy-act-1988:C2026C00227:sch1/app11/1"
CITED = Jsonb([{"clause_id": CLAUSE, "version_id": "C2026C00227"}])


def _chat_turn(
    conn: psycopg.Connection[tuple[object, ...]], override: dict[str, object] | None = None
) -> None:
    """A member's question by default; `override` turns it into anything else."""
    row: dict[str, object] = {
        "tenant_id": "acme",
        "chat_id": "c1",
        "turn_index": 0,
        "user_id": "uid-1",
        "role": "user",
        "content": "What must Acme do?",
        "outcome": None,
        "citations": Jsonb([]),
        "blocked": Jsonb([]),
        "as_of": dt.date(2026, 10, 14),
        "trace_id": "t1",
    } | (override or {})
    columns = ", ".join(row)
    conn.execute(
        f"INSERT INTO chat ({columns}) VALUES ({', '.join(['%s'] * len(row))})",  # pyright: ignore[reportArgumentType] — test-built column list
        tuple(row.values()),
    )


def _answer(**changes: object) -> dict[str, object]:
    return {"turn_index": 1, "role": "assistant", "content": None, **changes}


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_answer(outcome="verified", content="Text.", citations=CITED), id="verified"),
        pytest.param(_answer(outcome="blocked", blocked=Jsonb([CLAUSE])), id="blocked"),
        pytest.param(_answer(outcome="uncited"), id="uncited"),
        pytest.param(_answer(outcome="residency"), id="residency"),
        pytest.param(_answer(outcome="malformed_generation"), id="malformed"),
        pytest.param(_answer(outcome="internal"), id="internal"),
    ],
)
def test_a_chat_holds_a_question_and_what_the_member_was_shown(
    conn: psycopg.Connection[tuple[object, ...]], answer: dict[str, object]
) -> None:
    _insert_tenant(conn, "acme")
    _chat_turn(conn)
    _chat_turn(conn, answer)
    row = conn.execute("SELECT outcome FROM chat WHERE turn_index = 1").fetchone()
    assert row == (answer["outcome"],)


@pytest.mark.parametrize(
    "turn",
    [
        # An answer is what the member was shown: no text but a verified one's
        # (ADR-0003), and what it says it is.
        pytest.param(
            _answer(outcome="blocked", blocked=Jsonb([CLAUSE]), content="Prose"),
            id="blocked-with-text",
        ),
        pytest.param(_answer(outcome="uncited", content="Prose"), id="uncited-with-text"),
        pytest.param(_answer(outcome="residency", content="Prose"), id="residency-with-text"),
        pytest.param(_answer(outcome="internal", content="Prose"), id="internal-with-text"),
        pytest.param(_answer(outcome="blocked"), id="blocked-naming-nothing"),
        pytest.param(
            _answer(outcome="uncited", blocked=Jsonb([CLAUSE])), id="uncited-naming-blocked"
        ),
        pytest.param(_answer(outcome="uncited", citations=CITED), id="uncited-with-citations"),
        pytest.param(
            _answer(outcome="blocked", blocked=Jsonb([CLAUSE]), citations=CITED),
            id="blocked-with-citations",
        ),
        pytest.param(_answer(outcome="verified", content="Text."), id="verified-uncited"),
        pytest.param(_answer(outcome="verified", citations=CITED), id="verified-without-text"),
        pytest.param(
            _answer(outcome="verified", content=" ", citations=CITED), id="verified-blank-text"
        ),
        pytest.param(
            _answer(outcome="verified", content="T", citations=CITED, blocked=Jsonb([CLAUSE])),
            id="verified-and-blocked",
        ),
        pytest.param(_answer(outcome="maybe"), id="unknown-outcome"),
        pytest.param(_answer(), id="an-answer-that-ended-no-way"),
        # A question is the member's own words and nothing else.
        pytest.param({"outcome": "verified"}, id="question-with-an-outcome"),
        pytest.param({"content": None}, id="question-without-text"),
        pytest.param({"content": " "}, id="question-blank"),
        pytest.param({"citations": CITED}, id="question-with-citations"),
        pytest.param({"blocked": Jsonb([CLAUSE])}, id="question-with-blocked"),
        # Turns come in exchanges, and a chat holds only questions and answers.
        pytest.param({"turn_index": 1}, id="question-at-an-odd-index"),
        pytest.param(_answer(turn_index=2, outcome="uncited"), id="answer-at-an-even-index"),
        pytest.param({"turn_index": -2}, id="negative-index"),
        pytest.param({"role": "system"}, id="system-turn"),
        # Whose, which request, and for when.
        pytest.param({"user_id": None}, id="no-member"),
        pytest.param({"user_id": " "}, id="blank-member"),
        pytest.param({"trace_id": None}, id="no-trace"),
        pytest.param({"trace_id": " "}, id="blank-trace"),
        pytest.param({"as_of": None}, id="no-date"),
        pytest.param({"citations": Jsonb({"clause_id": CLAUSE})}, id="citations-not-a-list"),
        pytest.param(_answer(outcome="blocked", blocked=Jsonb(CLAUSE)), id="blocked-not-a-list"),
    ],
)
def test_a_chat_turn_that_is_not_one_of_those_shapes_is_refused(
    conn: psycopg.Connection[tuple[object, ...]], turn: dict[str, object]
) -> None:
    _insert_tenant(conn, "acme")
    with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation)):
        _chat_turn(conn, turn)


def test_a_chat_turn_is_one_row_per_chat_and_index(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    _insert_tenant(conn, "acme")
    _chat_turn(conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _chat_turn(conn)


def test_the_citations_comment_names_the_incomplete_key_0016(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """0016 (#228) re-sets the comment 0014 left describing the old element,
    and a citation carrying `incomplete` is accepted: no DDL was needed."""
    (comment,) = conn.execute(
        "SELECT col_description('chat'::regclass, attnum) FROM pg_attribute "
        "WHERE attrelid = 'chat'::regclass AND attname = 'citations'"
    ).fetchone() or (None,)
    assert isinstance(comment, str) and "incomplete" in comment
    answer: dict[str, object] = {
        "role": "assistant",
        "turn_index": 1,
        "content": "It is worked out by formula.",
        "outcome": "verified",
        "citations": Jsonb(
            [
                {
                    "clause_id": CLAUSE,
                    "version_id": "C2026C00227",
                    "incomplete": [{"reason": "image_not_text", "detail": "an image"}],
                }
            ]
        ),
    }
    _insert_tenant(conn, "acme")
    _chat_turn(conn)
    _chat_turn(conn, answer)


def test_every_chat_rule_is_validated_on_the_rows_there_are(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """Unlike 0013's, none is NOT VALID: nothing wrote `chat` before this, so
    there is no older row to leave alone."""
    rows = conn.execute(
        "SELECT conname, convalidated FROM pg_constraint "
        "WHERE conrelid = 'chat'::regclass AND contype = 'c'"
    ).fetchall()
    assert {name for name, _ in rows} >= {
        "chat_role",
        "chat_in_exchanges",
        "chat_user_id",
        "chat_trace_id",
        "chat_outcome",
        "chat_turn_shape",
    }
    assert all(validated for _, validated in rows)
