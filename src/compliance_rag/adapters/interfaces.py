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
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from compliance_rag.adapters.graph_query import BuiltQuery
from compliance_rag.adapters.graph_schema import (
    Bitemporal,
    CorrectionWrite,
    EdgeRef,
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
from compliance_rag.adapters.revision_authorization import RevisionAuthorization
from compliance_rag.assessment.coverage import (
    OBLIGATION_ID_RE,
    Coverage,
    CoverageRow,
    ExceptionOf,
    Outcome,
    QualifierOf,
)
from compliance_rag.assessment.environment import NotInEnvironment
from compliance_rag.assessment.evidence import (
    Evidence,
    EvidenceProvenance,
    Facts,
    FactsSnapshot,
)
from compliance_rag.corpus.models import (
    Chunk,
    ClauseRecord,
    DocumentClass,
    Incomplete,
    Scope,
    clause_ancestors,
    instrument_tenant_prefix,
    parse_stable_id,
    validate_tenant_id,
)
from compliance_rag.corpus.revision import RevisionRecord
from compliance_rag.corpus.submitted_parser import SubmittedFormat
from compliance_rag.settings import GeneratorName

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
    incomplete: Incomplete = ()
    """Its clause's incompleteness, from the stored chunk (ADR-0047): what
    marks it incomplete when it enters a prompt."""


UNSCOPED = "unscoped: "


@dataclass(frozen=True, slots=True)
class Owed:
    """How a clause a context owes bears on the obligation clauses there
    (ADR-0037, ADR-0044): the ones it is an exception to (says when they do
    not apply), and the ones it qualifies (says how they are satisfied, or
    what they require). Usually one kind; both only if it is an exception to
    one obligation and a qualifier of another."""

    excepts: tuple[str, ...] = ()
    qualifies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LawScope:
    """Which library-law clauses Track B may return (D-03, ADR-0030).

    Built from Track A's coverage (`LawScope.of`): the clauses its applicable
    and unassessable obligations cite, and the clauses inside them (#163). A
    library clause outside that set is never returned, by any channel, however
    well it matches. The tenant's own documents aren't law, and Track A
    doesn't scope them.

    It also includes those obligations' exceptions (#164, ADR-0037) and
    qualifiers (#182, ADR-0044), each with the clauses inside it, and says
    which clause each excepts (`exceptions`) or qualifies (`qualifiers`), so
    that an answer given an obligation's clause is given them too (`owed`),
    whatever the ranking did.

    `unscoped` is the one way to retrieve library law without Track A. It is
    for a question asked with no tenant profile (the Phase 1 retrieval eval,
    library-wide lookups), and it says why, so an unscoped call is visible in
    code and in traces — never an empty filter that silently means
    "everything". With no Track A, it knows no exceptions or qualifiers."""

    clause_ids: frozenset[str] | None
    reason: str
    exceptions: frozenset[ExceptionOf] = frozenset()
    """Each obligation clause with each of its exceptions, from the reviewed
    mapping through Track A (ADR-0037). Both are cited."""
    qualifiers: frozenset[QualifierOf] = frozenset()
    """Each obligation clause with each of its qualifiers, likewise
    (ADR-0044). Both are cited."""

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("a law scope says where it came from")
        if self.clause_ids is None and not self.reason.startswith(UNSCOPED):
            raise ValueError("an unscoped retrieval comes only from LawScope.unscoped(reason)")
        cited = self.clause_ids or frozenset[str]()
        for pair in (*self.exceptions, *self.qualifiers):
            if not cited.issuperset(pair):
                raise ValueError(f"{pair} names a clause the scope doesn't cite")

    @classmethod
    def of(cls, coverage: Coverage) -> LawScope:
        return cls(
            coverage.track_a_clause_ids,
            f"Track A for {coverage.tenant_id} on {coverage.as_of}",
            coverage.track_a_exceptions,
            coverage.track_a_qualifiers,
        )

    def owed(self, delivered: Iterable[str]) -> dict[str, Owed]:
        """The exceptions and qualifiers a context holding `delivered` must
        also hold: for each, the obligation clauses it excepts and those it
        qualifies. A clause is held if it, or a clause inside it, is
        delivered — a paragraph is its clause's text (ADR-0035). In clause id
        order."""
        held: set[str] = set()
        for clause_id in delivered:
            held |= {clause_id, *clause_ancestors(clause_id)}
        excepts: dict[str, set[str]] = {}
        qualifies: dict[str, set[str]] = {}
        for exception in self.exceptions:
            if exception.clause in held:
                excepts.setdefault(exception.exception, set()).add(exception.clause)
        for qualifier in self.qualifiers:
            if qualifier.clause in held:
                qualifies.setdefault(qualifier.qualifier, set()).add(qualifier.clause)
        return {
            c: Owed(tuple(sorted(excepts.get(c, ()))), tuple(sorted(qualifies.get(c, ()))))
            for c in sorted(excepts.keys() | qualifies.keys())
        }

    @classmethod
    def unscoped(cls, reason: str) -> LawScope:
        if not reason.strip():
            raise ValueError("an unscoped retrieval says where it came from")
        return cls(None, f"{UNSCOPED}{reason}")

    def admits(self, scope: Scope, clause_id: str) -> bool:
        """A cited clause admits the clauses inside it: the mapping cites a
        subclause (APP 11.2), and the law is stored down to its paragraphs
        (APP 11.2(b), (d)), which are its text (#163)."""
        if scope is not Scope.PUBLIC or self.clause_ids is None:
            return True
        return clause_id in self.clause_ids or not self.clause_ids.isdisjoint(
            clause_ancestors(clause_id)
        )

    @property
    def described(self) -> str:
        """For a span: the reason, and how many clauses it cites. Each also
        admits the clauses inside it (#163), which this doesn't count."""
        if self.clause_ids is None:
            return self.reason
        return f"{self.reason}: {len(self.clause_ids)} cited clause(s) and those inside them"


@dataclass(frozen=True, slots=True)
class PolicyCorpus:
    """The clauses of a tenant's policy corpus already sent as the CAG prefix
    (D-11). Retrieval never returns one of them, by any channel: the prefix is
    their authoritative copy, and the slots they'd take go to law (ADR-0033).

    It excludes tenant-scoped chunks only, so it can never narrow the law, and
    an external upload (never in the prefix) stays retrievable. Empty
    (`NO_PREFIX`) for a call that sends no prefix: the recall gate, a library
    question. Unlike `LawScope`, leaving it out can't widen what a tenant
    sees, only spend slots on clauses the model already has, so it isn't
    required (ADR-0033)."""

    clause_ids: frozenset[str] = frozenset()

    def excludes(self, scope: Scope, clause_id: str) -> bool:
        return scope is Scope.TENANT and clause_id in self.clause_ids

    @property
    def described(self) -> str:
        """For a span: how many clauses retrieval leaves out."""
        return f"{len(self.clause_ids)} clause(s) already in the CAG prefix"


NO_PREFIX = PolicyCorpus()
"""No CAG prefix sent: nothing is left out."""


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


class SourceKind(StrEnum):
    """The two parts of the repository an assessment is reproducible from: the
    corpus and the mappings (`CONTEXT.md`: Corpus version, Mapping version).
    Which paths make up each, and how a tag over them is named, is the git
    adapter's to know (`git_source_versions`, ADR-0034)."""

    CORPUS = "corpus"
    MAPPING = "mapping"


_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class SourceVersion:
    """A corpus or mapping version as a load recorded it (#153, ADR-0034): the
    commit the content was resolved to, and the tag that named it, if any.

    A tag can move; a commit can't, and reproducibility is the point, so the
    SHA is always present and the tag is the name it was known by. `tag` None
    means untagged: no tag over this content existed at load time, and the SHA
    alone names it."""

    kind: SourceKind
    sha: str
    tag: str | None = None

    def __post_init__(self) -> None:
        if not _SHA.fullmatch(self.sha):
            raise ValueError(f"a source version's sha is 40 lowercase hex characters: {self.sha!r}")
        if self.tag is not None and not self.tag.strip():
            raise ValueError("a source version's tag is a name, or None for untagged")

    @property
    def untagged(self) -> bool:
        return self.tag is None

    @property
    def described(self) -> str:
        """For a log line or a span: `mappings-2026.10 @ 1a2b3c4`."""
        return f"{self.tag or 'untagged'} @ {self.sha[:7]}"


@dataclass(frozen=True, slots=True)
class LoadedSource:
    """What the graph holds of one kind: the latest load that finished, and
    whether anything stands in the way of assessing against it.

    A load that failed may have left the graph part-written, so what stands in
    the way outlasts the failure: `loading` while it is open (running, or
    failed and not yet dealt with), and `blocked_by_abandoned` once someone has
    abandoned it, until a load of that kind finishes. Abandoning releases the
    hold so another load can begin; it doesn't make the graph trustworthy."""

    version: SourceVersion | None = None
    """None if no load of this kind has ever finished."""
    loading: bool = False
    """A load of this kind is open: begun, and neither finished nor closed."""
    blocked_by_abandoned: bool = False
    """A load of this kind was abandoned after it may have written, and none
    has finished since. A load closed having written nothing doesn't count."""


@dataclass(frozen=True, slots=True)
class LoadedSources:
    """What the graph holds, per kind (#153). The default is a graph nothing
    has been loaded into."""

    corpus: LoadedSource = LoadedSource()
    mapping: LoadedSource = LoadedSource()

    def of(self, kind: SourceKind) -> LoadedSource:
        return self.corpus if kind is SourceKind.CORPUS else self.mapping


@dataclass(frozen=True, slots=True)
class AssessmentRecord:
    """A recorded assessment (ledger `assessment`): for whom, for what date,
    against which corpus and mapping versions, in which request. Reproducible
    only with both versions (`CONTEXT.md`)."""

    tenant_id: str
    assessment_id: str
    user_id: str
    """Who asked: the member's email, as a subscription's `subscribed_by`."""
    corpus: SourceVersion
    mapping: SourceVersion
    as_of: dt.date
    trace_id: str
    check_id: str | None = None
    """The check this assessment judged, when it is a document check's (#299,
    ADR-0059 Q1): the submitted document it points at is that check's. `None`
    for a coverage assessment (`POST /v1/coverage`), which judges no document."""
    facts: FactsSnapshot | None = None
    """The facts the coverage was decided on, as the ledger held them (ADR-0034):
    every evidence row with its value, and their sha256. Recorded with the
    assessment of a check, in the same transaction; it records what was read and
    does not reconstruct facts as of a past date (#123)."""


def check_assessment_write(record: AssessmentRecord, rows: tuple[CoverageRow, ...]) -> None:
    """What every `LedgerStore` refuses before writing an assessment and its
    coverage (`record_coverage_assessment`), in this order. The database's own
    constraints refuse all of it again but that a version is of the right kind
    (ledger 0013); what only a store can refuse (the tenant not registered, the
    id already recorded) comes after, in both."""
    if not record.trace_id:
        raise ValueError("an assessment and its coverage carry the trace_id of their request")
    if not record.user_id.strip():
        raise ValueError("an assessment names who asked")
    if record.corpus.kind is not SourceKind.CORPUS or record.mapping.kind is not SourceKind.MAPPING:
        raise ValueError("an assessment records a corpus version and a mapping version")
    if len({row.obligation_id for row in rows}) != len(rows):
        raise ValueError("coverage names an obligation twice")
    for row in rows:
        if not row.reason.strip():
            raise ValueError(f"{row.obligation_id}: a coverage row says why")
        if row.outcome is not Outcome.UNASSESSABLE and not row.citations:
            raise ValueError(f"{row.obligation_id}: a decided obligation cites its clauses")


# --------------------------------------------------------------------------
# Check (#297, ADR-0059; CONTEXT.md: Check)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckRecord:
    """A recorded check (ledger `submitted_check`): one member's submission of
    a document to be checked, and the `submitted_document` version it points at
    (ADR-0059 Q1). Written once, when the document is stored; the assessment a
    later ticket records points back at it by `check_id`."""

    tenant_id: str
    check_id: str
    user_id: str
    """The submitter's uid (`Member.uid`, ADR-0036), never their email."""
    instrument_id: str
    version_id: str
    filename: str
    """The name the member's file had: shown, never trusted (its extension chose
    the format, and nothing else is read from it)."""
    document_format: SubmittedFormat
    trace_id: str
    """The request that submitted it."""
    created_at: dt.datetime | None = None
    """Stamped by the ledger on write; `None` on a record not yet written."""

    def visible_to(self, uid: str, *, admin: bool) -> bool:
        """A check is visible to its submitter and to the tenant's admins
        (ADR-0059 Q5); anyone else is told it does not exist, as for a chat
        that is not theirs (#162). One rule, so the page, the API and the
        source route cannot differ. `admin` is `deps.is_admin`'s answer, read
        from configuration on the request."""
        return admin or uid == self.user_id


def check_check_write(record: CheckRecord) -> None:
    """What every `LedgerStore` refuses before writing a check, in this order;
    the database's own constraints refuse the first four again. What only a
    store can refuse (the tenant not registered, the version not recorded or
    not a submitted document, the id or the version already checked) comes
    after, in both."""
    if not record.trace_id.strip():
        raise ValueError("a check carries the trace_id of its request")
    if not record.user_id.strip():
        raise ValueError("a check names its submitter")
    if not record.filename.strip():
        raise ValueError("a check names the file it was submitted as")
    if not record.check_id.strip():
        raise ValueError("a check has an id")
    if instrument_tenant_prefix(record.instrument_id) != record.tenant_id:
        raise ValueError(
            f"{record.instrument_id}: a tenant's check points at one of that tenant's documents"
        )


class CheckStage(StrEnum):
    """How far a check has got, from what the ledger holds (#355): the document
    is stored (`SUBMITTED`), its claims are extracted (`CLAIMS`, the extraction
    is done, however few claims it found), its findings are made (`FINDINGS`)."""

    SUBMITTED = "submitted"
    CLAIMS = "claims"
    FINDINGS = "findings"


class CheckRun(StrEnum):
    """What a check's latest run is doing, when it is not at rest (#355): a
    reservation held and not yet past its time-out (`RUNNING`), or the last run
    released as failed (`FAILED`). A reservation held past its time-out is
    neither: its request died, and the run may be taken over (`RESERVATION_TTL`)."""

    RUNNING = "running"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CheckListing:
    """One row of a list of checks (#355): metadata only. It carries no document
    text and no claim text, so a list is never a disclosure of what a check says."""

    check_id: str
    user_id: str
    """The submitter's uid (ADR-0036)."""
    filename: str
    title: str
    """The document's title (its `instrument_version`), which is the filename
    unless the member gave one."""
    created_at: dt.datetime
    stage: CheckStage
    run: CheckRun | None
    """The stage's next run, when one is under way or its last attempt failed:
    claims extraction when the stage is `SUBMITTED`, findings when it is `CLAIMS`."""


@dataclass(frozen=True, slots=True)
class CheckCursor:
    """Where a page of checks ends: the last row's `created_at` and `check_id`.
    The next page is the rows strictly older, in the list's order."""

    created_at: dt.datetime
    check_id: str


CHECK_PAGE_MAX = 100
"""The most checks one `list_checks` call returns."""


MAX_CLAIMS = 25
"""The most claims one check keeps (#298, ADR-0053): a reply with more is
refused whole, never cut to size."""

_PASSAGE_PATH = re.compile(r"p[1-9][0-9]*")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """One claim of a submitted document (#298, ADR-0059 Q2): a verbatim span of
    one of its passages, with the model's one-line label and the obligations and
    policy clauses the model picked to check it against (ADR-0053 F2). Always
    `inferred`: a claim is extracted from prose (CONTEXT.md: Provenance tier),
    and a claim is never promoted or mutated.

    **The ledger does not carry the document's words.** The span is named by its
    passage and character offsets in the document, with the sha256 of its text,
    so a reader reads it back from the stored passage and the hash proves it is
    the span the extraction found. A document purged (ADR-0021) takes its quotes
    with it. The summary is the model's, and only labels the quote."""

    claim_index: int
    """1-based, in document order."""
    passage: str
    """The passage's clause path (`p3`): the anchor a finding opens the document at."""
    quote_start: int
    quote_end: int
    """The span in the document's characters: `source[quote_start:quote_end]` is
    the quote, inside the passage's own `char_start:char_end`."""
    quote_sha256: str
    summary: str
    obligation_ids: tuple[str, ...]
    """From the obligation digest, and only from it (ADR-0053 F2)."""
    policy_clause_ids: tuple[str, ...]
    """From the tenant policy prefix, and only from it."""
    provenance: EvidenceProvenance = EvidenceProvenance.INFERRED


RESERVATION_TTL = dt.timedelta(minutes=10)
"""How long a reserved extraction may stay pending before another request may
take it over: a request that died holding one (the process was killed, so nothing
could mark it failed) must not block its check for good. Longer than any model
call, which `GeminiGenerator` times out and retries within a few minutes."""


class ExtractionAlreadyReserved(ValueError):
    """The check is already extracted, or another request holds it right now."""


@dataclass(frozen=True, slots=True)
class ClaimExtractionRecord:
    """The claims extracted from one check's document: written once, with every
    claim, when the extraction has been validated (ledger `check_extraction` and
    `check_claim`). A check with no claim still has its extraction recorded, so a
    document is extracted once and its cost is not spent again by a repeated
    request. The extraction is reserved before the model is called
    (`LedgerStore.reserve_extraction`), and this fills the reservation in."""

    tenant_id: str
    check_id: str
    as_of: dt.date
    """The date Track A decided the obligation digest on."""
    trace_id: str
    """The request that extracted them; every claim row carries it."""
    claims_dropped: int
    """How many the model returned that failed a check and were left out
    (traced, with the reasons, on the extraction's span)."""
    claims: tuple[ClaimRecord, ...]
    created_at: dt.datetime | None = None
    """Stamped by the ledger on write."""


def check_claims_write(record: ClaimExtractionRecord) -> None:
    """What every `LedgerStore` refuses before writing an extraction, in this
    order; the database's own constraints refuse them again. What only a store
    can refuse (a check it does not hold, one already extracted) comes after."""
    if not record.trace_id.strip():
        raise ValueError("an extraction carries the trace_id of its request")
    if not record.check_id.strip():
        raise ValueError("an extraction names its check")
    if record.claims_dropped < 0:
        raise ValueError("the number of claims dropped is not negative")
    if len(record.claims) > MAX_CLAIMS:
        raise ValueError(f"a check keeps at most {MAX_CLAIMS} claims")
    for number, claim in enumerate(record.claims, start=1):
        if claim.claim_index != number:
            raise ValueError("claims are numbered 1, 2, 3 ... in document order")
        if claim.provenance is not EvidenceProvenance.INFERRED:
            raise ValueError("a claim is inferred: it is extracted from prose")
        if _PASSAGE_PATH.fullmatch(claim.passage) is None:
            raise ValueError(f"{claim.passage!r} is not a passage path (p1, p2, ...)")
        if not 0 <= claim.quote_start < claim.quote_end:
            raise ValueError("a claim's span is a non-empty range of the document")
        if _SHA256_HEX.fullmatch(claim.quote_sha256) is None:
            raise ValueError("a claim carries the sha256 of its quote")
        if not claim.summary.strip() or len(claim.summary.splitlines()) != 1:
            raise ValueError("a claim's summary is one non-blank line")


# --------------------------------------------------------------------------
# Findings (#299, ADR-0053 F1-F6, ADR-0059; CONTEXT.md: Finding)
# --------------------------------------------------------------------------


class Verdict(StrEnum):
    """The four verdicts and where each ends (ADR-0053 F3, CONTEXT.md: Finding).
    No score (D-06)."""

    CONSISTENT = "consistent"
    CONFLICTS = "conflicts"
    NOT_ADDRESSED = "not_addressed"
    AMBIGUOUS = "ambiguous"


class FindingOutcome(StrEnum):
    """What a finding row is. Only `VERDICT` carries a verdict, and only one is
    ever shown: a withheld or failed finding stores none, so no reader, and no
    later code, can show what ADR-0003 kept from them."""

    VERDICT = "verdict"
    """The model's verdict, every citation verified (after any repair)."""
    CONDITIONAL = "conditional"
    """Against an obligation Track A can't decide (F4): built by code from Track
    A's reason and questions, with no model call and no verdict."""
    WITHHELD = "withheld"
    """A citation was blocked, or the finding cited nothing: the verdict is not
    stored and never shown (ADR-0003)."""
    FAILED = "failed"
    """No finding could be made: the reply was refused or silent, the model was
    unavailable, or a clause could not be delivered."""


class WithheldReason(StrEnum):
    """Why a finding is withheld (CONTEXT.md: Withheld finding): the verifier
    blocked a citation, or it cited nothing. In our words, never the model's."""

    CITATION_BLOCKED = "citation_blocked"
    UNCITED = "uncited"


class FailureReason(StrEnum):
    """Why a finding failed: no finding could be made for the target. In our
    words, never the model's."""

    MALFORMED_GENERATION = "malformed_generation"
    """The reply was not one valid set of findings."""
    NO_FINDING_RETURNED = "no_finding_returned"
    MALFORMED_FINDING = "malformed_finding"
    """The model's finding for it was refused by a check (its shape, an id it was not
    asked about, ...). Which check is on the span, and not said outward: a reason that
    named it ("ambiguous with no settle-by") would reveal the verdict class."""
    GENERATION_UNAVAILABLE = "generation_unavailable"
    """The model was not taking requests, for the call or for a citation repair."""
    CLAUSE_UNAVAILABLE = "clause_unavailable"
    """A clause the obligation, or what it owes, needs could not be delivered, so
    the model was not asked (ADR-0037, ADR-0044)."""
    BOUNDARY_COLLISION = "boundary_collision"


MAX_SETTLE_BY_CHARS = 400
"""The longest text that says what would settle an `ambiguous` finding."""
MAX_FINDINGS_PER_CLAIM = 20
"""The most findings one claim's call may return: at most ten obligations and ten
policy clauses are picked for a claim (`claims.MAX_PICKS`), one finding each. A
reply with more refuses the whole reply."""
MAX_CITATIONS_PER_FINDING = 10
"""The most citations one finding may carry."""


@dataclass(frozen=True, slots=True)
class FindingCitation:
    """A citation a finding was shown with, verified: the clause, the version it
    is of (so it stays clickable after the clause is superseded, ADR-0021), the
    model's quote (a verified substring of the clause's canonical text) and the
    canonicalisation it was verified under (ledger 0003). What a reader is shown
    for it is the clause's canonical text."""

    clause_id: str
    version_id: str
    quoted_text: str
    canonicalisation_version: int
    incomplete: Incomplete = ()

    def __post_init__(self) -> None:
        if parse_stable_id(self.clause_id)[1] != self.version_id:
            raise ValueError(f"{self.clause_id} is not a clause of version {self.version_id}")
        if not self.quoted_text.strip() or self.canonicalisation_version < 1:
            raise ValueError(
                "a finding's citation carries its quote and a canonicalisation version"
            )


@dataclass(frozen=True, slots=True)
class BlockedCitation:
    """A citation that failed verification: its clause and our word for why, never
    the model's quote (ADR-0003)."""

    clause_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class FindingRecord:
    """One finding of a check: one claim against one obligation (law) or one
    policy clause (policy), ADR-0053 F1. The claim is named by its number in the
    check's extraction, never by its words: the ledger does not carry the
    document's words (`ClaimRecord`)."""

    finding_id: str
    claim_index: int
    outcome: FindingOutcome
    obligation_id: str | None = None
    policy_clause_id: str | None = None
    verdict: Verdict | None = None
    settle_by: str | None = None
    """What would settle an `ambiguous` verdict (F3): required for it, and only
    for it. The model's text, shown labelled as such."""
    citations: tuple[FindingCitation, ...] = ()
    blocked: tuple[BlockedCitation, ...] = ()
    detail: WithheldReason | FailureReason | None = None
    """Why a finding is withheld (`WithheldReason`) or failed (`FailureReason`)."""
    unassessable_reason: str | None = None
    """For a conditional finding: Track A's reason, from the coverage row stored
    with the assessment, whose questions are the ones that would firm it up."""
    provenance: EvidenceProvenance = EvidenceProvenance.INFERRED
    """One tier, the weakest of the inputs (F5, ADR-0059 C1): the claim is
    `inferred` until confirmed (#301), so every finding is."""
    derived_from: str | None = None
    """The finding this one was derived from through an approved policy mapping
    (F6, #306). Unused until then."""

    @property
    def conditional(self) -> bool:
        return self.outcome is FindingOutcome.CONDITIONAL


def check_finding_write(finding: FindingRecord) -> None:
    """What every `LedgerStore` refuses before writing a finding, in this order;
    the database's own constraints refuse each again (ledger 0026)."""
    if not finding.finding_id.strip():
        raise ValueError("a finding has an id")
    if finding.claim_index < 1:
        raise ValueError("a finding names its claim, numbered from 1")
    if (finding.obligation_id is None) == (finding.policy_clause_id is None):
        raise ValueError(
            "a finding is against one obligation or one policy clause, never both or neither"
        )
    if finding.obligation_id is not None and not OBLIGATION_ID_RE.fullmatch(finding.obligation_id):
        raise ValueError(f"{finding.obligation_id!r} is not an obligation id")
    if finding.policy_clause_id is not None:
        parse_stable_id(finding.policy_clause_id)
    if finding.provenance is not EvidenceProvenance.INFERRED:
        raise ValueError("a finding is inferred until a claim is confirmed (#301)")
    shown = finding.outcome is FindingOutcome.VERDICT
    if shown != (finding.verdict is not None):
        raise ValueError("a finding has a verdict if and only if its outcome is a verdict")
    if finding.verdict is Verdict.AMBIGUOUS:
        if finding.settle_by is None or not finding.settle_by.strip():
            raise ValueError("an ambiguous finding names what would settle it")
        if len(finding.settle_by) > MAX_SETTLE_BY_CHARS:
            raise ValueError(f"settle-by text is at most {MAX_SETTLE_BY_CHARS} characters")
        if len(finding.settle_by.splitlines()) != 1:
            raise ValueError("settle-by text is one line")
    elif finding.settle_by is not None:
        raise ValueError("only an ambiguous finding carries settle-by text")
    if shown and not finding.citations:
        raise ValueError("a finding with a verdict cites at least one clause")
    if not shown and finding.citations:
        raise ValueError("a finding without a verdict shows no citations")
    if finding.outcome is not FindingOutcome.WITHHELD and finding.blocked:
        raise ValueError("only a withheld finding names blocked citations")
    if (finding.outcome in (FindingOutcome.WITHHELD, FindingOutcome.FAILED)) != (
        finding.detail is not None
    ):
        raise ValueError("a withheld or failed finding says why, and no other does")
    if (
        finding.outcome is FindingOutcome.WITHHELD
        and not isinstance(finding.detail, WithheldReason)
    ) or (
        finding.outcome is FindingOutcome.FAILED and not isinstance(finding.detail, FailureReason)
    ):
        raise ValueError("a withheld finding says a withheld reason, a failed one a failure")
    if finding.conditional != (finding.unassessable_reason is not None):
        raise ValueError("a conditional finding carries Track A's reason, and no other does")
    if finding.conditional and finding.obligation_id is None:
        raise ValueError("only an obligation is unassessable: a policy clause is never conditional")
    if finding.unassessable_reason is not None and not finding.unassessable_reason.strip():
        raise ValueError("a conditional finding's reason is not blank")


@dataclass(frozen=True, slots=True)
class FindingsRecord:
    """Everything one findings run writes, in one transaction (#299): the
    assessment, with the corpus and mapping versions it was computed against
    (ADR-0034); a coverage row for every obligation; the findings; and the usage
    of every model call the run made. All of it lands or none of it does, and
    once it has, it is never changed: a later check against newer facts is a new
    assessment (ADR-0034), and the earlier one is left as it was."""

    assessment: AssessmentRecord
    coverage: tuple[CoverageRow, ...]
    findings: tuple[FindingRecord, ...]
    usage: tuple[UsageRecord, ...]


FINDINGS_RESERVATION_TTL = dt.timedelta(minutes=30)
"""How long a reserved findings run may stay pending before another request may
take it over. Longer than the extraction's: a run is a warm-up call and then up
to 24 more, each of which `GeminiGenerator` may retry."""


class FindingsAlreadyReserved(ValueError):
    """The check's findings are already made, or another request holds the run
    right now."""


class CheckNotExtracted(ValueError):
    """The check has no completed extraction, so it has no claims to check."""


def check_findings_write(record: FindingsRecord, claim_count: int | None = None) -> None:
    """What every `LedgerStore` refuses before writing a findings run, in this
    order; the database's constraints refuse the rest again. `claim_count` is how
    many claims the check's extraction holds, when the store knows."""
    check_assessment_write(record.assessment, record.coverage)
    if record.assessment.check_id is None or not record.assessment.check_id.strip():
        raise ValueError("a findings run records the check it judged")
    if record.assessment.facts is None:
        raise ValueError("a findings run records the facts it was decided on (ADR-0034)")
    seen: set[tuple[int, str | None, str | None]] = set()
    covered = {row.obligation_id: row for row in record.coverage}
    for finding in record.findings:
        check_finding_write(finding)
        key = (finding.claim_index, finding.obligation_id, finding.policy_clause_id)
        if key in seen:
            raise ValueError("a claim has one finding against each obligation and policy clause")
        seen.add(key)
        if claim_count is not None and finding.claim_index > claim_count:
            raise ValueError(f"claim {finding.claim_index} is not one of the check's claims")
        if finding.obligation_id is not None:
            row = covered.get(finding.obligation_id)
            if row is None or row.outcome is Outcome.NOT_APPLICABLE:
                raise ValueError(
                    f"{finding.obligation_id}: a finding is against an obligation that applies "
                    "or might"
                )
            if (row.outcome is Outcome.UNASSESSABLE) != finding.conditional:
                raise ValueError(
                    f"{finding.obligation_id}: a finding is conditional if and only if Track A "
                    "can't decide the obligation"
                )
    ids = [f.finding_id for f in record.findings]
    if len(set(ids)) != len(ids):
        raise ValueError("finding ids are unique")
    for usage in record.usage:
        if usage.tenant_id != record.assessment.tenant_id:
            raise ValueError("usage rows are the run's tenant's")


# --------------------------------------------------------------------------
# Chat (#162, CONTEXT.md: Chat)
# --------------------------------------------------------------------------


class TurnRole(StrEnum):
    USER = "user"
    """The member's question."""
    ASSISTANT = "assistant"
    """What the member was shown in answer."""


class TurnOutcome(StrEnum):
    """How an answer turn ended, which is what the member was shown. One of
    these, or the turn is not an answer: the member's own question has none."""

    VERIFIED = "verified"
    """The verified text, with the citations it rests on."""
    BLOCKED = "blocked"
    """Withheld: a citation didn't match its clause (ADR-0003). The blocked
    citations' clause ids, and no text."""
    UNCITED = "uncited"
    """Withheld: the answer cited no clause, so nothing in it could be
    verified. No text."""
    RESIDENCY = "residency"
    """Refused by the residency gate (ADR-0032) before anything was sent."""
    MALFORMED_GENERATION = "malformed_generation"
    """The model's reply wasn't a valid answer; nothing in it is kept."""
    INTERNAL = "internal"
    """The answer couldn't be completed."""
    GENERATION_UNAVAILABLE = "generation_unavailable"
    """The model refused the request, for a reason in `Unavailability` (the
    429 quota case, #251). Kept with that reason, never the provider's words
    (#289). Rows recorded before it existed read `internal`."""
    POLICY_CORPUS_TOO_LARGE = "policy_corpus_too_large"
    """The CAG prefix was over the cap (ADR-0054), refused before any model was
    called. Kept with the measured estimate and the cap (#289). Rows recorded
    before it existed read `internal`."""

    @property
    def failed(self) -> bool:
        return self in (
            TurnOutcome.RESIDENCY,
            TurnOutcome.MALFORMED_GENERATION,
            TurnOutcome.INTERNAL,
            TurnOutcome.GENERATION_UNAVAILABLE,
            TurnOutcome.POLICY_CORPUS_TOO_LARGE,
        )


@dataclass(frozen=True, slots=True)
class PrefixMeasure:
    """What a request refused for an over-cap policy corpus was told: the
    prefix's estimated size and the cap, in tokens (ADR-0054). A measurement,
    never the policies' words."""

    estimated_tokens: int
    cap_tokens: int

    def __post_init__(self) -> None:
        for value in (self.estimated_tokens, self.cap_tokens):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:  # pyright: ignore[reportUnnecessaryIsInstance] — a stored value is not trusted
                raise ValueError("a prefix measure is two non-negative whole numbers")


@dataclass(frozen=True, slots=True)
class ChatCitation:
    """A citation an answer was shown with: the clause and the version of the
    instrument it is of, so it stays clickable after the clause is superseded
    (ADR-0021). Never the model's quote: what the member was shown is the
    clause's canonical text, which the clause store holds under this id.

    `incomplete` is the clause's incompleteness as the member was shown it
    (ADR-0047, #228): recorded with the turn, so a chat read back shows the
    flag the original answer carried. It is never recomputed from the corpus
    as it is today, and it is the verified clause's, never the model's."""

    clause_id: str
    version_id: str
    incomplete: Incomplete = ()
    """Empty for a complete clause, and for every turn recorded before the
    state existed: those answers were shown without a flag."""

    def __post_init__(self) -> None:
        if parse_stable_id(self.clause_id)[1] != self.version_id:
            raise ValueError(f"{self.clause_id} is not a clause of version {self.version_id}")


class UnreadableReason(StrEnum):
    """Why a stored citation couldn't be read back. A closed set of fixed
    words, so naming one never carries any of the row (#232)."""

    NOT_AN_OBJECT = "not_an_object"
    """The element isn't a JSON object."""
    NO_CLAUSE = "no_clause"
    """It names no clause, or not as a stable clause id. A `blocked` element
    that isn't a stable id is this too (#234)."""
    WRONG_VERSION = "wrong_version"
    """Its version isn't the version its clause id is of."""
    BAD_INCOMPLETE = "bad_incomplete"
    """Its recorded incompleteness isn't a list of valid reasons."""


@dataclass(frozen=True, slots=True)
class UnreadableCitation:
    """A stored citation that couldn't be read back: a corrupt row (#232). It
    holds its place in the turn's citations, so nothing is silently dropped and
    the member's history stays reachable, but it asserts nothing about the
    clause: it is never shown as verified text, and the incompleteness the
    member was shown with it is lost with it.

    `clause_id` is kept when the stored one is a well-formed stable id (the
    grammar admits nothing like prose), so the citation can still be named and
    clicked through to its clause; `version_id` only when it agrees with it.
    Nothing else of the element is kept, so a corrupt row's content never
    reaches a response or a log."""

    reason: UnreadableReason
    clause_id: str | None = None
    version_id: str | None = None


Citation = ChatCitation | UnreadableCitation
"""A citation as a chat reads back: whole, or marked unreadable."""

BlockedClause = str | UnreadableCitation
"""A blocked citation's clause id as a chat reads it back: the stable id, or an
`UnreadableCitation` in its place when the stored element isn't one (#234). An
unreadable one has no `clause_id`: a well-formed id is a readable entry."""


@dataclass(frozen=True, slots=True)
class UnreadableNotInEnvironment:
    """A stored named-instrument flag that couldn't be read back (a corrupt
    row): it holds its place, so the turn still says an instrument it named
    wasn't in the environment, and carries nothing of the row (#252, as
    #232 does for a citation)."""


StoredNotInEnvironment = NotInEnvironment | UnreadableNotInEnvironment
"""A named-instrument flag as a chat reads it back: whole, or marked
unreadable."""


@dataclass(frozen=True, slots=True)
class ChatAnswer:
    """What an answer turn holds: what the member was shown, and nothing else
    (ADR-0003). The model's unverified draft has no field here, so it cannot be
    recorded: a verified answer has its text and citations; a withheld one the
    ids of the citations that blocked it and no text; a failed one a marker
    only. The shape is checked on construction, so no store is ever handed a
    withheld answer that carries text."""

    outcome: TurnOutcome
    text: str | None = None
    """Verified answers only."""
    citations: tuple[ChatCitation, ...] = ()
    """Verified answers only: at least one."""
    blocked: tuple[str, ...] = ()
    """The clause ids of the blocked citations, for a `BLOCKED` answer only.
    Each is a stable id: the grammar admits nothing like prose."""
    not_in_environment: tuple[NotInEnvironment, ...] = ()
    """Each instrument the question named that the tenant's environment
    didn't hold (#252, ADR-0051), as the member was shown it beside the
    answer: set by the pipeline from the question, never by the model, and
    kept with a verified or a withheld answer alike. A failed request shows
    only its marker, so it carries none."""
    unavailable: Unavailability | None = None
    """Why the model refused, for `GENERATION_UNAVAILABLE` only: our word for
    it, never the provider's message (#289)."""
    prefix: PrefixMeasure | None = None
    """The estimate and the cap, for `POLICY_CORPUS_TOO_LARGE` only (#289)."""

    def __post_init__(self) -> None:
        if self.outcome.failed and self.not_in_environment:
            raise ValueError("a failed request is a marker only: it carries no flag")
        if (self.unavailable is not None) != (self.outcome is TurnOutcome.GENERATION_UNAVAILABLE):
            raise ValueError("a refused generation names its reason, and no other turn does")
        if (self.prefix is not None) != (self.outcome is TurnOutcome.POLICY_CORPUS_TOO_LARGE):
            raise ValueError("an over-cap corpus names its measure, and no other turn does")
        if self.outcome is TurnOutcome.VERIFIED:
            if self.text is None or not self.text.strip() or not self.citations or self.blocked:
                raise ValueError("a verified answer has its text and citations, and none blocked")
        else:
            if self.text is not None or self.citations:
                raise ValueError(
                    "an answer that was withheld or failed holds no text and no citations: "
                    "nothing the model wrote is kept (ADR-0003)"
                )
            if (self.outcome is TurnOutcome.BLOCKED) != bool(self.blocked):
                raise ValueError(
                    "a blocked answer names the citations that blocked it; no other does"
                )
        for clause_id in self.blocked:
            parse_stable_id(clause_id)


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One turn of a chat as the ledger holds it. A chat's turns alternate: the
    member's question at an even index, what they were shown in answer at the
    odd one after it."""

    tenant_id: str
    chat_id: str
    turn_index: int
    user_id: str
    """The member: `Member.uid`, as ADR-0036 keys a person's private
    additions. Never the email, which can be reassigned."""
    role: TurnRole
    content: str | None
    """The question, or a verified answer's text. None for a withheld or
    failed answer."""
    outcome: TurnOutcome | None
    """None for a question."""
    citations: tuple[Citation, ...]
    """As recorded; a corrupt stored one reads back as an `UnreadableCitation`
    in its place (#232). A turn about to be written holds none."""
    blocked: tuple[BlockedClause, ...]
    """As recorded; a corrupt stored element reads back as an
    `UnreadableCitation` in its place (#234)."""
    as_of: dt.date
    """The date the request read the law on."""
    trace_id: str
    created_at: dt.datetime | None = None
    """When the ledger recorded it; None until it has."""
    not_in_environment: tuple[StoredNotInEnvironment, ...] = ()
    """An answer's flags: the instruments its question named that the
    environment didn't hold (#252). As recorded, a corrupt stored one read
    back as an `UnreadableNotInEnvironment` in its place."""
    unavailable: Unavailability | None = None
    """For a `GENERATION_UNAVAILABLE` turn: the reason (#289)."""
    prefix: PrefixMeasure | None = None
    """For a `POLICY_CORPUS_TOO_LARGE` turn: the estimate and the cap (#289).
    None on such a turn too if its stored measure couldn't be read."""
    generator: GeneratorName | None = None
    """On an answer turn: the generator the deployment was running when the turn
    was written (ledger 0020, #292), so a chat read back later says whether a
    model or the demo stand-in wrote it. None on a question, and on a turn
    written before the column existed."""


@dataclass(frozen=True, slots=True)
class ChatExchange:
    """One question and what the member was shown for it: what a chat grows by,
    and always written together (`record_chat_exchange`)."""

    tenant_id: str
    chat_id: str
    user_id: str
    question: str
    answer: ChatAnswer
    as_of: dt.date
    trace_id: str
    generator: GeneratorName | None = None
    """Recorded with the answer turn (not the question). None only for a caller
    that doesn't know, which the API never is."""

    def turns(self, first_index: int) -> tuple[ChatTurn, ChatTurn]:
        """The two rows, the question at `first_index`."""
        answer = self.answer
        return (
            self._turn(first_index, TurnRole.USER, self.question, None, (), ()),
            self._turn(
                first_index + 1,
                TurnRole.ASSISTANT,
                answer.text,
                answer.outcome,
                answer.citations,
                answer.blocked,
                answer.not_in_environment,
                answer.unavailable,
                answer.prefix,
                self.generator,
            ),
        )

    def _turn(
        self,
        turn_index: int,
        role: TurnRole,
        content: str | None,
        outcome: TurnOutcome | None,
        citations: tuple[ChatCitation, ...],
        blocked: tuple[str, ...],
        not_in_environment: tuple[NotInEnvironment, ...] = (),
        unavailable: Unavailability | None = None,
        prefix: PrefixMeasure | None = None,
        generator: GeneratorName | None = None,
    ) -> ChatTurn:
        return ChatTurn(
            tenant_id=self.tenant_id,
            chat_id=self.chat_id,
            turn_index=turn_index,
            user_id=self.user_id,
            role=role,
            content=content,
            outcome=outcome,
            citations=citations,
            blocked=blocked,
            as_of=self.as_of,
            trace_id=self.trace_id,
            not_in_environment=not_in_environment,
            unavailable=unavailable,
            prefix=prefix,
            generator=generator,
        )


@dataclass(frozen=True, slots=True)
class ChatSummary:
    """A chat in a member's list."""

    chat_id: str
    started_at: dt.datetime
    updated_at: dt.datetime
    """When its latest turn was recorded."""
    turns: int
    first_question: str
    """What the member first asked: the chat's name in a list."""


def check_chat_exchange(exchange: ChatExchange) -> None:
    """What every `LedgerStore` refuses before writing an exchange, in this
    order. The database's constraints refuse all of it again (ledger 0014);
    what only a store can refuse (an unregistered tenant, a chat that is
    another member's) comes after, in both."""
    if not exchange.trace_id.strip():
        raise ValueError("a chat's turns carry the trace_id of their request")
    if not exchange.user_id.strip():
        raise ValueError("a chat belongs to a member")
    if not exchange.chat_id.strip():
        raise ValueError("a chat has an id")
    if not exchange.question.strip():
        raise ValueError("a question has text")
    if "\x00" in exchange.question or "\x00" in (exchange.answer.text or ""):
        raise ValueError("text may not contain NUL")


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

    @abstractmethod
    async def loaded_sources(self) -> LoadedSources:
        """The corpus and mapping versions the graph holds, and whether a load
        is open (#153): what an assessment records, and the reason it isn't
        recorded while a load runs."""


class GraphReader(ABC):
    """Builder-built reads of the persisted graph — all Track A needs."""

    @abstractmethod
    async def read(self, query: BuiltQuery) -> tuple[GraphRow, ...]:
        """Run one builder-built query."""

    @abstractmethod
    async def loaded_sources(self) -> LoadedSources:
        """The latest finished load of each kind, and whether one is open
        (#153). Not a traversal and returns no node of the law: it reads the
        record of the loads."""


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
    async def remove_by_erratum(self, edges: Sequence[EdgeRef], *, by: str, reason: str) -> int:
        """An acknowledged erratum (ADR-0050), in one transaction: each named
        edge, of a kind in `ERRATUM_EDGES` from a mapping item
        (`ERRATUM_SOURCES`) to a Clause, is deleted, and its source node's
        `corrections` trail gains one entry for the edges removed from it:
        when, by whom, why, `"erratum": true` and the edges. An edge already
        gone whose removal the trail records is left as it is (a re-run); one
        gone with no such record, or any other kind of edge, refuses the
        whole erratum. Nothing else on any node changes. Returns how many
        edges were deleted. All land, or none does."""

    @abstractmethod
    async def revise_clauses(self, authorization: RevisionAuthorization) -> int:
        """A parse correction of library law (ADR-0042), in one transaction:
        for each clause the authorised correction names, its Clause node's
        form — `canonical_sha256` and its incompleteness (ADR-0047) — goes
        from the recorded previous form to the new one, and the node's
        `corrections` trail gains when, by whom, why, the revision and the
        previous form. A node already in the new form is left as it is (a
        re-run); one in any other form, or missing, refuses the whole
        revision. Nothing else on the node changes. Returns how many nodes
        changed."""

    @abstractmethod
    async def begin_load(self, version: SourceVersion) -> str:
        """Record that a load of `version.kind` from `version` has started
        (#153); returns its id. Loads of a kind are serialised: this is refused
        while another of that kind is open (begun, and neither finished nor
        abandoned), atomically, so two can't both begin. Append-only, like the
        law the loads write: a record is never rewritten, and its end is set
        once. Not a node of the ontology and not bitemporal: it says what was
        loaded, not what was true. Times are the server's, not the caller's."""

    @abstractmethod
    async def finish_load(self, load_id: str) -> None:
        """Mark the load finished, once. An unknown load, or one already
        finished or abandoned, is refused. A load that never reaches this
        stays open, which is how a failed load is told from a successful one."""

    @abstractmethod
    async def abandon_load(self, load_id: str, by: str, reason: str, *, wrote: bool = True) -> None:
        """Close an open load that will never finish, naming who and why, once.
        Never done by the passing of time: someone looked at the failed load
        and decided. It releases the hold on the kind, so another load can
        begin, and it doesn't make the load a finished one, so the version the
        graph holds is unchanged.

        `wrote` says whether the load may have written. The default is yes,
        because nobody can tell a load that died from one that never started
        writing: such a load keeps assessments blocked
        (`LoadedSource.blocked_by_abandoned`) until a later load finishes.
        `wrote=False` is for a loader that knows it wrote nothing (its checks
        refused it under the hold); it blocks nothing. Abandoning a load that
        is only slow lets a second loader begin while the first is still
        writing, and the first's `finish_load` then fails after a full write:
        abandon one known to be dead. An unknown load, or one already finished
        or abandoned, is refused."""

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

    `policy_corpus` names the tenant clauses already sent as the CAG prefix:
    no method returns one of them, by any channel (ADR-0033).

    `user_id` is the signed-in member asking (`Member.uid`). A user-scoped
    chunk is one person's private addition: no method returns one except to
    the member who owns it, in that member's tenant, by any channel
    (ADR-0036). `None` — no member — admits no user-scoped chunk at all, so
    leaving it out can only narrow what comes back, never widen it.
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
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> RetrievalResult:
        """Dense + sparse channels fused with RRF inside the store (D-04).

        Each `per_channel` list, and `fused`, is ordered by score, highest
        first, and by `chunk_id`, lowest first, among equal scores, and is cut
        to `limit` only after that order is applied: a tie at the cut keeps
        the lowest `chunk_id`s (#160, #177). `fused` is RRF over the
        `per_channel` lists exactly as returned (`adapters.fusion`). So which
        tied candidates a channel hands to fusion, at which ranks, and the
        fused scores that follow come from the channel scores alone, never
        from the order a store found them in (ADR-0046)."""

    @abstractmethod
    async def lookup_clause(
        self,
        clause_id: str,
        *,
        law: LawScope,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> Candidate | None:
        """Exact clause-ID lookup. The router sends ID queries here (D-12).
        `scopes` narrows it exactly as it narrows `search`: a lookup is a
        channel too (ADR-0036)."""

    @abstractmethod
    async def lookup_lineage(
        self,
        clause_ids: tuple[str, ...],
        *,
        law: LawScope,
        tenant_id: str,
        scopes: tuple[Scope, ...],
        as_of: dt.date,
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> tuple[Candidate, ...]:
        """Every clause that contains one of `clause_ids` or is inside one,
        by path segment as `clause_ancestors` defines it (`s1/10` is neither
        inside `s1/1` nor containing it), through every filter `lookup_clause`
        applies; in `clause_id` order, unranked, on the keyword channel. A
        given id comes back only as the lineage of another. What delivers a
        ranked clause whole: its chapeaux and its paragraphs (ADR-0045)."""


class RetrievalAdmin(ABC):
    """The write side of a retrieval store (ADR-0021): never replace in
    place, never delete on update. A new version is upserted next to the old
    one and the old one is retired with `valid_to`; withdrawal and deletion
    need a `RemovalAuthorization`, which only `CorpusLifecycle` issues."""

    @abstractmethod
    async def upsert_chunks(self, chunks: tuple[Chunk, ...]) -> None: ...

    @abstractmethod
    async def holds_real_customer_data(self) -> bool:
        """Whether any stored chunk is not public law and not marked synthetic
        (a tenant's documents and a person's private additions alike, #166) —
        what closes offshore generation (ADR-0023). A chunk stored before the
        `synthetic` flag existed counts as real."""

    @abstractmethod
    async def version_chunk_ids(
        self, *, instrument_id: str, tenant_id: str, version_id: str
    ) -> frozenset[str]:
        """The `chunk_id` of every stored chunk of this version, visible or
        not: what tells a finished load from a partial one, or from a
        different clause set (#144)."""

    async def version_exists(self, *, instrument_id: str, tenant_id: str, version_id: str) -> bool:
        """Whether any chunk of this version is stored, visible or not —
        publishing over an existing version would be a replace in place."""
        stored = await self.version_chunk_ids(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        )
        return bool(stored)

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

    @abstractmethod
    async def stored_chunk_texts(
        self, *, instrument_id: str, tenant_id: str, version_id: str
    ) -> dict[str, str]:
        """The text every stored chunk of this version holds, by `chunk_id`,
        visible or not: what a revision compares with the parse (ADR-0042)."""

    @abstractmethod
    async def stored_chunk_incomplete(
        self, *, instrument_id: str, tenant_id: str, version_id: str
    ) -> dict[str, Incomplete]:
        """The incompleteness every stored chunk of this version holds, by
        `chunk_id`, visible or not (ADR-0047): `()` for a complete one,
        including a chunk stored before the state existed."""

    @abstractmethod
    async def revise_chunks(
        self, authorization: RevisionAuthorization, chunks: tuple[Chunk, ...]
    ) -> None:
        """A parse correction of library law (ADR-0042): each chunk, one the
        authorised revision names with exactly the form it records, is written
        over the stored chunk of the same id. A chunk whose text changes is
        re-embedded: its text and vectors change. One whose text is the stored
        text takes only its incompleteness, with no embedding (ADR-0047).
        Nothing else changes (valid time, withdrawal, scope, owner and the rest
        are the stored point's). A chunk not already stored is refused: a
        correction never adds one. Refused before anything is written."""


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
    async def record_coverage_assessment(
        self, record: AssessmentRecord, rows: tuple[CoverageRow, ...]
    ) -> None:
        """An assessment and its coverage, in one transaction (#153): the
        assessment against the corpus and mapping versions it was computed
        from, and one row per obligation (D-06) — outcome, reason, citations
        and, for an unassessable one, its questions. All of it lands or none
        of it does, so an assessment is never left without its coverage.
        Every row carries the request's `trace_id`. A repeated assessment id,
        an unregistered tenant, a blank user, or a decided obligation citing
        nothing is a `ValueError`."""

    @abstractmethod
    async def coverage_rows(self, tenant_id: str, assessment_id: str) -> tuple[CoverageRow, ...]:
        """An assessment's coverage, by obligation id."""

    @abstractmethod
    async def get_assessment(self, tenant_id: str, assessment_id: str) -> AssessmentRecord | None:
        """The recorded assessment, or None if this tenant has none by that id.
        One recorded before source versions were (#153) is a `ValueError`,
        never a guess."""

    @abstractmethod
    async def record_check(self, record: CheckRecord) -> None:
        """Record a check (#297): the document it submitted, by the
        `submitted_document` version the ledger already holds. Append-only.
        A `ValueError` and nothing written for: a blank trace, submitter, file
        name or id, or an instrument that is not the tenant's
        (`check_check_write`); a tenant the ledger does not know; a version the
        ledger has not recorded as a submitted document (the document is stored
        first, so a check never points at nothing); a check id, or a version,
        already checked."""

    @abstractmethod
    async def get_check(self, tenant_id: str, check_id: str) -> CheckRecord | None:
        """The check, or None if this tenant has none by that id. Not owner-keyed:
        who may see it is `CheckRecord.visible_to`, which every route applies,
        so a read here is never itself a disclosure."""

    @abstractmethod
    async def list_checks(
        self,
        tenant_id: str,
        uid: str,
        *,
        admin: bool,
        limit: int,
        before: CheckCursor | None = None,
    ) -> tuple[CheckListing, ...]:
        """The checks `uid` may see (#355), newest first, ties by id descending
        (bytewise): the rule is `CheckRecord.visible_to`, so an admin has the
        tenant's and anyone else their own. `before` continues after the last row
        of a page: only rows strictly older in that order. At most `limit`
        (1 to `CHECK_PAGE_MAX`; `ValueError` otherwise). **One read, whatever the
        number of rows**: each row's stage, run state and title come with it.
        Metadata only, never a document's or a claim's words."""

    @abstractmethod
    async def check_of_version(
        self, tenant_id: str, instrument_id: str, version_id: str
    ) -> CheckRecord | None:
        """The check that submitted this `submitted_document` version, or None:
        how the source route finds who owns a submitted document."""

    @abstractmethod
    async def reserve_extraction(
        self, tenant_id: str, check_id: str, *, as_of: dt.date, trace_id: str
    ) -> None:
        """Reserve a check's one extraction, atomically, before the model is
        called (#298): of any number of concurrent requests exactly one succeeds,
        and the rest raise `ExtractionAlreadyReserved` and must not call the
        model. The reservation is pending until `record_claims` completes it or
        `fail_extraction` releases it. A check whose last extraction failed, or
        whose reservation has been pending longer than `RESERVATION_TTL`, may be
        reserved again; one that is extracted may not. A `ValueError` for a
        blank trace or check id, and for a check this tenant does not hold."""

    @abstractmethod
    async def fail_extraction(self, tenant_id: str, check_id: str) -> None:
        """Release a pending reservation as failed, so the check can be extracted
        again. Nothing happens to an extraction that is not pending, so it is safe
        to call from every failure path."""

    @abstractmethod
    async def record_claims(self, record: ClaimExtractionRecord) -> None:
        """Complete a reserved extraction with its claims (#298), the extraction
        and every claim in one transaction: all of it lands or none of it does.
        Append-only: a check's claims are never replaced. A `ValueError` and
        nothing written for: a blank trace or check id, more than `MAX_CLAIMS`
        claims, claims not numbered from 1 in order, one that is not `inferred`
        or whose span, passage, hash or summary is not well formed
        (`check_claims_write`); a check this tenant does not hold or has not
        reserved (pending); a check already extracted."""

    @abstractmethod
    async def get_claims(self, tenant_id: str, check_id: str) -> ClaimExtractionRecord | None:
        """The check's completed extraction, or None if it has none (not run,
        pending or failed, or the tenant holds no such check). Not owner-keyed,
        like `get_check`: who may see a check is `CheckRecord.visible_to`,
        applied by every route first."""

    @abstractmethod
    async def reserve_findings(self, tenant_id: str, check_id: str, *, trace_id: str) -> None:
        """Reserve a check's one findings run, atomically, before any model is
        called (#299), as `reserve_extraction` does for the extraction: of any
        number of concurrent requests exactly one succeeds, and the rest raise
        `FindingsAlreadyReserved` and must not call the model. `CheckNotExtracted`
        if the check has no completed extraction (nothing to check); a failed run,
        or one pending longer than `FINDINGS_RESERVATION_TTL`, may be reserved
        again; one that is done may not. A `ValueError` for a blank trace or check
        id, and for a check this tenant does not hold."""

    @abstractmethod
    async def fail_findings(self, tenant_id: str, check_id: str, *, trace_id: str) -> None:
        """Release a pending findings reservation as failed, so the run can be
        made again, if it is still the one `trace_id` reserved. Nothing happens to
        one that is not pending, or that another request now holds (a run that
        outlived its reservation must not release its successor's)."""

    @abstractmethod
    async def record_findings(self, record: FindingsRecord) -> None:
        """Complete a reserved findings run (#299): the assessment, its coverage
        rows, every finding and every usage row, in one transaction, with the
        reservation marked done. All of it lands or none of it does. Append-only:
        nothing of it is ever updated, so an assessment keeps what it said
        (ADR-0034). A `ValueError` and nothing written for anything
        `check_findings_write` refuses; a claim the check's extraction does not
        hold; an unregistered tenant; a check not reserved (pending) or already
        done; an assessment id already recorded; and a reservation that is no
        longer this run's: it commits only if `record.assessment.trace_id` is the
        request that holds the reservation, so a run that outlived its own (taken
        over after `FINDINGS_RESERVATION_TTL`, or released) records nothing."""

    @abstractmethod
    async def get_findings(self, tenant_id: str, check_id: str) -> FindingsRecord | None:
        """The check's completed findings run as it was recorded, or None if it
        has none (not run, pending or failed). Findings in claim order, then
        obligations before policy clauses, each by id, bytewise. Not owner-keyed,
        like `get_claims`: who may see a check is `CheckRecord.visible_to`,
        applied by every route first. The usage rows are the run's own."""

    @abstractmethod
    async def record_chat_exchange(self, exchange: ChatExchange) -> None:
        """Append a question and what the member was shown for it to a chat
        (#162), in one transaction: both rows land or neither does, as the
        next two turns of the chat, or its first two if no chat has this id.
        Append-only, like evidence: there is no update and no delete. Every
        row carries the request's `trace_id` and the member's `user_id`
        (`Member.uid`, ADR-0036). Concurrent appends to one chat are numbered
        in the order they commit, never refused for it.

        A `ValueError` and nothing written for: a blank trace, member, chat
        or question, NUL in a text (`check_chat_exchange`); a tenant the
        ledger doesn't know; a chat that is another member's."""

    @abstractmethod
    async def chat_turns(self, tenant_id: str, user_id: str, chat_id: str) -> tuple[ChatTurn, ...]:
        """The member's chat, turn by turn. Empty for a chat that doesn't
        exist, belongs to another member or to another tenant: the three are
        not told apart, so the answer confirms nothing. A chat always has
        turns once it exists."""

    @abstractmethod
    async def chats(self, tenant_id: str, user_id: str) -> tuple[ChatSummary, ...]:
        """The member's chats, the one with the latest turn first (ties by
        chat id)."""

    @abstractmethod
    async def subscribe(
        self, *, tenant_id: str, instrument_id: str, subscribed_by: str, trace_id: str | None = None
    ) -> None:
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
    async def count_findings_citing_clauses(self, clause_ids: frozenset[str]) -> int:
        """Findings of *any* tenant with at least one citation of one of
        these clauses, by exact `clause_id`: library law is cited by every
        tenant, so a revision of it counts them all (ADR-0042)."""

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
        title: str,
    ) -> None:
        """A version comes into force (ledger `instrument_version`). The
        record the verifier checks a cited version against, independently of
        what retrieval returned (ADR-0021 mechanism 3). `document_class` is
        the instrument's (#148): the same for every version, and library law
        if and only if the tenant is `_public` (`check_document_class`).

        `title` is the document's title as its publisher gives it, recorded
        on the version and as the instrument's latest (ADR-0049): what a
        policy citation names the document by. Never the instrument id by
        default; an empty or multi-line one is refused (`validate_title`)."""

    @abstractmethod
    async def version_titles(
        self, tenant_id: str, versions: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], str]:
        """The recorded title of each `(instrument_id, version_id)` this
        tenant's ledger holds, in one read, however many are asked for: what
        a request reads once for every policy citation it shows (ADR-0049),
        never once per citation. A version not recorded for this tenant
        (another tenant's, or none at all) is left out, not an error."""

    @abstractmethod
    async def environment_titles(
        self, tenant_id: str, *, as_of: dt.date
    ) -> dict[str, frozenset[str]]:
        """The tenant's environment on `as_of` (D-17), as the ledger records
        it, in one read: each instrument its retrieval can see, to the
        recorded titles of its versions in force then (ADR-0049). Its own
        documents of every class but a submitted document (the subject of a
        check, never part of the environment: ADR-0053 U1), never withdrawn
        or purged, and the library
        instruments it subscribes to; `_public`'s is the whole library. What
        `answer()` decides a question's named instruments are held against
        (#252, ADR-0051)."""

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
    async def get_clause(
        self, clause_id: str, *, tenant_id: str, user_id: str | None = None
    ) -> ClauseRecord | None:
        """One clause by its stable ID, or `None` if it doesn't exist. A
        user-scoped clause is one person's private addition: only the member
        who owns it (`user_id`) is given it; anyone else gets `None`, as if it
        did not exist (ADR-0036)."""

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
    async def get_incomplete(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> dict[str, Incomplete]:
        """Every clause path in this instrument version that is incomplete,
        mapped to why (ADR-0047): kept on the manifest beside the hashes, so
        an ingest's drift check and a revision's plan compare a clause's whole
        form without reading every clause object. A path not listed is
        complete."""

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

    @abstractmethod
    async def record_revision(
        self, authorization: RevisionAuthorization, clauses: Sequence[ClauseRecord]
    ) -> None:
        """Apply a revision to the version it names (ADR-0042), the record
        first: the manifest takes the new form (hash and incompleteness,
        ADR-0047) of every path the record writes, and the record itself, in
        one write (an intent, as `put_clauses` writes the manifest first).
        Then each of `clauses`, a clause the record names with the form it
        records, is written over its object unless the object already holds
        it. A path whose manifest form is neither the record's previous nor
        its new one, or a removed path still listed, refuses it before
        anything is written.
        Idempotent: a record already on the manifest (by `revision_id`) isn't
        appended again, and a hash already new isn't rewritten — so a run that
        stopped part-way is finished by calling it again."""

    @abstractmethod
    async def revisions(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> tuple[RevisionRecord, ...]:
        """Every revision recorded on this version's manifest, oldest first."""


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
        """Score documents against the query, returning the top `top_k`.
        `documents` is never empty: the service refuses an empty list (a
        400), so a caller with nothing to score doesn't call (#331)."""


class Embedder(ABC):
    """Dense vectors. Facts are never embedded (D-14)."""

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class Unavailability(StrEnum):
    """Why a model can't take a request, in words that are ours, not its
    provider's (#251)."""

    QUOTA = "quota"
    """The provider refused it for quota: Vertex's 429 `RESOURCE_EXHAUSTED`,
    which is every request while a model's quota is zero."""
    ACCESS = "access"
    """The provider refused the caller, not the load: Vertex's 403
    `PERMISSION_DENIED` (the model not enabled for the project, the account
    not allowed to call it) or credentials it won't accept (#322, #326).
    Nothing the member can fix; the operator can."""


class GenerationUnavailable(RuntimeError):
    """The model can't take the request, for a reason in `Unavailability`.
    A `Generator` raises it in place of its SDK's error, so nothing the
    provider said reaches anything above the adapter: the SDK's exception is
    its `__cause__`, for the server's own log, and never in its message."""

    def __init__(self, reason: Unavailability) -> None:
        super().__init__(f"generation unavailable: {reason.value}")
        self.reason = reason


class ReplyKind(StrEnum):
    """The shape of JSON a call asks the model to reply in, so an adapter that
    can constrain the reply (Gemini's response schema) constrains it to the right
    one. A request, never a guarantee: the caller validates the reply strictly
    whatever the adapter did (CLAUDE.md: structured outputs are validated against
    a schema before use)."""

    ANSWER = "answer"
    """An answer with its citations, or one repaired citation
    (`assessment/draft.py`)."""
    CLAIMS = "claims"
    """The claims of a submitted document (`assessment/claims.py`, #298)."""
    FINDINGS = "findings"
    """The findings for one claim (`assessment/findings.py`, #299)."""


class Generator(ABC):
    """Gemini 3.5 Flash on Vertex (ADR-0057), or Claude on Vertex while it is
    dormant. Never authors a citation (D-02)."""

    @abstractmethod
    async def generate(
        self,
        *,
        system: str,
        cached_prefix: str | None,
        user: str,
        max_tokens: int,
        reply: ReplyKind = ReplyKind.ANSWER,
    ) -> Generation:
        """Generate with a cached prompt prefix for the tenant policy corpus
        (D-11). `reply` names the JSON shape the call wants (`ReplyKind`); the
        default is the answer's, so every call that does not say is unchanged.
        Raises `GenerationUnavailable` when the model refuses the request for a
        reason `Unavailability` names."""


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
    expires_at: int | None = field(default=None, compare=False)
    """When the token that signed them in expires (Unix seconds), for a verifier
    that has one. A session made from the token ends no later (#280). Not part
    of who they are, so not compared."""
    auth_time: int | None = field(default=None, compare=False)
    """When they last signed in (Unix seconds), from the token. The sign-in is
    what an account's `valid_since` revokes (#319), so a session cookie carries
    it and is checked against the account again. Not compared."""


class Unauthenticated(Exception):
    """No valid sign-in: 401. A bad, expired or revoked token or session, or an
    account that no longer exists."""


ACCOUNT_DISABLED = "this account is disabled"
"""What a refusal for a disabled account says: the API's pages tell it apart
from a person who is not a member (#319)."""


class NotAMember(Exception):
    """A valid sign-in for someone not admitted to the tenant: 403. Not on the
    member list, or the account is disabled."""


class VerifierUnavailable(Exception):
    """Something sign-in depends on can't be had right now (the signing keys,
    the account lookup), so nobody can be checked: 503. Always a refusal, never
    an admission (#319)."""


@dataclass(frozen=True, slots=True)
class AccountStatus:
    """What the identity provider says about one account (#319). Nothing else
    about it is read, kept or logged."""

    disabled: bool
    valid_since: int
    """Unix seconds: a token whose sign-in (`auth_time`) is earlier than this
    was revoked. 0 when the account's tokens were never revoked."""


class AccountDirectory(ABC):
    """The identity provider's own record of an account (#319), asked as the
    runtime identity and never with a user's token. A token's signature says who
    Google signed it for an hour ago; this says whether they may still sign in."""

    @abstractmethod
    async def lookup(self, uid: str) -> AccountStatus | None:
        """The account's status, or `None` when there is no such account (it was
        deleted). `VerifierUnavailable` when it can't be asked or the answer
        isn't understood: a caller treats that as a refusal."""

    @abstractmethod
    async def aclose(self) -> None: ...


class TokenVerifier(ABC):
    """Who signed in, from an identity provider's token (#146). The API's one
    way in; it never decides the tenant."""

    @abstractmethod
    async def verify(self, token: str) -> Member:
        """The member the token signs in, or `Unauthenticated` /
        `NotAMember` / `VerifierUnavailable`. Never a reason that echoes the
        token."""

    @abstractmethod
    async def recheck(self, member: Member, *, session: str) -> None:
        """Whether a member who signed in earlier (a session cookie, `session`)
        still may: `Unauthenticated` / `NotAMember` / `VerifierUnavailable`
        otherwise. May answer from a check made within the last few minutes of
        the same session, never longer (#319)."""

    @abstractmethod
    async def aclose(self) -> None: ...
