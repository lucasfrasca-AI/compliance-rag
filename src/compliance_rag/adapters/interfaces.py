"""Repository interfaces between the app and every external service (D-08).

The application talks only to these six protocols. No Cypher, Qdrant, Vertex or
SQL call exists outside `adapters/`. Each interface has in-memory fakes in
`fakes.py` and a contract test in `tests/contracts/`.

Two rules are encoded in the signatures rather than left to convention:

- every graph traversal takes `as_of` and a `max_depth` (D-05), and
- retrieval is two-track: `RetrievalStore` returns library law only from the
  clauses Track A cites (`LawScope`), and never ranks graph results (D-03).
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from compliance_rag.adapters.graph_query import BuiltQuery
from compliance_rag.adapters.graph_schema import (
    Bitemporal,
    CorrectionWrite,
    EdgeWrite,
    EndReason,
    GraphCounts,
    GraphRow,
    NodeRef,
    NodeWrite,
    Scalar,
    StoredNode,
    refuse_duplicates,
)
from compliance_rag.adapters.removal_authorization import RemovalAuthorization
from compliance_rag.assessment.coverage import Coverage, CoverageRow
from compliance_rag.assessment.evidence import Evidence, Facts
from compliance_rag.corpus.models import (
    Chunk,
    ClauseRecord,
    DocumentClass,
    Scope,
    validate_tenant_id,
)

# --------------------------------------------------------------------------
# Shared value objects
# --------------------------------------------------------------------------


class Channel(StrEnum):
    """One retrieval signal (`CONTEXT.md`)."""

    DENSE = "dense"
    SPARSE = "sparse"
    KEYWORD = "keyword"


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


UNSCOPED = "unscoped: "


@dataclass(frozen=True, slots=True)
class LawScope:
    """Which library-law clauses Track B may return (D-03, ADR-0030).

    Built from Track A's coverage (`LawScope.of`): the clauses its applicable
    and unassessable obligations cite. A library clause outside that set is
    never returned, by any channel, however well it matches. The tenant's own
    documents aren't law, and Track A doesn't scope them.

    `unscoped` is the one way to retrieve library law without Track A. It is
    for a question asked with no tenant profile (the Phase 1 retrieval eval,
    library-wide lookups), and it says why, so an unscoped call is visible in
    code and in traces — never an empty filter that silently means
    "everything"."""

    clause_ids: frozenset[str] | None
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("a law scope says where it came from")
        if self.clause_ids is None and not self.reason.startswith(UNSCOPED):
            raise ValueError("an unscoped retrieval comes only from LawScope.unscoped(reason)")

    @classmethod
    def of(cls, coverage: Coverage) -> LawScope:
        return cls(
            coverage.track_a_clause_ids,
            f"Track A for {coverage.tenant_id} on {coverage.as_of}",
        )

    @classmethod
    def unscoped(cls, reason: str) -> LawScope:
        if not reason.strip():
            raise ValueError("an unscoped retrieval says where it came from")
        return cls(None, f"{UNSCOPED}{reason}")

    def admits(self, scope: Scope, clause_id: str) -> bool:
        return scope is not Scope.PUBLIC or self.clause_ids is None or clause_id in self.clause_ids

    @property
    def described(self) -> str:
        """For a span: the reason, and how many clauses it admits."""
        if self.clause_ids is None:
            return self.reason
        return f"{self.reason}: {len(self.clause_ids)} clause(s)"


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
    """Every input token the request was billed for, cached ones included."""
    output_tokens: int
    cached_prefix_hit: bool = False
    stop_reason: str | None = None
    cache_read_input_tokens: int = 0
    """Of `input_tokens`, those served from the cached prefix (D-11)."""
    cache_creation_input_tokens: int = 0
    """Of `input_tokens`, those written to the cache by this request."""
    cost_usd: float = 0.0
    """From the real token counts at the adapter's published rates."""
    model: str = ""
    """The model that answered, as the adapter called it."""


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One generation call's real token counts and cost — ledger `usage`, keyed
    to the request's `trace_id` (#31; CLAUDE.md: every ledger row that results
    from a request stores its `trace_id`)."""

    tenant_id: str
    usage_id: str
    trace_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_prefix_hit: bool
    cost_usd: float


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
class PolicyVersion:
    """One version of a tenant's internal policy (#148)."""

    instrument_id: str
    version_id: str


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
    """Track A (D-03): every obligation in force on `as_of`, each applicable,
    not applicable or unassessable for this tenant (D-06), with the clauses
    that decide it. Unranked and untruncated: there is no top-k to pass.
    Every read behind it is a `BuiltQuery`, so it carries `as_of` and a depth
    cap (D-05)."""

    @abstractmethod
    async def coverage(self, facts: Facts, *, tenant_id: str, as_of: dt.date) -> Coverage:
        """Every obligation, evaluated against the tenant's evidence."""

    @abstractmethod
    async def crosswalks(
        self,
        obligation_id: str,
        *,
        as_of: dt.date,
        max_depth: int,
    ) -> tuple[str, ...]:
        """Obligation and control ids reachable by `CROSSWALKS_TO` within
        `max_depth` hops, in force on `as_of`."""


class GraphReader(ABC):
    """Builder-built reads of the persisted graph — all Track A needs."""

    @abstractmethod
    async def read(self, query: BuiltQuery) -> tuple[GraphRow, ...]:
        """Run one builder-built query."""


class GraphAdmin(GraphReader):
    """Writes to the persisted graph, and builder-built reads (Phase 2).

    The loaders (`graph/loader.py`) talk to this, never to the Neo4j adapter.
    Every write keeps D-05: nodes immutable except `valid_to`, set once;
    identical re-writes are no-ops. Every read is a `BuiltQuery`, so it
    carries `as_of` and a depth cap.
    """

    @abstractmethod
    async def put_node(
        self, node: NodeRef, bitemporal: Bitemporal, properties: Mapping[str, Scalar] | None = None
    ) -> bool:
        """True if created, False if an identical node existed."""

    @abstractmethod
    async def put_edge(
        self,
        edge_type: str,
        source: NodeRef,
        target: NodeRef,
        properties: Mapping[str, Scalar] | None = None,
    ) -> bool:
        """True if created, False if an identical edge existed."""

    async def put_nodes(self, writes: Sequence[NodeWrite]) -> int:
        """Many nodes, with `put_node`'s guarantees; returns how many were
        created. By default one at a time — an adapter overrides it to batch
        (issue #108). A batch naming one node twice is refused."""
        refuse_duplicates("node", [(w.node.label, w.node.stable_id) for w in writes])
        return sum([await self.put_node(w.node, w.bitemporal, w.properties) for w in writes])

    async def put_edges(self, writes: Sequence[EdgeWrite]) -> int:
        """Many edges, with `put_edge`'s guarantees; returns how many were
        created. A batch naming one edge twice is refused."""
        refuse_duplicates(
            "edge", [(w.edge_type, w.source.stable_id, w.target.stable_id) for w in writes]
        )
        return sum(
            [await self.put_edge(w.edge_type, w.source, w.target, w.properties) for w in writes]
        )

    @abstractmethod
    async def retire_node(self, node: NodeRef, *, valid_to: dt.date, end_reason: EndReason) -> None:
        """Set `valid_to` and `end_reason` once, from the open sentinel."""

    @abstractmethod
    async def amend(
        self,
        *,
        old: NodeRef,
        new: NodeRef,
        effective: dt.date,
        properties: Mapping[str, Scalar] | None = None,
        retire_with: tuple[NodeRef, ...] = (),
    ) -> None:
        """New node, `SUPERSEDES`, old retired as superseded — and
        `retire_with` retired on the same date — in one transaction."""

    @abstractmethod
    async def correct(
        self, corrections: Sequence[CorrectionWrite], *, by: str, reason: str
    ) -> None:
        """Cosmetic corrections in place (ADR-0031), all in one transaction:
        each changes only `COSMETIC` properties its node (or crosswalk edge)
        already has, and appends the previous wording, who and why to that
        node's or edge's `corrections` trail. All land, or none does."""

    @abstractmethod
    async def counts(self) -> GraphCounts:
        """Whole-graph node and edge totals."""

    @abstractmethod
    async def stored(self, nodes: Sequence[NodeRef]) -> dict[NodeRef, StoredNode]:
        """These nodes exactly as stored, at no date — properties, valid
        time, outgoing edges — for a loader to compare with what it is about
        to write. Absent nodes are absent from the result. Not a traversal and
        never an answer about the law: Track A reads only `BuiltQuery`s."""


class RetrievalStore(ABC):
    """Track B: hybrid retrieval over chunks, filtered to Track A's IDs (D-03).

    Every implementation restricts a tenant to its own documents plus the
    library instruments it subscribes to (ADR-0020), on every method — the
    caller cannot pass that filter in, so it cannot leave it out. Library law
    is further restricted to `law` (Track A's clauses, D-03), which every
    method requires.
    """

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        law: LawScope,
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
        law: LawScope,
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
    async def holds_real_tenant_data(self) -> bool:
        """Whether any stored chunk is tenant-scoped and not marked synthetic
        — what closes offshore generation (ADR-0023). A chunk stored before
        the `synthetic` flag existed counts as real."""

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
    async def record_evidence(self, evidence: Evidence) -> None:
        """Append one fact (issue #114). Append-only: refuses an evidence id
        already recorded, and a `supersedes` naming evidence this tenant
        doesn't have or that something else already supersedes."""

    @abstractmethod
    async def evidence(self, tenant_id: str) -> tuple[Evidence, ...]:
        """Every fact recorded for the tenant, superseded ones included (the
        record of what an earlier assessment read), in recording order."""

    @abstractmethod
    async def record_coverage(
        self, *, tenant_id: str, assessment_id: str, coverage: Coverage, trace_id: str
    ) -> None:
        """One row per obligation for a recorded assessment (D-06): outcome,
        reason, citations and, for an unassessable one, its questions."""

    @abstractmethod
    async def coverage_rows(self, tenant_id: str, assessment_id: str) -> tuple[CoverageRow, ...]:
        """An assessment's coverage, by obligation id."""

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
    async def record_usage(self, usage: UsageRecord) -> None: ...

    @abstractmethod
    async def usage(self, tenant_id: str) -> tuple[UsageRecord, ...]: ...

    @abstractmethod
    async def record_version(
        self,
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str,
        valid_from: dt.date,
        supersedes: str | None,
        document_class: DocumentClass,
    ) -> None:
        """A version comes into force (ledger `instrument_version`). The
        record the verifier checks a cited version against, independently of
        what retrieval returned (ADR-0021 mechanism 3). `document_class` is
        the instrument's (#148): the same for every version, and library law
        if and only if the tenant is `_public` (`check_document_class`)."""

    @abstractmethod
    async def policy_versions(self, tenant_id: str, *, as_of: dt.date) -> tuple[PolicyVersion, ...]:
        """The tenant's internal policy versions in force on `as_of` (#148):
        `valid_from <= as_of < valid_to`, never withdrawn or purged, sorted by
        instrument then version, bytewise. What `answer()` sends as the cached
        prefix (D-11). An instrument recorded before document classes existed
        (ledger 0011) is a `ValueError` naming it, never a guess."""

    @abstractmethod
    async def document_class(self, *, tenant_id: str, instrument_id: str) -> DocumentClass | None:
        """The instrument's class, or None if it has never been recorded (or
        was recorded before classes existed)."""

    @abstractmethod
    async def end_version(
        self, *, tenant_id: str, instrument_id: str, version_id: str, valid_to: dt.date
    ) -> None:
        """A version stops being in force on `valid_to` (exclusive)."""

    @abstractmethod
    async def version_validity(
        self, *, tenant_id: str, instrument_id: str, version_id: str
    ) -> tuple[dt.date, dt.date | None] | None:
        """`(valid_from, valid_to)` — `valid_to` None while in force — or
        `None` for a version the ledger has never recorded."""

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
    async def purge_marker(
        self, *, tenant_id: str, instrument_id: str, version_id: str
    ) -> tuple[str, str] | None:
        """`(purged_by, reason)` if this version was purged — by itself or as
        part of its whole instrument — else None."""

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

    async def put_clauses(self, clauses: Sequence[ClauseRecord]) -> None:
        """Store many clauses — what an ingest uses. By default one at a time;
        an adapter whose `put_clause` also rewrites a shared per-version
        object overrides this to write that object once (issue #99)."""
        for clause in clauses:
            await self.put_clause(clause)

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


class CanonicalisationVersionMismatch(ValueError):
    """A citation and a clause canonicalised under different versions — never
    comparable, however similar the text looks (issue #2)."""


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


@dataclass(frozen=True, slots=True)
class Member:
    """A signed-in person admitted to the tenant (#146)."""

    uid: str
    email: str


class Unauthenticated(Exception):
    """No valid sign-in token: 401."""


class NotAMember(Exception):
    """A valid token for someone not admitted to the tenant: 403."""


class VerifierUnavailable(Exception):
    """The signing keys can't be had, so no token can be checked: 503."""


class TokenVerifier(ABC):
    """Who signed in, from an identity provider's token (#146). The API's one
    way in; it never decides the tenant."""

    @abstractmethod
    async def verify(self, token: str) -> Member:
        """The member the token signs in, or `Unauthenticated` /
        `NotAMember` / `VerifierUnavailable`. Never a reason that echoes the
        token."""

    @abstractmethod
    async def aclose(self) -> None: ...
