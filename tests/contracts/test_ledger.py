"""The `LedgerStore` contract (issue #69): one definition, run against
`FakeLedgerStore` here and against `PostgresLedgerStore` on real Postgres
(`tests/test_postgres_ledger_store.py`), so the two can't silently diverge.

It uses only the interface: tenants are registered with `put_profile`,
library instruments are recorded before anyone subscribes to them (the
database requires it), and findings are seeded through a callable each backend
supplies — `LedgerStore` has no finding writer yet.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable

import pytest

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.interfaces import (
    LedgerStore,
    Profile,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
)

SeedFinding = Callable[[str, str], Awaitable[None]]
"""(tenant_id, cited clause_id) -> records one finding citing that clause."""

LAW = "privacy-act-1988"
POLICY = "acme/privacy-policy"
JAN, JUN = dt.date(2026, 1, 1), dt.date(2026, 6, 1)


async def assert_ledger_contract(ledger: LedgerStore, seed_finding: SeedFinding) -> None:
    # ---- tenants and profiles
    await ledger.put_profile(Profile("acme", jurisdictions=("AU",), sectors=("retail",)))
    await ledger.put_profile(Profile("other-corp"))
    profile = await ledger.get_profile("acme")
    assert profile is not None and profile.jurisdictions == ("AU",)
    assert profile.sectors == ("retail",)
    assert await ledger.get_profile("nobody") is None

    # ---- assessments carry their trace_id
    await ledger.record_assessment(
        tenant_id="acme",
        assessment_id="a1",
        corpus_version="corpus-2026.10",
        mapping_version="mappings-2026.10",
        as_of=JUN,
        trace_id="f" * 32,
    )
    row = await ledger.get_assessment("acme", "a1")
    assert row is not None and row["trace_id"] == "f" * 32 and row["as_of"] == JUN
    assert await ledger.get_assessment("other-corp", "a1") is None

    # ---- library versions, then subscriptions to them (ADR-0020, ADR-0025)
    for instrument in (LAW, "iso-27001"):
        await ledger.record_version(
            tenant_id="_public",
            instrument_id=instrument,
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
        )
    await ledger.subscribe(tenant_id="acme", instrument_id=LAW, subscribed_by="u1")
    await ledger.subscribe(tenant_id="acme", instrument_id=LAW, subscribed_by="u2")  # idempotent
    await ledger.subscribe(tenant_id="acme", instrument_id="iso-27001", subscribed_by="u1")
    await ledger.subscribe(tenant_id="other-corp", instrument_id=LAW, subscribed_by="u1")
    await ledger.unsubscribe(tenant_id="acme", instrument_id=LAW)
    await ledger.unsubscribe(tenant_id="acme", instrument_id="never-subscribed")  # no-op
    assert await ledger.subscribed_instruments("acme") == frozenset({"iso-27001"})
    assert await ledger.subscribed_instruments("other-corp") == frozenset({LAW})
    for tenant_id, instrument_id in (("acme", POLICY), ("_public", LAW)):
        with pytest.raises(ValueError):
            await ledger.subscribe(
                tenant_id=tenant_id, instrument_id=instrument_id, subscribed_by="u"
            )

    # ---- version validity is a record of its own (ADR-0021 mechanism 3)
    await ledger.record_version(
        tenant_id="acme", instrument_id=POLICY, version_id="v1", valid_from=JAN, supersedes=None
    )
    await ledger.record_version(
        tenant_id="acme", instrument_id=POLICY, version_id="v2", valid_from=JUN, supersedes="v1"
    )
    await ledger.end_version(tenant_id="acme", instrument_id=POLICY, version_id="v1", valid_to=JUN)
    assert await ledger.version_validity(
        tenant_id="acme", instrument_id=POLICY, version_id="v1"
    ) == (JAN, JUN)
    assert await ledger.version_validity(
        tenant_id="acme", instrument_id=POLICY, version_id="v2"
    ) == (JUN, None)
    assert await ledger.version_validity(
        tenant_id="_public", instrument_id=LAW, version_id="v1"
    ) == (JAN, None)
    assert (
        await ledger.version_validity(tenant_id="acme", instrument_id=POLICY, version_id="v9")
        is None
    )
    with pytest.raises(ValueError, match="already recorded"):
        await ledger.record_version(
            tenant_id="acme",
            instrument_id=POLICY,
            version_id="v1",
            valid_from=JAN,
            supersedes=None,
        )
    with pytest.raises(KeyError):
        await ledger.end_version(
            tenant_id="acme", instrument_id=POLICY, version_id="v9", valid_to=JUN
        )

    # ---- findings citing, by stable-id prefix
    await seed_finding("acme", f"{POLICY}:v1:3/1")
    await seed_finding("acme", f"{POLICY}:v2:1/1")
    await seed_finding("other-corp", f"{POLICY}:v1:3/1")
    assert await ledger.count_findings_citing("acme", f"{POLICY}:v1:") == 1
    assert await ledger.count_findings_citing("acme", f"{POLICY}:") == 2
    assert await ledger.count_findings_citing("acme", "iso-27001:") == 0

    # ---- withdraw and purge markers
    await ledger.mark_withdrawn(
        tenant_id="acme", instrument_id=POLICY, version_id="v1", withdrawn_by="admin"
    )
    assert await ledger.is_withdrawn(tenant_id="acme", instrument_id=POLICY, version_id="v1")
    assert not await ledger.is_withdrawn(tenant_id="acme", instrument_id=POLICY, version_id="v2")
    await ledger.mark_purged(
        tenant_id="acme", instrument_id=POLICY, version_id="v1", purged_by="u1", reason="hold"
    )
    await ledger.mark_withdrawn(  # idempotent: the first withdrawal stands
        tenant_id="acme", instrument_id=POLICY, version_id="v1", withdrawn_by="someone-else"
    )
    assert await ledger.is_withdrawn(tenant_id="acme", instrument_id=POLICY, version_id="v1")
    for remove in (ledger.mark_withdrawn, ledger.mark_purged):
        with pytest.raises(KeyError):  # nothing recorded to mark
            await _remove(remove, "acme", POLICY, "v9")
        with pytest.raises(ValueError, match="library law"):
            await _remove(remove, "_public", LAW, "v1")
    marker = ledger.purge_marker
    assert await marker(tenant_id="acme", instrument_id=POLICY, version_id="v1") == ("u1", "hold")
    assert await marker(tenant_id="acme", instrument_id=POLICY, version_id="v2") is None

    # ---- removal audit, in the order it happened
    for audit_id, outcome in (("au-1", RemovalOutcome.AUTHORIZED), ("au-2", RemovalOutcome.FAILED)):
        await ledger.record_removal_audit(
            RemovalAuditEntry(
                audit_id=audit_id,
                tenant_id="acme",
                instrument_id=POLICY,
                version_id=None,
                action=RemovalAction.PURGE,
                outcome=outcome,
                actor="u1",
                reason="cleanup",
                findings_citing=2,
            )
        )
    trail = await ledger.removal_audit("acme")
    assert [(e.audit_id, e.outcome) for e in trail] == [
        ("au-1", RemovalOutcome.AUTHORIZED),
        ("au-2", RemovalOutcome.FAILED),
    ]
    assert trail[0].findings_citing == 2 and trail[0].version_id is None
    assert await ledger.removal_audit("other-corp") == ()


async def _remove(
    method: Callable[..., Awaitable[None]], tenant_id: str, instrument_id: str, version_id: str
) -> None:
    kwargs = {"tenant_id": tenant_id, "instrument_id": instrument_id, "version_id": version_id}
    if method.__name__ == "mark_withdrawn":
        await method(**kwargs, withdrawn_by="u")
    else:
        await method(**kwargs, purged_by="u", reason="r")


async def test_the_fake_ledger_satisfies_the_contract() -> None:
    ledger = FakeLedgerStore()

    async def seed(tenant_id: str, clause_id: str) -> None:
        ledger.seed_finding(tenant_id, clause_id)

    await assert_ledger_contract(ledger, seed)
