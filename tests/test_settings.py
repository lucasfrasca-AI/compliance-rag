"""Settings is the only environment reader, and it enforces residency."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from pydantic_settings import SettingsConfigDict

from compliance_rag.settings import REGION, Settings
from compliance_rag.telemetry import ResidencyError, assert_in_region


class IsolatedSettings(Settings):
    """`Settings` with `.env` switched off, so tests see only what they pass."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove ambient values so a developer's shell can't change an assertion."""
    for name in (
        "ENV",
        "TENANT_ID",
        "GCP_REGION",
        "VERTEX_EMBEDDING_DIMS",
        "OTEL_SINKS",
        "QDRANT_API_KEY",
        "NEO4J_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def test_defaults_are_sydney_and_dev_tenant() -> None:
    settings = IsolatedSettings()
    assert settings.gcp_region == REGION
    assert settings.tenant_id == "dev-tenant"
    assert settings.vertex_embedding_dims == 1536


def test_region_outside_sydney_is_rejected() -> None:
    with pytest.raises(ValueError, match=REGION):
        IsolatedSettings(gcp_region="us-central1")


def test_embedding_dims_are_fixed() -> None:
    with pytest.raises(ValueError, match="1536"):
        IsolatedSettings(vertex_embedding_dims=768)


def test_ranking_api_is_gated_to_the_dev_tenant() -> None:
    """D-15 / SERVICES.md: the Ranking API is CI-only and synthetic-only."""
    assert IsolatedSettings(tenant_id="dev-tenant").ranking_api_allowed is True
    assert IsolatedSettings(tenant_id="acme").ranking_api_allowed is False


def test_secrets_are_not_in_repr() -> None:
    settings = IsolatedSettings(
        qdrant_api_key=SecretStr("super-secret"), neo4j_password=SecretStr("hunter2")
    )
    rendered = repr(settings)
    assert "super-secret" not in rendered
    assert "hunter2" not in rendered


def test_secrets_need_an_explicit_unwrap() -> None:
    settings = IsolatedSettings(qdrant_api_key=SecretStr("super-secret"))
    assert str(settings.qdrant_api_key) == "**********"
    assert settings.qdrant_api_key.get_secret_value() == "super-secret"


def test_sinks_parse_to_a_tuple() -> None:
    assert IsolatedSettings(otel_sinks="cloud-logging").sinks == ("cloud-logging",)
    assert IsolatedSettings(otel_sinks=" a , b ").sinks == ("a", "b")


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://localhost:6333",
        "https://reranker-abc.australia-southeast1.run.app",
        "https://xyz.gcp.australia-southeast1.cloud.qdrant.io:6333",
    ],
)
def test_in_region_endpoints_are_accepted(endpoint: str) -> None:
    assert_in_region(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://cloud.langfuse.com",
        "https://reranker-abc.us-central1.run.app",
        "https://otlp.eu.example.com:4317",
        "https://api.phoenix.arize.com",
    ],
)
def test_offshore_endpoints_refuse_to_start(endpoint: str) -> None:
    """D-16: no offshore telemetry endpoint, not even for synthetic data."""
    with pytest.raises(ResidencyError, match=REGION):
        assert_in_region(endpoint)


def test_unknown_sink_is_rejected() -> None:
    """D-16: adding a sink requires an ADR, not a config string."""
    from compliance_rag.telemetry import configure

    with pytest.raises(ValueError, match="unknown telemetry sink"):
        configure(IsolatedSettings(otel_sinks="langfuse-cloud"))
