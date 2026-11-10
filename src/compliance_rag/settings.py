"""The only reader of environment configuration (`CLAUDE.md` code standards).

`.env` locally, Secret Manager in Cloud Run. Nothing else in the package reads
`os.environ`. Secrets are never logged: `repr` of this object redacts them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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

MIN_SESSION_SECRET_CHARS = 32
"""A session secret shorter than this is refused: the cookie's signature is
only as strong as the key (#280)."""

BEDROCK_REGION = "ap-southeast-2"
"""Sydney, the only Region the Bedrock backend is called from (ADR-0056). Its
`au.` geographic inference profile processes every request in Sydney or
Melbourne, so it is the gate's key for generation that stays in Australia."""

BEDROCK_INFERENCE_PROFILE = "au.anthropic.claude-sonnet-5"
"""The one inference profile the Bedrock backend may name: the Australian
geographic profile, whose destinations are Sydney and Melbourne only. An exact
allowlist, not a prefix: `global.`, `us.`, `apac.` route outside Australia, and
a prefix alone is not proof of where a request goes (ADR-0056). Adding a model
means adding its profile here, after reading its destinations."""

VERTEX_GENERATION_REGIONS = frozenset({REGION, BUILD_PHASE_OFFSHORE_REGION})
"""What `VERTEX_CLAUDE_REGION` may be. Unchanged from the build phase (ADR-0023,
ADR-0024)."""

AUSTRALIAN_GENERATION_REGIONS = frozenset({REGION, BEDROCK_REGION})
"""Where generation runs inside Australia, and so where no offshore gate
fires: Vertex in Sydney, or Bedrock's `au.` profile called from Sydney."""

GENERATION_REGIONS = VERTEX_GENERATION_REGIONS | {BEDROCK_REGION}
"""Every key the residency gates accept: `Settings.generation_region` is one of
these, whichever backend is in use."""

GenerationBackend = Literal["bedrock", "vertex"]
"""`GENERATION_BACKEND`: which provider serves Claude (ADR-0056)."""

GeneratorName = Literal["claude", "demo"]
"""What `GENERATOR` can name, and what the ledger records with an answer turn
(ledger 0020, #292)."""


def _addresses(raw: str) -> frozenset[str]:
    """A comma-separated list of emails: the one parser for `API_MEMBERS` and
    `API_ADMINS`."""
    return frozenset(e.strip() for e in raw.split(",") if e.strip())


class Settings(BaseSettings):
    """Environment configuration, validated once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,  # a refused secret never echoes into the error (#280)
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
    generator: GeneratorName = "claude"
    """`GENERATOR`: which `Generator` the API builds (#292). `claude` is Claude
    through `GENERATION_BACKEND` (Bedrock's Australian profile by default; on
    `vertex`, `VERTEX_CLAUDE_MODEL` is required and the residency gate runs);
    `demo` is the in-process stand-in that calls no model
    (`adapters/demo_generator.py`), which runs in Sydney and so requires
    `VERTEX_CLAUDE_REGION=australia-southeast1`. While it is `demo`, every
    page the API serves carries a banner saying so, and every answer event
    reports it."""
    generation_backend: GenerationBackend = "bedrock"
    """`GENERATION_BACKEND` (ADR-0056): `bedrock` is Claude Sonnet 5 through
    Bedrock's Australian inference profile, called from Sydney, so every
    request is processed in Australia; `vertex` is the build-phase path
    (`VERTEX_CLAUDE_*`), kept until it is removed. Only `GENERATOR=claude`
    uses either."""
    vertex_claude_model: str = ""
    vertex_claude_region: str = REGION
    """Sydney by default — fail closed. The build phase opts into the US
    multi-region explicitly (`VERTEX_CLAUDE_REGION=us`, ADR-0024). Read only
    with `GENERATION_BACKEND=vertex`."""

    # --- Bedrock -----------------------------------------------------------
    bedrock_region: str = BEDROCK_REGION
    """The Region Bedrock is called from: Sydney only (ADR-0056)."""
    bedrock_inference_profile: str = BEDROCK_INFERENCE_PROFILE
    """The Australian geographic inference profile, whose destinations are
    Sydney and Melbourne only. Anything but that exact profile is refused."""
    aws_role_arn: str = ""
    """`AWS_ROLE_ARN`: on Cloud Run, the role the runtime service account's
    Google ID token is exchanged for (STS `AssumeRoleWithWebIdentity`). No AWS
    key exists anywhere (`adapters/aws_credentials.py`)."""
    aws_profile: str = ""
    """`AWS_PROFILE`: locally, a named profile (IAM Identity Center or SSO).
    Set this or `AWS_ROLE_ARN`, not both."""
    aws_web_identity_audience: str = "sts.amazonaws.com"
    """The audience asked of Google for the ID token, which the role's trust
    policy pins as `accounts.google.com:oaud` (docs/setup/aws-bedrock.md)."""
    aws_bearer_token_bedrock: SecretStr = SecretStr("")
    """`AWS_BEARER_TOKEN_BEDROCK`, which the Anthropic SDK would otherwise pick
    up from the environment on its own: a long-lived Bedrock API key. Read here
    only to refuse it."""
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
    api_admins: str = ""
    """Comma-separated emails of the members who are also admins (ADR-0053 U6):
    they alone write subscriptions and record profile evidence. An admin is a
    member first, so an address here that `api_members` lacks is refused. Read
    on every request, so removing one ends their admin access at once. Empty
    makes no one an admin."""
    api_sign_in_providers: str = "google.com,password"
    """Firebase sign-in providers whose verified email is trusted. Google and
    email/password prove ownership; an arbitrary OIDC or SAML provider can
    assert any address (#145)."""
    firebase_web_api_key: str = ""
    """The Firebase project's web API key, which the sign-in page hands the web
    SDK (#280). It identifies the project and is not a secret (Firebase's own
    guidance), so it is a plain string. Empty: the sign-in page isn't served."""
    firebase_auth_domain: str = ""
    """The web SDK's `authDomain`. Empty: `<firebase_project_id>.firebaseapp.com`."""
    session_secret: SecretStr = SecretStr("")
    """Signs the session cookie (#280): Secret Manager in Cloud Run, `.env`
    locally. At least `MIN_SESSION_SECRET_CHARS` random characters. Empty:
    cookie sessions are off, and only the Bearer header signs anyone in."""
    api_allowed_origins: str = ""
    """Comma-separated origins (`https://host`) besides the request's own whose
    state-changing requests may carry the session cookie (#280). Empty: only
    the request's own origin."""
    api_docs: bool = False
    """Serve the OpenAPI schema and /docs. Off unless asked for, so a
    deployment missing `ENV` doesn't publish it."""
    offshore_generation_testers: str = ""
    """Comma-separated member emails the operator registers as build-phase
    testers: people who ask only about public law and synthetic data. While
    generation runs outside Australia, only their questions are sent to it
    (ADR-0032). Empty: no member's question leaves Australia. Ignored when
    generation runs in Sydney."""

    # --- Where this process runs -------------------------------------------
    k_service: str = ""
    """`K_SERVICE`, set by Cloud Run in every deployed service. Read only so a
    local-only tool refuses to start there."""
    cloud_run_job: str = ""
    """`CLOUD_RUN_JOB`, set by Cloud Run in every job, for the same reason."""

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
        if v not in VERTEX_GENERATION_REGIONS:
            raise ValueError(
                f"vertex_claude_region must be one of {sorted(VERTEX_GENERATION_REGIONS)} — "
                f"never `global` or any other region (ADR-0023); got {v!r}"
            )
        return v

    @field_validator("bedrock_region")
    @classmethod
    def _bedrock_region_is_sydney(cls, v: str) -> str:
        if v != BEDROCK_REGION:
            raise ValueError(
                f"bedrock_region must be {BEDROCK_REGION}: the Australian inference profile is "
                f"called from Sydney only (ADR-0056); got {v!r}"
            )
        return v

    @field_validator("bedrock_inference_profile")
    @classmethod
    def _bedrock_profile_is_australian(cls, v: str) -> str:
        if v != BEDROCK_INFERENCE_PROFILE:
            raise ValueError(
                f"bedrock_inference_profile must be {BEDROCK_INFERENCE_PROFILE!r}, the Australian "
                "geographic profile; `global.`, `us.`, `apac.` and any other profile can route "
                f"outside Australia (ADR-0056); got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _no_long_lived_aws_credentials(self) -> Settings:
        # The SDK reads this variable by itself, and a bearer key would then
        # replace the federated credentials without a word.
        if (
            self.generation_backend == "bedrock"
            and self.aws_bearer_token_bedrock.get_secret_value()
        ):
            raise ValueError(
                "AWS_BEARER_TOKEN_BEDROCK is a long-lived Bedrock API key and is refused: "
                "credentials come from web-identity federation or a named profile (ADR-0056)"
            )
        return self

    @model_validator(mode="after")
    def _demo_generator_runs_in_sydney(self) -> Settings:
        # The residency gates ask where generation runs. The demo generator
        # runs in this process, so they must be told Sydney: an offshore
        # region beside it would say the question leaves Australia when it
        # doesn't, and would let a tester list decide what it must not (#292).
        if self.generator == "demo" and self.vertex_claude_region != REGION:
            raise ValueError(
                f"GENERATOR=demo runs in this process, so vertex_claude_region must be "
                f"{REGION}; got {self.vertex_claude_region!r}"
            )
        return self

    @field_validator("session_secret")
    @classmethod
    def _session_secret_is_long_enough(cls, v: SecretStr) -> SecretStr:
        # The length only: the message never carries the value.
        if v.get_secret_value() and len(v.get_secret_value()) < MIN_SESSION_SECRET_CHARS:
            raise ValueError(
                f"session_secret must be at least {MIN_SESSION_SECRET_CHARS} characters"
            )
        return v

    @field_validator("vertex_embedding_dims")
    @classmethod
    def _dims(cls, v: int) -> int:
        if v != 1536:
            raise ValueError(f"vertex_embedding_dims is fixed at 1536 (PHASES.md Phase 1); got {v}")
        return v

    @property
    def generation_region(self) -> str:
        """The key the residency gates ask about, by backend: Bedrock's `au.`
        profile called from Sydney (`bedrock_region`, already refused unless
        `ap-southeast-2`), or the Vertex region. A Bedrock request is processed
        in Sydney or Melbourne, so its key is not offshore (ADR-0056)."""
        if self.generation_backend == "bedrock":
            return self.bedrock_region
        return self.vertex_claude_region

    @property
    def generation_model(self) -> str:
        """What is asked to generate, by backend: the Vertex model, or the
        Bedrock inference profile."""
        if self.generation_backend == "bedrock":
            return self.bedrock_inference_profile
        return self.vertex_claude_model

    @property
    def ranking_api_allowed(self) -> bool:
        """The Vertex Ranking API is CI-only and synthetic-only (D-15, SERVICES.md)."""
        return self.tenant_id == "dev-tenant"

    @property
    def members(self) -> frozenset[str]:
        """`api_members` as a set (the verifier compares case-insensitively)."""
        return _addresses(self.api_members)

    @property
    def admins(self) -> frozenset[str]:
        """`api_admins` as a set, parsed as `members` is and compared by
        `firebase_auth.is_member`; being listed is not enough without being a
        member as well."""
        return _addresses(self.api_admins)

    @property
    def sign_in_providers(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.api_sign_in_providers.split(",") if p.strip())

    @property
    def allowed_origins(self) -> frozenset[str]:
        return frozenset(
            o.strip().rstrip("/").lower() for o in self.api_allowed_origins.split(",") if o.strip()
        )

    @property
    def web_auth_domain(self) -> str:
        return self.firebase_auth_domain or f"{self.firebase_project_id}.firebaseapp.com"

    @property
    def offshore_testers(self) -> frozenset[str]:
        """`offshore_generation_testers` as a set, lowercased as the sign-in
        verifier lowercases members."""
        return frozenset(
            e.strip().lower() for e in self.offshore_generation_testers.split(",") if e.strip()
        )

    @property
    def production_markers(self) -> tuple[str, ...]:
        """What says this process is deployed, by name: `ENV=cloud`, or a
        variable Cloud Run sets. Empty on a laptop or a CI runner."""
        markers = (
            ("ENV=cloud", self.env == "cloud"),
            ("K_SERVICE", bool(self.k_service)),
            ("CLOUD_RUN_JOB", bool(self.cloud_run_job)),
        )
        return tuple(name for name, present in markers if present)

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
