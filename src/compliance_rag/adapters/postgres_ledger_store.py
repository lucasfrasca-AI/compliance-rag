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
from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from typing import Any, Literal, LiteralString, cast
from urllib.parse import urlparse

import psycopg
from opentelemetry import trace
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from compliance_rag.adapters.interfaces import (
    CHECK_PAGE_MAX,
    FINDINGS_RESERVATION_TTL,
    RESERVATION_TTL,
    AssessmentRecord,
    BlockedCitation,
    BlockedClause,
    ChatCitation,
    ChatExchange,
    ChatSummary,
    ChatTurn,
    CheckCursor,
    CheckListing,
    CheckNotExtracted,
    CheckRecord,
    CheckRun,
    CheckStage,
    Citation,
    ClaimExtractionRecord,
    ClaimRecord,
    ExtractionAlreadyReserved,
    FailureReason,
    FindingCitation,
    FindingOutcome,
    FindingRecord,
    FindingsAlreadyReserved,
    FindingsRecord,
    LedgerStore,
    PolicyVersion,
    PrefixMeasure,
    Profile,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
    SourceKind,
    SourceVersion,
    StoredNotInEnvironment,
    TurnOutcome,
    TurnRole,
    Unavailability,
    UnreadableCitation,
    UnreadableNotInEnvironment,
    UnreadableReason,
    UsageRecord,
    Verdict,
    WithheldReason,
    check_assessment_write,
    check_chat_exchange,
    check_check_write,
    check_claims_write,
    check_findings_write,
)
from compliance_rag.assessment.coverage import (
    CoverageRow,
    Outcome,
    question_json,
    question_of_json,
)
from compliance_rag.assessment.environment import NotInEnvironment
from compliance_rag.assessment.evidence import (
    Evidence,
    EvidenceProvenance,
    FactsSnapshot,
    Period,
    check_evidence_write,
    decode_fact,
    encode_fact,
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
    validate_title,
)
from compliance_rag.corpus.submitted_parser import SubmittedFormat
from compliance_rag.settings import GeneratorName, Settings

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


def _blocked_row(b: BlockedClause) -> str:
    """A blocked clause id as its JSONB element."""
    if isinstance(b, UnreadableCitation):  # read back from a corrupt row, never recorded
        raise ValueError("an unreadable blocked entry isn't written back")
    return b


def _stored_clause_id(raw: object) -> tuple[str, str] | None:
    """A stored clause id and the version it is of, or None when it isn't a
    stable id. `isprintable` as well as the grammar: a tenant-prefixed id's
    tenant part (`[^/:]+`) admits a line break, and a citation's id is kept and
    reaches a span attribute and the response."""
    if not isinstance(raw, str) or not raw.isprintable():
        return None
    try:
        _, version_id, _ = parse_stable_id(raw)
    except ValueError:
        return None
    return raw, version_id


def _blocked_of_row(raw: object) -> BlockedClause:
    """A stored `blocked` element back to its clause id, or to an
    `UnreadableCitation` in its place when it isn't a stable id (#234): a
    corrupt row must not make the member's history unreachable. Nothing of the
    element is kept, so a corrupt row's content never reaches a response or a
    log; the reason is the fixed word `no_clause`."""
    named = _stored_clause_id(raw)
    return UnreadableCitation(UnreadableReason.NO_CLAUSE) if named is None else named[0]


def _citation_of_row(raw: object) -> Citation:
    """A stored element back to its citation, or to an `UnreadableCitation` when
    it can't be: a corrupt row must not make the member's history unreachable
    (#232). The reason is a fixed word and the only parts of the element kept
    are a well-formed clause id and the version that agrees with it."""
    if not isinstance(raw, dict):
        return UnreadableCitation(UnreadableReason.NOT_AN_OBJECT)
    element = cast("dict[str, object]", raw)
    version_id = element.get("version_id")
    named = _stored_clause_id(element.get("clause_id"))
    if named is None:
        return UnreadableCitation(UnreadableReason.NO_CLAUSE)
    clause_id, clause_version = named
    if version_id != clause_version:
        return UnreadableCitation(UnreadableReason.WRONG_VERSION, clause_id)
    try:
        incomplete = incomplete_of_json(element.get("incomplete"))
    except ValueError:  # pydantic's ValidationError is one; its message quotes the input
        return UnreadableCitation(UnreadableReason.BAD_INCOMPLETE, clause_id, clause_version)
    return ChatCitation(clause_id, clause_version, incomplete)


def _flag_unreadable(
    tenant_id: str,
    chat_id: str,
    turn_index: int,
    entries: tuple[Citation, ...] | tuple[BlockedClause, ...],
    kind: Literal["citation", "blocked"],
) -> None:
    """Says on the request's span, and in the log, that a stored element of a
    turn's `kind` (`citation` or `blocked`) was unreadable: where it is (ids and
    position) and the fixed reason, never any of the row. An operator learns of
    the corruption; the member still gets their chat."""
    span = trace.get_current_span()
    for position, c in enumerate(entries):
        if not isinstance(c, UnreadableCitation):
            continue
        attributes: dict[str, str | int] = {
            "chat.tenant_id": tenant_id,
            "chat.id": chat_id,
            "chat.turn_index": turn_index,
            f"chat.{kind}_index": position,
            f"chat.{kind}_unreadable_reason": c.reason.value,
        }
        if c.clause_id is not None:
            attributes[f"chat.{kind}_clause_id"] = c.clause_id
        span.add_event(f"chat.{kind}_unreadable", attributes)
        log.warning(
            "a stored chat %s is unreadable: tenant=%s chat=%s turn=%d %s=%d reason=%s",
            kind,
            tenant_id,
            chat_id,
            turn_index,
            kind,
            position,
            c.reason.value,
        )


def _named_absent_row(n: StoredNotInEnvironment) -> dict[str, str | None]:
    """A named-instrument flag as its JSONB element (#252)."""
    if isinstance(n, UnreadableNotInEnvironment):  # read back from a corrupt row, never recorded
        raise ValueError("an unreadable flag isn't written back")
    return n.json()


def _detail_row(turn: ChatTurn) -> Jsonb | None:
    """A refusal's detail as its JSONB: the reason, or the measure, and
    nothing else (#289). Our own words and two numbers, never the provider's
    message. None on every other turn."""
    if turn.unavailable is not None:
        return Jsonb({"reason": turn.unavailable.value})
    if turn.prefix is not None:
        return Jsonb(
            {
                "estimated_tokens": turn.prefix.estimated_tokens,
                "cap_tokens": turn.prefix.cap_tokens,
            }
        )
    return None


def _generator_of_row(raw: object) -> GeneratorName | None:
    """The generator a turn was written under; the column's CHECK admits these three
    and NULL (`claude` is a dormant backend, and its earlier turns stay), so anything
    else would be a schema this code doesn't know."""
    if raw is None:
        return None
    if raw == "claude":
        return "claude"
    if raw == "gemini":
        return "gemini"
    if raw == "demo":
        return "demo"
    raise ValueError(f"a chat turn records generator {raw!r}, which this code doesn't know")


def _detail_of_row(raw: object) -> tuple[Unavailability | None, PrefixMeasure | None]:
    """A stored detail back to its reason or measure. The database's constraint
    admits nothing else, so a value that isn't one means the constraint was
    dropped: the turn keeps its outcome and loses the detail, rather than
    making the chat unreadable."""
    if not isinstance(raw, dict):
        return None, None
    detail = cast("dict[str, object]", raw)
    try:
        if "reason" in detail:
            return Unavailability(str(detail["reason"])), None
        estimated, cap = detail["estimated_tokens"], detail["cap_tokens"]
        if isinstance(estimated, int) and isinstance(cap, int):
            return None, PrefixMeasure(estimated, cap)
    except (KeyError, ValueError):
        pass
    return None, None


def _named_absent_of_row(raw: object) -> StoredNotInEnvironment:
    """A stored element back to its flag, or to an `UnreadableNotInEnvironment`
    in its place when it can't be (as #232 reads a citation): the turn still
    says something it named wasn't held, and nothing of the element is kept."""
    try:
        return NotInEnvironment.of_json(raw)
    except ValueError:
        return UnreadableNotInEnvironment()


def _flag_unreadable_absent(
    tenant_id: str, chat_id: str, turn_index: int, entries: tuple[StoredNotInEnvironment, ...]
) -> None:
    """Says on the span, and in the log, where a stored flag was unreadable:
    ids and position only, never any of the row."""
    span = trace.get_current_span()
    for position, n in enumerate(entries):
        if isinstance(n, UnreadableNotInEnvironment):
            span.add_event(
                "chat.not_in_environment_unreadable",
                {
                    "chat.tenant_id": tenant_id,
                    "chat.id": chat_id,
                    "chat.turn_index": turn_index,
                    "chat.not_in_environment_index": position,
                },
            )
            log.warning(
                "a stored chat flag is unreadable: tenant=%s chat=%s turn=%d position=%d",
                tenant_id,
                chat_id,
                turn_index,
                position,
            )


_CHECK_SELECT: LiteralString = (
    "SELECT tenant_id, check_id, user_id, instrument_id, version_id, filename, "
    "document_format, trace_id, created_at FROM submitted_check"
)


_CLAIM_SELECT: LiteralString = (
    "SELECT claim_index, passage, quote_start, quote_end, quote_sha256, summary, "
    "obligation_ids, policy_clause_ids, provenance FROM check_claim "
    "WHERE tenant_id = %s AND check_id = %s ORDER BY claim_index"
)


def _claim_of_row(row: TupleRow) -> ClaimRecord:
    return ClaimRecord(
        claim_index=int(row[0]),
        passage=str(row[1]),
        quote_start=int(row[2]),
        quote_end=int(row[3]),
        quote_sha256=str(row[4]),
        summary=str(row[5]),
        obligation_ids=tuple(str(i) for i in cast("list[object]", row[6])),
        policy_clause_ids=tuple(str(i) for i in cast("list[object]", row[7])),
        provenance=EvidenceProvenance(str(row[8])),
    )


def _run_of(status: object, fresh: bool) -> CheckRun | None:
    """A reservation's state for a list (`CheckRun`): failed, or pending and not
    yet past its time-out; a stale pending one is neither (its request died)."""
    if status == "failed":
        return CheckRun.FAILED
    return CheckRun.RUNNING if status == "pending" and fresh else None


def _check_of_row(row: TupleRow) -> CheckRecord:
    return CheckRecord(
        tenant_id=str(row[0]),
        check_id=str(row[1]),
        user_id=str(row[2]),
        instrument_id=str(row[3]),
        version_id=str(row[4]),
        filename=str(row[5]),
        document_format=SubmittedFormat(str(row[6])),
        trace_id=str(row[7]),
        created_at=row[8],
    )


async def _write_assessment(
    conn: psycopg.AsyncConnection[TupleRow], record: AssessmentRecord, rows: tuple[CoverageRow, ...]
) -> None:
    """The assessment row and its coverage rows, on the caller's transaction."""
    await conn.execute(
        "INSERT INTO assessment (tenant_id, assessment_id, user_id, corpus_version, "
        "corpus_sha, mapping_version, mapping_sha, as_of, trace_id, check_id, facts, "
        "facts_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
            record.check_id,
            None if record.facts is None else Jsonb(record.facts.json()),
            None if record.facts is None else record.facts.sha256,
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


def _finding_citation_row(c: FindingCitation) -> dict[str, object]:
    """A finding's citation as its JSONB element. `canonicalisation_version` is
    required by the table's own constraint (0003); the incompleteness rides in
    the element only when there is any, as a chat's does."""
    row: dict[str, object] = {
        "clause_id": c.clause_id,
        "version_id": c.version_id,
        "quoted_text": c.quoted_text,
        "canonicalisation_version": c.canonicalisation_version,
    }
    if c.incomplete:
        row["incomplete"] = incomplete_json(c.incomplete)
    return row


def _finding_citation_of_row(raw: object) -> FindingCitation:
    element = cast("dict[str, object]", raw)
    return FindingCitation(
        clause_id=str(element["clause_id"]),
        version_id=str(element["version_id"]),
        quoted_text=str(element["quoted_text"]),
        canonicalisation_version=int(cast("int", element["canonicalisation_version"])),
        incomplete=incomplete_of_json(element.get("incomplete")),
    )


_FINDING_SELECT: LiteralString = (
    "SELECT finding_id, claim_index, outcome, obligation_id, policy_clause_id, verdict, "
    "settle_by, citations, blocked, outcome_detail, unassessable_reason, provenance_tier, "
    "derived_from FROM finding WHERE tenant_id = %s AND assessment_id = %s "
    "ORDER BY claim_index, (obligation_id IS NULL), "
    'COALESCE(obligation_id, policy_clause_id) COLLATE "C"'
)


def _finding_of_row(row: TupleRow) -> FindingRecord:
    return FindingRecord(
        finding_id=str(row[0]),
        claim_index=int(row[1]),
        outcome=FindingOutcome(str(row[2])),
        obligation_id=None if row[3] is None else str(row[3]),
        policy_clause_id=None if row[4] is None else str(row[4]),
        verdict=None if row[5] is None else Verdict(str(row[5])),
        settle_by=None if row[6] is None else str(row[6]),
        citations=tuple(_finding_citation_of_row(c) for c in cast("list[object]", row[7])),
        blocked=tuple(
            BlockedCitation(str(b["clause_id"]), str(b["outcome"]))
            for b in cast("list[dict[str, object]]", row[8])
        ),
        detail=None
        if row[9] is None
        else (
            WithheldReason
            if FindingOutcome(str(row[2])) is FindingOutcome.WITHHELD
            else FailureReason
        )(str(row[9])),
        unassessable_reason=None if row[10] is None else str(row[10]),
        provenance=EvidenceProvenance(str(row[11])),
        derived_from=None if row[12] is None else str(row[12]),
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
                        Jsonb(encode_fact(evidence.value)),
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
                value=decode_fact(r[2]),
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
                await _write_assessment(conn, record, rows)
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
            "trace_id, check_id, facts, facts_sha256 FROM assessment "
            "WHERE tenant_id = %s AND assessment_id = %s",
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
                        "outcome, citations, blocked, as_of, trace_id, not_in_environment, "
                        "outcome_detail, generator, created_at) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "clock_timestamp())",
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
                                Jsonb([_blocked_row(b) for b in turn.blocked]),
                                turn.as_of,
                                turn.trace_id,
                                Jsonb([_named_absent_row(n) for n in turn.not_in_environment]),
                                _detail_row(turn),
                                turn.generator,
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

    async def record_check(self, record: CheckRecord) -> None:
        check_check_write(record)
        try:
            async with self._connection() as conn, conn.transaction():
                # The version must be a submitted document's: the foreign key
                # names the version, not its class.
                row = await (
                    await conn.execute(
                        "SELECT document_class FROM instrument "
                        "WHERE tenant_id = %s AND instrument_id = %s",
                        (record.tenant_id, record.instrument_id),
                    )
                ).fetchone()
                if row is not None and row[0] != DocumentClass.SUBMITTED_DOCUMENT.value:
                    raise ValueError(f"{record.instrument_id} is not a submitted document")
                await conn.execute(
                    "INSERT INTO submitted_check (tenant_id, check_id, user_id, instrument_id, "
                    "version_id, filename, document_format, trace_id, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())",
                    (
                        record.tenant_id,
                        record.check_id,
                        record.user_id,
                        record.instrument_id,
                        record.version_id,
                        record.filename,
                        record.document_format.value,
                        record.trace_id,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            raise ValueError(
                f"check {record.check_id} is already recorded, or "
                f"{record.instrument_id}:{record.version_id} is already checked"
            ) from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ValueError(
                f"tenant {record.tenant_id!r} isn't registered in the ledger, or "
                f"{record.instrument_id}:{record.version_id} isn't recorded: a check points at "
                "a document that is stored"
            ) from exc
        except psycopg.errors.CheckViolation as exc:
            raise ValueError(f"check refused: {exc.diag.constraint_name}") from None

    async def get_check(self, tenant_id: str, check_id: str) -> CheckRecord | None:
        row = await self._fetchone(
            f"{_CHECK_SELECT} WHERE tenant_id = %s AND check_id = %s", (tenant_id, check_id)
        )
        return None if row is None else _check_of_row(row)

    async def check_of_version(
        self, tenant_id: str, instrument_id: str, version_id: str
    ) -> CheckRecord | None:
        row = await self._fetchone(
            f"{_CHECK_SELECT} WHERE tenant_id = %s AND instrument_id = %s AND version_id = %s",
            (tenant_id, instrument_id, version_id),
        )
        return None if row is None else _check_of_row(row)

    async def list_checks(
        self,
        tenant_id: str,
        uid: str,
        *,
        admin: bool,
        limit: int,
        before: CheckCursor | None = None,
    ) -> tuple[CheckListing, ...]:
        if not 1 <= limit <= CHECK_PAGE_MAX:
            raise ValueError(f"limit is 1 to {CHECK_PAGE_MAX}, not {limit}")
        # One read: the stage and the run state come from two LEFT JOINs on the
        # reservations' primary keys, the title from the version the foreign key
        # guarantees, so the number of checks never changes the number of queries.
        # The order is `submitted_check_listing`'s (0027).
        select: LiteralString = (
            "SELECT c.check_id, c.user_id, c.filename, v.title, c.created_at, "
            "e.status, e.status = 'pending' AND e.reserved_at >= clock_timestamp() - %s, "
            "f.status, f.status = 'pending' AND f.reserved_at >= clock_timestamp() - %s "
            "FROM submitted_check c "
            "JOIN instrument_version v ON (v.tenant_id, v.instrument_id, v.version_id) = "
            "(c.tenant_id, c.instrument_id, c.version_id) "
            "LEFT JOIN check_extraction e "
            "ON (e.tenant_id, e.check_id) = (c.tenant_id, c.check_id) "
            "LEFT JOIN check_findings_run f "
            "ON (f.tenant_id, f.check_id) = (c.tenant_id, c.check_id) "
            "WHERE c.tenant_id = %s"
        )
        order: LiteralString = ' ORDER BY c.created_at DESC, c.check_id COLLATE "C" DESC LIMIT %s'
        params: list[Any] = [
            RESERVATION_TTL,
            FINDINGS_RESERVATION_TTL,
            tenant_id,
        ]  # Any: SQL params
        sql = select
        if not admin:
            sql += " AND c.user_id = %s"
            params.append(uid)
        if before is not None:
            sql += (
                " AND (c.created_at < %s OR "
                '(c.created_at = %s AND c.check_id COLLATE "C" < %s COLLATE "C"))'
            )
            params += [before.created_at, before.created_at, before.check_id]
        params.append(limit)
        rows = await self._fetchall(sql + order, tuple(params))
        listings: list[CheckListing] = []
        for r in rows:
            extraction, extracting, findings, finding = r[5], r[6], r[7], r[8]
            if findings == "done":
                stage, run = CheckStage.FINDINGS, None
            elif extraction == "done":
                stage, run = CheckStage.CLAIMS, _run_of(findings, bool(finding))
            else:
                stage, run = CheckStage.SUBMITTED, _run_of(extraction, bool(extracting))
            listings.append(
                CheckListing(
                    check_id=str(r[0]),
                    user_id=str(r[1]),
                    filename=str(r[2]),
                    title=str(r[3]),
                    created_at=r[4],
                    stage=stage,
                    run=run,
                )
            )
        return tuple(listings)

    async def reserve_extraction(
        self, tenant_id: str, check_id: str, *, as_of: dt.date, trace_id: str
    ) -> None:
        if not trace_id.strip() or not check_id.strip():
            raise ValueError(
                "an extraction carries the trace_id of its request and names its check"
            )
        try:
            # One statement, so of concurrent requests exactly one inserts, or
            # takes over a failed or abandoned reservation; the rest get no row.
            row = await self._fetchone(
                "INSERT INTO check_extraction (tenant_id, check_id, as_of, trace_id, status, "
                "reserved_at) VALUES (%s, %s, %s, %s, 'pending', clock_timestamp()) "
                "ON CONFLICT (tenant_id, check_id) DO UPDATE SET as_of = EXCLUDED.as_of, "
                "trace_id = EXCLUDED.trace_id, status = 'pending', "
                "reserved_at = EXCLUDED.reserved_at, claims_dropped = 0, completed_at = NULL "
                "WHERE check_extraction.status = 'failed' OR (check_extraction.status = 'pending' "
                "AND check_extraction.reserved_at < clock_timestamp() - %s) RETURNING 1",
                (tenant_id, check_id, as_of, trace_id, RESERVATION_TTL),
            )
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ValueError(f"tenant {tenant_id!r} holds no check {check_id}") from exc
        if row is None:
            raise ExtractionAlreadyReserved(f"check {check_id} is already extracted")

    async def fail_extraction(self, tenant_id: str, check_id: str) -> None:
        await self._execute(
            "UPDATE check_extraction SET status = 'failed' "
            "WHERE tenant_id = %s AND check_id = %s AND status = 'pending'",
            (tenant_id, check_id),
        )

    async def record_claims(self, record: ClaimExtractionRecord) -> None:
        check_claims_write(record)
        try:
            async with self._connection() as conn, conn.transaction():
                done = await (
                    await conn.execute(
                        "UPDATE check_extraction SET status = 'done', as_of = %s, trace_id = %s, "
                        "claims_dropped = %s, completed_at = clock_timestamp() "
                        "WHERE tenant_id = %s AND check_id = %s AND status = 'pending' "
                        "RETURNING 1",
                        (
                            record.as_of,
                            record.trace_id,
                            record.claims_dropped,
                            record.tenant_id,
                            record.check_id,
                        ),
                    )
                ).fetchone()
                if done is None:
                    held = await (
                        await conn.execute(
                            "SELECT status FROM check_extraction "
                            "WHERE tenant_id = %s AND check_id = %s",
                            (record.tenant_id, record.check_id),
                        )
                    ).fetchone()
                    if held is not None and held[0] == "done":
                        raise ValueError(f"check {record.check_id} is already extracted")
                    if held is None:
                        raise ValueError(
                            f"tenant {record.tenant_id!r} holds no check {record.check_id}, "
                            "or has not reserved its extraction"
                        )
                    raise ValueError(
                        f"check {record.check_id} has not been reserved for extraction"
                    )
                async with conn.cursor() as cursor:
                    await cursor.executemany(
                        "INSERT INTO check_claim (tenant_id, check_id, claim_index, passage, "
                        "quote_start, quote_end, quote_sha256, summary, obligation_ids, "
                        "policy_clause_ids, provenance, trace_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        [
                            (
                                record.tenant_id,
                                record.check_id,
                                claim.claim_index,
                                claim.passage,
                                claim.quote_start,
                                claim.quote_end,
                                claim.quote_sha256,
                                claim.summary,
                                list(claim.obligation_ids),
                                list(claim.policy_clause_ids),
                                claim.provenance.value,
                                record.trace_id,
                            )
                            for claim in record.claims
                        ],
                    )
        except psycopg.errors.CheckViolation as exc:
            raise ValueError(f"claims refused: {exc.diag.constraint_name}") from None

    async def get_claims(self, tenant_id: str, check_id: str) -> ClaimExtractionRecord | None:
        async with self._connection() as conn:
            head = await (
                await conn.execute(
                    "SELECT as_of, trace_id, claims_dropped, completed_at FROM check_extraction "
                    "WHERE tenant_id = %s AND check_id = %s AND status = 'done'",
                    (tenant_id, check_id),
                )
            ).fetchone()
            if head is None:
                return None
            rows = await (await conn.execute(_CLAIM_SELECT, (tenant_id, check_id))).fetchall()
        return ClaimExtractionRecord(
            tenant_id=tenant_id,
            check_id=check_id,
            as_of=head[0],
            trace_id=str(head[1]),
            claims_dropped=int(head[2]),
            claims=tuple(_claim_of_row(r) for r in rows),
            created_at=head[3],
        )

    async def reserve_findings(self, tenant_id: str, check_id: str, *, trace_id: str) -> None:
        if not trace_id.strip() or not check_id.strip():
            raise ValueError(
                "a findings run carries the trace_id of its request and names its check"
            )
        async with self._connection() as conn:
            # One statement, so of concurrent requests exactly one inserts, or
            # takes over a failed or abandoned reservation; the rest get no row.
            # Only a completed extraction has claims to check.
            row = await (
                await conn.execute(
                    "INSERT INTO check_findings_run (tenant_id, check_id, trace_id, status, "
                    "reserved_at) SELECT %s, %s, %s, 'pending', clock_timestamp() WHERE EXISTS ("
                    "SELECT 1 FROM check_extraction WHERE tenant_id = %s AND check_id = %s "
                    "AND status = 'done') "
                    "ON CONFLICT (tenant_id, check_id) DO UPDATE SET "
                    "trace_id = EXCLUDED.trace_id, status = 'pending', "
                    "reserved_at = EXCLUDED.reserved_at, completed_at = NULL "
                    "WHERE check_findings_run.status = 'failed' OR "
                    "(check_findings_run.status = 'pending' "
                    "AND check_findings_run.reserved_at < clock_timestamp() - %s) RETURNING 1",
                    (tenant_id, check_id, trace_id, tenant_id, check_id, FINDINGS_RESERVATION_TTL),
                )
            ).fetchone()
            if row is not None:
                return
            extracted = await (
                await conn.execute(
                    "SELECT status FROM check_extraction WHERE tenant_id = %s AND check_id = %s",
                    (tenant_id, check_id),
                )
            ).fetchone()
            if extracted is None or extracted[0] != "done":
                held = await (
                    await conn.execute(
                        "SELECT 1 FROM submitted_check WHERE tenant_id = %s AND check_id = %s",
                        (tenant_id, check_id),
                    )
                ).fetchone()
                if held is None:
                    raise ValueError(f"tenant {tenant_id!r} holds no check {check_id}")
                raise CheckNotExtracted(f"check {check_id} has not been extracted")
        raise FindingsAlreadyReserved(f"check {check_id} already has its findings")

    async def fail_findings(self, tenant_id: str, check_id: str, *, trace_id: str) -> None:
        await self._execute(
            "UPDATE check_findings_run SET status = 'failed' "
            "WHERE tenant_id = %s AND check_id = %s AND status = 'pending' AND trace_id = %s",
            (tenant_id, check_id, trace_id),
        )

    async def record_findings(self, record: FindingsRecord) -> None:
        check_findings_write(record)
        assessment = record.assessment
        assert assessment.check_id is not None  # check_findings_write refused a record without
        try:
            async with self._connection() as conn, conn.transaction():
                held = await (
                    await conn.execute(
                        "SELECT status, trace_id FROM check_findings_run "
                        "WHERE tenant_id = %s AND check_id = %s FOR UPDATE",
                        (assessment.tenant_id, assessment.check_id),
                    )
                ).fetchone()
                if held is None or held[0] == "failed":
                    raise ValueError(f"check {assessment.check_id} has not been reserved")
                if held[0] == "done":
                    raise ValueError(f"check {assessment.check_id} already has its findings")
                if held[1] != assessment.trace_id:
                    raise ValueError(
                        f"check {assessment.check_id}: the findings reservation is no longer "
                        "this run's"
                    )
                claims = await (
                    await conn.execute(
                        "SELECT count(*) FROM check_claim WHERE tenant_id = %s AND check_id = %s",
                        (assessment.tenant_id, assessment.check_id),
                    )
                ).fetchone()
                for finding in record.findings:
                    if claims is None or finding.claim_index > int(claims[0]):
                        raise ValueError(
                            f"claim {finding.claim_index} is not one of the check's claims"
                        )
                await _write_assessment(conn, assessment, record.coverage)
                async with conn.cursor() as cursor:
                    await cursor.executemany(
                        "INSERT INTO finding (tenant_id, finding_id, assessment_id, check_id, "
                        "claim_index, obligation_id, policy_clause_id, verdict, provenance_tier, "
                        "assessable, unassessable_reason, citations, settle_by, outcome, "
                        "outcome_detail, blocked, derived_from, trace_id) VALUES "
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        [
                            (
                                assessment.tenant_id,
                                f.finding_id,
                                assessment.assessment_id,
                                assessment.check_id,
                                f.claim_index,
                                f.obligation_id,
                                f.policy_clause_id,
                                None if f.verdict is None else f.verdict.value,
                                f.provenance.value,
                                not f.conditional,
                                f.unassessable_reason,
                                Jsonb([_finding_citation_row(c) for c in f.citations]),
                                f.settle_by,
                                f.outcome.value,
                                None if f.detail is None else f.detail.value,
                                Jsonb(
                                    [
                                        {"clause_id": b.clause_id, "outcome": b.outcome}
                                        for b in f.blocked
                                    ]
                                ),
                                f.derived_from,
                                assessment.trace_id,
                            )
                            for f in record.findings
                        ],
                    )
                    await cursor.executemany(
                        "INSERT INTO usage (tenant_id, usage_id, trace_id, model, input_tokens, "
                        "output_tokens, cached_prefix_hit, cost_usd) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [
                            (
                                u.tenant_id,
                                u.usage_id,
                                u.trace_id,
                                u.model,
                                u.input_tokens,
                                u.output_tokens,
                                u.cached_prefix_hit,
                                u.cost_usd,
                            )
                            for u in record.usage
                        ],
                    )
                await conn.execute(
                    "UPDATE check_findings_run SET status = 'done', trace_id = %s, "
                    "completed_at = clock_timestamp() WHERE tenant_id = %s AND check_id = %s",
                    (assessment.trace_id, assessment.tenant_id, assessment.check_id),
                )
        except psycopg.errors.ForeignKeyViolation as exc:
            raise ValueError(
                f"tenant {assessment.tenant_id!r} isn't registered in the ledger, or the check "
                "or a claim is not one it holds"
            ) from exc
        except psycopg.errors.UniqueViolation as exc:
            raise ValueError(f"assessment {assessment.assessment_id} is already recorded") from exc
        except (psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation) as exc:
            raise ValueError(f"findings refused: {exc.diag.constraint_name}") from None

    async def get_findings(self, tenant_id: str, check_id: str) -> FindingsRecord | None:
        async with self._connection() as conn:
            head = await (
                await conn.execute(
                    "SELECT a.assessment_id, a.user_id, a.corpus_version, a.corpus_sha, "
                    "a.mapping_version, a.mapping_sha, a.as_of, a.trace_id, a.check_id, "
                    "a.facts, a.facts_sha256 FROM assessment a JOIN check_findings_run r "
                    "ON r.tenant_id = a.tenant_id AND r.check_id = a.check_id "
                    "WHERE a.tenant_id = %s AND a.check_id = %s AND r.status = 'done'",
                    (tenant_id, check_id),
                )
            ).fetchone()
            if head is None:
                return None
            assessment_id = str(head[0])
            rows = await (
                await conn.execute(_FINDING_SELECT, (tenant_id, assessment_id))
            ).fetchall()
            used = await (
                await conn.execute(
                    "SELECT usage_id, trace_id, model, input_tokens, output_tokens, "
                    "cached_prefix_hit, cost_usd FROM usage "
                    "WHERE tenant_id = %s AND trace_id = %s ORDER BY created_at, usage_id",
                    (tenant_id, str(head[7])),
                )
            ).fetchall()
        return FindingsRecord(
            assessment=assessment_from_row(tenant_id, assessment_id, head[1:]),
            coverage=await self.coverage_rows(tenant_id, assessment_id),
            findings=tuple(_finding_of_row(r) for r in rows),
            usage=tuple(
                UsageRecord(
                    tenant_id=tenant_id,
                    usage_id=str(u[0]),
                    trace_id=str(u[1]),
                    model=str(u[2]),
                    input_tokens=int(u[3]),
                    output_tokens=int(u[4]),
                    cached_prefix_hit=bool(u[5]),
                    cost_usd=float(u[6]),
                )
                for u in used
            ),
        )

    async def chat_turns(self, tenant_id: str, user_id: str, chat_id: str) -> tuple[ChatTurn, ...]:
        rows = await self._fetchall(
            "SELECT turn_index, role, content, outcome, citations, blocked, as_of, trace_id, "
            "created_at, not_in_environment, outcome_detail, generator FROM chat "
            "WHERE tenant_id = %s AND user_id = %s AND chat_id = %s ORDER BY turn_index",
            (tenant_id, user_id, chat_id),
        )
        turns: list[ChatTurn] = []
        for r in rows:
            citations = tuple(_citation_of_row(c) for c in cast("list[object]", r[4]))
            blocked = tuple(_blocked_of_row(b) for b in cast("list[object]", r[5]))
            absent = tuple(_named_absent_of_row(n) for n in cast("list[object]", r[9]))
            _flag_unreadable(tenant_id, chat_id, int(r[0]), citations, "citation")
            _flag_unreadable(tenant_id, chat_id, int(r[0]), blocked, "blocked")
            _flag_unreadable_absent(tenant_id, chat_id, int(r[0]), absent)
            unavailable, prefix = _detail_of_row(r[10])
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
                    blocked=blocked,
                    as_of=r[6],
                    trace_id=str(r[7]),
                    created_at=r[8],
                    not_in_environment=absent,
                    unavailable=unavailable,
                    prefix=prefix,
                    generator=_generator_of_row(r[11]),
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
        title: str,
    ) -> None:
        check_document_class(tenant_id, document_class)
        validate_title(title)
        scope = "public" if tenant_id == PUBLIC_TENANT_ID else "tenant"
        try:
            async with self._connection() as conn:
                # The instrument's title is its latest recorded version's
                # (ADR-0049); each version keeps its own below.
                await conn.execute(
                    "INSERT INTO instrument "
                    "(tenant_id, instrument_id, title, jurisdiction, scope, document_class) "
                    "VALUES (%s, %s, %s, 'AU', %s, %s) "
                    "ON CONFLICT (tenant_id, instrument_id) DO UPDATE SET title = EXCLUDED.title",
                    (tenant_id, instrument_id, title, scope, document_class.value),
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
                    (tenant_id, instrument_id, version_id, title, valid_from, supersedes),
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

    async def environment_titles(
        self, tenant_id: str, *, as_of: dt.date
    ) -> dict[str, frozenset[str]]:
        # The tenant's own versions, and `_public`'s of the instruments it
        # subscribes to (`_public` holds no subscription, and owns them all).
        # A submitted document is the subject of a check, never part of the
        # environment (ADR-0051, ADR-0053 U1); an unclassified one (before
        # 0011) is not left out.
        rows = await self._fetchall(
            "SELECT v.instrument_id, v.title FROM instrument_version v "
            "JOIN instrument i USING (tenant_id, instrument_id) "
            "WHERE (v.tenant_id = %s OR (v.tenant_id = %s AND v.instrument_id IN "
            "(SELECT instrument_id FROM tenant_instrument WHERE tenant_id = %s))) "
            "AND v.valid_from <= %s AND (v.valid_to IS NULL OR %s < v.valid_to) "
            "AND v.withdrawn_at IS NULL AND v.purged_at IS NULL "
            "AND i.document_class IS DISTINCT FROM 'submitted_document'",
            (tenant_id, PUBLIC_TENANT_ID, tenant_id, as_of, as_of),
        )
        found: dict[str, set[str]] = {}
        for r in rows:
            found.setdefault(str(r[0]), set()).add(str(r[1]))
        return {i: frozenset(t) for i, t in found.items()}

    async def version_titles(
        self, tenant_id: str, versions: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], str]:
        wanted = sorted(set(versions))
        if not wanted:
            return {}
        rows = await self._fetchall(
            "SELECT v.instrument_id, v.version_id, v.title FROM instrument_version v "
            "JOIN unnest(%s::text[], %s::text[]) AS w(instrument_id, version_id) "
            "USING (instrument_id, version_id) WHERE v.tenant_id = %s",
            ([i for i, _ in wanted], [v for _, v in wanted], tenant_id),
        )
        return {(str(r[0]), str(r[1])): str(r[2]) for r in rows}

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
        check_id=str(row[7]) if len(row) > 7 and row[7] is not None else None,
        facts=FactsSnapshot.of_json(row[8], str(row[9]))
        if len(row) > 9 and row[8] is not None
        else None,
    )
