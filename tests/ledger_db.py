"""A scratch Postgres database for the tests that build a ledger from nothing
(#293): the runner's own, and the seed's end to end.

Each gets its own database on the server `DATABASE_URL` names, created empty
and dropped afterwards, so nothing here touches the migrated database the rest
of the suite shares. CI sets `REQUIRE_LEDGER_DB`, so a missing `DATABASE_URL`
fails these tests there instead of skipping them (as `REQUIRE_NEO4J` does for
the graph).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REQUIRED = bool(os.environ.get("REQUIRE_LEDGER_DB"))

needs_ledger_db = pytest.mark.skipif(
    not DATABASE_URL and not REQUIRED,
    reason="DATABASE_URL not set: needs a Postgres server (CI sets it and REQUIRE_LEDGER_DB)",
)


@contextmanager
def scratch_database() -> Generator[str]:
    """The DSN of a new, empty database on the server `DATABASE_URL` names,
    dropped on exit."""
    assert DATABASE_URL, "REQUIRE_LEDGER_DB is set but DATABASE_URL is not"
    name = f"ledger_scratch_{uuid.uuid4().hex[:12]}"
    # A name this function generated, never input.
    admin = psycopg.connect(DATABASE_URL, autocommit=True)
    try:
        admin.execute(cast("LiteralString", f'CREATE DATABASE "{name}"'))
        try:
            yield make_conninfo(DATABASE_URL, dbname=name)
        finally:
            admin.execute(cast("LiteralString", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.close()
