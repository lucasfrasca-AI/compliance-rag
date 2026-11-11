"""The demo generator and `GENERATION_BACKEND` (issues #292, #326).

- the generator's own behaviour, at its home in `adapters/demo_generator.py`:
  quotes that pass the verifier, "withhold" gives a withheld answer, a repair
  never repairs;
- `Settings`: `GENERATION_BACKEND=demo` refuses an offshore `GEMINI_REGION` or
  `VERTEX_CLAUDE_REGION`;
- `build_app`: `demo` never imports or constructs either model adapter, and
  `gemini` or `claude` with no model refuses to start;
- the banner on every server-rendered page and every turn fragment, withheld
  and failed turns included, and on none when a model is answering;
- `"generator"` on each answer event;
- `GET /` redirects to `/chats`.
"""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from compliance_rag.adapters import gemini_generator, vertex_claude_generator
from compliance_rag.adapters.demo_generator import FAKE_BANNER, FakeDemoGenerator
from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.interfaces import ChatAnswer, ChatExchange, LedgerStore, TurnOutcome
from compliance_rag.api import main as api_main
from compliance_rag.api.app import create_app
from compliance_rag.api.banner import TEMPLATE_DIR
from compliance_rag.api.sign_in import STATIC_DIR
from compliance_rag.api.sources import source_href
from compliance_rag.assessment.prompt import REPAIR_SYSTEM_PROMPT
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION, REGION, Settings
from tests.api_support import api_settings, fake_api_services
from tests.test_api_answers import AS_OF, QUESTION
from tests.test_api_answers import _events as events_of  # pyright: ignore[reportPrivateUsage]
from tests.test_api_answers import _ledger as acme_ledger  # pyright: ignore[reportPrivateUsage]
from tests.test_api_pages import (
    APP11,
    ORIGIN,
    TENANT,
    UID,
    Exploding,
    client_for,
    outcomes,
    page_settings,
    scan,
    sign_in,
    text_of,
)

DEMO = {"generation_backend": "demo"}
WITHHOLD = f"{QUESTION} Please withhold this one."


def demo_settings() -> Settings:
    return page_settings(**DEMO)


# ---- Settings -----------------------------------------------------------------------------


def test_the_generator_is_gemini_unless_demo_is_asked_for() -> None:
    assert Settings(_env_file=None).generator == "gemini"  # pyright: ignore[reportCallIssue]
    assert api_settings(generation_backend="demo").generator == "demo"


def test_settings_refuse_the_demo_generator_with_an_offshore_region() -> None:
    with pytest.raises(ValidationError, match="GENERATION_BACKEND=demo"):
        api_settings(generation_backend="demo", gemini_region=BUILD_PHASE_OFFSHORE_REGION)
    with pytest.raises(ValidationError, match="vertex_claude_region"):
        api_settings(generation_backend="demo", vertex_claude_region=BUILD_PHASE_OFFSHORE_REGION)
    # In Sydney, explicitly or by default, it is accepted: what the gates ask
    # is where the generator runs.
    assert api_settings(generation_backend="demo", gemini_region=REGION).generator == "demo"
    assert api_settings(generation_backend="demo").gemini_region == REGION


def test_gemini_and_claude_may_run_offshore_and_nothing_else_is_a_generator() -> None:
    offshore = api_settings(generation_backend="gemini", gemini_region=BUILD_PHASE_OFFSHORE_REGION)
    assert offshore.generation_region == BUILD_PHASE_OFFSHORE_REGION
    dormant = api_settings(
        generation_backend="claude", vertex_claude_region=BUILD_PHASE_OFFSHORE_REGION
    )
    assert dormant.generation_region == BUILD_PHASE_OFFSHORE_REGION
    # each backend's gates read its own region, never the other's
    assert api_settings(generation_backend="claude", gemini_region="us").generation_region == REGION
    for gone in ("gpt", "bedrock", "vertex"):
        with pytest.raises(ValidationError):
            api_settings(generation_backend=gone)


def test_the_generator_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENERATION_BACKEND", "demo")
    monkeypatch.setenv("GEMINI_REGION", REGION)
    assert Settings(_env_file=None).generator == "demo"  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("GEMINI_REGION", BUILD_PHASE_OFFSHORE_REGION)
    with pytest.raises(ValidationError, match="GENERATION_BACKEND=demo"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


# ---- build_app ------------------------------------------------------------------------------


class _GraphReader:
    """What `Neo4jGraph.open` returns, as `build_app` uses it."""

    async def verify(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _open_graph(_settings: Settings) -> _GraphReader:
    return _GraphReader()


def _ledger(_settings: Settings) -> FakeLedgerStore:
    return FakeLedgerStore()


def _clause_store(_bucket: str) -> FakeClauseStore:
    return FakeClauseStore()


def _pipeline(_settings: Settings, *, ledger: LedgerStore) -> RetrievalPipeline:
    return RetrievalPipeline(FakeRetrievalStore(ledger=ledger), FakeReranker())


def _no_telemetry(_settings: Settings) -> None:
    return None


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_app`'s stores replaced by fakes, so it can run without any."""
    monkeypatch.setattr(api_main.Neo4jGraph, "open", _open_graph)
    monkeypatch.setattr(api_main.PostgresLedgerStore, "from_settings", _ledger)
    monkeypatch.setattr(api_main.GCSClauseStore, "for_bucket", _clause_store)
    monkeypatch.setattr(api_main.RetrievalPipeline, "from_settings", _pipeline)
    monkeypatch.setattr(api_main.telemetry, "configure", _no_telemetry)


def test_with_the_demo_generator_build_app_never_imports_or_constructs_gemini(
    monkeypatch: pytest.MonkeyPatch, stores: None
) -> None:
    # Importing the module, or building the class, would now fail loudly.
    monkeypatch.setitem(sys.modules, "compliance_rag.adapters.gemini_generator", None)
    monkeypatch.setitem(sys.modules, "compliance_rag.adapters.vertex_claude_generator", None)

    def constructed(*_: object, **__: object) -> None:
        raise AssertionError("a model generator was built")

    for adapter in (
        gemini_generator.GeminiGenerator,
        vertex_claude_generator.VertexClaudeGenerator,
    ):
        monkeypatch.setattr(adapter, "create", constructed)
        monkeypatch.setattr(adapter, "__init__", constructed)
    app = api_main.build_app(demo_settings())
    # Starting it (the lifespan runs the startup checks) builds nothing either,
    # and it answers.
    with TestClient(app):
        pass
    generator = app.state.services.pipeline.generator
    assert isinstance(generator, FakeDemoGenerator)


def test_with_gemini_the_generator_is_built_through_the_gate_at_startup(
    monkeypatch: pytest.MonkeyPatch, stores: None
) -> None:
    built: list[Settings] = []

    async def create(settings: Settings, **_: object) -> FakeGenerator:
        built.append(settings)
        return FakeGenerator()

    monkeypatch.setattr(gemini_generator.GeminiGenerator, "create", create)
    settings = api_settings(gemini_model="gemini-3.5-flash", gemini_region="us")
    app = api_main.build_app(settings)
    assert built == []  # not at import or build: at startup, where the gate can ask the store
    with TestClient(app):
        pass
    assert built == [settings]
    generator = app.state.services.pipeline.generator
    assert isinstance(generator, api_main.GeneratorAtStartup)


def test_claude_is_built_through_the_same_gate_at_startup(
    monkeypatch: pytest.MonkeyPatch, stores: None
) -> None:
    built: list[Settings] = []

    async def create(settings: Settings, **_: object) -> FakeGenerator:
        built.append(settings)
        return FakeGenerator()

    monkeypatch.setattr(vertex_claude_generator.VertexClaudeGenerator, "create", create)
    settings = api_settings(
        generation_backend="claude",
        vertex_claude_model="claude-sonnet-5",
        vertex_claude_region="us",
    )
    app = api_main.build_app(settings)
    with TestClient(app):
        pass
    assert built == [settings]
    # and the app's tenant-text gate is offshore with it, though GEMINI_REGION
    # is still Sydney: every gate reads the active backend's region
    assert settings.gemini_region == REGION
    assert app.state.services.pipeline.tenant_text.generation_region == "us"


def test_a_model_backend_with_no_model_refuses_to_start(stores: None) -> None:
    with pytest.raises(ValueError, match="GEMINI_MODEL"):
        api_main.build_app(api_settings(generation_backend="gemini", gemini_model=""))
    with pytest.raises(ValueError, match="VERTEX_CLAUDE_MODEL"):
        api_main.build_app(api_settings(generation_backend="claude", vertex_claude_model=""))
    # `demo` needs no model.
    api_main.build_app(demo_settings())


# ---- the generator's behaviour, at its new home -----------------------------------------------


async def test_the_demo_generators_quotes_are_verbatim_so_the_verifier_passes_them() -> None:
    async with await client_for(FakeDemoGenerator(), settings=demo_settings()) as client:
        await sign_in(client)
        response = await client.post("/v1/answers", json={"question": QUESTION, "as_of": AS_OF})
    events = events_of(response.text)
    assert [name for name, _ in events] == ["coverage", "retrieval", "answer"]
    answer = events[-1][1]
    assert answer["verified"] is True and answer["blocked"] == []
    assert answer["text"].startswith(FAKE_BANNER)
    # the top-ranked retrieved clause, and Acme's first policy clause
    cited = [c["clause_id"] for c in answer["citations"]]
    assert len(cited) == 2 and cited[1].startswith("acme/")


async def test_a_question_that_says_withhold_gives_a_withheld_answer() -> None:
    async with await client_for(FakeDemoGenerator(), settings=demo_settings()) as client:
        await sign_in(client)
        response = await client.post("/v1/answers", json={"question": WITHHOLD, "as_of": AS_OF})
    answer = events_of(response.text)[-1][1]
    assert answer["verified"] is False and answer["text"] is None
    assert answer["blocked"], "the misquote is what blocked it"
    assert "withheld" in answer["withheld"]


async def test_the_demo_generator_never_repairs_and_calls_no_model() -> None:
    clause = "acme/privacy-policy:v1:1"
    fenced = f"[{'a' * 24} clause {clause} (x)]\nSome words.\n[{'a' * 24} end]"
    generation = await FakeDemoGenerator().generate(
        system=REPAIR_SYSTEM_PROMPT, cached_prefix=None, user=fenced, max_tokens=10
    )
    assert generation.model == "fake-demo-generator"
    assert (generation.input_tokens, generation.output_tokens) == (0, 0)
    assert '"clause_id": "acme/privacy-policy:v1:1"' in generation.text
    assert "Some words." not in generation.text  # the repair quotes nothing of the clause


async def test_with_nothing_fenced_the_demo_generator_cites_nothing() -> None:
    generation = await FakeDemoGenerator().generate(
        system="s", cached_prefix=None, user="Question: hello", max_tokens=10
    )
    assert '"citations": []' in generation.text


# ---- the banner -------------------------------------------------------------------------------


def banners(page: str) -> list[dict[str, str]]:
    return [a for _, a in scan(page).elements if a.get("class") == "fake-banner"]


@pytest.mark.parametrize(
    ("generator", "question", "outcome"),
    [
        pytest.param(FakeDemoGenerator(), QUESTION, "verified", id="verified"),
        pytest.param(FakeDemoGenerator(), WITHHOLD, "withheld", id="withheld"),
        pytest.param(Exploding(), QUESTION, "internal", id="failed"),
        pytest.param(FakeGenerator("not JSON"), QUESTION, "malformed_generation", id="malformed"),
    ],
)
async def test_every_page_and_turn_fragment_carries_the_banner_while_the_demo_is_on(
    generator: Any, question: str, outcome: str
) -> None:
    async with await client_for(generator, settings=demo_settings()) as client:
        # the sign-in page needs no session
        pages = {"/signin": (await client.get("/signin")).text}
        await sign_in(client)
        response = await client.post("/v1/answers", json={"question": question, "as_of": AS_OF})
        events = events_of(response.text)
        chat = events[0][1]["chat_id"]
        pages |= {
            "/chats": (await client.get("/chats")).text,
            "/chats/new": (await client.get("/chats/new")).text,
            f"/chats/{chat}": (await client.get(f"/chats/{chat}")).text,
            f"/chats/{chat}/turns": (await client.get(f"/chats/{chat}/turns")).text,
            "an error page": (await client.get("/chats/not-a-chat")).text,
        }
    assert outcomes(pages[f"/chats/{chat}/turns"]) == [outcome]
    for path, page in pages.items():
        assert FAKE_BANNER in text_of(page), path
        assert banners(page), path
    # the fragment names it on the answer turn itself, whichever outcome it shows
    assert len(banners(pages[f"/chats/{chat}/turns"])) == 1


@pytest.mark.parametrize(
    ("generator", "question"),
    [
        pytest.param(FakeGenerator(), QUESTION, id="uncited"),
        pytest.param(Exploding(), QUESTION, id="failed"),
    ],
)
async def test_no_page_says_fake_while_a_model_is_answering(
    generator: FakeGenerator, question: str
) -> None:
    async with await client_for(
        generator, settings=page_settings(generation_backend="gemini")
    ) as client:
        signin = (await client.get("/signin")).text
        await sign_in(client)
        response = await client.post("/v1/answers", json={"question": question, "as_of": AS_OF})
        chat = events_of(response.text)[0][1]["chat_id"]
        shown = [
            signin,
            *[
                (await client.get(path)).text
                for path in ("/chats", "/chats/new", f"/chats/{chat}", f"/chats/{chat}/turns")
            ],
        ]
    for page in shown:
        assert "FAKE GENERATOR" not in page and "fake-banner" not in page


def test_the_banner_is_written_by_the_server_not_by_the_script() -> None:
    script = (STATIC_DIR / "ask.js").read_text(encoding="utf-8")
    signin = (STATIC_DIR / "signin.js").read_text(encoding="utf-8")
    assert "FAKE GENERATOR" not in script + signin and "fake-banner" not in script + signin


# ---- the banner follows the turn, not the setting ---------------------------------------------


async def test_a_turn_the_demo_wrote_keeps_its_banner_when_the_deployment_runs_gemini() -> None:
    """The ledger records which generator wrote an answer turn (0020): after a
    switch to a model, the stand-in's answers are still labelled, and a page's
    own banner is gone."""
    ledger = await acme_ledger()
    async with await client_for(FakeDemoGenerator(), ledger=ledger, settings=demo_settings()) as c:
        await sign_in(c)
        response = await c.post("/v1/answers", json={"question": QUESTION, "as_of": AS_OF})
        chat = events_of(response.text)[0][1]["chat_id"]
    async with await client_for(
        FakeGenerator(), ledger=ledger, settings=page_settings(generation_backend="gemini")
    ) as c:
        await sign_in(c)
        fragment = (await c.get(f"/chats/{chat}/turns")).text
        page = (await c.get(f"/chats/{chat}")).text
        chats = (await c.get("/chats")).text
        read_back = (await c.get(f"/v1/chats/{chat}")).json()
    assert FAKE_BANNER in text_of(fragment) and len(banners(fragment)) == 1
    assert len(banners(page)) == 1  # the turn's own: the page has none, the deployment is gemini
    assert banners(chats) == []
    assert [t.get("generator") for t in read_back["turns"]] == [None, "demo"]


async def test_a_gemini_turn_under_gemini_carries_no_banner() -> None:
    async with await client_for(
        FakeGenerator(), settings=page_settings(generation_backend="gemini")
    ) as c:
        await sign_in(c)
        response = await c.post("/v1/answers", json={"question": QUESTION, "as_of": AS_OF})
        chat = events_of(response.text)[0][1]["chat_id"]
        fragment = (await c.get(f"/chats/{chat}/turns")).text
        read_back = (await c.get(f"/v1/chats/{chat}")).json()
    assert outcomes(fragment) and banners(fragment) == [] and "FAKE" not in fragment
    assert [t.get("generator") for t in read_back["turns"]] == [None, "gemini"]


async def test_a_gemini_turn_read_back_under_the_demo_is_not_labelled_fake() -> None:
    ledger = await acme_ledger()
    async with await client_for(FakeGenerator(), ledger=ledger, settings=page_settings()) as c:
        await sign_in(c)
        response = await c.post("/v1/answers", json={"question": QUESTION, "as_of": AS_OF})
        chat = events_of(response.text)[0][1]["chat_id"]
    async with await client_for(FakeDemoGenerator(), ledger=ledger, settings=demo_settings()) as c:
        await sign_in(c)
        fragment = (await c.get(f"/chats/{chat}/turns")).text
        page = (await c.get(f"/chats/{chat}")).text
    assert banners(fragment) == []  # a model wrote that turn
    assert len(banners(page)) == 1  # the page's own: the deployment runs the demo now


async def test_a_turn_written_before_the_column_has_no_generator_and_no_banner() -> None:
    ledger = await acme_ledger()
    chat = uuid.uuid4().hex
    await ledger.record_chat_exchange(
        ChatExchange(
            tenant_id=TENANT,
            chat_id=chat,
            user_id=UID,
            question=QUESTION,
            answer=ChatAnswer(TurnOutcome.UNCITED),
            as_of=dt.date.fromisoformat(AS_OF),
            trace_id="a" * 32,
        )
    )
    async with await client_for(FakeGenerator(), ledger=ledger, settings=page_settings()) as c:
        await sign_in(c)
        fragment = (await c.get(f"/chats/{chat}/turns")).text
        read_back = (await c.get(f"/v1/chats/{chat}")).json()
    assert outcomes(fragment) == ["withheld"] and banners(fragment) == []
    assert read_back["turns"][1]["generator"] is None


async def test_the_banner_is_one_partial_and_one_stylesheet() -> None:
    """Every page that carries it renders `_banner.html` and links `banner.css`;
    no other template or stylesheet holds its markup or its rules."""
    for template in TEMPLATE_DIR.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        if template.name == "_banner.html":
            assert 'class="fake-banner"' in text and 'role="note"' in text
        else:
            assert "fake-banner" not in text, template.name
    for sheet in STATIC_DIR.glob("*.css"):
        text = sheet.read_text(encoding="utf-8")
        assert ("fake-banner" in text) == (sheet.name == "banner.css"), sheet.name
    async with await client_for(FakeGenerator(), settings=demo_settings()) as c:
        signin = (await c.get("/signin")).text
        await sign_in(c)
        chats = (await c.get("/chats")).text
        css = await c.get("/static/banner.css")
    assert css.status_code == 200 and ".fake-banner" in css.text
    for page in (signin, chats):
        assert scan(page).where("link", href="/static/banner.css")
        (banner,) = banners(page)
        assert banner["role"] == "note"  # not an alert: it is always there, not news


# ---- what carries no banner ---------------------------------------------------------------------


async def test_a_source_page_is_exempt_because_it_holds_no_generated_text() -> None:
    """`/v1/sources/...` serves stored law and policy text, never generated text,
    so it carries no banner even while the demo is on. Pinned here, and stated in
    the API's README."""
    async with await client_for(FakeDemoGenerator(), settings=demo_settings()) as c:
        await sign_in(c)
        path = source_href(APP11).split("#", 1)[0]
        source = await c.get(path)
    assert source.status_code == 200
    assert "FAKE GENERATOR" not in source.text and "fake-banner" not in source.text
    readme = (Path(api_main.__file__).parent / "README.md").read_text(encoding="utf-8")
    assert "source pages" in readme.lower() and "exempt" in readme.lower()


# ---- the JSON API -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("generator", "settings", "question", "verified"),
    [
        pytest.param(FakeDemoGenerator(), DEMO, QUESTION, True, id="demo-verified"),
        pytest.param(FakeDemoGenerator(), DEMO, WITHHOLD, False, id="demo-withheld"),
        pytest.param(FakeGenerator(), {}, QUESTION, False, id="gemini-uncited"),
    ],
)
async def test_each_answer_event_names_its_generator(
    generator: FakeGenerator, settings: dict[str, str], question: str, verified: bool
) -> None:
    async with await client_for(generator, settings=page_settings(**settings)) as client:
        await sign_in(client)
        response = await client.post("/v1/answers", json={"question": question, "as_of": AS_OF})
    answer = events_of(response.text)[-1][1]
    assert answer["verified"] is verified
    assert answer["generator"] == settings.get("generation_backend", "gemini")


# ---- the root ------------------------------------------------------------------------------------


async def test_the_root_redirects_to_the_chat_list_without_a_session() -> None:
    app = create_app(fake_api_services(settings=page_settings()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as c:
        response = await c.get("/")
        health = await c.get("/health")
    assert (response.status_code, response.headers["location"]) == (303, "/chats")
    assert health.status_code == 200
