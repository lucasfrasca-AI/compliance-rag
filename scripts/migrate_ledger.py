"""Bring the ledger's schema up to date (#293): every `db/ledger/*.sql` not yet
recorded in `schema_migration`, in filename order, each in its own transaction.
Safe to run again: a database that is up to date is left as it is.

Reads DATABASE_URL through `Settings` and never prints it.

    uv run python -m scripts.migrate_ledger
    uv run python -m scripts.migrate_ledger --expect-up-to-date   # CI's second run

A database built before this runner existed (a local `crag-test-pg`, a
docker-compose volume made with the old `initdb.d` mount) already holds files
up to some number. Record those as applied, without running them, and apply
the rest:

    uv run python -m scripts.migrate_ledger --baseline-through 0019

An applied file that was edited or removed refuses the run before anything is
applied: add a new migration instead.
"""

from __future__ import annotations

import argparse
import asyncio

from compliance_rag.adapters.ledger_migrations import (
    LedgerMigrator,
    MigrationFailed,
    MigrationRefused,
)
from compliance_rag.adapters.postgres_ledger_store import LedgerUnreachable
from compliance_rag.settings import Settings


async def main(*, baseline_through: str | None, expect_up_to_date: bool) -> int:
    migrator = LedgerMigrator.from_settings(Settings())
    try:
        run = await migrator.migrate(baseline_through=baseline_through)
    except (MigrationRefused, MigrationFailed, LedgerUnreachable) as exc:
        print(f"migrate_ledger: {exc}")
        return 2
    for name in run.baselined:
        print(f"baselined (recorded, not run): {name}")
    for name in run.applied:
        print(f"applied: {name}")
    print(
        f"{len(run.applied)} applied, {len(run.baselined)} baselined, "
        f"{run.already_applied} already applied"
    )
    if expect_up_to_date and (run.applied or run.baselined):
        print("migrate_ledger: --expect-up-to-date, but this run changed the database")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--baseline-through",
        metavar="NNNN",
        default=None,
        help="record the files up to and including this number as applied, without running them",
    )
    parser.add_argument(
        "--expect-up-to-date",
        action="store_true",
        help="exit 1 if the run applied or baselined anything: proves a re-run changes nothing",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            main(
                baseline_through=arguments.baseline_through,
                expect_up_to_date=arguments.expect_up_to_date,
            )
        )
    )
