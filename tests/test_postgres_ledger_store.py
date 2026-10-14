"""`PostgresLedgerStore` on a real Postgres 16 (issue #69): the same shared
contracts `FakeLedgerStore` runs — the ledger contract, the corpus-lifecycle
contract and the retrieval-store contract — plus what only a database adds.

Needs a migrated ledger (CI's `ci` job applies every `db/ledger/*.sql`).
Each test starts from an empty ledger: the fixture truncates every ledger
table and re-seeds the library row (ADR-0025). It refuses to do that to any
database not on localhost — it exists for CI's service container and a
local Docker Postgres, never anything shared.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import psycopg
import pytest

from compliance_rag.adapters import postgres_ledger_store
from compliance_rag.adapters.fakes import FakeClauseStore, FakeRetrievalStore
from compliance_rag.adapters.interfaces import (
    AssessmentRecord,
    PolicyVersion,
    Profile,
    SourceKind,
    SourceVersion,
)
from compliance_rag.adapters.postgres_ledger_store import PostgresLedgerStore
from compliance_rag.assessment.coverage import CoverageRow, Outcome
from compliance_rag.corpus.models import DocumentClass
from tests.contracts.test_corpus_lifecycle import assert_corpus_lifecycle_contract
from tests.contracts.test_interfaces import (
    LIBRARY_INSTRUMENT,
    assert_retrieval_store_contract,
    contract_chunks,
)
from tests.contracts.test_ledger import assert_ledger_contract

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — needs a migrated ledger Postgres "
    "(CI's service container, or docker compose locally)",
)

_LEDGER_TABLES = (
    "tenant_instrument, corpus_removal_audit, finding, evidence, assessment, usage, chat, "
    "document_manifest, clause_version, clause, instrument_version, instrument, app_user, tenant"
)


async def empty_ledger() -> PostgresLedgerStore:
    """The local/CI test ledger, emptied, with only the library owner seeded.
    Refuses any other database."""
    url = urlparse(DATABASE_URL)
    if url.hostname not in {"localhost", "127.0.0.1"} or url.path != "/compliance_rag":
        raise RuntimeError(
            f"refusing to truncate {url.hostname}{url.path}: only the local/CI "
            "test ledger (localhost/compliance_rag) is ever emptied"
        )
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(f"TRUNCATE {_LEDGER_TABLES} CASCADE")
        await conn.execute(
            "INSERT INTO tenant (tenant_id, name, is_library) "
            "VALUES ('_public', 'Library (public law)', TRUE)"
        )
    return PostgresLedgerStore(DATABASE_URL)


@pytest.fixture
async def ledger() -> AsyncIterator[PostgresLedgerStore]:
    yield await empty_ledger()


async def _seed_finding(tenant_id: str, clause_id: str) -> None:
    """A finding citing `clause_id`, under a fresh assessment — `LedgerStore`
    has no finding writer yet, so the test writes the rows directly."""
    assessment_id = uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(
            "INSERT INTO assessment (tenant_id, assessment_id, corpus_version, corpus_sha, "
            "mapping_version, mapping_sha, as_of, trace_id) "
            "VALUES (%s, %s, 'c', %s, 'm', %s, '2026-10-01', %s)",
            (tenant_id, assessment_id, "a" * 40, "b" * 40, "0" * 32),
        )
        await conn.execute(
            "INSERT INTO finding (tenant_id, finding_id, assessment_id, claim_text, verdict, "
            "provenance_tier, citations, trace_id) VALUES (%s, %s, %s, 'c', 'consistent', "
            "'parsed', %s, %s)",
            (
                tenant_id,
                uuid.uuid4().hex,
                assessment_id,
                f'[{{"clause_id": "{clause_id}", "canonicalisation_version": 2}}]',
                "0" * 32,
            ),
        )


async def test_postgres_satisfies_the_ledger_contract(ledger: PostgresLedgerStore) -> None:
    await assert_ledger_contract(ledger, _seed_finding)


async def test_postgres_satisfies_the_corpus_lifecycle_contract(
    ledger: PostgresLedgerStore,
) -> None:
    store = FakeRetrievalStore(ledger=ledger)

    async def stored() -> frozenset[str]:
        return store.stored_chunk_ids

    await assert_corpus_lifecycle_contract(
        store, store, ledger, FakeClauseStore(), stored, _seed_finding
    )


async def test_postgres_satisfies_the_retrieval_store_contract(
    ledger: PostgresLedgerStore,
) -> None:
    """The retrieval contract reads subscriptions from the ledger, so it runs
    on the Postgres ledger too, with the library instrument recorded first."""
    await ledger.put_profile(Profile("acme"))
    await ledger.record_version(
        tenant_id="_public",
        instrument_id=LIBRARY_INSTRUMENT,
        version_id="C2026C00227",
        valid_from=dt.date(2000, 1, 1),
        supersedes=None,
        document_class=DocumentClass.LIBRARY_LAW,
    )
    await assert_retrieval_store_contract(
        FakeRetrievalStore(contract_chunks(), ledger=ledger), ledger
    )


async def test_a_subscription_to_an_instrument_the_library_doesnt_hold_is_refused(
    ledger: PostgresLedgerStore,
) -> None:
    """What the database adds over the fake (ADR-0025)."""
    await ledger.put_profile(Profile("acme"))
    with pytest.raises(ValueError, match="doesn't hold"):
        await ledger.subscribe(tenant_id="acme", instrument_id="spam-act-2003", subscribed_by="u")


async def test_a_version_needs_its_tenant_registered(ledger: PostgresLedgerStore) -> None:
    with pytest.raises(ValueError, match="isn't registered"):
        await ledger.record_version(
            tenant_id="nobody",
            instrument_id="nobody/policy",
            version_id="v1",
            valid_from=dt.date(2026, 1, 1),
            supersedes=None,
            document_class=DocumentClass.INTERNAL_POLICY,
        )


async def test_a_policy_recorded_before_document_class_is_refused_until_classified(
    ledger: PostgresLedgerStore,
) -> None:
    """0011 leaves a tenant's earlier rows unclassified rather than guessing:
    reading them as policies is refused, naming them, until the next version
    states the class."""
    await ledger.put_profile(Profile("acme"))
    jan, mar, jun = dt.date(2026, 1, 1), dt.date(2026, 3, 1), dt.date(2026, 6, 1)
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(
            "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction, scope) "
            "VALUES ('acme', 'acme/old-policy', 'acme/old-policy', 'AU', 'tenant')"
        )
        await conn.execute(
            "INSERT INTO instrument_version (tenant_id, instrument_id, version_id, title, "
            "valid_from) VALUES ('acme', 'acme/old-policy', 'v1', 'acme/old-policy', %s)",
            (jan,),
        )
    with pytest.raises(ValueError, match=r"acme/old-policy.*no document class"):
        await ledger.policy_versions("acme", as_of=mar)
    await ledger.record_version(
        tenant_id="acme",
        instrument_id="acme/old-policy",
        version_id="v2",
        valid_from=jun,
        supersedes="v1",
        document_class=DocumentClass.INTERNAL_POLICY,
    )
    assert await ledger.policy_versions("acme", as_of=mar) == (
        PolicyVersion("acme/old-policy", "v1"),
    )


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param(
            CoverageRow("OB-BAD", Outcome.APPLICABLE, " ", ("x:v:1",), ()), id="no-reason"
        ),
        pytest.param(CoverageRow("OB-BAD", Outcome.APPLICABLE, "r", (), ()), id="uncited"),
    ],
)
async def test_a_coverage_row_the_database_refuses_takes_the_assessment_with_it(
    ledger: PostgresLedgerStore, monkeypatch: pytest.MonkeyPatch, refused: CoverageRow
) -> None:
    """One transaction (#153): the assessment is inserted first and its rows
    after, so a row refused last must undo the first. The adapter's own checks
    refuse these rows before any SQL, so they are switched off to let the
    database's constraints be what refuses."""

    def unchecked(*_: object) -> None:
        return None

    monkeypatch.setattr(postgres_ledger_store, "check_assessment_write", unchecked)
    await ledger.put_profile(Profile("acme"))
    good = CoverageRow("OB-GOOD", Outcome.APPLICABLE, "r", ("x:v:1",), ())
    record = AssessmentRecord(
        tenant_id="acme",
        assessment_id="a1",
        user_id="lead@acme.example",
        corpus=SourceVersion(SourceKind.CORPUS, "a" * 40, "corpus-2026.10"),
        mapping=SourceVersion(SourceKind.MAPPING, "b" * 40),
        as_of=dt.date(2026, 10, 14),
        trace_id="t",
    )
    with pytest.raises(ValueError, match="assessment refused"):
        await ledger.record_coverage_assessment(record, (good, refused))
    assert await ledger.get_assessment("acme", "a1") is None
    assert await ledger.coverage_rows("acme", "a1") == ()
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        cursor = await conn.execute("SELECT count(*) FROM assessment")
        assert await cursor.fetchone() == (0,)
