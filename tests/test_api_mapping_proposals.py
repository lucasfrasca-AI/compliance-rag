"""Proposing policy mappings over the API (#305, ADR-0053 F6, ADR-0059 Q8 and C2):
`POST /v1/policy-mappings/proposals` streams `coverage` then `proposals` (or `error`), and
`GET /v1/policy-mappings` reads back what the ledger holds.

Everything is real but the stores and the model: the API's own app, the real sign-in over
stand-in keys, Track A over a fake graph holding the corpus's signed mappings (so the
obligation digest is the real one), Acme's six synthetic policies in the ledger and clause
store, and the real proposer over a scripted model. Offshore tests run with generation at
`us`, as in the build phase. Nothing is billable.

The tenant is `acme`. `lead@acme.example` is an admin (and the one build-phase tester where a
test says so); `second@acme.example` is a member who is not.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Any, cast

import httpx
import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGraphStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import (
    Generation,
    GenerationUnavailable,
    Generator,
    MappingStatus,
    ReplyKind,
    Unavailability,
)
from compliance_rag.api import mapping_proposals
from compliance_rag.api.app import create_app
from compliance_rag.assessment.coverage import Coverage
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.harness import golden_clause_store
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import MEMBER, bearer, fake_api_services, verifier_for
from tests.claims_support import ScriptedGenerator
from tests.findings_support import (
    DAY,
    TENANT,
    evidence_of,
    golden_world_ledger,
    graph_over_mappings,
)
from tests.mapping_support import (
    APP11_1,
    APP11_2,
    MODEL,
    SEC_3_1,
    SECURITY,
    NamedModel,
    item,
    reply,
)
from tests.test_api_check_claims import events_of
from tests.test_api_checks import (
    ADMIN_TOKEN,
    ORIGIN,
    SECOND,
    SUBMITTER_TOKEN,
    THIRD_TOKEN,
    _settings,  # pyright: ignore[reportPrivateUsage]
)

ROOT = pathlib.Path(__file__).parents[1]
PROPOSE = "/v1/policy-mappings/proposals"
LIST = "/v1/policy-mappings"
SECURITY_BODY = {"instrument_id": SECURITY[0], "version_id": SECURITY[1]}
OFFSHORE: dict[str, Any] = {  # Any: settings field values
    "gemini_region": BUILD_PHASE_OFFSHORE_REGION,
    "offshore_generation_testers": MEMBER,
}


class GraphSpy:
    """Track A's graph, as a proposing run may use it: it can read the coverage and nothing
    else. Any other attribute, a read of the loads or a crosswalk as much as a write (`put_node`,
    `put_edge`, anything a graph writer has), **raises at once** and is recorded in `refused`, so
    a write fails the test where it happens rather than going unnoticed."""

    ALLOWED = frozenset({"coverage"})

    def __init__(self, inner: FakeGraphStore) -> None:
        self._inner = inner
        self.entered: list[str] = []
        self.refused: list[str] = []

    async def coverage(self, *args: Any, **kwargs: Any) -> Coverage:  # Any: passed through
        self.entered.append("coverage")
        return await self._inner.coverage(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:  # Any: whatever was asked for, which is refused
        # Only reached for an attribute this class does not define: every other method.
        if name.startswith("__"):
            raise AttributeError(name)
        self.refused.append(name)
        raise AssertionError(
            f"a proposing run touched the graph's {name!r}: it may only read coverage"
        )


@dataclass
class World:
    ledger: FakeLedgerStore
    clauses: FakeClauseStore
    graph: GraphSpy
    model: Any  # Any: a scripted generator, or a stand-in for one
    app: Any  # Any: the FastAPI app, only handed to a transport

    def client(self, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url=ORIGIN,
            headers={"Origin": ORIGIN, **bearer(token)},
        )


async def world(
    model: Any = None,  # Any: a Generator
    **settings: Any,  # Any: settings field values
) -> World:
    ledger = await golden_world_ledger(evidence_of("GA-05"))
    clauses = await golden_clause_store()
    store = FakeRetrievalStore(ledger=ledger)
    scripted = model or NamedModel()
    configured = _settings(**settings)
    graph = GraphSpy(graph_over_mappings())
    app = create_app(
        fake_api_services(
            settings=configured,
            verifier=verifier_for(api_members=configured.api_members, **settings),
            ledger=ledger,
            clauses=clauses,
            graph=cast("Any", graph),  # a GraphStore as far as a run may use one
            pipeline=AssessmentPipeline(
                RetrievalPipeline(store, FakeReranker()),
                scripted,
                Verifier(clauses, ledger, CanonicalCitationVerifier()),
                ledger,
                tenant_text=TenantTextGate.from_settings(configured),
            ),
            lifecycle=CorpusLifecycle(
                ledger=ledger,
                retrieval=store,
                clauses=clauses,
                generation_region=configured.generation_region,
                synthetic_registry=SyntheticRegistry.load(),
            ),
        )
    )
    return World(ledger, clauses, graph, scripted, app)


@pytest.fixture(autouse=True)
def _a_fixed_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mapping_proposals, "today", lambda: DAY)


async def propose(
    w: World, token: str = ADMIN_TOKEN, body: dict[str, Any] | None = None
) -> tuple[httpx.Response, list[tuple[str, dict[str, Any]]]]:  # Any: JSON
    async with w.client(token) as client:
        response = await client.post(PROPOSE, json=SECURITY_BODY if body is None else body)
    events = events_of(response.text) if response.status_code == 200 else []
    return response, events


def tree_hash(*roots: str) -> str:
    digest = hashlib.sha256()
    for root in roots:
        for path in sorted((ROOT / root).rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


# ---- the stages and the result ---------------------------------------------------------------


async def test_an_admin_asks_and_the_stream_carries_coverage_then_the_proposals_recorded() -> None:
    w = await world()
    response, events = await propose(w)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [name for name, _ in events] == ["coverage", "proposals"]
    coverage, proposals = events[0][1], events[1][1]
    assert coverage["instrument_id"] == SECURITY[0] and coverage["as_of"] == DAY.isoformat()
    assert sum(coverage["counts"].values()) > 0

    assert proposals["accepted"] == 2 and proposals["appended"] == 2
    assert proposals["dropped"] == 0 and proposals["dropped_reasons"] == []
    assert proposals["proposer"] == MODEL and proposals["generator"] == "gemini"
    assert proposals["counts"] == {"proposed": 2, "approved": 0, "rejected": 0}
    assert proposals["cost_usd"] > 0 and proposals["trace_id"]
    first, second = proposals["mappings"]
    assert first["policy_clause_id"] == SEC_3_1 and first["obligation_id"] == APP11_1
    assert first["kind"] == "implements" and first["status"] == "proposed"
    assert first["approver"] is None and first["proposer"] == MODEL
    assert second["kind"] == "partially_implements"
    # a clause as a reader cites it, linked to its source; the model's words, labelled
    assert first["cite"].endswith(", 3.1") and first["href"].startswith("/")
    assert first["rationale"] == {"text": first["rationale"]["text"], "source": "model"}
    assert first["trace_id"] == proposals["trace_id"]

    # the ledger holds what was shown
    rows = await w.ledger.policy_mappings(TENANT)
    assert [m.mapping_id for m in rows] == [first["mapping_id"], second["mapping_id"]]
    assert {m.status for m in rows} == {MappingStatus.PROPOSED}
    assert {m.proposer for m in rows} == {MODEL}
    assert len(await w.ledger.usage(TENANT)) == 1


async def test_what_was_recorded_is_read_back_by_an_admin_alone() -> None:
    w = await world()
    await propose(w)
    async with w.client(ADMIN_TOKEN) as client:
        everything = (await client.get(LIST)).json()
        one = (await client.get(LIST, params=SECURITY_BODY)).json()
        none = (
            await client.get(
                LIST, params={"instrument_id": "acme/privacy-policy", "version_id": "v1"}
            )
        ).json()
        half = await client.get(LIST, params={"instrument_id": SECURITY[0]})
    assert everything["counts"]["proposed"] == 2 and len(everything["mappings"]) == 2
    # where the version's run stands, by the one rule the page uses
    assert everything["runs"] == [
        {
            "instrument_id": SECURITY[0],
            "version_id": SECURITY[1],
            "running": False,
            "unfinished": False,
        }
    ]
    assert one == everything and none["mappings"] == []
    assert half.status_code == 422 and half.json()["detail"]["error"] == "name_both"
    async with w.client(SUBMITTER_TOKEN) as client:  # a member who is not an admin
        assert (await client.get(LIST)).status_code == 403
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=w.app), base_url=ORIGIN
    ) as anonymous:
        assert (await anonymous.get(LIST)).status_code == 401


async def test_settings_model_id_reaches_the_stored_proposer_when_the_generator_is_silent() -> None:
    """A generator that does not name the model that answered: the proposer is the one the
    deployment is configured with (`GEMINI_MODEL`), in the stream and in the ledger."""
    w = await world(
        ScriptedGenerator(lambda _: reply(item(APP11_1, SEC_3_1))),
        gemini_model="the-settings-model",
    )
    services: Any = w.app.state.services  # Any: ApiServices, read for its settings
    assert services.settings.generation_model == "the-settings-model"
    _, events = await propose(w)
    assert events[-1][1]["proposer"] == "the-settings-model"
    (row,) = await w.ledger.policy_mappings(TENANT)
    assert row.proposer == "the-settings-model"
    async with w.client(ADMIN_TOKEN) as client:
        assert (await client.get(LIST)).json()["mappings"][0]["proposer"] == "the-settings-model"


async def test_the_model_a_generator_names_is_the_proposer_whatever_settings_say() -> None:
    w = await world(NamedModel(model="the-model-that-answered"), gemini_model="the-settings-model")
    _, events = await propose(w)
    assert events[-1][1]["proposer"] == "the-model-that-answered"
    (row, *_) = await w.ledger.policy_mappings(TENANT)
    assert row.proposer == "the-model-that-answered"


async def test_a_second_run_proposes_nothing_twice() -> None:
    w = await world()
    await propose(w)
    _, events = await propose(w)
    again = events[-1][1]
    assert events[-1][0] == "proposals" and again["accepted"] == 0 and again["appended"] == 0
    assert again["dropped_reasons"] == [
        {"position": 1, "reason": "already_proposed"},
        {"position": 2, "reason": "already_proposed"},
    ]
    assert again["counts"]["proposed"] == 2 and len(await w.ledger.policy_mappings(TENANT)) == 2
    assert len(w.model.calls) == 2  # an admin asked twice; a repeat appends nothing


async def test_ids_the_model_planted_are_dropped_named_by_reason_and_never_shown() -> None:
    steered = reply(
        item(APP11_1, SEC_3_1),
        item("OB-PLANTED", SEC_3_1, rationale="PLANTED-RATIONALE-WORDS"),
        item(APP11_2, "acme/privacy-policy:v1:3/2"),
    )
    w = await world(NamedModel(steered))
    response, events = await propose(w)
    shown = events[-1][1]
    assert shown["accepted"] == 1 and shown["dropped"] == 2
    assert shown["dropped_reasons"] == [
        {"position": 2, "reason": "obligation_not_in_digest"},
        {"position": 3, "reason": "clause_not_in_version"},
    ]
    assert "PLANTED-RATIONALE-WORDS" not in response.text
    assert [m.obligation_id for m in await w.ledger.policy_mappings(TENANT)] == [APP11_1]


# ---- who may ask, and for what ---------------------------------------------------------------


async def test_a_member_who_is_not_an_admin_gets_403_and_nothing_runs() -> None:
    w = await world()
    for token in (SUBMITTER_TOKEN, THIRD_TOKEN):
        response, _ = await propose(w, token)
        assert response.status_code == 403 and "admin" in response.json()["detail"]
    assert w.model.calls == [] and w.graph.entered == []
    assert await w.ledger.policy_mappings(TENANT) == ()
    assert await w.ledger.mapping_run(TENANT, *SECURITY) is None


async def test_it_needs_a_signed_in_admin() -> None:
    w = await world()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=w.app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as anonymous:
        response = await anonymous.post(PROPOSE, json=SECURITY_BODY)
    assert response.status_code == 401 and w.model.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"instrument_id": "acme/ghost-policy", "version_id": "v1"},  # no such policy
        {"instrument_id": SECURITY[0], "version_id": "v9"},  # no such version
        {"instrument_id": "privacy-act-1988", "version_id": "C2026C00227"},  # law, not a policy
        {"instrument_id": "other-corp/privacy-policy", "version_id": "v1"},  # another tenant's
        {"instrument_id": "acme/data security\n", "version_id": "v1"},  # not an id at all
    ],
    ids=["ghost", "no-version", "law", "other-tenant", "not-an-id"],
)
async def test_a_version_that_is_not_a_policy_in_force_is_not_found_and_nothing_is_reserved(
    body: dict[str, Any],  # Any: JSON
) -> None:
    w = await world()
    response, _ = await propose(w, body=body)
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "not_a_policy_version"
    assert w.model.calls == [] and w.graph.entered == []
    assert await w.ledger.policy_mappings(TENANT) == ()
    assert await w.ledger.mapping_run(TENANT, body["instrument_id"], body["version_id"]) is None


async def test_a_version_that_has_ended_is_not_in_force_so_it_is_not_proposed_for() -> None:
    w = await world()
    await w.ledger.end_version(
        tenant_id=TENANT, instrument_id=SECURITY[0], version_id=SECURITY[1], valid_to=DAY
    )
    response, _ = await propose(w)
    assert response.status_code == 404 and w.model.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"instrument_id": SECURITY[0]},
        {**SECURITY_BODY, "status": "approved"},  # no field a body could set a status by
        {**SECURITY_BODY, "proposer": "someone"},
        {"instrument_id": "", "version_id": "v1"},
        {"instrument_id": 7, "version_id": "v1"},
    ],
)
async def test_a_body_that_names_more_or_less_than_a_version_is_refused(
    body: dict[str, Any],  # Any: JSON
) -> None:
    w = await world()
    response, _ = await propose(w, body=body)
    assert response.status_code == 422 and w.model.calls == []
    assert await w.ledger.policy_mappings(TENANT) == ()


# ---- nothing is written to the graph, or to a file (ADR-0018, ADR-0059 C2) ---------------------


async def test_nothing_is_written_to_the_graph_or_the_repo() -> None:
    before = tree_hash("corpus", "db")
    w = await world()
    await propose(w)
    await propose(w)
    # the graph was read for Track A's coverage and for nothing else: the spy raises on any
    # other method (a write above all), so this is a run-time fact, not a reading of imports
    assert set(w.graph.entered) == {"coverage"} and w.graph.refused == []
    assert tree_hash("corpus", "db") == before  # no mapping YAML, no schema, no instrument
    # the proposals are in the ledger: the one place a tenant's mapping lives
    assert len(await w.ledger.policy_mappings(TENANT)) == 2


# ---- the residency gates, as for Ask -----------------------------------------------------------


async def test_the_graph_spy_raises_on_a_write_and_on_every_read_but_coverage() -> None:
    """The spy itself: what it refuses is the claim the test above rests on."""
    spy = GraphSpy(graph_over_mappings())
    for name in ("put_node", "put_edge", "put_nodes", "put_edges", "loaded_sources", "crosswalks"):
        with pytest.raises(AssertionError, match=name):
            getattr(spy, name)
    assert spy.refused == [
        "put_node",
        "put_edge",
        "put_nodes",
        "put_edges",
        "loaded_sources",
        "crosswalks",
    ]


async def test_offshore_only_a_registered_tester_may_ask() -> None:
    w = await world(**{**OFFSHORE, "offshore_generation_testers": SECOND})
    response, events = await propose(w)  # the admin is not a tester
    assert response.status_code == 200 and [name for name, _ in events] == ["error"]
    assert events[0][1]["error"] == "residency" and "tester" in events[0][1]["message"]
    assert events[0][1]["instrument_id"] == SECURITY[0]
    assert w.model.calls == [] and await w.ledger.usage(TENANT) == ()
    assert await w.ledger.policy_mappings(TENANT) == ()
    # refused before the version was read or reserved: no run row, and no graph read
    assert await w.ledger.mapping_runs(TENANT) == {} and w.graph.entered == []


async def test_offshore_a_refused_admin_learns_nothing_about_versions_and_creates_no_run() -> None:
    """The offshore gate comes before the version read and the reservation, so what a
    refused admin is told is the same for a version that exists, one that does not, and law."""
    w = await world(**{**OFFSHORE, "offshore_generation_testers": SECOND})
    told: list[tuple[int, object]] = []
    for body in (
        SECURITY_BODY,
        {"instrument_id": "acme/ghost-policy", "version_id": "v1"},
        {"instrument_id": SECURITY[0], "version_id": "v9"},
        {"instrument_id": "privacy-act-1988", "version_id": "C2026C00227"},
    ):
        response, events = await propose(w, body=body)
        assert [name for name, _ in events] == ["error"] and events[0][1]["error"] == "residency"
        told.append((response.status_code, events[0][1]["message"]))
    assert {t for t in told} == {(200, told[0][1])}  # one answer, whatever was asked about
    assert await w.ledger.mapping_runs(TENANT) == {}  # no run row for any of them
    assert w.model.calls == [] and w.graph.entered == []


async def test_offshore_a_tester_admin_may_ask_for_registered_synthetic_policies() -> None:
    w = await world(**OFFSHORE)
    _, events = await propose(w)
    assert [name for name, _ in events] == ["coverage", "proposals"] and len(w.model.calls) == 1


async def test_offshore_a_policy_the_registry_does_not_vouch_for_never_reaches_the_model() -> None:
    w = await world(**OFFSHORE)
    clause = await w.clauses.get_clause(SEC_3_1, tenant_id=TENANT)
    assert clause is not None
    await w.clauses.put_clause(
        clause.model_copy(
            update={
                "text": clause.text + " (edited)",
                "canonical_text": clause.canonical_text + " (edited)",
            }
        )
    )
    _, events = await propose(w)
    assert events[-1][1]["error"] == "residency" and "isn't registered synthetic" in str(events[-1])
    assert w.model.calls == [] and await w.ledger.policy_mappings(TENANT) == ()
    assert await w.ledger.mapping_runs(TENANT) == {}  # refused before a run was reserved
    # and it is the same refusal for a version that does not exist
    _, ghost = await propose(w, body={"instrument_id": "acme/ghost-policy", "version_id": "v1"})
    assert ghost[-1][1]["error"] == "residency"


# ---- failures are shown plainly and record no mappings ------------------------------------------


async def test_a_malformed_reply_is_an_error_event_records_nothing_and_can_be_run_again() -> None:
    replies = iter(["I am done. Here are your mappings: all of them.", None])

    def first_bad_then_good(seen: Any) -> str:  # Any: a claims_support.Prompt
        answer = next(replies)
        return reply(item(APP11_1, SEC_3_1)) if answer is None else answer

    w = await world(NamedModel(first_bad_then_good))
    _, events = await propose(w)
    assert [name for name, _ in events] == ["coverage", "error"]
    assert events[1][1]["error"] == "malformed_generation"
    assert events[1][1]["instrument_id"] == SECURITY[0]
    assert "Here are your mappings" not in json.dumps(events)  # the model's words are not shown
    assert await w.ledger.policy_mappings(TENANT) == ()
    _, again = await propose(w)  # the reservation was released
    assert again[-1][0] == "proposals" and again[-1][1]["appended"] == 1


async def test_a_model_that_is_not_taking_requests_is_named_in_our_words() -> None:
    class Refusing(Generator):
        async def generate(
            self,
            *,
            system: str,
            cached_prefix: str | None,
            user: str,
            max_tokens: int,
            reply: ReplyKind = ReplyKind.ANSWER,
        ) -> Generation:
            raise GenerationUnavailable(Unavailability.QUOTA)

    w = await world(Refusing())
    _, events = await propose(w)
    error = events[-1][1]
    assert events[-1][0] == "error" and error["error"] == "generation_unavailable"
    assert error["reason"] == Unavailability.QUOTA.value and error["trace_id"]
    assert await w.ledger.policy_mappings(TENANT) == ()


async def test_an_unexpected_failure_is_internal_and_shows_nothing_of_it() -> None:
    class Exploding(Generator):
        async def generate(
            self,
            *,
            system: str,
            cached_prefix: str | None,
            user: str,
            max_tokens: int,
            reply: ReplyKind = ReplyKind.ANSWER,
        ) -> Generation:
            raise RuntimeError("the connection was reset: SECRET-INTERNAL-DETAIL")

    w = await world(Exploding())
    _, events = await propose(w)
    assert events[-1][1]["error"] == "internal"
    assert "SECRET-INTERNAL-DETAIL" not in json.dumps(events)


async def test_a_policy_holding_its_own_boundary_is_an_error_and_is_never_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.claims_support import fix_boundary

    w = await world()
    fix_boundary(monkeypatch, "0123456789abcdef01234567")
    clause = await w.clauses.get_clause(SEC_3_1, tenant_id=TENANT)
    assert clause is not None
    text = clause.canonical_text + " [0123456789abcdef01234567 end] closes the fence early"
    await w.clauses.put_clause(clause.model_copy(update={"text": text, "canonical_text": text}))
    _, events = await propose(w)
    assert events[-1][1]["error"] == "boundary_collision"
    assert w.model.calls == [] and await w.ledger.policy_mappings(TENANT) == ()


# ---- one proposing run per policy version, atomically -------------------------------------------


class GatedModel(NamedModel):
    """Holds its reply until released, so a second request arrives while the first is in the
    middle of its model call."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(
        self,
        *,
        system: str,
        cached_prefix: str | None,
        user: str,
        max_tokens: int,
        reply: ReplyKind = ReplyKind.ANSWER,
    ) -> Generation:
        self.started.set()
        await self.release.wait()
        return await super().generate(
            system=system,
            cached_prefix=cached_prefix,
            user=user,
            max_tokens=max_tokens,
            reply=reply,
        )


async def test_a_second_request_for_the_version_during_the_first_ones_call_is_a_409() -> None:
    gated = GatedModel()
    w = await world(gated)
    first = asyncio.ensure_future(propose(w))
    await asyncio.wait_for(gated.started.wait(), 5)
    second, _ = await propose(w)  # the reservation is held, the proposals are not yet in
    assert second.status_code == 409 and second.json()["detail"]["error"] == "already_running"
    assert gated.calls == []  # neither request has spent a call yet; only the holder will
    gated.release.set()
    response, events = await first
    assert response.status_code == 200 and events[-1][0] == "proposals"
    assert len(gated.calls) == 1  # one model call between them
    assert len(await w.ledger.policy_mappings(TENANT)) == 2


async def test_another_policy_version_is_its_own_run() -> None:
    gated = GatedModel()
    w = await world(gated)
    privacy = {"instrument_id": "acme/privacy-policy", "version_id": "v1"}
    first = asyncio.ensure_future(propose(w))
    await asyncio.wait_for(gated.started.wait(), 5)
    second = asyncio.ensure_future(propose(w, body=privacy))
    held = None
    for _ in range(500):  # until the second holds its own reservation
        held = await w.ledger.mapping_run(TENANT, "acme/privacy-policy", "v1")
        if held is not None:
            break
        await asyncio.sleep(0.01)
    assert held is not None and held.status == "pending"
    for version in (SECURITY, ("acme/privacy-policy", "v1")):
        run = await w.ledger.mapping_run(TENANT, *version)
        assert run is not None and run.status == "pending"  # two runs at once, one each
    gated.release.set()
    responses = [(await first)[0], (await second)[0]]
    assert [r.status_code for r in responses] == [200, 200] and len(gated.calls) == 2


async def test_a_failure_releases_the_reservation_and_a_retry_is_not_a_409() -> None:
    attempts: list[int] = []

    def fails_once(seen: Any) -> str:  # Any: a claims_support.Prompt
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("the connection was reset")
        return reply(item(APP11_1, SEC_3_1))

    w = await world(ScriptedGenerator(fails_once))
    _, failed = await propose(w)
    assert failed[-1][1]["error"] == "internal"
    again, events = await propose(w)
    assert again.status_code == 200 and events[-1][0] == "proposals"
    third, _ = await propose(w)  # a run that is done may be asked for again
    assert third.status_code == 200


async def test_a_cross_origin_request_carrying_the_admins_session_is_refused_and_nothing_runs() -> (
    None
):
    w = await world()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=w.app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        signed_in = await client.post("/session", headers=bearer(ADMIN_TOKEN))
        assert signed_in.status_code == 200
        refused = await client.post(
            PROPOSE, json=SECURITY_BODY, headers={"Origin": "https://x.example"}
        )
        allowed = await client.post(PROPOSE, json=SECURITY_BODY)  # the page's own, by cookie
    assert refused.status_code == 403
    assert allowed.status_code == 200 and len(w.model.calls) == 1  # only the one that was ours
