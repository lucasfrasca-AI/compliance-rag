"""The throwaway demo server (#247): its guards (loopback only, never where
deployed, the fake banner), that the production app still requires sign-in,
and one question asked end to end through the demo app with fakes, down to
the anchor a law citation opens at."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from scripts import demo_server
from scripts.demo_server import (
    DEMO_MEMBER,
    DEMO_TENANT,
    FAKE_BANNER,
    FailureLog,
    FakeDemoGenerator,
    RecordingGenerator,
    RefusedToStart,
    SnapshotThenSydney,
    assemble,
    create_demo_app,
    demo_graph,
    demo_ledger,
    demo_member_as,
    refuse_unless_local,
    refuse_unless_local_qdrant,
    render_page,
)

from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.interfaces import Embedder, Generation, Generator, Member
from compliance_rag.api import deps
from compliance_rag.api.app import create_app
from compliance_rag.assessment.evidence import Facts
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, parse_stable_id
from compliance_rag.eval.applicability import compare
from compliance_rag.eval.embedding_cache import EmbeddingCache, text_key
from compliance_rag.eval.golden_corpus import (
    LAW_INSTRUMENT_ID,
    LAW_VERSION_ID,
    golden_chunks,
    golden_clauses,
)
from compliance_rag.eval.harness import golden_clause_store, law_source_html
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION, REGION, Settings
from tests.api_support import api_settings, fake_api_services
from tests.vertex_quota import PLACEHOLDER_TOKEN, SDK_WORDS, quota_exhausted_generator

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "What must Acme do to protect the personal information it holds from misuse and loss?"
BANNER_DIV = 'id="fake-banner"'
Events = list[tuple[str, dict[str, Any]]]  # Any: JSON


def _settings(**overrides: Any) -> Settings:  # Any: settings field values
    return api_settings(tenant_id=DEMO_TENANT, **overrides)


# --- The guards ----------------------------------------------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "::", "::1", "192.168.1.20", ""])
def test_the_demo_binds_nothing_but_127_0_0_1(host: str) -> None:
    with pytest.raises(RefusedToStart, match=r"127\.0\.0\.1 only"):
        refuse_unless_local(host, _settings())
    refuse_unless_local("127.0.0.1", _settings())


@pytest.mark.parametrize(
    ("variable", "value", "marker"),
    [
        ("K_SERVICE", "compliance-rag-api", "K_SERVICE"),
        ("CLOUD_RUN_JOB", "ingest", "CLOUD_RUN_JOB"),
        ("ENV", "cloud", "ENV=cloud"),
    ],
)
def test_the_demo_refuses_to_start_where_a_production_marker_is_set(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, marker: str
) -> None:
    monkeypatch.setenv(variable, value)
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
    assert marker in settings.production_markers
    with pytest.raises(RefusedToStart, match=marker):
        refuse_unless_local("127.0.0.1", settings)


def test_main_refuses_before_building_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guards run before telemetry, the stores or the server exist."""

    def built(*_: object, **__: object) -> None:
        raise AssertionError("nothing is built once the demo refuses")

    monkeypatch.setattr(demo_server, "serve", built)
    monkeypatch.setattr(demo_server.telemetry, "configure", built)
    with pytest.raises(RefusedToStart, match=r"127\.0\.0\.1 only"):
        demo_server.main(["--host", "0.0.0.0", "--fake-generator"])
    monkeypatch.setenv("K_SERVICE", "compliance-rag-api")
    with pytest.raises(RefusedToStart, match="K_SERVICE"):
        demo_server.main(["--fake-generator"])


def test_the_demo_loads_its_corpus_into_a_local_qdrant_only() -> None:
    refuse_unless_local_qdrant("http://127.0.0.1:6333")
    refuse_unless_local_qdrant("http://localhost:56460")
    for remote in (
        "https://abc.australia-southeast1-0.gcp.cloud.qdrant.io:6333",
        "http://10.0.0.5",
    ):
        with pytest.raises(RefusedToStart, match="local Qdrant only"):
            refuse_unless_local_qdrant(remote)


def test_the_fake_generator_is_never_the_default() -> None:
    assert demo_server._parser().parse_args([]).fake_generator is False  # pyright: ignore[reportPrivateUsage]
    real = demo_server.demo_settings(qdrant_url="http://127.0.0.1:6333", fake_generator=False)
    fake = demo_server.demo_settings(qdrant_url="http://127.0.0.1:6333", fake_generator=True)
    assert real.tenant_id == fake.tenant_id == DEMO_TENANT
    # The fake runs in process: the gates are told so. The real one keeps
    # whatever region is configured, offshore included.
    assert fake.vertex_claude_region == REGION
    assert real.vertex_claude_region == Settings().vertex_claude_region


def test_the_fake_banner_is_on_the_page_exactly_when_the_fake_generator_is() -> None:
    assert BANNER_DIV in render_page(fake_generator=True)
    assert FAKE_BANNER in render_page(fake_generator=True)
    assert BANNER_DIV not in render_page(fake_generator=False)


def test_the_page_is_labelled_a_throwaway_demo_and_writes_no_server_value_as_html() -> None:
    page = (ROOT / "demo" / "index.html").read_text(encoding="utf-8")
    assert "THROWAWAY DEMO" in page[: page.index("<main>")]
    assert "innerHTML" not in page and "insertAdjacentHTML" not in page
    assert "<script src" not in page  # no framework, nothing fetched from elsewhere


# --- Production is untouched ------------------------------------------------------


async def test_the_production_app_still_requires_sign_in() -> None:
    """Building the demo app overrides sign-in on that app alone: the
    production app, built after it in the same process, still refuses an
    unsigned request."""
    demo = await _demo_app()
    assert list(demo.dependency_overrides) == [deps.current_member]
    production = create_app(fake_api_services())
    assert production.dependency_overrides == {}
    transport = httpx.ASGITransport(app=production)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        asked = await client.post("/v1/answers", json={"question": QUESTION})
        me = await client.get("/v1/me")
        assert (await client.get("/")).status_code == 404  # no page in production
    assert asked.status_code == me.status_code == 401


def test_nothing_in_the_package_imports_the_demo() -> None:
    package = ROOT / "src" / "compliance_rag"
    importers = [p for p in package.rglob("*.py") if "demo_server" in p.read_text(encoding="utf-8")]
    assert importers == []
    main = (package / "api" / "main.py").read_text(encoding="utf-8")
    assert "FirebaseTokenVerifier(" in main and "dependency_overrides" not in main


# --- End to end, with fakes ------------------------------------------------------


async def _law_sources() -> FakeClauseStore:
    """The golden excerpt's annotated HTML, as the corpus bucket stores it."""
    store = FakeClauseStore()
    await store.put_manifest(
        LAW_INSTRUMENT_ID,
        LAW_VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=2,
        source_html=law_source_html(),
    )
    return store


async def _demo_app(
    generator: Generator | None = None,
    failures: FailureLog | None = None,
    fake: bool = True,
    *,
    settings: Settings | None = None,
    member: Member = DEMO_MEMBER,
) -> FastAPI:
    ledger = await demo_ledger()
    services = assemble(
        settings or _settings(),
        ledger=ledger,
        clauses=await golden_clause_store(),
        graph=demo_graph(),
        retrieval=RetrievalPipeline(
            FakeRetrievalStore(golden_chunks(), ledger=ledger), FakeReranker()
        ),
        generator=generator or FakeDemoGenerator(),
    )
    return create_demo_app(
        services,
        law_sources=await _law_sources(),
        failures=failures or FailureLog(),
        fake_generator=fake,
        member=member,
    )


def _events(stream: str) -> Events:
    events: Events = []
    for block in stream.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo")


async def _ask(app: FastAPI, question: str = QUESTION) -> tuple[httpx.Response, Events]:
    async with _client(app) as c:
        response = await c.post("/v1/answers", json={"question": question, "as_of": "2026-10-12"})
    return response, _events(response.text)


async def test_a_question_streams_to_citations_that_open_at_their_clause() -> None:
    app = await _demo_app()
    response, events = await _ask(app)
    # No Authorization header: the demo member asked.
    assert response.status_code == 200 and response.headers["X-Trace-Id"]
    assert [name for name, _ in events] == ["coverage", "retrieval", "answer"]
    answer = events[-1][1]
    assert answer["verified"] is True
    assert answer["text"].startswith(FAKE_BANNER)
    canonical = {c.clause_id: c.canonical_text for c in golden_clauses()}
    law = [c for c in answer["citations"] if c["clause_id"].startswith(f"{LAW_INSTRUMENT_ID}:")]
    policies = [c for c in answer["citations"] if c["clause_id"].startswith("acme/")]
    assert len(law) == 1 and len(policies) == 1

    async with _client(app) as c:
        # Law: the stored source, at an anchor that exists in it (ADR-0019).
        opened = (await c.get(f"/demo/open/{law[0]['clause_id']}")).json()
        path, anchor = opened["href"].split("#", 1)
        source = await c.get(path)
        assert source.status_code == 200
        assert f'id="{anchor}"' in source.text
        assert "sandbox" in source.headers["Content-Security-Policy"]
        # A policy: no anchor; the page shows the text the citation carries.
        policy = (await c.get(f"/demo/open/{policies[0]['clause_id']}")).json()
        assert policy["href"] is None and "I2" in policy["why"]
        # The production display form, from the recorded title (ADR-0049).
        assert policies[0]["cite"].startswith("Acme Pty Ltd")
        assert law[0]["cite"].startswith(("APP", "s "))
        for citation in answer["citations"]:
            assert citation["canonical_text"] == canonical[citation["clause_id"]]
            assert citation["version_id"] == parse_stable_id(citation["clause_id"])[1]
        # The page says who is asking, and that the generator is fake.
        config = (await c.get("/demo/config")).json()
        assert config["fake_generator"] is True and config["member"] == DEMO_MEMBER.email
        assert BANNER_DIV in (await c.get("/")).text


async def test_a_withheld_answer_is_shown_as_withheld() -> None:
    _, events = await _ask(await _demo_app(), f"{QUESTION} (withhold)")
    answer = events[-1][1]
    assert answer["verified"] is False
    assert answer["text"] is None and answer["citations"] == []
    assert "withheld" in answer["withheld"] and answer["blocked"]


class _Unrecognised(Generator):
    """A failure the API has no name for, so it reports `internal`."""

    async def generate(
        self, *, system: str, cached_prefix: str | None, user: str, max_tokens: int
    ) -> Generation:
        raise RuntimeError(
            "the connection to the model was reset (Authorization: Bearer ya29.not-a-real-token)"
        )


async def test_an_unrecognised_generation_failure_is_shown_plainly_and_without_a_token() -> None:
    failures = FailureLog()
    app = await _demo_app(RecordingGenerator(_Unrecognised(), failures), failures, fake=False)
    response, events = await _ask(app)
    assert events[-1] == ("error", {**events[-1][1], "error": "internal"})
    async with _client(app) as c:
        found = (await c.get(f"/demo/failures/{response.headers['X-Trace-Id']}")).json()
        page = (await c.get("/")).text
    [failure] = found["failures"]
    assert failure["where"] == "generation (Claude on Vertex)"
    assert "connection to the model was reset" in failure["error"]
    assert "ya29" not in failure["error"] and "[redacted]" in failure["error"]
    assert BANNER_DIV not in page


OFFSHORE = {"vertex_claude_region": BUILD_PHASE_OFFSHORE_REGION}
LISTED = "lucas.frasca@example.com"


async def test_a_vertex_quota_refusal_is_shown_as_generation_unavailable_end_to_end() -> None:
    """A listed tester asks, and Vertex answers 429 `RESOURCE_EXHAUSTED` (#251):
    the SDK's own client raises `RateLimitError` inside the real
    `VertexClaudeGenerator`, wrapped as `demo_server` wraps it. The stream
    ends in `generation_unavailable`, which the page renders as its own state,
    and none of the SDK's words reach the page by any route."""
    ledger = await demo_ledger()
    failures = FailureLog()
    requests: list[httpx.Request] = []
    store = FakeRetrievalStore(golden_chunks(), ledger=ledger)
    app = await _demo_app(
        RecordingGenerator(quota_exhausted_generator(store, requests), failures),
        failures,
        fake=False,
        settings=_settings(**OFFSHORE, offshore_generation_testers=LISTED),
        member=demo_member_as(LISTED),
    )
    response, events = await _ask(app)
    assert len(requests) == 1
    assert [name for name, _ in events] == ["coverage", "retrieval", "error"]
    error = events[-1][1]
    assert error["error"] == "generation_unavailable" and error["reason"] == "quota"
    assert error["trace_id"] == response.headers["X-Trace-Id"]
    async with _client(app) as c:
        found = (await c.get(f"/demo/failures/{error['trace_id']}")).json()
        page = (await c.get("/")).text
    shown = response.text + json.dumps(found)
    for leaked in (*SDK_WORDS, PLACEHOLDER_TOKEN, "Traceback"):
        assert leaked not in shown
    # The page's state for it: its own words, and no call for the failure's text.
    assert 'data.error === "generation_unavailable"' in page
    assert "Generation unavailable — the model's quota isn't granted yet" in page


async def test_before_the_member_is_listed_the_residency_refusal_is_its_own_state() -> None:
    """Offshore, with the tester list naming someone else: the gate refuses
    before anything is computed (ADR-0032), and the page renders it as a
    refusal, naming who asked and how to be listed."""
    settings = _settings(**OFFSHORE, offshore_generation_testers=LISTED)
    app = await _demo_app(FakeDemoGenerator(), fake=False, settings=settings)
    _, events = await _ask(app)
    assert [name for name, _ in events] == ["error"]
    assert events[0][1]["error"] == "residency" and "ADR-0032" in events[0][1]["message"]
    async with _client(app) as c:
        page = (await c.get("/")).text
        config = (await c.get("/demo/config")).json()
    assert 'data.error === "residency"' in page
    assert "Not sent — the residency gate refused this question" in page
    assert "OFFSHORE_GENERATION_TESTERS" in page and "--member" in page
    assert config["member"] == DEMO_MEMBER.email


async def test_asking_as_a_listed_member_passes_the_gate_as_in_production() -> None:
    """`--member` in any case is lowercased as the sign-in verifier lowercases
    an email, so the operator's list decides exactly as it does for a
    signed-in member (ADR-0032)."""
    settings = _settings(**OFFSHORE, offshore_generation_testers=LISTED.upper())
    app = await _demo_app(settings=settings, member=demo_member_as("Lucas.Frasca@Example.COM"))
    _, events = await _ask(app)
    assert [name for name, _ in events] == ["coverage", "retrieval", "answer"]
    assert events[-1][1]["verified"] is True
    async with _client(app) as c:
        assert (await c.get("/demo/config")).json()["member"] == LISTED


def test_member_names_who_asks_lowercased_and_defaults_to_the_demo_member() -> None:
    parse = demo_server._parser().parse_args  # pyright: ignore[reportPrivateUsage]
    assert parse([]).member == DEMO_MEMBER
    assert parse(["--member", "Lucas.Frasca@Example.COM"]).member == Member(
        uid=DEMO_MEMBER.uid, email=LISTED
    )
    for bad in ("not-an-email", "two words@example.com", "a@b@c"):
        with pytest.raises(SystemExit):
            parse(["--member", bad])


def test_main_serves_as_the_member_it_was_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served: dict[str, object] = {}

    async def serve(*_: object, **kwargs: object) -> None:
        served.update(kwargs)

    monkeypatch.setattr(demo_server, "serve", serve)
    def configured(_: Settings) -> None:
        pass

    monkeypatch.setattr(demo_server.telemetry, "configure", configured)
    monkeypatch.setenv("VERTEX_CLAUDE_REGION", BUILD_PHASE_OFFSHORE_REGION)
    monkeypatch.setenv("OFFSHORE_GENERATION_TESTERS", "")
    demo_server.main(["--member", "Lucas.Frasca@Example.COM"])
    assert served["member"] == Member(uid=DEMO_MEMBER.uid, email=LISTED)
    out = capsys.readouterr().out
    assert f"{LISTED} isn't in OFFSHORE_GENERATION_TESTERS" in out
    assert f"Asking as {LISTED}" in out


async def test_the_source_route_serves_library_law_only() -> None:
    app = await _demo_app()
    async with _client(app) as c:
        assert (await c.get("/demo/source/acme%2Fprivacy-policy/v1")).status_code == 404
        assert (await c.get(f"/demo/source/{LAW_INSTRUMENT_ID}/C1999C00001")).status_code == 404
        assert (await c.get("/demo/open/not-a-clause")).status_code == 404


# --- The question's embedding --------------------------------------------------------


class _Counting(Embedder):
    def __init__(self) -> None:
        self.asked: list[tuple[str, ...]] = []

    @property
    def dimensions(self) -> int:
        return 3

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.asked.append(texts)
        return tuple((1.0, 0.0, 0.0) for _ in texts)


async def test_only_text_the_snapshot_lacks_is_embedded_fresh() -> None:
    cached = (0.0, 1.0, 0.0)
    cache = EmbeddingCache(
        model="m",
        dimensions=3,
        vectors={text_key("a chunk"): cached},
        generated_on="2026-10-21",
        task_type="t",
        sdk="s",
    )
    fresh = _Counting()
    embedder = SnapshotThenSydney(cache, fresh, FailureLog())
    assert await embedder.embed(("a chunk",)) == (cached,)
    assert fresh.asked == []
    vectors = await embedder.embed(("a chunk", "a question", "a question"))
    assert vectors == (cached, (1.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    assert fresh.asked == [("a question",)]


async def test_the_demo_ledger_holds_acme_subscribed_with_its_profiles_facts() -> None:
    ledger: FakeLedgerStore = await demo_ledger()
    assert await ledger.subscribed_instruments(DEMO_TENANT) == {LAW_INSTRUMENT_ID}
    facts = {(e.field, e.value) for e in await ledger.evidence(DEMO_TENANT)}
    assert facts == {(f.field, f.value) for f in demo_server.demo_profile().facts}


async def test_the_demo_graph_gives_its_profile_the_signed_outcomes() -> None:
    """Track A in the demo is the gate's: its profile's coverage matches the
    signed applicability labels, incomplete clauses included (ADR-0047)."""
    case = demo_server.demo_profile()
    ledger = await demo_ledger()
    coverage = await demo_graph().coverage(
        Facts.of(await ledger.evidence(DEMO_TENANT)), tenant_id=DEMO_TENANT, as_of=case.as_of
    )
    assert compare(case, coverage) == ()
