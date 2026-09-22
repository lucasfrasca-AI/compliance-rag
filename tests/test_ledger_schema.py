"""Ledger schema DDL, proven against a real Postgres (issue #3).

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

import os
from collections.abc import Iterator

import psycopg
import pytest

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
