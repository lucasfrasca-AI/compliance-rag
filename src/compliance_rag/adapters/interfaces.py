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

from compliance_rag.adapters.removal_authorization import RemovalAuthorization
from compliance_rag.corpus.models import Chunk, ClauseRecord, Scope, validate_tenant_id

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


class RemovalAction(StrEnum):
    """The removal operations that are audited (ADR-0021). Unsubscribe isn't
    one: it touches no store (ADR-0020)."""

    WITHDRAW = "withdraw"
    PURGE = "purge"
    REPLACE_IN_PLACE = "replace_in_place"


class RemovalOutcome(StrEnum):
    REFUSED = "refused"
    AUTHORIZED = "authorized"
    """Written before any store is touched, so a crash mid-removal still
    leaves a trace."""
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RemovalAuditEntry:
    audit_id: str
    tenant_id: str
    instrument_id: str
    version_id: str | None
    action: RemovalAction
    outcome: RemovalOutcome
    actor: str
    reason: str
    findings_citing: int | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ManifestMetadata:
    """The version-level fields of a `manifest.json` that aren't a
    per-clause hash (issue #19): the canonicalisation rule every in-scope
    clause was hashed under, and the sha256 of the exact `source.html`
    bytes the parser injected anchors into. `ClauseStore.get_manifest`'s
    own `dict[str, str]` return stays the per-clause view; this is the
    rest of the same manifest.json.
    """

    canonicalisation_version: int
    source_html_sha256: str


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
    """Track B: hybrid retrieval over chunks, filtered to Track A's IDs (D-03).

    Every implementation restricts a tenant to its own documents plus the
    library instruments it subscribes to (ADR-0020), on every method — the
    caller cannot pass that filter in, so it cannot leave it out.
    """

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


class RetrievalAdmin(ABC):
    """The write side of a retrieval store (ADR-0021): never replace in
    place, never delete on update. A new version is upserted next to the old
    one and the old one is retired with `valid_to`; withdrawal and deletion
    need a `RemovalAuthorization`, which only `CorpusLifecycle` issues."""

    @abstractmethod
    async def upsert_chunks(self, chunks: tuple[Chunk, ...]) -> None: ...

    @abstractmethod
    async def version_exists(self, *, instrument_id: str, tenant_id: str, version_id: str) -> bool:
        """Whether any chunk of this version is stored, visible or not —
        publishing over an existing version would be a replace in place."""

    @abstractmethod
    async def retire_version(
        self, *, instrument_id: str, tenant_id: str, version_id: str, valid_to: dt.date
    ) -> None:
        """Set `valid_to` on every chunk of this version. Nothing is removed."""

    @abstractmethod
    async def withdraw_version(self, authorization: RemovalAuthorization) -> None:
        """Exclude the target from retrieval at every `as_of`, keeping its
        text and vectors so prior citations still verify."""

    @abstractmethod
    async def delete_chunks(self, authorization: RemovalAuthorization) -> None:
        """Actually delete the target's chunks."""


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

    @abstractmethod
    async def subscribe(self, *, tenant_id: str, instrument_id: str, subscribed_by: str) -> None:
        """Tick one library instrument for a tenant (ADR-0020). Library ids
        only — a tenant's own documents are already in its environment.
        Idempotent: subscribing twice leaves one row."""

    @abstractmethod
    async def unsubscribe(self, *, tenant_id: str, instrument_id: str) -> None:
        """Remove the subscription row and nothing else (ADR-0020): no store
        is touched, so another tenant's view never changes and this tenant's
        prior citations still resolve."""

    @abstractmethod
    async def subscribed_instruments(self, tenant_id: str) -> frozenset[str]:
        """Library instrument ids this tenant may retrieve from. Empty for
        `_public`, which sees the library by ownership, not subscription."""

    @abstractmethod
    async def count_findings_citing(self, tenant_id: str, stable_id_prefix: str) -> int:
        """Findings of this tenant with at least one citation whose
        `clause_id` starts with `stable_id_prefix` — `instrument:` for every
        version, `instrument:version:` for one (ADR-0021)."""

    @abstractmethod
    async def mark_withdrawn(
        self, *, tenant_id: str, instrument_id: str, version_id: str, withdrawn_by: str
    ) -> None: ...

    @abstractmethod
    async def is_withdrawn(
        self, *, tenant_id: str, instrument_id: str, version_id: str
    ) -> bool: ...

    @abstractmethod
    async def mark_purged(
        self,
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str | None,
        purged_by: str,
        reason: str,
    ) -> None:
        """The ledger keeps the version row with a purged marker; nothing
        about who, when and why is ever deleted (ADR-0021). `version_id`
        None marks every version row of the instrument."""

    @abstractmethod
    async def record_removal_audit(self, entry: RemovalAuditEntry) -> None: ...

    @abstractmethod
    async def removal_audit(self, tenant_id: str) -> tuple[RemovalAuditEntry, ...]: ...


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

    @abstractmethod
    async def put_manifest(
        self,
        instrument_id: str,
        version_id: str,
        *,
        tenant_id: str,
        canonicalisation_version: int,
        source_html: str,
    ) -> None:
        """Store the exact HTML the parser injected anchors into as
        `source.html`, and record `canonicalisation_version` plus its
        sha256 in this instrument version's `manifest.json` (issue #19),
        alongside the clause paths `put_clause` already makes visible via
        `get_manifest`. A re-parse that changes the anchors without this
        being called again would leave a stale `source.html` on disk with
        no way to detect it — which is why the hash is recorded here, not
        only the bytes themselves.
        """

    @abstractmethod
    async def get_manifest_metadata(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> ManifestMetadata | None:
        """The version-level fields last written by `put_manifest`, or
        `None` if this instrument version has never been ingested."""

    @abstractmethod
    async def get_source_html(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> str | None:
        """The exact HTML last stored by `put_manifest` for this instrument
        version, or `None` if none has been stored yet."""

    @abstractmethod
    async def purge(self, authorization: RemovalAuthorization) -> None:
        """Delete every stored object under the target — clauses, source,
        manifest, and every noncurrent version of each (ADR-0021)."""


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
