"""The tenant's own endpoints (issue #147): profile evidence, Track A's
coverage of it, and library subscriptions, through the API with a signed-in
member.

Track A runs over the signed Privacy Act mapping (#118) on the fake graph,
which runs the same evaluator as Neo4j. The GA-05 history shows a fact
recorded through the API changing the coverage the API reports.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.interfaces import LedgerStore, Profile
from compliance_rag.api.app import create_app
from compliance_rag.api.deps import ApiServices
from compliance_rag.assessment.evidence import EvidenceProvenance
from compliance_rag.eval.applicability import GoldenFact, load_applicability_set
from compliance_rag.graph.mappings import load_mapping_set
from tests.test_api_auth import api_settings, bearer, mint, verifier_for
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for

TENANT = "acme"
AS_OF = "2026-10-12"
APP11 = "OB-PA-APP11-1"
GA05 = next(c for c in load_applicability_set().cases if c.id == "GA-05")


def _services(ledger: LedgerStore) -> ApiServices:
    labels = load_applicability_set()
    return ApiServices(
        settings=api_settings(),
        verifier=verifier_for(),
        ledger=ledger,
        graph=fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels),
    )


async def _client_for(ledger: LedgerStore) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(_services(ledger))),
        base_url="http://api",
        headers=bearer(mint()),
    )


@pytest.fixture
async def ledger() -> FakeLedgerStore:
    """The tenant is registered when it's provisioned, before anyone signs in."""
    store = FakeLedgerStore()
    await store.put_profile(Profile(TENANT))
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
        else {"since": fact.since.isoformat() if fact.since else None}
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
    assert {"clause_id": "privacy-act-1988:C2026C00227:s6d/4/a", "cite": "s 6D(4)(a)"} in app11[
        "citations"
    ]
    assert coverage["counts"] == {"applicable": 35, "not_applicable": 2, "unassessable": 0}


async def test_conflicting_facts_are_reported_never_resolved(client: httpx.AsyncClient) -> None:
    for form in ("body_corporate", "partnership"):
        await _assert(client, GoldenFact(field="legal_form", value=form, since=dt.date(2015, 3, 2)))
    profile = (await client.get("/v1/profile", params={"as_of": AS_OF})).json()
    assert profile["readings"]["legal_form"]["conflict"] is True
    assert profile["readings"]["legal_form"]["value"] is None
    assert await _app11(client) == "unassessable"


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
    assert await ledger.evidence("other-tenant") == ()


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
    profile = (await client.get("/v1/profile", params={"as_of": AS_OF})).json()
    current = {e["evidence_id"]: e["current"] for e in profile["evidence"]}
    assert current == {first["evidence_id"]: False, response.json()["evidence_id"]: True}
    assert profile["evidence"][1]["period"]["label"] == "FY2025-26"


@pytest.mark.parametrize(
    ("change", "names"),
    [
        pytest.param({"provenance": "inferred"}, "provenance", id="a-provenance"),
        pytest.param({"tenant_id": "other-tenant"}, "tenant_id", id="a-tenant"),
        pytest.param({"asserted_by": "someone"}, "asserted_by", id="an-author"),
        pytest.param({"field": "Annual Turnover"}, "snake_case", id="a-bad-field"),
        pytest.param(
            {"period": {"since": "2015-03-02", "financial_year": 2025}}, "one of", id="two-periods"
        ),
        pytest.param({"value_date": "2015-03-02"}, "exactly one", id="two-values"),
        pytest.param({"source": ""}, "source", id="no-source"),
    ],
)
async def test_an_invalid_fact_is_a_422_naming_what_is_wrong(
    client: httpx.AsyncClient,
    change: dict[str, Any],  # Any: JSON
    names: str,
) -> None:
    body = {
        **_body(GoldenFact(field="legal_form", value="trust", since=dt.date(2020, 1, 1))),
        **change,
    }
    response = await client.post("/v1/profile/evidence", json=body)
    assert response.status_code == 422
    assert names in response.text


async def test_a_coverage_is_recorded_and_reads_back_row_for_row(
    client: httpx.AsyncClient,
) -> None:
    for fact in GA05.facts:
        await _assert(client, fact)
    recorded = await client.post("/v1/coverage", params={"as_of": AS_OF})
    assert recorded.status_code == 201
    body = recorded.json()
    back = (await client.get(f"/v1/coverage/{body['assessment_id']}")).json()
    assert back["as_of"] == AS_OF
    assert back["corpus_version"] == "privacy-act-1988:C2026C00227"
    assert back["mapping_version"].startswith("sha256:")
    assert {
        o["obligation_id"]: (o["outcome"], o["reason"], o["citations"]) for o in back["obligations"]
    } == {
        o["obligation_id"]: (o["outcome"], o["reason"], o["citations"]) for o in body["obligations"]
    }
    assert (await client.get("/v1/coverage/no-such-assessment")).status_code == 404


async def test_the_same_mapping_gives_the_same_mapping_version(client: httpx.AsyncClient) -> None:
    first = (await client.post("/v1/coverage", params={"as_of": AS_OF})).json()
    await _assert(client, GoldenFact(field="is_agency", value=False, since=dt.date(2015, 3, 2)))
    second = (await client.post("/v1/coverage", params={"as_of": AS_OF})).json()
    versions = [
        (await client.get(f"/v1/coverage/{r['assessment_id']}")).json()["mapping_version"]
        for r in (first, second)
    ]
    assert versions[0] == versions[1]


# ---- subscriptions ----------------------------------------------------------------


async def test_subscribe_and_unsubscribe_touch_the_subscription_row_only(
    client: httpx.AsyncClient, ledger: FakeLedgerStore
) -> None:
    assert (await client.put("/v1/subscriptions/privacy-act-1988")).status_code == 204
    assert (await client.get("/v1/subscriptions")).json() == {"instruments": ["privacy-act-1988"]}
    subscriptions = ledger._subscriptions  # pyright: ignore[reportPrivateUsage] — who subscribed
    assert subscriptions[(TENANT, "privacy-act-1988")] == "lead@acme.example"
    assert (await client.delete("/v1/subscriptions/privacy-act-1988")).status_code == 204
    assert (await client.get("/v1/subscriptions")).json() == {"instruments": []}


async def test_a_subscription_to_a_non_library_id_is_a_422(client: httpx.AsyncClient) -> None:
    response = await client.put("/v1/subscriptions/not:a-library-id")
    assert response.status_code == 422


# ---- signed in, on the configured tenant ------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/profile"),
        ("POST", "/v1/profile/evidence"),
        ("GET", "/v1/coverage"),
        ("POST", "/v1/coverage"),
        ("GET", "/v1/coverage/x"),
        ("GET", "/v1/subscriptions"),
        ("PUT", "/v1/subscriptions/privacy-act-1988"),
        ("DELETE", "/v1/subscriptions/privacy-act-1988"),
    ],
)
async def test_every_endpoint_needs_a_signed_in_member(
    ledger: FakeLedgerStore, method: str, path: str
) -> None:
    async with await _client_for(ledger) as c:
        response = await c.request(method, path, headers={"Authorization": ""})
    assert response.status_code == 401


# ---- the real ledger --------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set — needs a migrated ledger")
async def test_recording_a_coverage_works_on_postgres() -> None:
    """The coverage rows reference their assessment row: the order the API
    writes them in must hold on the real ledger, not just the fake."""
    from tests.test_postgres_ledger_store import empty_ledger

    postgres = await empty_ledger()
    await postgres.put_profile(Profile(TENANT))
    async with await _client_for(postgres) as c:
        for fact in GA05.facts:
            await _assert(c, fact)
        recorded = (await c.post("/v1/coverage", params={"as_of": AS_OF})).json()
        back = (await c.get(f"/v1/coverage/{recorded['assessment_id']}")).json()
    assert len(back["obligations"]) == 37
    assert {o["outcome"] for o in back["obligations"]} == {"applicable", "not_applicable"}
