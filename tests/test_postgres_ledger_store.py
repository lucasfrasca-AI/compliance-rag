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

from compliance_rag.adapters.fakes import FakeClauseStore, FakeRetrievalStore
from compliance_rag.adapters.interfaces import Profile
from compliance_rag.adapters.postgres_ledger_store import PostgresLedgerStore
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


@pytest.fixture
async def ledger() -> AsyncIterator[PostgresLedgerStore]:
    host = urlparse(DATABASE_URL).hostname
    if host not in {"localhost", "127.0.0.1"}:
        raise RuntimeError(f"refusing to truncate a ledger on {host!r}: tests run on localhost")
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(f"TRUNCATE {_LEDGER_TABLES} CASCADE")
        await conn.execute(
            "INSERT INTO tenant (tenant_id, name, is_library) "
            "VALUES ('_public', 'Library (public law)', TRUE)"
        )
    yield PostgresLedgerStore(DATABASE_URL)


async def _seed_finding(tenant_id: str, clause_id: str) -> None:
    """A finding citing `clause_id`, under a fresh assessment — `LedgerStore`
    has no finding writer yet, so the test writes the rows directly."""
    assessment_id = uuid.uuid4().hex
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(
            "INSERT INTO assessment (tenant_id, assessment_id, corpus_version, mapping_version, "
            "as_of, trace_id) VALUES (%s, %s, 'c', 'm', '2026-10-01', %s)",
            (tenant_id, assessment_id, "0" * 32),
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
        )
