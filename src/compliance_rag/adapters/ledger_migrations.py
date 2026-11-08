"""The ledger's schema, applied by a tracked runner (issue #293).

`db/ledger/*.sql` used to be applied by CI's `psql` loop or by docker-compose's
`initdb.d` mount, which leave no record of what was applied: re-running either
against a built database fails at 0002, whose `ADD CONSTRAINT` is unguarded.
This runner keeps that record in `schema_migration` (file name, sha256 of its
bytes, when it was applied), so a database can be brought up, or brought up to
date, by running it again and again.

- **Each unapplied file runs in its own transaction**, with its record written
  in that same transaction: a file is applied and recorded, or neither. The
  files carry their own `BEGIN;`/`COMMIT;` (so `psql -f` stays atomic for each
  of them); the runner takes those lines out and opens the transaction itself,
  because a `COMMIT;` inside the file would commit before the record is
  written, and a crash between the two would leave a file applied and
  unrecorded, which the next run would apply again.
- **An applied file is never edited.** A recorded file whose bytes changed, or
  that is no longer in the directory, refuses the whole run before anything is
  applied. So does an unapplied file that sorts before one already applied: the
  files' order is part of what they mean.
- **`baseline_through`** is for a database built before the runner existed
  (a local `crag-test-pg`, a docker-compose volume): it records the files up to
  and including that number as applied, without running them, and the run
  carries on with whatever follows. It is refused on a database with no ledger
  tables, where it would record a schema that isn't there.
- One runner at a time: a session advisory lock is held for the run.

This is the one place the repository runs DDL, and it is an adapter, so the
rule that no SQL is written outside `adapters/` holds. The DDL itself lives in
tracked `.sql` files; nothing here builds a statement from input.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import LiteralString, cast

import psycopg

from compliance_rag.adapters.postgres_ledger_store import LedgerUnreachable
from compliance_rag.settings import Settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "ledger"
LOCK_KEY = 2930001
"""The advisory lock one runner holds for its whole run."""

_FILE_NAME = re.compile(r"(?P<number>\d{4})_[a-z0-9_]+\.sql")
_OWN_TRANSACTION = re.compile(r"(?:BEGIN|COMMIT);[ \t]*")
"""A line that is only `BEGIN;` or `COMMIT;`: the transaction a file opens for
itself, which the runner opens instead."""
_OTHER_CONTROL = re.compile(
    r"[ \t]*(?:ROLLBACK|ABORT|BEGIN|COMMIT|START\s+TRANSACTION|SAVEPOINT\s+\S+|"
    r"RELEASE(?:\s+SAVEPOINT)?\s+\S+)(?:\s+(?:WORK|TRANSACTION))?[ \t]*;[ \t]*",
    re.IGNORECASE,
)
"""A top-level line that is only a transaction-control statement, left once the
file's own is taken out. The runner can't keep a file atomic with its record if
the file ends the transaction some other way, so it refuses the file. `END;` is
not here: on a line of its own it is far more often the close of a function
body than the SQL synonym of COMMIT."""
_DOLLAR_QUOTE = re.compile(r"\$[A-Za-z_]*\$")

_CREATE_TABLE: LiteralString = (
    "CREATE TABLE IF NOT EXISTS schema_migration ("
    "filename TEXT PRIMARY KEY, "
    "sha256 TEXT NOT NULL, "
    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
)


class MigrationRefused(RuntimeError):
    """The run was refused before anything was applied."""


class MigrationFailed(RuntimeError):
    """A file's own SQL failed. It was rolled back and not recorded; the files
    before it stay applied."""


@dataclass(frozen=True, slots=True)
class MigrationFile:
    name: str
    number: str
    sha256: str
    sql: str


@dataclass(frozen=True, slots=True)
class MigrationRun:
    """What one run did."""

    applied: tuple[str, ...]
    """Files run, in order."""
    baselined: tuple[str, ...]
    """Files recorded as applied without running them."""
    already_applied: int
    """Files the database already had recorded when the run began."""


def read_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[MigrationFile, ...]:
    """Every `NNNN_name.sql` in `directory`, in filename order. A file that
    isn't named so, or a number used twice, is refused."""
    files: list[MigrationFile] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationRefused(f"{path.name} isn't named NNNN_name.sql")
        raw = path.read_bytes()
        files.append(
            MigrationFile(
                name=path.name,
                number=match["number"],
                sha256=hashlib.sha256(raw).hexdigest(),
                sql=raw.decode("utf-8"),
            )
        )
    numbers = [f.number for f in files]
    repeated = sorted({n for n in numbers if numbers.count(n) > 1})
    if repeated:
        raise MigrationRefused(f"migration number(s) used twice: {', '.join(repeated)}")
    if not files:
        raise MigrationRefused(f"no migrations in {directory}")
    return tuple(files)


def _top_level_lines(sql: str) -> list[tuple[str, bool]]:
    """Each line of `sql` and whether it starts outside any dollar-quoted body
    (`$$ ... $$`, `$f$ ... $f$`), where a `BEGIN`, `END;` or `COMMIT;` belongs
    to a function or procedure and not to the file's own transaction. A `--`
    comment is not scanned for quotes."""
    open_tag: str | None = None
    lines: list[tuple[str, bool]] = []
    for line in sql.splitlines(keepends=True):
        lines.append((line, open_tag is None))
        code = line.split("--", 1)[0] if open_tag is None else line
        for quote in _DOLLAR_QUOTE.finditer(code):
            if open_tag is None:
                open_tag = quote.group()
            elif quote.group() == open_tag:
                open_tag = None
    return lines


def transaction_body(migration: MigrationFile) -> str:
    """The file's statements without the top-level `BEGIN;` and `COMMIT;`
    lines it wraps itself in. Refused if it ends the transaction in any other
    way: another transaction-control statement on a top-level line of its own.
    A function body that has its own `BEGIN ... END` is not touched."""
    kept: list[str] = []
    for line, top_level in _top_level_lines(migration.sql):
        text = line.rstrip("\r\n")
        if top_level and _OWN_TRANSACTION.fullmatch(text):
            continue
        if top_level and _OTHER_CONTROL.fullmatch(text):
            raise MigrationRefused(
                f"{migration.name} controls its own transaction ({text.strip()!r}): "
                "only a BEGIN; and COMMIT; line are allowed"
            )
        kept.append(line)
    return "".join(kept)


class LedgerMigrator:
    def __init__(self, dsn: str, directory: Path = MIGRATIONS_DIR) -> None:
        if not dsn:
            raise ValueError("no ledger configured (DATABASE_URL)")
        self._dsn = dsn
        self._directory = directory

    @classmethod
    def from_settings(cls, settings: Settings, directory: Path = MIGRATIONS_DIR) -> LedgerMigrator:
        return cls(settings.database_url.get_secret_value(), directory)

    async def migrate(self, *, baseline_through: str | None = None) -> MigrationRun:
        files = read_migrations(self._directory)
        if baseline_through is not None and baseline_through not in {f.number for f in files}:
            raise MigrationRefused(
                f"--baseline-through {baseline_through!r} isn't the number of a file in "
                f"{self._directory.name}/"
            )
        # Every file's body is checked before the database is touched.
        for f in files:
            transaction_body(f)
        try:
            conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        except psycopg.OperationalError as exc:
            raise LedgerUnreachable(f"the ledger can't be reached ({type(exc).__name__})") from None
        async with conn:
            await conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
            await conn.execute(_CREATE_TABLE)
            recorded = await self._recorded(conn)
            self._refuse_if_inconsistent(files, recorded)

            baselined: tuple[MigrationFile, ...] = ()
            if baseline_through is not None:
                baselined = tuple(
                    f for f in files if f.number <= baseline_through and f.name not in recorded
                )
                if baselined and not await self._has_ledger_tables(conn):
                    raise MigrationRefused(
                        "the database has no ledger tables, so there is nothing to baseline: "
                        "run without --baseline-through to build it"
                    )
            skipped = {f.name for f in baselined}
            pending = tuple(f for f in files if f.name not in recorded and f.name not in skipped)

            async with conn.transaction():
                for f in baselined:
                    await self._record(conn, f)
            applied: list[str] = []
            for f in pending:
                try:
                    async with conn.transaction():
                        await conn.execute(cast("LiteralString", transaction_body(f)))
                        await self._record(conn, f)
                except psycopg.Error as exc:
                    position = exc.diag.statement_position
                    raise MigrationFailed(
                        f"{f.name} failed and was rolled back ({type(exc).__name__}, "
                        f"SQLSTATE {exc.sqlstate or 'unknown'}: {exc.diag.message_primary or exc}"
                        + (f", at character {position} of its statement" if position else "")
                        + f"); {len(applied)} file(s) before it stay applied"
                    ) from exc
                applied.append(f.name)
            return MigrationRun(
                applied=tuple(applied),
                baselined=tuple(f.name for f in baselined),
                already_applied=len(recorded),
            )

    @staticmethod
    async def _recorded(conn: psycopg.AsyncConnection[tuple[object, ...]]) -> dict[str, str]:
        cursor = await conn.execute("SELECT filename, sha256 FROM schema_migration")
        return {str(name): str(sha) for name, sha in await cursor.fetchall()}

    @staticmethod
    async def _has_ledger_tables(conn: psycopg.AsyncConnection[tuple[object, ...]]) -> bool:
        cursor = await conn.execute("SELECT to_regclass('tenant') IS NOT NULL")
        row = await cursor.fetchone()
        return bool(row and row[0])

    @staticmethod
    async def _record(
        conn: psycopg.AsyncConnection[tuple[object, ...]], migration: MigrationFile
    ) -> None:
        await conn.execute(
            "INSERT INTO schema_migration (filename, sha256) VALUES (%s, %s)",
            (migration.name, migration.sha256),
        )

    @staticmethod
    def _refuse_if_inconsistent(files: tuple[MigrationFile, ...], recorded: dict[str, str]) -> None:
        """Nothing is applied while any applied file has changed or gone, or
        a file that isn't applied belongs before one that is."""
        by_name = {f.name: f for f in files}
        problems: list[str] = []
        for name, sha in sorted(recorded.items()):
            current = by_name.get(name)
            if current is None:
                problems.append(f"{name} is applied but is no longer in the directory")
            elif current.sha256 != sha:
                problems.append(f"{name} is applied and has changed since")
        if recorded:
            latest = max(recorded)
            problems.extend(
                f"{f.name} isn't applied but sorts before {latest}, which is"
                for f in files
                if f.name not in recorded and f.name < latest
            )
        if problems:
            raise MigrationRefused(
                "nothing was applied: "
                + "; ".join(problems)
                + ". An applied migration is never edited or removed: add a new file."
            )
