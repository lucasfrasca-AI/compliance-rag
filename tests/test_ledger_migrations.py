"""The ledger migration runner (#293), against real Postgres.

Each database test builds its own empty database (`tests/ledger_db.py`), so
nothing here touches the migrated one the rest of the suite shares. The
runner is run on the repository's real `db/ledger/` for the claims about what
it does to the ledger, and on a few files written to a temporary directory for
the ones about how it refuses, which need a file to edit or to fail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest

from compliance_rag.adapters.ledger_migrations import (
    MIGRATIONS_DIR,
    LedgerMigrator,
    MigrationFailed,
    MigrationFile,
    MigrationRefused,
    read_migrations,
    transaction_body,
)
from tests.ledger_db import needs_ledger_db, scratch_database

REAL_FILES = tuple(f.name for f in read_migrations())


def _file(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


def _query(dsn: str, sql: str) -> list[tuple[object, ...]]:
    # Test SQL, written here, never input.
    with psycopg.connect(dsn, autocommit=True) as conn:
        cursor = conn.execute(cast("LiteralString", sql))
        return cursor.fetchall() if cursor.description else []


def _recorded(dsn: str) -> list[str]:
    return [str(r[0]) for r in _query(dsn, "SELECT filename FROM schema_migration ORDER BY 1")]


def _tables(dsn: str) -> set[str]:
    rows = _query(dsn, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {str(r[0]) for r in rows}


@pytest.fixture
def dsn() -> Iterator[str]:
    with scratch_database() as scratch:
        yield scratch


# ---- the files themselves (no database) ---------------------------------------------


@pytest.mark.parametrize("name", REAL_FILES)
def test_every_real_migration_runs_inside_the_runners_transaction(name: str) -> None:
    """Every file in `db/ledger/`, not a sample: none ends its own transaction
    other than by the `BEGIN;`/`COMMIT;` lines the runner takes out."""
    migration = next(f for f in read_migrations() if f.name == name)
    body = transaction_body(migration)
    assert "BEGIN;" not in body and "COMMIT;" not in body
    assert body.strip(), f"{name} is empty once its transaction lines are taken out"


@pytest.mark.parametrize(
    "sql", ["SELECT 1;\nROLLBACK;\n", "COMMIT;\nSELECT 1;\nBEGIN;\nSELECT 2;\nCOMMIT;\nCOMMIT ;\n"]
)
def test_a_file_that_ends_its_own_transaction_otherwise_is_refused(sql: str) -> None:
    migration = MigrationFile(name="0001_x.sql", number="0001", sha256="", sql=sql)
    with pytest.raises(MigrationRefused, match="controls its own transaction"):
        transaction_body(migration)


async def test_a_baseline_naming_no_file_is_refused_before_the_database_is_reached() -> None:
    with pytest.raises(MigrationRefused, match="isn't the number of a file"):
        await LedgerMigrator("postgresql://nobody@127.0.0.1:1/none").migrate(
            baseline_through="9999"
        )


def test_a_file_not_named_by_number_is_refused(tmp_path: Path) -> None:
    _file(tmp_path, "init.sql", "SELECT 1;")
    with pytest.raises(MigrationRefused, match=r"isn't named NNNN_name\.sql"):
        read_migrations(tmp_path)


def test_a_number_used_twice_is_refused(tmp_path: Path) -> None:
    _file(tmp_path, "0001_a.sql", "SELECT 1;")
    _file(tmp_path, "0001_b.sql", "SELECT 1;")
    with pytest.raises(MigrationRefused, match="used twice"):
        read_migrations(tmp_path)


# ---- against a real database ---------------------------------------------------------


@needs_ledger_db
async def test_all_of_db_ledger_applies_to_an_empty_database_and_a_second_run_applies_nothing(
    dsn: str,
) -> None:
    first = await LedgerMigrator(dsn).migrate()
    assert first.applied == REAL_FILES and first.baselined == () and first.already_applied == 0
    assert _recorded(dsn) == sorted(REAL_FILES)
    assert {"tenant", "evidence", "chat", "schema_migration"} <= _tables(dsn)

    second = await LedgerMigrator(dsn).migrate()
    assert second.applied == () and second.baselined == ()
    assert second.already_applied == len(REAL_FILES)


@needs_ledger_db
async def test_each_file_is_recorded_with_the_sha256_of_its_bytes(dsn: str) -> None:
    await LedgerMigrator(dsn).migrate()
    rows = _query(dsn, "SELECT filename, sha256, applied_at IS NOT NULL FROM schema_migration")
    assert {(str(n), str(s)) for n, s, _ in rows} == {(f.name, f.sha256) for f in read_migrations()}
    assert all(r[2] for r in rows)


@needs_ledger_db
async def test_a_changed_applied_file_refuses_the_run_with_nothing_applied(
    dsn: str, tmp_path: Path
) -> None:
    _file(tmp_path, "0001_a.sql", "BEGIN;\nCREATE TABLE a (n INT);\nCOMMIT;\n")
    await LedgerMigrator(dsn, tmp_path).migrate()
    _file(tmp_path, "0001_a.sql", "BEGIN;\nCREATE TABLE a (n INT, edited INT);\nCOMMIT;\n")
    _file(tmp_path, "0002_b.sql", "CREATE TABLE b (n INT);\n")

    with pytest.raises(MigrationRefused, match=r"nothing was applied.*0001_a.sql.*changed"):
        await LedgerMigrator(dsn, tmp_path).migrate()
    assert _recorded(dsn) == ["0001_a.sql"]
    assert "b" not in _tables(dsn)


@needs_ledger_db
async def test_a_missing_applied_file_refuses_the_run_with_nothing_applied(
    dsn: str, tmp_path: Path
) -> None:
    _file(tmp_path, "0001_a.sql", "CREATE TABLE a (n INT);\n")
    _file(tmp_path, "0002_b.sql", "CREATE TABLE b (n INT);\n")
    await LedgerMigrator(dsn, tmp_path).migrate()
    (tmp_path / "0001_a.sql").unlink()
    _file(tmp_path, "0003_c.sql", "CREATE TABLE c (n INT);\n")

    with pytest.raises(MigrationRefused, match=r"nothing was applied.*0001_a.sql.*no longer"):
        await LedgerMigrator(dsn, tmp_path).migrate()
    assert _recorded(dsn) == ["0001_a.sql", "0002_b.sql"]
    assert "c" not in _tables(dsn)


@needs_ledger_db
async def test_a_new_file_that_sorts_before_an_applied_one_refuses_the_run(
    dsn: str, tmp_path: Path
) -> None:
    _file(tmp_path, "0002_b.sql", "CREATE TABLE b (n INT);\n")
    await LedgerMigrator(dsn, tmp_path).migrate()
    _file(tmp_path, "0001_a.sql", "CREATE TABLE a (n INT);\n")

    with pytest.raises(MigrationRefused, match=r"0001_a.sql isn't applied but sorts before"):
        await LedgerMigrator(dsn, tmp_path).migrate()
    assert "a" not in _tables(dsn)


@needs_ledger_db
async def test_baseline_through_records_files_without_executing_them(
    dsn: str, tmp_path: Path
) -> None:
    """0002 would fail if it ran (it is the unguarded constraint the old loop
    stopped at); baselined, it is recorded and 0003 is applied after it."""
    _file(tmp_path, "0001_init.sql", "CREATE TABLE tenant (tenant_id TEXT PRIMARY KEY);\n")
    _file(tmp_path, "0002_boom.sql", "SELECT 1 / 0;\n")
    _file(tmp_path, "0003_after.sql", "CREATE TABLE after (n INT);\n")
    _query(dsn, "CREATE TABLE tenant (tenant_id TEXT PRIMARY KEY)")  # built before the runner

    run = await LedgerMigrator(dsn, tmp_path).migrate(baseline_through="0002")

    assert run.baselined == ("0001_init.sql", "0002_boom.sql")
    assert run.applied == ("0003_after.sql",)
    assert _recorded(dsn) == ["0001_init.sql", "0002_boom.sql", "0003_after.sql"]
    assert "after" in _tables(dsn)


@needs_ledger_db
async def test_baseline_through_the_real_files_on_a_database_the_old_way_built(dsn: str) -> None:
    """A database made by the old loop (`psql -f` each file), as `crag-test-pg`
    and a docker-compose volume are: baselined through the last file, then
    run, it changes nothing and records every file."""
    last = REAL_FILES[-1][:4]
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name in REAL_FILES:
            conn.execute(cast("LiteralString", (MIGRATIONS_DIR / name).read_text(encoding="utf-8")))
    before = _tables(dsn)

    run = await LedgerMigrator(dsn).migrate(baseline_through=last)

    assert run.baselined == REAL_FILES and run.applied == ()
    assert _tables(dsn) == before | {"schema_migration"}
    assert _recorded(dsn) == sorted(REAL_FILES)


@needs_ledger_db
async def test_baseline_through_is_refused_on_a_database_with_no_ledger(dsn: str) -> None:
    with pytest.raises(MigrationRefused, match="nothing to baseline"):
        await LedgerMigrator(dsn).migrate(baseline_through=REAL_FILES[-1][:4])
    assert _recorded(dsn) == []


@needs_ledger_db
async def test_a_file_that_fails_is_rolled_back_and_not_recorded(dsn: str, tmp_path: Path) -> None:
    _file(tmp_path, "0001_a.sql", "CREATE TABLE a (n INT);\n")
    _file(tmp_path, "0002_bad.sql", "BEGIN;\nCREATE TABLE half (n INT);\nSELECT 1 / 0;\nCOMMIT;\n")

    with pytest.raises(MigrationFailed, match=r"0002_bad.sql failed and was rolled back"):
        await LedgerMigrator(dsn, tmp_path).migrate()
    assert _recorded(dsn) == ["0001_a.sql"]
    assert "half" not in _tables(dsn)

    _file(tmp_path, "0002_bad.sql", "CREATE TABLE half (n INT);\n")
    run = await LedgerMigrator(dsn, tmp_path).migrate()
    assert run.applied == ("0002_bad.sql",) and "half" in _tables(dsn)


@needs_ledger_db
async def test_two_runners_at_once_apply_each_file_once(dsn: str) -> None:
    runs = await asyncio.gather(LedgerMigrator(dsn).migrate(), LedgerMigrator(dsn).migrate())
    applied = [name for run in runs for name in run.applied]
    assert sorted(applied) == sorted(REAL_FILES)
    assert _recorded(dsn) == sorted(REAL_FILES)
