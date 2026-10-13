"""The tenant's own endpoints (issue #147): profile evidence, Track A's
coverage of it, and library subscriptions, through the API with a signed-in
member.

Track A runs over the signed Privacy Act mapping (#118) on the fake graph,
which runs the same evaluator as Neo4j. The GA-05 history shows a fact
recorded through the API changing the coverage the API reports.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.interfaces import LedgerStore, Profile
from compliance_rag.api import tenant
from compliance_rag.api.app import create_app
from compliance_rag.api.deps import ApiServices
from compliance_rag.assessment.evidence import Evidence, EvidenceProvenance, Period
from compliance_rag.eval.applicability import GoldenFact, load_applicability_set
from compliance_rag.graph.mappings import load_mapping_set
from tests.test_api_auth import api_settings, bearer, mint, verifier_for
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for

TENANT = "acme"
AS_OF = "2026-10-12"
APP11 = "OB-PA-APP11-1"
GA05 = next(c for c in load_applicability_set().cases if c.id == "GA-05")


def _services(ledger: LedgerStore, **extra: Any) -> ApiServices:  # Any: ApiServices fields
    labels = load_applicability_set()
    return ApiServices(
        settings=api_settings(),
        verifier=verifier_for(),
        ledger=ledger,
        graph=fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels),
        **extra,
    )


async def _client_for(ledger: LedgerStore) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(_services(ledger))),
        base_url="http://api",
        headers=bearer(mint()),
    )


@pytest.fixture
async def ledger() -> FakeLedgerStore:
    """The tenant is registered when it's provisioned, before anyone signs
    in. Another tenant's data sits beside it, never visible to acme."""
    store = FakeLedgerStore()
    for tenant_id in (TENANT, "other-tenant"):
        await store.put_profile(Profile(tenant_id))
    await store.record_evidence(
        Evidence(
            tenant_id="other-tenant",
            evidence_id="other-1",
            field="legal_form",
            value="trust",
            period=Period(dt.date(2010, 1, 1)),
            provenance=EvidenceProvenance.ASSERTED,
            source="other",
            asserted_by="someone@other.example",
            trace_id="f" * 32,
        )
    )
    await store.subscribe(
        tenant_id="other-tenant", instrument_id="spam-act-2003", subscribed_by="x"
    )
    return store


@pytest.fixture
async def client(ledger: FakeLedgerStore) -> AsyncIterator[httpx.AsyncClient]:
    async with await _client_for(ledger) as c:
        yield c


def _body(fact: GoldenFact, *, source: str = "the compliance lead") -> dict[str, Any]:  # Any: JSON
    value: dict[str, Any] = (  # Any: JSON
        {"value_date": fact.value.isoformat()}
        if isinstance(fact.value, dt.date)
        else {"value": fact.value}
    )
    period: dict[str, Any] = (  # Any: JSON
        {"financial_year": fact.financial_year}
        if fact.financial_year is not None
        else {"start": fact.since.isoformat() if fact.since else None}
    )
    return {"field": fact.field, **value, "period": period, "source": source}


async def _assert(client: httpx.AsyncClient, fact: GoldenFact) -> dict[str, Any]:  # Any: JSON
    response = await client.post("/v1/profile/evidence", json=_body(fact))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()  # Any: JSON
    return body


async def _app11(client: httpx.AsyncClient) -> str:
    coverage = (await client.get("/v1/coverage", params={"as_of": AS_OF})).json()
    return next(o["outcome"] for o in coverage["obligations"] if o["obligation_id"] == APP11)


# ---- profile and coverage ---------------------------------------------------------


async def test_a_fact_recorded_through_the_api_changes_the_coverage(
    client: httpx.AsyncClient,
) -> None:
    history = [
        f for f in GA05.facts if not (f.field == "annual_turnover_aud" and f.financial_year == 2023)
    ]
    for fact in history:
        await _assert(client, fact)
    assert await _app11(client) == "unassessable"
    await _assert(
        client, GoldenFact(field="annual_turnover_aud", value=3_200_000, financial_year=2023)
    )
    assert await _app11(client) == "applicable"
    coverage = (await client.get("/v1/coverage", params={"as_of": AS_OF})).json()
    app11 = next(o for o in coverage["obligations"] if o["obligation_id"] == APP11)
    s6d4a = {"clause_id": "privacy-act-1988:C2026C00227:s6d/4/a", "cite": "s 6D(4)(a)"}
    assert s6d4a in app11["citations"]
    assert coverage["counts"] == {"applicable": 35, "not_applicable": 2, "unassessable": 0}
    assert coverage["tenant_id"] == TENANT


async def test_unanswered_questions_come_with_their_clauses(client: httpx.AsyncClient) -> None:
    coverage = (await client.get("/v1/coverage", params={"as_of": AS_OF})).json()
    app11 = next(o for o in coverage["obligations"] if o["obligation_id"] == APP11)
    assert app11["outcome"] == "unassessable" and app11["questions"]
    legal_form = next(q for q in app11["questions"] if q["field"] == "legal_form")
    assert legal_form["question"] and legal_form["cites"]


async def test_the_default_date_is_today_in_sydney(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tenant, "today", lambda: dt.date(2026, 5, 1))
    coverage = (await client.get("/v1/coverage")).json()
    assert coverage["as_of"] == "2026-05-01"
    # Before C2026C00227, the only compilation loaded: unassessable, every one (GA-07).
    assert coverage["counts"]["unassessable"] == 37


@pytest.mark.parametrize("as_of", ["9999-07-01", "1066-10-14"])
async def test_a_date_outside_the_calendar_the_law_uses_is_a_422(
    client: httpx.AsyncClient, as_of: str
) -> None:
    assert (await client.get("/v1/coverage", params={"as_of": as_of})).status_code == 422


async def test_conflicting_facts_are_reported_never_resolved(client: httpx.AsyncClient) -> None:
    for form in ("body_corporate", "partnership"):
        await _assert(client, GoldenFact(field="legal_form", value=form, since=dt.date(2015, 3, 2)))
    profile = (await client.get("/v1/profile")).json()
    [reading] = [r for r in profile["readings"] if r["field"] == "legal_form"]
    assert reading["conflict"] is True and reading["value"] is None
    assert len(reading["evidence_ids"]) == 2
    assert await _app11(client) == "unassessable"


async def test_a_financial_years_figure_reads_for_that_year(client: httpx.AsyncClient) -> None:
    await _assert(
        client, GoldenFact(field="annual_turnover_aud", value=2_500_000, financial_year=2025)
    )
    profile = (await client.get("/v1/profile")).json()
    [reading] = profile["readings"]
    assert reading["period"]["label"] == "FY2025-26"
    assert reading["value"] == 2_500_000 and reading["conflict"] is False


async def test_a_recorded_fact_is_asserted_by_the_member_with_the_requests_trace(
    client: httpx.AsyncClient, ledger: FakeLedgerStore
) -> None:
    response = await client.post(
        "/v1/profile/evidence",
        params={"tenant_id": "other-tenant"},
        json=_body(
            GoldenFact(field="carries_on_a_business", value=True, since=dt.date(2015, 3, 2))
        ),
    )
    assert response.status_code == 201
    [stored] = await ledger.evidence(TENANT)
    assert stored.provenance is EvidenceProvenance.ASSERTED
    assert stored.asserted_by == "lead@acme.example"
    assert stored.trace_id == response.headers["x-trace-id"]
    assert [e.evidence_id for e in await ledger.evidence("other-tenant")] == ["other-1"]


async def test_a_correction_supersedes_and_the_profile_shows_both(
    client: httpx.AsyncClient,
) -> None:
    first = await _assert(
        client, GoldenFact(field="annual_turnover_aud", value=2_500_000, financial_year=2025)
    )
    body = _body(GoldenFact(field="annual_turnover_aud", value=2_600_000, financial_year=2025))
    response = await client.post(
        "/v1/profile/evidence", json={**body, "supersedes": first["evidence_id"]}
    )
    assert response.status_code == 201
    profile = (await client.get("/v1/profile")).json()
    current = {e["evidence_id"]: e["current"] for e in profile["evidence"]}
    assert current == {first["evidence_id"]: False, response.json()["evidence_id"]: True}
    assert [r["value"] for r in profile["readings"]] == [2_600_000]


async def test_only_settling_evidence_is_current(
    client: httpx.AsyncClient, ledger: FakeLedgerStore
) -> None:
    """An inferred row superseding an asserted one doesn't unsettle it
    (#114), and the profile says what Track A reads."""
    asserted = await _assert(
        client, GoldenFact(field="legal_form", value="body_corporate", since=dt.date(2015, 3, 2))
    )
    await ledger.record_evidence(
        Evidence(
            tenant_id=TENANT,
            evidence_id="inferred-1",
            field="legal_form",
            value="trust",
            period=Period(dt.date(2015, 3, 2)),
            provenance=EvidenceProvenance.INFERRED,
            source="a document",
            supersedes=asserted["evidence_id"],
        )
    )
    profile = (await client.get("/v1/profile")).json()
    current = {e["evidence_id"]: e["current"] for e in profile["evidence"]}
    assert current == {asserted["evidence_id"]: True, "inferred-1": False}
    assert [r["value"] for r in profile["readings"]] == ["body_corporate"]


VALID = _body(GoldenFact(field="legal_form", value="trust", since=dt.date(2020, 1, 1)))


@pytest.mark.parametrize(
    ("change", "names"),
    [
        pytest.param({"provenance": "inferred"}, "provenance", id="a-provenance"),
        pytest.param({"tenant_id": "other-tenant"}, "tenant_id", id="a-tenant"),
        pytest.param({"asserted_by": "someone"}, "asserted_by", id="an-author"),
        pytest.param({"field": "Annual Turnover"}, "snake_case", id="a-bad-field"),
        pytest.param(
            {"period": {"start": "2015-03-02", "financial_year": 2025}},
            "financial_year",
            id="two-periods",
        ),
        pytest.param({"value_date": "2015-03-02"}, "exactly one", id="two-values"),
        pytest.param({"source": ""}, "source", id="no-source"),
        pytest.param({"value": float("nan")}, "value", id="nan"),
        pytest.param({"value": float("inf")}, "value", id="infinity"),
        pytest.param({"value": "a\x00b"}, "NUL", id="nul"),
        pytest.param({"value": ["x"] * 101}, "value", id="too-many-items"),
        pytest.param({"value": "x" * 2001}, "value", id="too-long"),
        pytest.param({"period": {"financial_year": 9999}}, "financial_year", id="year-9999"),
    ],
)
async def test_an_invalid_fact_is_a_422_naming_what_is_wrong(
    client: httpx.AsyncClient,
    change: dict[str, Any],  # Any: JSON
    names: str,
) -> None:
    # NaN and infinity aren't JSON, so they go as the literals Python emits.
    content = json.dumps({**VALID, **change}).encode()
    response = await client.post(
        "/v1/profile/evidence", content=content, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    assert names in response.text


# ---- subscriptions ----------------------------------------------------------------


async def test_subscribe_and_unsubscribe_touch_the_subscription_row_only(
    client: httpx.AsyncClient, ledger: FakeLedgerStore
) -> None:
    response = await client.put("/v1/subscriptions/privacy-act-1988")
    assert response.status_code == 204
    assert (await client.get("/v1/subscriptions")).json() == {"instruments": ["privacy-act-1988"]}
    assert (
        ledger.subscription_traces[(TENANT, "privacy-act-1988")] == response.headers["x-trace-id"]
    )
    assert (await client.delete("/v1/subscriptions/privacy-act-1988")).status_code == 204
    assert (await client.get("/v1/subscriptions")).json() == {"instruments": []}
    assert await ledger.subscribed_instruments("other-tenant") == frozenset({"spam-act-2003"})


async def test_a_subscription_to_a_non_library_id_is_a_422(client: httpx.AsyncClient) -> None:
    response = await client.put("/v1/subscriptions/not:a-library-id")
    assert response.status_code == 422


# ---- signed in, on the configured tenant ------------------------------------------

ROUTES = [
    ("GET", "/v1/profile"),
    ("POST", "/v1/profile/evidence"),
    ("GET", "/v1/coverage"),
    ("GET", "/v1/subscriptions"),
    ("PUT", "/v1/subscriptions/privacy-act-1988"),
    ("DELETE", "/v1/subscriptions/privacy-act-1988"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_endpoint_needs_a_signed_in_member(
    ledger: FakeLedgerStore, method: str, path: str
) -> None:
    async with await _client_for(ledger) as c:
        response = await c.request(method, path, headers={"Authorization": ""})
    assert response.status_code == 401


async def test_no_endpoint_shows_another_tenants_data(client: httpx.AsyncClient) -> None:
    hostile = {"tenant_id": "other-tenant"}
    headers = {"X-Tenant-Id": "other-tenant"}
    profile = (await client.get("/v1/profile", params=hostile, headers=headers)).json()
    assert profile["tenant_id"] == TENANT and profile["evidence"] == []
    coverage = (await client.get("/v1/coverage", params=hostile, headers=headers)).json()
    assert coverage["tenant_id"] == TENANT
    subscriptions = await client.get("/v1/subscriptions", params=hostile, headers=headers)
    assert subscriptions.json() == {"instruments": []}


# ---- startup and shutdown ---------------------------------------------------------


def test_a_failing_startup_check_stops_the_app() -> None:
    async def unreachable() -> None:
        raise ConnectionError("graph unreachable")

    app = create_app(_services(FakeLedgerStore(), checks=(unreachable,)))
    with pytest.raises(ConnectionError, match="graph unreachable"), TestClient(app):
        pass


def test_every_closer_runs_on_shutdown_even_if_one_raises() -> None:
    closed: list[str] = []

    async def failing() -> None:
        closed.append("failing")
        raise RuntimeError("close failed")

    async def driver() -> None:
        closed.append("driver")

    app = create_app(_services(FakeLedgerStore(), closers=(failing, driver)))
    with pytest.raises(RuntimeError, match="close failed"), TestClient(app):
        pass
    assert sorted(closed) == ["driver", "failing"]


# ---- the real ledger --------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set — needs a migrated ledger")
async def test_evidence_and_subscriptions_round_trip_on_postgres() -> None:
    import psycopg

    from tests.test_postgres_ledger_store import empty_ledger

    postgres = await empty_ledger()
    try:
        await postgres.put_profile(Profile(TENANT))
        await postgres.record_version(
            tenant_id="_public",
            instrument_id="privacy-act-1988",
            version_id="C2026C00227",
            valid_from=dt.date(2026, 6, 4),
            supersedes=None,
        )
        async with await _client_for(postgres) as c:
            for fact in GA05.facts:
                await _assert(c, fact)
            assert await _app11(c) == "applicable"
            subscribed = await c.put("/v1/subscriptions/privacy-act-1988")
            assert subscribed.status_code == 204
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            cursor = await conn.execute(
                "SELECT trace_id FROM tenant_instrument WHERE tenant_id = %s", (TENANT,)
            )
            assert await cursor.fetchone() == (subscribed.headers["x-trace-id"],)
    finally:
        # The schema tests that follow alphabetically expect no committed tenant.
        await empty_ledger()
