"""The only reader of environment configuration (`CLAUDE.md` code standards).

`.env` locally, Secret Manager in Cloud Run. Nothing else in the package reads
`os.environ`. Secrets are never logged: `repr` of this object redacts them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REGION = "australia-southeast1"
"""Every store, every telemetry sink, and every query-time byte except
build-phase generation stays here (D-07, D-15, D-16, ADR-0023)."""

BUILD_PHASE_OFFSHORE_REGION = "us"
"""The US multi-region endpoint (`aiplatform.us.rep.googleapis.com`), where
`claude-sonnet-5` is served and enabled for this project (ADR-0024; it has no
Sydney or Singapore endpoint). Allowed for generation only, and only while
the corpus is public law plus synthetic data —
`adapters/generation_residency.py` enforces that (ADR-0023)."""

GENERATION_REGIONS = frozenset({REGION, BUILD_PHASE_OFFSHORE_REGION})


class Settings(BaseSettings):
    """Environment configuration, validated once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Literal["local", "ci", "cloud"] = "local"
    tenant_id: str = "dev-tenant"

    # --- GCP ---------------------------------------------------------------
    gcp_project_id: str = ""
    gcp_project_number: str = ""
    gcp_region: str = REGION
    corpus_bucket: str = ""
    gcp_log_bucket: str = "compliance-rag-logs"

    # --- Vertex ------------------------------------------------------------
    vertex_claude_model: str = ""
    vertex_claude_region: str = REGION
    """Sydney by default — fail closed. The build phase opts into the US
    multi-region explicitly (`VERTEX_CLAUDE_REGION=us`, ADR-0024)."""
    vertex_embedding_model: str = "gemini-embedding-001"
    vertex_embedding_dims: int = 1536
    vertex_ranking_model: str = "semantic-ranker-default"
    vertex_ranking_region: str = "us-central1"

    # --- Stores ------------------------------------------------------------
    qdrant_url: str = ""
    qdrant_api_key: SecretStr = SecretStr("")
    qdrant_collection: str = "clauses"
    qdrant_cluster_name: str = ""
    neo4j_uri: str = ""
    neo4j_username: str = ""
    neo4j_password: SecretStr = SecretStr("")
    neo4j_database: str = ""
    """On Aura Free the database name is the instance ID, and so is the
    username — never "neo4j". No default, so a missing value fails loudly
    instead of addressing a database that doesn't exist."""
    database_url: SecretStr = SecretStr("")

    # --- Reranker ----------------------------------------------------------
    # Values are fixed by .env.example; vertex_ci is the Ranking API path,
    # which is CI-only and synthetic-only (D-15).
    reranker_backend: Literal["cross_encoder", "colbert", "vertex_ci"] = "cross_encoder"
    reranker_url: str = ""
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_max_tokens: int = 512
    # Above the measured cold-start p95 for a 50-candidate rerank (37 s on
    # Cloud Run, BUILD_LOG 2026-10-06) — the latency decision is #32's.
    reranker_timeout_seconds: float = 60.0

    # --- Telemetry ---------------------------------------------------------
    otel_service_name: str = "compliance-rag"
    otel_sinks: str = Field(
        default="gcp_logging",
        description="Comma-separated sink names. Config only — never a call site (D-16).",
    )

    # --- API ---------------------------------------------------------------
    firebase_project_id: str = ""
    """The Firebase project whose ID tokens the API accepts: their `aud`, and
    the issuer `https://securetoken.google.com/<id>` (#146). Required in
    `env=cloud`."""
    api_members: str = ""
    """Comma-separated verified emails admitted to `tenant_id`. A Firebase
    account alone isn't membership (#145, default 1). Empty admits no one."""
    api_sign_in_providers: str = "google.com,password"
    """Firebase sign-in providers whose verified email is trusted. Google and
    email/password prove ownership; an arbitrary OIDC or SAML provider can
    assert any address (#145)."""
    api_docs: bool = False
    """Serve the OpenAPI schema and /docs. Off unless asked for, so a
    deployment missing `ENV` doesn't publish it."""

    # --- Web fallback ------------------------------------------------------
    web_search_provider: Literal["brave", "tavily", "none"] = "none"
    web_search_api_key: SecretStr = SecretStr("")

    @field_validator("gcp_region")
    @classmethod
    def _region_is_sydney(cls, v: str) -> str:
        if v != REGION:
            raise ValueError(f"gcp_region must be {REGION} (D-07); got {v!r}")
        return v

    @field_validator("vertex_claude_region")
    @classmethod
    def _generation_region(cls, v: str) -> str:
        if v not in GENERATION_REGIONS:
            raise ValueError(
                f"vertex_claude_region must be one of {sorted(GENERATION_REGIONS)} — never "
                f"`global` or any other region (ADR-0023); got {v!r}"
            )
        return v

    @field_validator("vertex_embedding_dims")
    @classmethod
    def _dims(cls, v: int) -> int:
        if v != 1536:
            raise ValueError(f"vertex_embedding_dims is fixed at 1536 (PHASES.md Phase 1); got {v}")
        return v

    @property
    def ranking_api_allowed(self) -> bool:
        """The Vertex Ranking API is CI-only and synthetic-only (D-15, SERVICES.md)."""
        return self.tenant_id == "dev-tenant"

    @property
    def members(self) -> frozenset[str]:
        """`api_members` as a set (the verifier compares case-insensitively)."""
        return frozenset(e.strip() for e in self.api_members.split(",") if e.strip())

    @property
    def sign_in_providers(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.api_sign_in_providers.split(",") if p.strip())

    @property
    def sinks(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.otel_sinks.split(",") if s.strip())

    def __repr__(self) -> str:
        return (
            f"Settings(env={self.env!r}, tenant_id={self.tenant_id!r}, region={self.gcp_region!r})"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so `.env` is read once."""
    return Settings()
