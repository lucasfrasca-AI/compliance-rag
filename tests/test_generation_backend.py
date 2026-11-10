"""`GENERATION_BACKEND` (#322, ADR-0056): which Claude the API and the operator
scripts build, how it is configured from the environment, and that a deploy
that can't authenticate fails at startup rather than at the first question."""

from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

from compliance_rag.adapters import bedrock_claude_generator, vertex_claude_generator
from compliance_rag.adapters.bedrock_claude_generator import BedrockClaudeGenerator
from compliance_rag.adapters.fakes import FakeGenerator, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.generator_factory import build_generator, generation_label
from compliance_rag.api import main as api_main
from compliance_rag.settings import Settings
from tests.api_support import api_settings
from tests.test_demo_generator import (
    stores,  # noqa: F401  # pyright: ignore[reportUnusedImport] — fixture
)


def _corpus() -> FakeRetrievalStore:
    return FakeRetrievalStore(ledger=FakeLedgerStore())


# ---- Settings, from the environment ------------------------------------------------------


def test_settings_read_the_bedrock_variables_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("GENERATION_BACKEND", "BEDROCK_REGION", "BEDROCK_INFERENCE_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/compliance-rag-bedrock")
    monkeypatch.setenv("AWS_PROFILE", "")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
    assert settings.generation_backend == "bedrock"
    assert settings.bedrock_region == "ap-southeast-2"
    assert settings.bedrock_inference_profile == "au.anthropic.claude-sonnet-5"
    assert settings.aws_role_arn.endswith(":role/compliance-rag-bedrock")
    monkeypatch.setenv("GENERATION_BACKEND", "vertex")
    assert Settings(_env_file=None).generation_backend == "vertex"  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("GENERATION_BACKEND", "openai")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("BEDROCK_INFERENCE_PROFILE", "global.anthropic.claude-sonnet-5"),
        ("BEDROCK_INFERENCE_PROFILE", "us.anthropic.claude-sonnet-5"),
        ("BEDROCK_INFERENCE_PROFILE", "apac.anthropic.claude-sonnet-5"),
        ("BEDROCK_REGION", "us-east-1"),
        ("BEDROCK_REGION", "ap-southeast-4"),
    ],
)
def test_a_non_australian_profile_or_source_region_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError, match="bedrock_"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg


# ---- the factory --------------------------------------------------------------------------


async def test_bedrock_is_built_without_asking_the_store_and_without_vertex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "compliance_rag.adapters.vertex_claude_generator", None)
    generator = await build_generator(api_settings(aws_profile="local"), corpus=_corpus())
    assert isinstance(generator, BedrockClaudeGenerator)


async def test_vertex_is_built_through_its_own_gate_when_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Settings] = []

    async def create(settings: Settings, **_: object) -> FakeGenerator:
        built.append(settings)
        return FakeGenerator()

    monkeypatch.setattr(vertex_claude_generator.VertexClaudeGenerator, "create", create)
    settings = api_settings(generation_backend="vertex", vertex_claude_model="claude-sonnet-5")
    assert isinstance(await build_generator(settings, corpus=_corpus()), FakeGenerator)
    assert built == [settings]


def test_the_label_names_the_model_and_where_it_runs() -> None:
    bedrock = generation_label(api_settings(aws_profile="local"))
    assert "au.anthropic.claude-sonnet-5" in bedrock and "ap-southeast-2" in bedrock
    vertex = generation_label(
        api_settings(
            generation_backend="vertex",
            vertex_claude_model="claude-sonnet-5",
            vertex_claude_region="us",
        )
    )
    assert "claude-sonnet-5" in vertex and "us" in vertex and "Vertex" in vertex


# ---- the API ------------------------------------------------------------------------------


def test_bedrock_with_neither_a_role_nor_a_profile_refuses_to_start(stores: None) -> None:  # noqa: F811
    """Before any store is opened — and with nothing to authenticate with there
    is no default, because the default chain reads long-lived keys."""
    with pytest.raises(ValueError, match="AWS_ROLE_ARN"):
        api_main.build_app(api_settings())


def test_with_bedrock_the_generator_is_built_at_startup_and_vertex_is_never_imported(
    monkeypatch: pytest.MonkeyPatch,
    stores: None,  # noqa: F811
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setitem(sys.modules, "compliance_rag.adapters.vertex_claude_generator", None)
    built: list[Settings] = []

    def create(settings: Settings, **_: object) -> FakeGenerator:
        built.append(settings)
        return FakeGenerator()

    monkeypatch.setattr(bedrock_claude_generator.BedrockClaudeGenerator, "create", create)
    settings = api_settings(aws_role_arn="arn:aws:iam::123456789012:role/compliance-rag-bedrock")
    app = api_main.build_app(settings)
    assert built == []
    with TestClient(app):
        pass
    assert built == [settings]
    assert isinstance(app.state.services.pipeline.generator, api_main.GeneratorAtStartup)


def test_the_demo_generator_needs_no_aws_configuration(stores: None) -> None:  # noqa: F811
    api_main.build_app(api_settings(generator="demo"))


def test_the_sdk_name_guards_would_catch_the_bedrock_modules_if_retrieval_loaded_them() -> None:
    """`test_recall_gate` and `test_retrieve_wiring` look for these names in a
    fresh interpreter's modules; this proves each really is one."""
    import subprocess

    program = """
import sys
import compliance_rag.adapters.bedrock_claude_generator
names = list(sys.modules)
assert any(m.startswith("botocore") for m in names)
assert any(m.startswith("anthropic") for m in names)
assert any(m.startswith("compliance_rag") and "bedrock" in m for m in names)
assert any(m.startswith("compliance_rag") and "aws_" in m for m in names)
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
