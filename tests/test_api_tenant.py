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
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from compliance_rag.adapters.fakes import FakeGraphStore, FakeLedgerStore
from compliance_rag.adapters.interfaces import (
    AssessmentRecord,
    GraphStore,
    LedgerStore,
    LoadedSource,
    LoadedSources,
    Profile,
    SourceKind,
    SourceVersion,
)
from compliance_rag.api import tenant
from compliance_rag.api.app import create_app
from compliance_rag.api.deps import ApiServices
from compliance_rag.assessment.coverage import Coverage
from compliance_rag.assessment.evidence import Evidence, EvidenceProvenance, Facts, Period
from compliance_rag.corpus.models import DocumentClass
from compliance_rag.eval.applicability import GoldenFact, load_applicability_set
from compliance_rag.graph.mappings import load_mapping_set
from tests.api_support import bearer, fake_api_services, mint
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for

TENANT = "acme"
AS_OF = "2026-10-12"
APP11 = "OB-PA-APP11-1"
GA05 = next(c for c in load_applicability_set().cases if c.id == "GA-05")


CORPUS_VERSION = SourceVersion(SourceKind.CORPUS, "c" * 40, "corpus-2026.10")
MAPPING_VERSION = SourceVersion(SourceKind.MAPPING, "d" * 40)  # no tag existed at load time
LOADED = LoadedSources(corpus=LoadedSource(CORPUS_VERSION), mapping=LoadedSource(MAPPING_VERSION))
"""Both loads finished: one tagged, one not."""


def _graph(sources: LoadedSources) -> FakeGraphStore:
    labels = load_applicability_set()
    graph = fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
    graph.sources = sources
    return graph


def _services(
    ledger: LedgerStore,
    sources: LoadedSources = LOADED,
    **extra: Any,  # Any: ApiServices fields
) -> ApiServices:
    return fake_api_services(
        ledger=ledger, graph=extra.pop("graph", None) or _graph(sources), **extra
    )


async def _client_for(
    ledger: LedgerStore,
    sources: LoadedSources = LOADED,
    **extra: Any,  # Any: ApiServices fields
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(_services(ledger, sources, **extra))),
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


# ---- recorded assessments (#153) --------------------------------------------------

UUID4_HEX = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}")
LEFT_OUT = ("instrument_id", "summary")
"""What computed coverage says that the ledger's `coverage` rows don't hold."""


async def _assess(client: httpx.AsyncClient, **body: Any) -> httpx.Response:  # Any: JSON
    return await client.post("/v1/coverage", json={"as_of": AS_OF, **body})


def _nothing_may_be_recorded(ledger: FakeLedgerStore, monkeypatch: pytest.MonkeyPatch) -> None:
    async def recorded(**_: object) -> None:
        raise AssertionError("an assessment was recorded")

    monkeypatch.setattr(ledger, "record_coverage_assessment", recorded)


async def test_an_assessment_is_recorded_with_its_versions_and_read_back_row_for_row(
    client: httpx.AsyncClient, ledger: FakeLedgerStore
) -> None:
    for fact in GA05.facts:
        await _assert(client, fact)
    response = await _assess(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert UUID4_HEX.fullmatch(body["assessment_id"])
    assert body["tenant_id"] == TENANT and body["as_of"] == AS_OF
    assert body["corpus_version"] == {"tag": "corpus-2026.10", "sha": "c" * 40, "untagged": False}
    assert body["mapping_version"] == {"tag": None, "sha": "d" * 40, "untagged": True}
    assert body["recorded_by"] == "lead@acme.example"
    assert body["counts"] == {"applicable": 35, "not_applicable": 2, "unassessable": 0}

    # Read back, it is what was returned.
    read = await client.get(f"/v1/coverage/{body['assessment_id']}")
    assert read.status_code == 200
    assert read.json() == body

    # Row for row what Track A computed on that date, less what the ledger doesn't keep.
    live = (await client.get("/v1/coverage", params={"as_of": AS_OF})).json()
    computed = {o["obligation_id"]: o for o in live["obligations"]}
    assert [o["obligation_id"] for o in body["obligations"]] == sorted(computed)
    for obligation in body["obligations"]:
        expected = {
            k: v for k, v in computed[obligation["obligation_id"]].items() if k not in LEFT_OUT
        }
        assert obligation == expected

    # And in the ledger: in the member's name, with this request's trace_id.
    stored = await ledger.get_assessment(TENANT, body["assessment_id"])
    assert stored is not None
    assert (stored.user_id, stored.trace_id) == (
        "lead@acme.example",
        response.headers["x-trace-id"],
    )
    assert (stored.corpus, stored.mapping) == (CORPUS_VERSION, MAPPING_VERSION)
    assert len(await ledger.coverage_rows(TENANT, body["assessment_id"])) == 37


async def test_an_unassessable_obligation_is_recorded_with_what_would_settle_it(
    client: httpx.AsyncClient,
) -> None:
    body = (await _assess(client)).json()  # no evidence at all
    app11 = next(o for o in body["obligations"] if o["obligation_id"] == APP11)
    assert app11["outcome"] == "unassessable" and app11["questions"]
    assert all(q["question"] and q["cites"] for q in app11["questions"])
    assert (await client.get(f"/v1/coverage/{body['assessment_id']}")).json() == body


async def test_the_member_who_asked_is_who_it_is_recorded_for(
    ledger: FakeLedgerStore,
) -> None:
    headers = bearer(mint(email="Second@Acme.example", sub="uid-456"))
    async with await _client_for(ledger) as c:
        body = (await c.post("/v1/coverage", json={}, headers=headers)).json()
    assert body["recorded_by"] == "second@acme.example"


async def test_each_assessment_is_its_own_record(
    client: httpx.AsyncClient, ledger: FakeLedgerStore
) -> None:
    first = (await _assess(client)).json()
    await _assert(client, GA05.facts[0])
    second = (await _assess(client)).json()
    assert first["assessment_id"] != second["assessment_id"]
    for body in (first, second):
        assert (await client.get(f"/v1/coverage/{body['assessment_id']}")).json() == body


async def test_the_default_date_of_an_assessment_is_today_in_sydney(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tenant, "today", lambda: dt.date(2026, 5, 1))
    empty_body = await client.post("/v1/coverage", json={})
    no_body = await client.post("/v1/coverage")
    for response in (empty_body, no_body):
        assert response.status_code == 201, response.text
        assert response.json()["as_of"] == "2026-05-01"
        assert response.json()["counts"]["unassessable"] == 37


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"as_of": "9999-07-01"}, id="a-date-after-the-calendar"),
        pytest.param({"as_of": "1066-10-14"}, id="a-date-before-it"),
        pytest.param({"as_of": "not-a-date"}, id="not-a-date"),
        pytest.param({"tenant_id": "other-tenant"}, id="a-tenant"),
        pytest.param({"corpus_version": "corpus-2099"}, id="a-version-named-by-the-request"),
        pytest.param({"mapping_version": {"tag": "x", "sha": "e" * 40}}, id="another-version"),
        pytest.param({"user_id": "someone@else.example"}, id="a-user"),
    ],
)
async def test_an_invalid_assessment_request_is_a_422_and_records_nothing(
    client: httpx.AsyncClient,
    ledger: FakeLedgerStore,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],  # Any: JSON
) -> None:
    _nothing_may_be_recorded(ledger, monkeypatch)
    assert (await client.post("/v1/coverage", json=body)).status_code == 422


async def test_nothing_is_recorded_while_the_graph_holds_no_finished_load(
    ledger: FakeLedgerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _nothing_may_be_recorded(ledger, monkeypatch)
    async with await _client_for(ledger, LoadedSources()) as c:
        response = await _assess(c)
    assert response.status_code == 503
    # It says why, and the fix: both loaders, by name, and the ticket.
    for named in ("#153", "corpus version", "mapping version"):
        assert named in response.json()["detail"]
    assert "scripts.load_graph_structure" in response.json()["detail"]
    assert "scripts.load_mappings" in response.json()["detail"]
    assert "retry-after" not in response.headers  # loading again is what's needed, not waiting


@pytest.mark.parametrize("missing", list(SourceKind))
async def test_nothing_is_recorded_while_either_kind_has_no_finished_load(
    ledger: FakeLedgerStore, monkeypatch: pytest.MonkeyPatch, missing: SourceKind
) -> None:
    _nothing_may_be_recorded(ledger, monkeypatch)
    sources = LoadedSources(
        corpus=LoadedSource(CORPUS_VERSION if missing is SourceKind.MAPPING else None),
        mapping=LoadedSource(MAPPING_VERSION if missing is SourceKind.CORPUS else None),
    )
    async with await _client_for(ledger, sources) as c:
        response = await _assess(c)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert f"{missing.value} version" in detail
    other = next(k for k in SourceKind if k is not missing)
    assert f"{other.value} version" not in detail
    assert tenant.LOADERS[missing] in detail and tenant.LOADERS[other] not in detail


@pytest.mark.parametrize("loading", list(SourceKind))
@pytest.mark.parametrize("finished_before", [True, False], ids=["reload", "first-load"])
async def test_nothing_is_recorded_while_a_load_is_unfinished(
    ledger: FakeLedgerStore,
    monkeypatch: pytest.MonkeyPatch,
    loading: SourceKind,
    finished_before: bool,
) -> None:
    """Running, or failed: either way the graph may be part-written."""
    _nothing_may_be_recorded(ledger, monkeypatch)
    held = {SourceKind.CORPUS: CORPUS_VERSION, SourceKind.MAPPING: MAPPING_VERSION}
    sources = LoadedSources(
        corpus=LoadedSource(
            held[SourceKind.CORPUS]
            if finished_before or loading is not SourceKind.CORPUS
            else None,
            loading=loading is SourceKind.CORPUS,
        ),
        mapping=LoadedSource(
            held[SourceKind.MAPPING]
            if finished_before or loading is not SourceKind.MAPPING
            else None,
            loading=loading is SourceKind.MAPPING,
        ),
    )
    async with await _client_for(ledger, sources) as c:
        response = await _assess(c)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert f"a {loading.value} load hasn't finished" in response.json()["detail"]


@pytest.mark.parametrize("abandoned", list(SourceKind))
@pytest.mark.parametrize("finished_before", [True, False], ids=["reload", "first-load"])
async def test_nothing_is_recorded_after_a_load_was_abandoned_until_one_finishes(
    ledger: FakeLedgerStore,
    monkeypatch: pytest.MonkeyPatch,
    abandoned: SourceKind,
    finished_before: bool,
) -> None:
    """Abandoning a failed load frees the next one to begin; it doesn't make
    the graph the failed load may have written to trustworthy. Waiting won't
    help, so there is no Retry-After; a load must finish."""
    _nothing_may_be_recorded(ledger, monkeypatch)
    held = {SourceKind.CORPUS: CORPUS_VERSION, SourceKind.MAPPING: MAPPING_VERSION}

    def state(kind: SourceKind) -> LoadedSource:
        if kind is not abandoned:
            return LoadedSource(held[kind])
        return LoadedSource(held[kind] if finished_before else None, blocked_by_abandoned=True)

    sources = LoadedSources(corpus=state(SourceKind.CORPUS), mapping=state(SourceKind.MAPPING))
    async with await _client_for(ledger, sources) as c:
        response = await _assess(c)
    assert response.status_code == 503
    assert "retry-after" not in response.headers
    detail = response.json()["detail"]
    assert f"{abandoned.value} load was abandoned" in detail and "none has finished since" in detail
    other = next(k for k in SourceKind if k is not abandoned)
    assert tenant.LOADERS[abandoned] in detail and tenant.LOADERS[other] not in detail


async def test_an_open_load_is_waited_for_before_an_abandoned_one_is_dealt_with(
    ledger: FakeLedgerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _nothing_may_be_recorded(ledger, monkeypatch)
    sources = LoadedSources(
        corpus=LoadedSource(CORPUS_VERSION, blocked_by_abandoned=True),
        mapping=LoadedSource(MAPPING_VERSION, loading=True),
    )
    async with await _client_for(ledger, sources) as c:
        response = await _assess(c)
    assert response.status_code == 503 and response.headers["retry-after"] == "30"


class LoadChangesMidRequest(GraphStore):
    """A graph whose loads change while Track A is working: after the
    coverage is computed, `then` is what it holds."""

    def __init__(self, inner: FakeGraphStore, then: LoadedSources) -> None:
        self._inner = inner
        self._then = then

    async def coverage(self, facts: Facts, *, tenant_id: str, as_of: dt.date) -> Coverage:
        covered = await self._inner.coverage(facts, tenant_id=tenant_id, as_of=as_of)
        self._inner.sources = self._then
        return covered

    async def crosswalks(
        self, obligation_id: str, *, as_of: dt.date, max_depth: int
    ) -> tuple[str, ...]:
        return await self._inner.crosswalks(obligation_id, as_of=as_of, max_depth=max_depth)

    async def loaded_sources(self) -> LoadedSources:
        return await self._inner.loaded_sources()


@pytest.mark.parametrize(
    "then",
    [
        pytest.param(
            LoadedSources(
                corpus=LoadedSource(CORPUS_VERSION),
                mapping=LoadedSource(MAPPING_VERSION, loading=True),
            ),
            id="a-load-started",
        ),
        pytest.param(
            LoadedSources(
                corpus=LoadedSource(CORPUS_VERSION),
                mapping=LoadedSource(
                    replace(MAPPING_VERSION, sha="e" * 40, tag="mappings-2026.11")
                ),
            ),
            id="a-load-finished-with-another-version",
        ),
        pytest.param(
            LoadedSources(corpus=LoadedSource(CORPUS_VERSION), mapping=LoadedSource()),
            id="the-record-went-away",
        ),
    ],
)
async def test_nothing_is_recorded_if_the_loads_changed_while_coverage_was_computed(
    ledger: FakeLedgerStore, monkeypatch: pytest.MonkeyPatch, then: LoadedSources
) -> None:
    _nothing_may_be_recorded(ledger, monkeypatch)
    graph = LoadChangesMidRequest(_graph(LOADED), then)
    async with await _client_for(ledger, graph=graph) as c:
        response = await _assess(c)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert "while the coverage was computed" in response.json()["detail"]


async def test_the_same_loads_before_and_after_are_recorded(ledger: FakeLedgerStore) -> None:
    graph = LoadChangesMidRequest(_graph(LOADED), LOADED)
    async with await _client_for(ledger, graph=graph) as c:
        assert (await _assess(c)).status_code == 201


async def test_another_tenants_assessment_is_not_found(
    client: httpx.AsyncClient, ledger: FakeLedgerStore
) -> None:
    theirs, mine = uuid.uuid4().hex, uuid.uuid4().hex
    for tenant_id, assessment_id in (("other-tenant", theirs), (TENANT, mine)):
        await ledger.record_coverage_assessment(
            AssessmentRecord(
                tenant_id=tenant_id,
                assessment_id=assessment_id,
                user_id="someone@example.com",
                corpus=CORPUS_VERSION,
                mapping=MAPPING_VERSION,
                as_of=dt.date(2026, 10, 12),
                trace_id="a" * 32,
            ),
            (),
        )
    assert (await client.get(f"/v1/coverage/{mine}")).status_code == 200
    hostile = {"tenant_id": "other-tenant"}
    response = await client.get(
        f"/v1/coverage/{theirs}", params=hostile, headers={"X-Tenant-Id": "other-tenant"}
    )
    assert response.status_code == 404
    assert (await client.get(f"/v1/coverage/{uuid.uuid4().hex}")).status_code == 404


async def test_an_assessment_is_recorded_for_the_configured_tenant_only(
    client: httpx.AsyncClient, ledger: FakeLedgerStore
) -> None:
    body = (await _assess(client)).json()
    assert body["tenant_id"] == TENANT
    assert await ledger.get_assessment("other-tenant", body["assessment_id"]) is None
    assert await ledger.coverage_rows("other-tenant", body["assessment_id"]) == ()


@pytest.mark.parametrize(
    "assessment_id",
    [
        "not-an-id",
        "0" * 31,
        "0" * 33,
        uuid.uuid4().hex.upper(),
        uuid.uuid1().hex,  # a uuid, but not a version 4 one
    ],
)
async def test_an_assessment_id_that_is_not_a_uuid4_is_a_422(
    client: httpx.AsyncClient, assessment_id: str
) -> None:
    assert (await client.get(f"/v1/coverage/{assessment_id}")).status_code == 422


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
    ("POST", "/v1/coverage"),
    ("GET", f"/v1/coverage/{uuid.uuid4().hex}"),
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
            document_class=DocumentClass.LIBRARY_LAW,
            title="The document's title",
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


@pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set — needs a migrated ledger")
async def test_an_assessment_and_its_coverage_land_together_on_postgres() -> None:
    import psycopg

    from tests.test_postgres_ledger_store import empty_ledger

    postgres = await empty_ledger()
    try:
        await postgres.put_profile(Profile(TENANT))
        async with await _client_for(postgres) as c:
            for fact in GA05.facts:
                await _assert(c, fact)
            response = await _assess(c)
            assert response.status_code == 201, response.text
            body = response.json()
            assert (await c.get(f"/v1/coverage/{body['assessment_id']}")).json() == body
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            cursor = await conn.execute(
                "SELECT user_id, corpus_version, corpus_sha, mapping_version, mapping_sha, as_of, "
                "trace_id FROM assessment WHERE tenant_id = %s AND assessment_id = %s",
                (TENANT, body["assessment_id"]),
            )
            assert await cursor.fetchall() == [
                (
                    "lead@acme.example",
                    "corpus-2026.10",  # the tag
                    "c" * 40,  # and the commit it pointed at
                    None,  # untagged
                    "d" * 40,
                    dt.date.fromisoformat(AS_OF),
                    response.headers["x-trace-id"],
                )
            ]
            cursor = await conn.execute(
                "SELECT count(*), count(DISTINCT trace_id), min(trace_id) FROM coverage "
                "WHERE tenant_id = %s AND assessment_id = %s",
                (TENANT, body["assessment_id"]),
            )
            assert await cursor.fetchone() == (
                len(body["obligations"]),
                1,
                response.headers["x-trace-id"],
            )
    finally:
        await empty_ledger()
