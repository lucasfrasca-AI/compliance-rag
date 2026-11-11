"""`GENERATION_BACKEND` (#326, ADR-0057): which generator (`gemini`, the default;
`claude`, dormant; `demo`) the API and the operator scripts build, how it is
configured from the environment, and that the factory builds it only through its
residency gate."""

from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

from compliance_rag.adapters import gemini_generator, vertex_claude_generator
from compliance_rag.adapters.fakes import FakeGenerator, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.generator_factory import build_generator, generation_label
from compliance_rag.settings import Settings
from tests.api_support import api_settings


def _corpus() -> FakeRetrievalStore:
    return FakeRetrievalStore(ledger=FakeLedgerStore())


# ---- Settings, from the environment ------------------------------------------------------


def test_settings_read_the_generation_variables_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("GENERATION_BACKEND", "GEMINI_MODEL", "GEMINI_REGION"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
    assert settings.generation_backend == "gemini"
    assert settings.gemini_model == "gemini-3.5-flash"
    assert settings.gemini_region == "australia-southeast1"  # fail closed: `us` is opted into
    monkeypatch.setenv("GEMINI_REGION", "us")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    offshore = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert (offshore.generation_region, offshore.generation_model) == ("us", "gemini-3.5-flash")
    monkeypatch.setenv("GENERATION_BACKEND", "claude")
    monkeypatch.setenv("VERTEX_CLAUDE_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("VERTEX_CLAUDE_REGION", "us")
    dormant = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert dormant.generation_backend == "claude"
    assert (dormant.generation_region, dormant.generation_model) == ("us", "claude-sonnet-5")
    monkeypatch.setenv("GENERATION_BACKEND", "demo")
    monkeypatch.setenv("GEMINI_REGION", "australia-southeast1")
    monkeypatch.setenv("VERTEX_CLAUDE_REGION", "australia-southeast1")
    assert Settings(_env_file=None).generation_backend == "demo"  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("backend", ["bedrock", "vertex", "openai"])
def test_a_backend_that_does_not_exist_is_refused_not_ignored(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    monkeypatch.setenv("GENERATION_BACKEND", backend)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("region", ["global", "ap-southeast-2", "us-central1", "eu"])
def test_a_region_that_is_neither_sydney_nor_the_us_multi_region_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, region: str
) -> None:
    monkeypatch.setenv("GEMINI_REGION", region)
    with pytest.raises(ValidationError, match="gemini_region"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


# ---- the factory --------------------------------------------------------------------------


async def test_gemini_is_built_through_its_own_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[Settings] = []

    async def create(settings: Settings, **_: object) -> FakeGenerator:
        built.append(settings)
        return FakeGenerator()

    monkeypatch.setattr(gemini_generator.GeminiGenerator, "create", create)
    settings = api_settings(gemini_region="us")
    assert isinstance(await build_generator(settings, corpus=_corpus()), FakeGenerator)
    assert built == [settings]


async def test_claude_is_built_through_its_own_gate_and_never_loads_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Settings] = []

    async def create(settings: Settings, **_: object) -> FakeGenerator:
        built.append(settings)
        return FakeGenerator()

    monkeypatch.setitem(sys.modules, "compliance_rag.adapters.gemini_generator", None)
    monkeypatch.setattr(vertex_claude_generator.VertexClaudeGenerator, "create", create)
    settings = api_settings(generation_backend="claude", vertex_claude_model="claude-sonnet-5")
    assert isinstance(await build_generator(settings, corpus=_corpus()), FakeGenerator)
    assert built == [settings]


async def test_the_factory_builds_no_model_for_the_demo_backend() -> None:
    with pytest.raises(ValueError, match="builds no model"):
        await build_generator(api_settings(generation_backend="demo"), corpus=_corpus())


def test_the_label_names_the_model_and_where_it_runs() -> None:
    label = generation_label(api_settings(gemini_region="us"))
    assert "gemini-3.5-flash" in label and "us" in label and "Vertex" in label
    claude = generation_label(
        api_settings(
            generation_backend="claude",
            vertex_claude_model="claude-sonnet-5",
            vertex_claude_region="us",
        )
    )
    assert "claude-sonnet-5" in claude and "us" in claude


# ---- the guards that keep a generator out of retrieval ------------------------------------


def test_the_sdk_name_guards_would_catch_the_gemini_module_if_retrieval_loaded_it() -> None:
    """`test_recall_gate` and `test_retrieve_wiring` look for generator module
    names in a fresh interpreter's modules (the SDK itself, `google.genai`, is
    also the embedder's, so it can't be the tell); this proves the adapter
    really is one of those names, and that no Claude SDK comes with it."""
    import subprocess

    program = """
import sys
import compliance_rag.adapters.gemini_generator
names = list(sys.modules)
assert any(m.startswith("compliance_rag") and "generator" in m for m in names)
assert not any(m.startswith(("anthropic", "botocore", "boto3")) for m in names)
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
