"""`LedgerStore` on Postgres (issue #69) — the ledger is the one store that
can't be rebuilt (D-09), and since ADR-0020/ADR-0021 it carries real
guarantees: subscriptions decide what a tenant can retrieve; version validity
is what the verifier trusts (ADR-0021 mechanism 3); withdraw/purge markers and
the removal audit are the record of who removed what.

Every method opens its own short connection and commits on success. The
build-phase volume makes a pool unnecessary; one connection per call is
correct under concurrency and easy to reason about.

Behaviour matches `FakeLedgerStore` (the shared contracts run against both),
with the database adding what a dictionary can't: a subscription must name an
instrument the library holds, and a version row needs its tenant registered.
Both surface as `ValueError` with the reason, not a raw driver error.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, LiteralString

import psycopg
from psycopg.rows import TupleRow

from compliance_rag.adapters.interfaces import (
    LedgerStore,
    Profile,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
    UsageRecord,
)
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    validate_library_instrument_id,
    validate_tenant_id,
)
from compliance_rag.settings import Settings

_PROFILE_LISTS = ("jurisdictions", "sectors", "actor_roles", "data_categories", "regulators")


def _refuse_library(tenant_id: str, what: str) -> None:
    """Library law is unsubscribed from, never withdrawn or purged (ADR-0021);
    the database's CHECK would refuse it too, as a raw constraint error."""
    if tenant_id == PUBLIC_TENANT_ID:
        raise ValueError(f"library law is never {what} (ADR-0021)")


class PostgresLedgerStore(LedgerStore):
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("no ledger configured (DATABASE_URL)")
        self._dsn = dsn

    @classmethod
    def from_settings(cls, settings: Settings) -> PostgresLedgerStore:
        return cls(settings.database_url.get_secret_value())

    @asynccontextmanager
    async def _connection(self) -> AsyncGenerator[psycopg.AsyncConnection[TupleRow]]:
        async with await psycopg.AsyncConnection.connect(self._dsn) as conn:
            yield conn  # commits on clean exit, rolls back on an exception

    async def _execute(self, sql: LiteralString, params: tuple[Any, ...]) -> int:  # Any: SQL params
        async with self._connection() as conn:
            cursor = await conn.execute(sql, params)
            return cursor.rowcount

    async def _fetchone(
        self,
        sql: LiteralString,
        params: tuple[Any, ...],  # Any: SQL params
    ) -> TupleRow | None:
        async with self._connection() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchone()

    async def _fetchall(
        self,
        sql: LiteralString,
        params: tuple[Any, ...],  # Any: SQL params
    ) -> list[TupleRow]:
        async with self._connection() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchall()

    # ---- profiles and assessments -------------------------------------------

    async def get_profile(self, tenant_id: str) -> Profile | None:
        row = await self._fetchone("SELECT profile FROM tenant WHERE tenant_id = %s", (tenant_id,))
        if row is None or row[0] is None:
            return None
        data: dict[str, Any] = row[0]  # Any: JSON document
        return Profile(
            tenant_id=tenant_id,
            size_band=data.get("size_band"),
            **{k: tuple(data.get(k, ())) for k in _PROFILE_LISTS},
        )

    async def put_profile(self, profile: Profile) -> None:
        document: dict[str, object] = {k: list(getattr(profile, k)) for k in _PROFILE_LISTS}
        document["size_band"] = profile.size_band
        await self._execute(
            "INSERT INTO tenant (tenant_id, name, profile) VALUES (%s, %s, %s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET profile = EXCLUDED.profile",
            (profile.tenant_id, profile.tenant_id, json.dumps(document)),
        )

    async def record_assessment(
        self,
        *,
        tenant_id: str,
        assessment_id: str,
        corpus_version: str,
        mapping_version: str,
        as_of: dt.date,
        trace_id: str,
    ) -> None:
        await self._execute(
            "INSERT INTO assessment "
            "(tenant_id, assessment_id, corpus_version, mapping_version, as_of, trace_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (tenant_id, assessment_id, corpus_version, mapping_version, as_of, trace_id),
        )

    async def get_assessment(self, tenant_id: str, assessment_id: str) -> dict[str, object] | None:
        row = await self._fetchone(
            "SELECT corpus_version, mapping_version, as_of, trace_id FROM assessment "
            "WHERE tenant_id = %s AND assessment_id = %s",
            (tenant_id, assessment_id),
        )
        if row is None:
            return None
        return {
            "tenant_id": tenant_id,
            "assessment_id": assessment_id,
            "corpus_version": row[0],
            "mapping_version": row[1],
            "as_of": row[2],
            "trace_id": row[3],
        }

    # ---- usage (#31) ----------------------------------------------------------

    async def record_usage(self, usage: UsageRecord) -> None:
        await self._execute(
            "INSERT INTO usage (tenant_id, usage_id, trace_id, model, input_tokens, "
            "output_tokens, cached_prefix_hit, cost_usd) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                usage.tenant_id,
                usage.usage_id,
                usage.trace_id,
                usage.model,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_prefix_hit,
                usage.cost_usd,
            ),
        )

    async def usage(self, tenant_id: str) -> tuple[UsageRecord, ...]:
        rows = await self._fetchall(
            "SELECT usage_id, trace_id, model, input_tokens, output_tokens, cached_prefix_hit, "
            "cost_usd FROM usage WHERE tenant_id = %s ORDER BY created_at, usage_id",
            (tenant_id,),
        )
        return tuple(
            UsageRecord(
                tenant_id=tenant_id,
                usage_id=str(r[0]),
                trace_id=str(r[1]),
                model=str(r[2]),
                input_tokens=int(r[3]),
                output_tokens=int(r[4]),
                cached_prefix_hit=bool(r[5]),
                cost_usd=float(r[6]),
            )
            for r in rows
        )

    # ---- subscriptions (ADR-0020) -------------------------------------------

    async def subscribe(self, *, tenant_id: str, instrument_id: str, subscribed_by: str) -> None:
        validate_tenant_id(tenant_id)
        validate_library_instrument_id(instrument_id)
        try:
            await self._execute(
                "INSERT INTO tenant_instrument (tenant_id, instrument_id, subscribed_by) "
                "VALUES (%s, %s, %s) ON CONFLICT (tenant_id, instrument_id) DO NOTHING",
                (tenant_id, instrument_id, subscribed_by),
            )
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ValueError(
                f"cannot subscribe {tenant_id!r} to {instrument_id!r}: the tenant isn't "
                "registered or the library doesn't hold that instrument"
            ) from exc

    async def unsubscribe(self, *, tenant_id: str, instrument_id: str) -> None:
        await self._execute(
            "DELETE FROM tenant_instrument WHERE tenant_id = %s AND instrument_id = %s",
            (tenant_id, instrument_id),
        )

    async def subscribed_instruments(self, tenant_id: str) -> frozenset[str]:
        rows = await self._fetchall(
            "SELECT instrument_id FROM tenant_instrument WHERE tenant_id = %s", (tenant_id,)
        )
        return frozenset(str(r[0]) for r in rows)

    async def count_findings_citing(self, tenant_id: str, stable_id_prefix: str) -> int:
        row = await self._fetchone(
            "SELECT count_findings_citing(%s, %s)", (tenant_id, stable_id_prefix)
        )
        return int(row[0]) if row else 0

    # ---- versions (ADR-0021) ------------------------------------------------

    async def record_version(
        self,
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str,
        valid_from: dt.date,
        supersedes: str | None,
    ) -> None:
        scope = "public" if tenant_id == PUBLIC_TENANT_ID else "tenant"
        try:
            async with self._connection() as conn:
                await conn.execute(
                    "INSERT INTO instrument "
                    "(tenant_id, instrument_id, title, jurisdiction, scope) "
                    "VALUES (%s, %s, %s, 'AU', %s) "
                    "ON CONFLICT (tenant_id, instrument_id) DO NOTHING",
                    (tenant_id, instrument_id, instrument_id, scope),
                )
                await conn.execute(
                    "INSERT INTO instrument_version "
                    "(tenant_id, instrument_id, version_id, title, status, valid_from, supersedes) "
                    "VALUES (%s, %s, %s, %s, 'in_force', %s, %s)",
                    (tenant_id, instrument_id, version_id, instrument_id, valid_from, supersedes),
                )
        except psycopg.errors.UniqueViolation as exc:
            # Re-recording would reset valid_to and silently un-retire it.
            raise ValueError(
                f"{instrument_id}:{version_id} is already recorded for {tenant_id}"
            ) from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ValueError(f"tenant {tenant_id!r} isn't registered in the ledger") from exc

    async def end_version(
        self, *, tenant_id: str, instrument_id: str, version_id: str, valid_to: dt.date
    ) -> None:
        updated = await self._execute(
            "UPDATE instrument_version SET valid_to = %s "
            "WHERE tenant_id = %s AND instrument_id = %s AND version_id = %s",
            (valid_to, tenant_id, instrument_id, version_id),
        )
        if updated == 0:
            raise KeyError(f"{instrument_id}:{version_id} was never recorded for {tenant_id}")

    async def version_validity(
        self, *, tenant_id: str, instrument_id: str, version_id: str
    ) -> tuple[dt.date, dt.date | None] | None:
        row = await self._fetchone(
            "SELECT valid_from, valid_to FROM instrument_version "
            "WHERE tenant_id = %s AND instrument_id = %s AND version_id = %s",
            (tenant_id, instrument_id, version_id),
        )
        return (row[0], row[1]) if row else None

    async def mark_withdrawn(
        self, *, tenant_id: str, instrument_id: str, version_id: str, withdrawn_by: str
    ) -> None:
        """Idempotent: a second withdrawal keeps the first one's who and when."""
        _refuse_library(tenant_id, "withdrawn")
        updated = await self._execute(
            "UPDATE instrument_version SET withdrawn_at = COALESCE(withdrawn_at, now()), "
            "withdrawn_by = COALESCE(withdrawn_by, %s) "
            "WHERE tenant_id = %s AND instrument_id = %s AND version_id = %s",
            (withdrawn_by, tenant_id, instrument_id, version_id),
        )
        if updated == 0:
            raise KeyError(f"{instrument_id}:{version_id} was never recorded for {tenant_id}")

    async def is_withdrawn(self, *, tenant_id: str, instrument_id: str, version_id: str) -> bool:
        row = await self._fetchone(
            "SELECT withdrawn_at IS NOT NULL FROM instrument_version "
            "WHERE tenant_id = %s AND instrument_id = %s AND version_id = %s",
            (tenant_id, instrument_id, version_id),
        )
        return bool(row and row[0])

    async def mark_purged(
        self,
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str | None,
        purged_by: str,
        reason: str,
    ) -> None:
        """`version_id` None marks every version row of the instrument. Nothing
        recorded to mark is a `KeyError`, as for `mark_withdrawn`."""
        _refuse_library(tenant_id, "purged")
        updated = await self._execute(
            "UPDATE instrument_version SET purged_at = now(), purged_by = %s, purge_reason = %s "
            "WHERE tenant_id = %s AND instrument_id = %s "
            "AND (%s::text IS NULL OR version_id = %s)",
            (purged_by, reason, tenant_id, instrument_id, version_id, version_id),
        )
        if updated == 0:
            raise KeyError(f"{instrument_id}:{version_id} was never recorded for {tenant_id}")

    async def purge_marker(
        self, *, tenant_id: str, instrument_id: str, version_id: str
    ) -> tuple[str, str] | None:
        """`(purged_by, reason)` for one version, or None."""
        row = await self._fetchone(
            "SELECT purged_by, purge_reason FROM instrument_version "
            "WHERE tenant_id = %s AND instrument_id = %s AND version_id = %s "
            "AND purged_at IS NOT NULL",
            (tenant_id, instrument_id, version_id),
        )
        return (str(row[0]), str(row[1])) if row else None

    # ---- removal audit (ADR-0021) -------------------------------------------

    async def record_removal_audit(self, entry: RemovalAuditEntry) -> None:
        await self._execute(
            "INSERT INTO corpus_removal_audit (audit_id, tenant_id, instrument_id, version_id, "
            "action, outcome, actor, reason, findings_citing, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                entry.audit_id,
                entry.tenant_id,
                entry.instrument_id,
                entry.version_id,
                entry.action.value,
                entry.outcome.value,
                entry.actor,
                entry.reason,
                entry.findings_citing,
                entry.detail,
            ),
        )

    async def removal_audit(self, tenant_id: str) -> tuple[RemovalAuditEntry, ...]:
        rows = await self._fetchall(
            "SELECT audit_id, tenant_id, instrument_id, version_id, action, outcome, actor, "
            "reason, findings_citing, detail FROM corpus_removal_audit "
            "WHERE tenant_id = %s ORDER BY seq",
            (tenant_id,),
        )
        return tuple(
            RemovalAuditEntry(
                audit_id=str(r[0]),
                tenant_id=str(r[1]),
                instrument_id=str(r[2]),
                version_id=r[3],
                action=RemovalAction(r[4]),
                outcome=RemovalOutcome(r[5]),
                actor=str(r[6]),
                reason=str(r[7]),
                findings_citing=r[8],
                detail=str(r[9]),
            )
            for r in rows
        )
