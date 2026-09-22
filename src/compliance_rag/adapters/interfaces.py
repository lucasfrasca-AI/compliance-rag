"""Repository interfaces between the app and every external service (D-08).

The application talks only to these six protocols. No Cypher, Qdrant, Vertex or
SQL call exists outside `adapters/`. Each interface has in-memory fakes in
`fakes.py` and a contract test in `tests/contracts/`.

Two rules are encoded in the signatures rather than left to convention:

- every graph traversal takes `as_of` and a `max_depth` (D-05), and
- retrieval is two-track: `RetrievalStore` filters on obligation IDs supplied by
  the caller from Track A, and never ranks graph results (D-03).
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from compliance_rag.corpus.models import ClauseRecord, Scope, validate_tenant_id

# --------------------------------------------------------------------------
# Shared value objects
# --------------------------------------------------------------------------


class Channel(StrEnum):
    """One retrieval signal (`CONTEXT.md`)."""

    DENSE = "dense"
    SPARSE = "sparse"
    KEYWORD = "keyword"


class Assessability(StrEnum):
    """Whether Track A could decide an obligation applies (D-06)."""

    ASSESSABLE = "assessable"
    UNASSESSABLE = "unassessable"


@dataclass(frozen=True, slots=True)
class Obligation:
    """A graph node for a clause that imposes a requirement."""

    obligation_id: str
    clause_id: str
    instrument_id: str
    summary: str
    assessability: Assessability = Assessability.ASSESSABLE
    unassessable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Profile:
    """A tenant's structured facts. Lives in the ledger; never embedded (D-14).

    `Profile` is where a tenant enters the system (`CONTEXT.md`: Tenant id,
    issue #3), so `tenant_id` is validated here rather than only where a
    clause or chunk happens to be built. `_public` is rejected: a profile is
    always a real company, never public law.
    """

    tenant_id: str
    jurisdictions: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    actor_roles: tuple[str, ...] = ()
    data_categories: tuple[str, ...] = ()
    regulators: tuple[str, ...] = ()
    size_band: str | None = None

    def __post_init__(self) -> None:
        validate_tenant_id(self.tenant_id)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One retrieval hit, before or after fusion."""

    chunk_id: str
    clause_id: str
    text: str
    score: float
    channel: Channel | None = None
    obligation_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Both channel candidate lists plus the fused list.

    Both channels are logged before fusion (`CLAUDE.md` code standards), which
    is why the per-channel lists are returned rather than discarded.
    """

    fused: tuple[Candidate, ...]
    per_channel: dict[Channel, tuple[Candidate, ...]] = field(
        default_factory=dict[Channel, tuple[Candidate, ...]]
    )


@dataclass(frozen=True, slots=True)
class ScoredDocument:
    """A reranker's score for one candidate."""

    index: int
    score: float


@dataclass(frozen=True, slots=True)
class Generation:
    """A generator's structured output plus what it cost."""

    text: str
    input_tokens: int
    output_tokens: int
    cached_prefix_hit: bool = False
    stop_reason: str | None = None


# --------------------------------------------------------------------------
# Interfaces
# --------------------------------------------------------------------------


class GraphStore(ABC):
    """Track A: deterministic traversal from profile to applicable obligations.

    Every method takes `as_of`. A traversal without it is a test failure (D-05),
    which is why it has no default.
    """

    @abstractmethod
    async def applicable_obligations(
        self,
        profile: Profile,
        *,
        as_of: dt.date,
        max_depth: int,
    ) -> tuple[Obligation, ...]:
        """Every obligation that binds this profile on `as_of`.

        Unranked and untruncated: the caller must not impose a top-k (D-03).
        """

    @abstractmethod
    async def obligation(self, obligation_id: str, *, as_of: dt.date) -> Obligation | None:
        """One obligation as it stood on `as_of`."""

    @abstractmethod
    async def crosswalks(
        self,
        obligation_id: str,
        *,
        as_of: dt.date,
        max_depth: int,
    ) -> tuple[Obligation, ...]:
        """Obligations reachable by `CROSSWALKS_TO` within `max_depth` hops."""


class RetrievalStore(ABC):
    """Track B: hybrid retrieval over chunks, filtered to Track A's IDs (D-03)."""

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        obligation_ids: tuple[str, ...],
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        limit: int,
    ) -> RetrievalResult:
        """Dense + sparse channels fused with RRF inside the store (D-04)."""

    @abstractmethod
    async def lookup_clause(
        self,
        clause_id: str,
        *,
        tenant_id: str,
        as_of: dt.date,
    ) -> Candidate | None:
        """Exact clause-ID lookup. The router sends ID queries here (D-12)."""


class LedgerStore(ABC):
    """Postgres. The one non-derivable store (D-09)."""

    @abstractmethod
    async def get_profile(self, tenant_id: str) -> Profile | None: ...

    @abstractmethod
    async def put_profile(self, profile: Profile) -> None: ...

    @abstractmethod
    async def record_assessment(
        self,
        *,
        tenant_id: str,
        assessment_id: str,
        corpus_version: str,
        mapping_version: str,
        as_of: dt.date,
        trace_id: str,
    ) -> None:
        """Every ledger row that results from a request stores its `trace_id`."""

    @abstractmethod
    async def get_assessment(
        self, tenant_id: str, assessment_id: str
    ) -> dict[str, object] | None: ...


class ClauseStore(ABC):
    """Canonical text and per-version manifests (`PHASES.md`: Cloud Storage
    layout, issue #15). The only seam a Cloud Storage call crosses — nothing
    else in the codebase reads or writes object storage.
    """

    @abstractmethod
    async def get_clause(self, clause_id: str, *, tenant_id: str) -> ClauseRecord | None:
        """One clause by its stable ID, or `None` if it doesn't exist."""

    @abstractmethod
    async def put_clause(self, clause: ClauseRecord) -> None:
        """Store one clause, keyed by its stable ID and `clause.tenant_id`."""

    @abstractmethod
    async def get_manifest(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> dict[str, str]:
        """Every clause path in this instrument version, mapped to its
        `canonical_sha256` — one `manifest.json` per instrument version
        (`PHASES.md`).
        """


class Reranker(ABC):
    """Second-stage scoring, in Australia or not at all (D-15)."""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: tuple[str, ...],
        *,
        top_k: int,
    ) -> tuple[ScoredDocument, ...]:
        """Score documents against the query, returning the top `top_k`."""


class Embedder(ABC):
    """Dense vectors. Facts are never embedded (D-14)."""

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class Generator(ABC):
    """Claude on Vertex. Never authors a citation (D-02)."""

    @abstractmethod
    async def generate(
        self,
        *,
        system: str,
        cached_prefix: str | None,
        user: str,
        max_tokens: int,
    ) -> Generation:
        """Generate with a cached prompt prefix for the tenant policy corpus (D-11)."""


class CitationVerifier(ABC):
    """Deterministic check that a citation matches canonical text (D-02)."""

    @abstractmethod
    async def verify(
        self,
        clause_id: str,
        quoted_text: str,
        *,
        clauses: tuple[ClauseRecord, ...],
        canonicalisation_version: int,
    ) -> bool:
        """True only if the quote appears in the clause's canonical text.

        `canonicalisation_version` is the version the citation was recorded
        under (`CONTEXT.md`: Canonicalisation version, issue #2). Comparing it
        against a clause canonicalised under a different version would
        silently compare text produced by different rules, so implementations
        must raise rather than attempt it — never a bool for "I don't know".
        """
