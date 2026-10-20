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
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, LiteralString, cast
from urllib.parse import urlparse

import psycopg
from opentelemetry import trace
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from compliance_rag.adapters.interfaces import (
    AssessmentRecord,
    ChatCitation,
    ChatExchange,
    ChatSummary,
    ChatTurn,
    Citation,
    LedgerStore,
    PolicyVersion,
    Profile,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
    SourceKind,
    SourceVersion,
    TurnOutcome,
    TurnRole,
    UnreadableCitation,
    UnreadableReason,
    UsageRecord,
    check_assessment_write,
    check_chat_exchange,
)
from compliance_rag.assessment.coverage import (
    CoverageRow,
    Outcome,
    question_json,
    question_of_json,
)
from compliance_rag.assessment.evidence import (
    Evidence,
    EvidenceProvenance,
    FactValue,
    Period,
    check_evidence_write,
)
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    DocumentClass,
    check_document_class,
    incomplete_json,
    incomplete_of_json,
    parse_stable_id,
    validate_library_instrument_id,
    validate_tenant_id,
)
from compliance_rag.settings import Settings

log = logging.getLogger(__name__)
_PROFILE_LISTS = ("jurisdictions", "sectors", "actor_roles", "data_categories", "regulators")


def _refuse_library(tenant_id: str, what: str) -> None:
    """Library law is unsubscribed from, never withdrawn or purged (ADR-0021);
    the database's CHECK would refuse it too, as a raw constraint error."""
    if tenant_id == PUBLIC_TENANT_ID:
        raise ValueError(f"library law is never {what} (ADR-0021)")


def _citation_row(c: Citation) -> dict[str, object]:
    """A chat turn's citation as its JSONB element. The incompleteness the
    member was shown rides in the element, only when there is any, as it does
    in every other store (ADR-0047): absent is complete. The column is JSONB
    and its constraints look at the array, not the elements, so this is not a
    schema change (#228)."""
    if isinstance(c, UnreadableCitation):  # read back from a corrupt row, never recorded
        raise ValueError("an unreadable citation isn't written back")
    row: dict[str, object] = {"clause_id": c.clause_id, "version_id": c.version_id}
    if c.incomplete:
        row["incomplete"] = incomplete_json(c.incomplete)
    return row


def _citation_of_row(raw: object) -> Citation:
    """A stored element back to its citation, or to an `UnreadableCitation` when
    it can't be: a corrupt row must not make the member's history unreachable
    (#232). The reason is a fixed word and the only parts of the element kept
    are a well-formed clause id and the version that agrees with it."""
    if not isinstance(raw, dict):
        return UnreadableCitation(UnreadableReason.NOT_AN_OBJECT)
    element = cast("dict[str, object]", raw)
    clause_id, version_id = element.get("clause_id"), element.get("version_id")
    try:
        if not isinstance(clause_id, str) or not clause_id.isprintable():
            # `isprintable` too: the id's pattern ends in `$`, which matches
            # before a trailing newline, and the id reaches a log line.
            raise ValueError("no clause id")
        _, clause_version, _ = parse_stable_id(clause_id)
    except ValueError:
        return UnreadableCitation(UnreadableReason.NO_CLAUSE)
    if version_id != clause_version:
        return UnreadableCitation(UnreadableReason.WRONG_VERSION, clause_id)
    try:
        incomplete = incomplete_of_json(element.get("incomplete"))
    except ValueError:  # pydantic's ValidationError is one; its message quotes the input
        return UnreadableCitation(UnreadableReason.BAD_INCOMPLETE, clause_id, clause_version)
    return ChatCitation(clause_id, clause_version, incomplete)


def _flag_unreadable(
    tenant_id: str, chat_id: str, turn_index: int, citations: tuple[Citation, ...]
) -> None:
    """Says on the request's span, and in the log, that a stored citation was
    unreadable: where it is (ids and position) and the fixed reason, never any
    of the row. An operator learns of the corruption; the member still gets
    their chat."""
    span = trace.get_current_span()
    for position, c in enumerate(citations):
        if not isinstance(c, UnreadableCitation):
            continue
        attributes: dict[str, str | int] = {
            "chat.tenant_id": tenant_id,
            "chat.id": chat_id,
            "chat.turn_index": turn_index,
            "chat.citation_index": position,
            "chat.citation_unreadable_reason": c.reason.value,
        }
        if c.clause_id is not None:
            attributes["chat.citation_clause_id"] = c.clause_id
        span.add_event("chat.citation_unreadable", attributes)
        log.warning(
            "a stored chat citation is unreadable: tenant=%s chat=%s turn=%d citation=%d reason=%s",
            tenant_id,
            chat_id,
            turn_index,
            position,
            c.reason.value,
        )


class LedgerUnreachable(ConnectionError):
    """The configured ledger didn't answer. The message names where it is,
    never who it connects as."""


class PostgresLedgerStore(LedgerStore):
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("no ledger configured (DATABASE_URL)")
        self._dsn = dsn

    @classmethod
    def from_settings(cls, settings: Settings) -> PostgresLedgerStore:
        return cls(settings.database_url.get_secret_value())

    @property
    def location(self) -> str:
        """`host:port/database`: where the ledger is, without credentials."""
        dsn = urlparse(self._dsn)
        return f"{dsn.hostname}:{dsn.port or 5432}{dsn.path}"

    async def ping(self) -> None:
        """Raise `LedgerUnreachable` unless the ledger answers: an operator
        script checks this before writing anything elsewhere (#144)."""
        try:
            await self._execute("SELECT 1", ())
        except psycopg.OperationalError as exc:
            raise LedgerUnreachable(
                f"the ledger at {self.location} can't be reached ({type(exc).__name__})"
            ) from None

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

    # ---- evidence (#114) --------------------------------------------------------

    async def record_evidence(self, evidence: Evidence) -> None:
        check_evidence_write(evidence)
        try:
            async with self._connection() as conn, conn.transaction():
                if evidence.supersedes is not None:
                    # The FK proves the old row exists; only a read proves it is
                    # about the same field. Locked, so the check holds to commit.
                    old = await (
                        await conn.execute(
                            "SELECT field FROM evidence WHERE tenant_id = %s AND evidence_id = %s "
                            "FOR UPDATE",
                            (evidence.tenant_id, evidence.supersedes),
                        )
                    ).fetchone()
                    if old is not None and old[0] != evidence.field:
                        raise ValueError(
                            f"evidence about {evidence.field} cannot supersede evidence about "
                            f"{old[0]}"
                        )
                await conn.execute(
                    "INSERT INTO evidence (tenant_id, evidence_id, field, value, period_start, "
                    "period_end, provenance_tier, source, asserted_by, supersedes, trace_id, "
                    "document_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        evidence.tenant_id,
                        evidence.evidence_id,
                        evidence.field,
                        Jsonb(_encode_fact(evidence.value)),
                        evidence.period.start,
                        evidence.period.end,
                        evidence.provenance.value,
                        evidence.source,
                        evidence.asserted_by,
                        evidence.supersedes,
                        evidence.trace_id,
                        evidence.document_id,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "evidence_one_successor":
                raise ValueError(f"evidence {evidence.supersedes} is already superseded") from exc
            raise ValueError(
                f"evidence {evidence.evidence_id} is already recorded (append-only)"
            ) from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            if exc.diag.constraint_name == "evidence_supersedes_fk":
                raise ValueError(
                    f"evidence {evidence.supersedes} to supersede isn't recorded"
                ) from exc
            raise ValueError(
                f"tenant {evidence.tenant_id!r} or document {evidence.document_id!r} isn't "
                "registered in the ledger"
            ) from exc
        except psycopg.errors.CheckViolation as exc:
            raise ValueError(f"evidence refused by {exc.diag.constraint_name}") from exc

    async def evidence(self, tenant_id: str) -> tuple[Evidence, ...]:
        rows = await self._fetchall(
            "SELECT evidence_id, field, value, period_start, period_end, provenance_tier, "
            "source, asserted_by, supersedes, trace_id, document_id, recorded_at "
            "FROM evidence WHERE tenant_id = %s ORDER BY recorded_at, evidence_id",
            (tenant_id,),
        )
        return tuple(
            Evidence(
                tenant_id=tenant_id,
                evidence_id=str(r[0]),
                field=str(r[1]),
                value=_decode_fact(r[2]),
                period=Period(start=r[3], end=r[4]),
                provenance=EvidenceProvenance(str(r[5])),
                source=str(r[6]),
                asserted_by=None if r[7] is None else str(r[7]),
                supersedes=None if r[8] is None else str(r[8]),
                trace_id=None if r[9] is None else str(r[9]),
                document_id=None if r[10] is None else str(r[10]),
                recorded_at=r[11],
            )
            for r in rows
        )

    async def record_coverage_assessment(
        self, record: AssessmentRecord, rows: tuple[CoverageRow, ...]
    ) -> None:
        """The assessment row and every coverage row in one transaction
        (#153): a row the database refuses rolls the assessment back with it,
        so there is never an assessment without its coverage."""
        check_assessment_write(record, rows)
        try:
            async with self._connection() as conn, conn.transaction():
                await conn.execute(
                    "INSERT INTO assessment (tenant_id, assessment_id, user_id, corpus_version, "
                    "corpus_sha, mapping_version, mapping_sha, as_of, trace_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        record.tenant_id,
                        record.assessment_id,
                        record.user_id,
                        record.corpus.tag,
                        record.corpus.sha,
                        record.mapping.tag,
                        record.mapping.sha,
                        record.as_of,
                        record.trace_id,
                    ),
                )
                async with conn.cursor() as cursor:
                    await cursor.executemany(
                        "INSERT INTO coverage (tenant_id, assessment_id, obligation_id, outcome, "
                        "reason, citations, questions, trace_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [
                            (
                                record.tenant_id,
                                record.assessment_id,
                                row.obligation_id,
                                row.outcome.value,
                                row.reason,
                                Jsonb(list(row.citations)),
                                Jsonb([question_json(q) for q in row.questions]),
                                record.trace_id,
                            )
                            for row in rows
                        ],
                    )
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ValueError(f"tenant {record.tenant_id!r} isn't registered in the ledger") from exc
        except psycopg.errors.UniqueViolation as exc:
            raise ValueError(f"assessment {record.assessment_id} is already recorded") from exc
        except (psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation) as exc:
            raise ValueError(f"assessment refused: {exc.diag.message_primary}") from exc

    async def coverage_rows(self, tenant_id: str, assessment_id: str) -> tuple[CoverageRow, ...]:
        rows = await self._fetchall(
            "SELECT obligation_id, outcome, reason, citations, questions FROM coverage "
            "WHERE tenant_id = %s AND assessment_id = %s "
            'ORDER BY obligation_id COLLATE "C"',  # bytewise, as the fake and the API sort
            (tenant_id, assessment_id),
        )
        return tuple(
            CoverageRow(
                obligation_id=str(r[0]),
                outcome=Outcome(str(r[1])),
                reason=str(r[2]),
                citations=tuple(str(c) for c in cast("list[object]", r[3])),
                questions=tuple(
                    question_of_json(cast("dict[str, object]", q))
                    for q in cast("list[object]", r[4])
                ),
            )
            for r in rows
        )

    async def get_assessment(self, tenant_id: str, assessment_id: str) -> AssessmentRecord | None:
        row = await self._fetchone(
            "SELECT user_id, corpus_version, corpus_sha, mapping_version, mapping_sha, as_of, "
            "trace_id FROM assessment WHERE tenant_id = %s AND assessment_id = %s",
            (tenant_id, assessment_id),
        )
        return None if row is None else assessment_from_row(tenant_id, assessment_id, row)

    # ---- chat history (#162) ----------------------------------------------------

    async def record_chat_exchange(self, exchange: ChatExchange) -> None:
        """The question and the answer in one transaction, numbered under a
        lock on the chat: a row the database refuses takes the other with it."""
        check_chat_exchange(exchange)
        try:
            async with self._connection() as conn, conn.transaction():
                # One writer per chat at a time, until commit. The next index is
                # read and then used, so two appends reading the same one would
                # collide on the key; a new chat has no row to lock, hence an
                # advisory lock. A tenant id holds no ':', so the key is unambiguous.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"chat:{exchange.tenant_id}:{exchange.chat_id}",),
                )
                last = await (
                    await conn.execute(
                        "SELECT turn_index, user_id FROM chat "
                        "WHERE tenant_id = %s AND chat_id = %s ORDER BY turn_index DESC LIMIT 1",
                        (exchange.tenant_id, exchange.chat_id),
                    )
                ).fetchone()
                if last is not None and last[1] != exchange.user_id:
                    raise ValueError(f"chat {exchange.chat_id} belongs to another member")
                async with conn.cursor() as cursor:
                    await cursor.executemany(
                        # `clock_timestamp()`, not the default `now()`: that is when
                        # the transaction began, before it waited for the lock, so
                        # a chat's latest turn could carry the earlier time.
                        "INSERT INTO chat (tenant_id, chat_id, turn_index, user_id, role, content, "
                        "outcome, citations, blocked, as_of, trace_id, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())",
                        [
                            (
                                turn.tenant_id,
                                turn.chat_id,
                                turn.turn_index,
                                turn.user_id,
                                turn.role.value,
                                turn.content,
                                None if turn.outcome is None else turn.outcome.value,
                                Jsonb([_citation_row(c) for c in turn.citations]),
                                Jsonb(list(turn.blocked)),
                                turn.as_of,
                                turn.trace_id,
                            )
                            for turn in exchange.turns(0 if last is None else int(last[0]) + 1)
                        ],
                    )
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ValueError(
                f"tenant {exchange.tenant_id!r} isn't registered in the ledger"
            ) from exc
        except (psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation) as exc:
            # Not `from exc`: Postgres' message carries the failing row, which is
            # the member's question and what they were shown, and a chain is
            # what `log.exception` prints.
            raise ValueError(f"chat turn refused: {exc.diag.message_primary}") from None

    async def chat_turns(self, tenant_id: str, user_id: str, chat_id: str) -> tuple[ChatTurn, ...]:
        rows = await self._fetchall(
            "SELECT turn_index, role, content, outcome, citations, blocked, as_of, trace_id, "
            "created_at FROM chat WHERE tenant_id = %s AND user_id = %s AND chat_id = %s "
            "ORDER BY turn_index",
            (tenant_id, user_id, chat_id),
        )
        turns: list[ChatTurn] = []
        for r in rows:
            citations = tuple(_citation_of_row(c) for c in cast("list[object]", r[4]))
            _flag_unreadable(tenant_id, chat_id, int(r[0]), citations)
            turns.append(
                ChatTurn(
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    turn_index=int(r[0]),
                    user_id=user_id,
                    role=TurnRole(str(r[1])),
                    content=None if r[2] is None else str(r[2]),
                    outcome=None if r[3] is None else TurnOutcome(str(r[3])),
                    citations=citations,
                    blocked=tuple(str(b) for b in cast("list[object]", r[5])),
                    as_of=r[6],
                    trace_id=str(r[7]),
                    created_at=r[8],
                )
            )
        return tuple(turns)

    async def chats(self, tenant_id: str, user_id: str) -> tuple[ChatSummary, ...]:
        rows = await self._fetchall(
            "SELECT chat_id, min(created_at), max(created_at), count(*), "
            "(array_agg(content ORDER BY turn_index))[1] FROM chat "
            "WHERE tenant_id = %s AND user_id = %s GROUP BY chat_id "
            'ORDER BY max(created_at) DESC, chat_id COLLATE "C"',  # ties by id, as the fake
            (tenant_id, user_id),
        )
        return tuple(
            ChatSummary(
                chat_id=str(r[0]),
                started_at=r[1],
                updated_at=r[2],
                turns=int(r[3]),
                first_question=str(r[4]),
            )
            for r in rows
        )

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

    async def subscribe(
        self, *, tenant_id: str, instrument_id: str, subscribed_by: str, trace_id: str | None = None
    ) -> None:
        validate_tenant_id(tenant_id)
        validate_library_instrument_id(instrument_id)
        try:
            await self._execute(
                "INSERT INTO tenant_instrument (tenant_id, instrument_id, subscribed_by, trace_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, instrument_id) DO NOTHING",
                (tenant_id, instrument_id, subscribed_by, trace_id),
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

    async def count_findings_citing_clauses(self, clause_ids: frozenset[str]) -> int:
        if not clause_ids:
            return 0
        row = await self._fetchone(
            "SELECT count_findings_citing_clauses(%s)", (sorted(clause_ids),)
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
        document_class: DocumentClass,
    ) -> None:
        check_document_class(tenant_id, document_class)
        scope = "public" if tenant_id == PUBLIC_TENANT_ID else "tenant"
        try:
            async with self._connection() as conn:
                await conn.execute(
                    "INSERT INTO instrument "
                    "(tenant_id, instrument_id, title, jurisdiction, scope, document_class) "
                    "VALUES (%s, %s, %s, 'AU', %s, %s) "
                    "ON CONFLICT (tenant_id, instrument_id) DO NOTHING",
                    (tenant_id, instrument_id, instrument_id, scope, document_class.value),
                )
                # An instrument recorded before 0011 has no class: this
                # version states it. One already classified must agree.
                cursor = await conn.execute(
                    "UPDATE instrument SET document_class = COALESCE(document_class, %s) "
                    "WHERE tenant_id = %s AND instrument_id = %s RETURNING document_class",
                    (document_class.value, tenant_id, instrument_id),
                )
                stored = await cursor.fetchone()
                if stored is not None and stored[0] != document_class.value:
                    raise ValueError(f"{instrument_id} is {stored[0]}, not {document_class.value}")
                await conn.execute(
                    "INSERT INTO instrument_version "
                    "(tenant_id, instrument_id, version_id, title, valid_from, supersedes) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
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
            # Version-and-retire ends a version only by supersession (ADR-0021);
            # status is derived from these two (ADR-0029). Set once: the same
            # date again is a no-op, a different one matches no row.
            "UPDATE instrument_version SET valid_to = %s, end_reason = 'superseded' "
            "WHERE tenant_id = %s AND instrument_id = %s AND version_id = %s "
            "AND (valid_to IS NULL OR (valid_to = %s AND end_reason = 'superseded'))",
            (valid_to, tenant_id, instrument_id, version_id, valid_to),
        )
        if updated == 0:
            validity = await self.version_validity(
                tenant_id=tenant_id, instrument_id=instrument_id, version_id=version_id
            )
            if validity is None:
                raise KeyError(f"{instrument_id}:{version_id} was never recorded for {tenant_id}")
            raise ValueError(
                f"{instrument_id}:{version_id} already ended on {validity[1]}; "
                "valid_to is set once (D-05)"
            )

    async def policy_versions(self, tenant_id: str, *, as_of: dt.date) -> tuple[PolicyVersion, ...]:
        rows = await self._fetchall(
            "SELECT v.instrument_id, v.version_id, i.document_class "
            "FROM instrument_version v JOIN instrument i USING (tenant_id, instrument_id) "
            "WHERE v.tenant_id = %s AND v.valid_from <= %s "
            "AND (v.valid_to IS NULL OR %s < v.valid_to) "
            "AND v.withdrawn_at IS NULL AND v.purged_at IS NULL "
            "AND (i.document_class = 'internal_policy' OR i.document_class IS NULL) "
            'ORDER BY v.instrument_id COLLATE "C", v.version_id COLLATE "C"',
            (tenant_id, as_of, as_of),
        )
        unclassified = sorted({str(r[0]) for r in rows if r[2] is None})
        if unclassified:
            raise ValueError(
                f"{tenant_id}: {unclassified} have no document class (recorded before "
                "0011): record the next version with its class, never a guessed one"
            )
        return tuple(PolicyVersion(str(r[0]), str(r[1])) for r in rows)

    async def document_class(self, *, tenant_id: str, instrument_id: str) -> DocumentClass | None:
        row = await self._fetchone(
            "SELECT document_class FROM instrument WHERE tenant_id = %s AND instrument_id = %s",
            (tenant_id, instrument_id),
        )
        return DocumentClass(str(row[0])) if row and row[0] is not None else None

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


def assessment_from_row(tenant_id: str, assessment_id: str, row: TupleRow) -> AssessmentRecord:
    """An `assessment` row — `user_id`, the corpus tag and sha, the mapping
    tag and sha, `as_of`, `trace_id` — as a record. One written before 0013
    (tests only: no deployed ledger holds one) names no commit, and is refused
    rather than read as if it did."""
    if row[0] is None or row[2] is None or row[4] is None:
        raise ValueError(
            f"assessment {assessment_id} was recorded before source versions (#153): "
            "it names no commit to reproduce it from"
        )
    return AssessmentRecord(
        tenant_id=tenant_id,
        assessment_id=assessment_id,
        user_id=str(row[0]),
        corpus=SourceVersion(SourceKind.CORPUS, sha=str(row[2]), tag=row[1]),
        mapping=SourceVersion(SourceKind.MAPPING, sha=str(row[4]), tag=row[3]),
        as_of=row[5],
        trace_id=str(row[6]),
    )


def _encode_fact(value: FactValue) -> object:
    """JSON for a fact value. A date is tagged so it reads back as a date,
    not a string that happens to look like one."""
    if isinstance(value, dt.date):
        return {"date": value.isoformat()}
    if isinstance(value, tuple):
        return list(value)
    return value


def _decode_fact(raw: object) -> FactValue:
    if isinstance(raw, dict):
        tagged = cast("dict[str, object]", raw)
        if set(tagged) != {"date"} or not isinstance(tagged["date"], str):
            raise ValueError(f"unreadable evidence value {raw!r}")
        return dt.date.fromisoformat(tagged["date"])
    if isinstance(raw, list):
        return tuple(str(v) for v in cast("list[object]", raw))
    if isinstance(raw, bool | int | float | str):
        return raw
    raise ValueError(f"unreadable evidence value {raw!r}")
