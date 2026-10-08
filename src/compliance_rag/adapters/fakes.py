"""In-memory implementations of every interface, for tests (D-08).

These are the only implementations in the repo at Phase 0. No adapter talks to a
real service yet. The fakes are deliberately strict where the interface makes a
promise: `FakeGraphStore` rejects a traversal without a depth cap, and
`FakeRetrievalStore` refuses to return a chunk outside the Track A filter.
"""

from __future__ import annotations

import datetime as dt
import zlib

from compliance_rag.adapters.interfaces import (
    Candidate,
    Channel,
    ClauseStore,
    Embedder,
    Generation,
    Generator,
    GraphStore,
    LedgerStore,
    ManifestMetadata,
    Obligation,
    Profile,
    RemovalAuditEntry,
    Reranker,
    RetrievalAdmin,
    RetrievalResult,
    RetrievalStore,
    ScoredDocument,
)
from compliance_rag.adapters.removal_authorization import RemovalAuthorization
from compliance_rag.corpus.models import (
    Chunk,
    ClauseRecord,
    Scope,
    canonical_sha256,
    canonicalise,
    parse_stable_id,
    sha256_hex,
    validate_library_instrument_id,
    validate_tenant_id,
)
from compliance_rag.verifier.citations import CanonicalCitationVerifier


class FakeGraphStore(GraphStore):
    """Traversal over dictionaries, with the same invariants as the real thing."""

    def __init__(
        self,
        obligations: dict[str, Obligation] | None = None,
        applicability: dict[str, tuple[str, ...]] | None = None,
        crosswalk_edges: dict[str, tuple[str, ...]] | None = None,
        valid_from: dict[str, dt.date] | None = None,
    ) -> None:
        self._obligations = obligations or {}
        # jurisdiction/sector/role token -> obligation ids it makes applicable
        self._applicability = applicability or {}
        self._crosswalks = crosswalk_edges or {}
        self._valid_from = valid_from or {}

    def _in_force(self, obligation_id: str, as_of: dt.date) -> bool:
        start = self._valid_from.get(obligation_id)
        return start is None or as_of >= start

    async def applicable_obligations(
        self,
        profile: Profile,
        *,
        as_of: dt.date,
        max_depth: int,
    ) -> tuple[Obligation, ...]:
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1 (D-05: every traversal is capped)")
        tokens = (
            *profile.jurisdictions,
            *profile.sectors,
            *profile.actor_roles,
            *profile.data_categories,
        )
        found: dict[str, Obligation] = {}
        for token in tokens:
            for oid in self._applicability.get(token, ()):
                if oid in self._obligations and self._in_force(oid, as_of):
                    found[oid] = self._obligations[oid]
        return tuple(found[k] for k in sorted(found))

    async def obligation(self, obligation_id: str, *, as_of: dt.date) -> Obligation | None:
        found = self._obligations.get(obligation_id)
        if found is None or not self._in_force(obligation_id, as_of):
            return None
        return found

    async def crosswalks(
        self,
        obligation_id: str,
        *,
        as_of: dt.date,
        max_depth: int,
    ) -> tuple[Obligation, ...]:
        if max_depth < 1:
            raise ValueError("max_depth must be >= 1 (D-05: every traversal is capped)")
        seen: dict[str, Obligation] = {}
        frontier = [obligation_id]
        for _ in range(max_depth):
            nxt: list[str] = []
            for current in frontier:
                for target in self._crosswalks.get(current, ()):
                    if target in seen or target == obligation_id:
                        continue
                    node = self._obligations.get(target)
                    if node is not None and self._in_force(target, as_of):
                        seen[target] = node
                        nxt.append(target)
            frontier = nxt
            if not frontier:
                break
        return tuple(seen[k] for k in sorted(seen))


def _version_key(chunk: Chunk) -> tuple[str, str, str]:
    instrument_id, version_id, _ = parse_stable_id(chunk.clause_id)
    return (chunk.tenant_id, instrument_id, version_id)


def _authorized(chunk: Chunk, authorization: RemovalAuthorization) -> bool:
    return chunk.tenant_id == authorization.tenant_id and chunk.clause_id.startswith(
        authorization.stable_id_prefix
    )


class FakeRetrievalStore(RetrievalStore, RetrievalAdmin):
    """Token-overlap 'dense' and 'sparse' channels with a deterministic fusion.

    Reads subscriptions from `ledger` on every call, exactly as
    `QdrantRetrievalStore` does — a library chunk is visible only to a tenant
    subscribed to its instrument, or to `_public`, which owns it (ADR-0020).
    A withdrawn version is invisible at every `as_of` (ADR-0021).
    """

    def __init__(self, chunks: tuple[Chunk, ...] = (), *, ledger: LedgerStore) -> None:
        self._chunks = {c.chunk_id: c for c in chunks}
        # Per stored chunk, like Qdrant's per-point payload flag: a chunk
        # upserted after the withdrawal is not withdrawn.
        self._withdrawn: set[str] = set()
        self._ledger = ledger

    async def upsert_chunks(self, chunks: tuple[Chunk, ...]) -> None:
        for chunk in chunks:
            self._chunks[chunk.chunk_id] = chunk
            self._withdrawn.discard(chunk.chunk_id)

    async def version_exists(self, *, instrument_id: str, tenant_id: str, version_id: str) -> bool:
        return any(
            _version_key(c) == (tenant_id, instrument_id, version_id) for c in self._chunks.values()
        )

    async def retire_version(
        self, *, instrument_id: str, tenant_id: str, version_id: str, valid_to: dt.date
    ) -> None:
        for chunk_id, chunk in list(self._chunks.items()):
            if _version_key(chunk) == (tenant_id, instrument_id, version_id):
                self._chunks[chunk_id] = chunk.model_copy(update={"valid_to": valid_to})

    async def withdraw_version(self, authorization: RemovalAuthorization) -> None:
        self._withdrawn.update(
            c.chunk_id for c in self._chunks.values() if _authorized(c, authorization)
        )

    async def delete_chunks(self, authorization: RemovalAuthorization) -> None:
        for chunk_id, chunk in list(self._chunks.items()):
            if _authorized(chunk, authorization):
                del self._chunks[chunk_id]

    @property
    def stored_chunk_ids(self) -> frozenset[str]:
        """Everything stored, visible or not — what tests check to prove a
        withdrawal kept the text and a purge removed it."""
        return frozenset(self._chunks)

    @staticmethod
    def _overlap(query: str, text: str) -> float:
        q = set(canonicalise(query).lower().split())
        t = set(canonicalise(text).lower().split())
        if not q or not t:
            return 0.0
        return len(q & t) / len(q)

    def _visible(
        self, chunk: Chunk, tenant_id: str, scopes: tuple[Scope, ...], subscribed: frozenset[str]
    ) -> bool:
        if chunk.scope not in scopes or chunk.chunk_id in self._withdrawn:
            return False
        if chunk.tenant_id == tenant_id:
            return True
        if chunk.scope is Scope.PUBLIC:
            instrument_id, _, _ = parse_stable_id(chunk.clause_id)
            return instrument_id in subscribed
        return False

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
        allowed = set(obligation_ids)
        subscribed = await self._ledger.subscribed_instruments(tenant_id)
        pool = [
            c
            for c in self._chunks.values()
            if self._visible(c, tenant_id, scopes, subscribed)
            # D-03: Track B only ever returns chunks under Track A's obligations.
            and (not allowed or (c.obligation_id is not None and c.obligation_id in allowed))
            and as_of >= c.valid_from
            and as_of < c.valid_to
        ]

        def rank(channel: Channel) -> tuple[Candidate, ...]:
            scored = [
                Candidate(
                    chunk_id=c.chunk_id,
                    clause_id=c.clause_id,
                    text=c.text,
                    score=self._overlap(query, c.text),
                    channel=channel,
                    obligation_id=c.obligation_id,
                )
                for c in pool
            ]
            scored = [s for s in scored if s.score > 0]
            scored.sort(key=lambda s: (-s.score, s.chunk_id))
            return tuple(scored[:limit])

        per_channel = {Channel.DENSE: rank(Channel.DENSE), Channel.SPARSE: rank(Channel.SPARSE)}

        # Reciprocal rank fusion, the same shape Qdrant applies natively (D-04).
        k = 60
        rrf: dict[str, float] = {}
        holder: dict[str, Candidate] = {}
        for candidates in per_channel.values():
            for position, candidate in enumerate(candidates, start=1):
                rrf[candidate.chunk_id] = rrf.get(candidate.chunk_id, 0.0) + 1.0 / (k + position)
                holder.setdefault(candidate.chunk_id, candidate)
        fused = tuple(
            sorted(
                (
                    Candidate(
                        chunk_id=cid,
                        clause_id=holder[cid].clause_id,
                        text=holder[cid].text,
                        score=score,
                        channel=None,
                        obligation_id=holder[cid].obligation_id,
                    )
                    for cid, score in rrf.items()
                ),
                key=lambda c: (-c.score, c.chunk_id),
            )[:limit]
        )
        return RetrievalResult(fused=fused, per_channel=per_channel)

    async def lookup_clause(
        self,
        clause_id: str,
        *,
        tenant_id: str,
        as_of: dt.date,
    ) -> Candidate | None:
        subscribed = await self._ledger.subscribed_instruments(tenant_id)
        every_scope = (Scope.PUBLIC, Scope.TENANT, Scope.USER)
        for c in self._chunks.values():
            if c.clause_id != clause_id:
                continue
            if not self._visible(c, tenant_id, every_scope, subscribed):
                continue
            if not (c.valid_from <= as_of < c.valid_to):
                continue
            return Candidate(
                chunk_id=c.chunk_id,
                clause_id=c.clause_id,
                text=c.text,
                score=1.0,
                channel=Channel.KEYWORD,
                obligation_id=c.obligation_id,
            )
        return None


class FakeLedgerStore(LedgerStore):
    """Dictionaries keyed by tenant. Every row carries `tenant_id`."""

    def __init__(self) -> None:
        self._profiles: dict[str, Profile] = {}
        self._assessments: dict[tuple[str, str], dict[str, object]] = {}
        self._subscriptions: dict[tuple[str, str], str] = {}  # (tenant, instrument) -> by
        self._findings: list[tuple[str, tuple[str, ...]]] = []  # (tenant, cited clause_ids)
        self.withdrawn: dict[tuple[str, str, str], str] = {}  # (tenant, instrument, version) -> by
        self._versions: dict[tuple[str, str, str], tuple[dt.date, dt.date | None]] = {}
        self.purged: dict[tuple[str, str, str | None], tuple[str, str]] = {}  # -> (by, reason)
        self._audit: list[RemovalAuditEntry] = []

    async def get_profile(self, tenant_id: str) -> Profile | None:
        return self._profiles.get(tenant_id)

    async def put_profile(self, profile: Profile) -> None:
        self._profiles[profile.tenant_id] = profile

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
        self._assessments[(tenant_id, assessment_id)] = {
            "tenant_id": tenant_id,
            "assessment_id": assessment_id,
            "corpus_version": corpus_version,
            "mapping_version": mapping_version,
            "as_of": as_of,
            "trace_id": trace_id,
        }

    async def get_assessment(self, tenant_id: str, assessment_id: str) -> dict[str, object] | None:
        return self._assessments.get((tenant_id, assessment_id))

    async def subscribe(self, *, tenant_id: str, instrument_id: str, subscribed_by: str) -> None:
        validate_tenant_id(tenant_id)
        validate_library_instrument_id(instrument_id)
        self._subscriptions.setdefault((tenant_id, instrument_id), subscribed_by)

    async def unsubscribe(self, *, tenant_id: str, instrument_id: str) -> None:
        self._subscriptions.pop((tenant_id, instrument_id), None)

    async def subscribed_instruments(self, tenant_id: str) -> frozenset[str]:
        return frozenset(i for (t, i) in self._subscriptions if t == tenant_id)

    def seed_finding(self, tenant_id: str, *cited_clause_ids: str) -> None:
        """Test seeding only: a finding of `tenant_id` citing these clauses.
        Recording findings isn't on `LedgerStore` yet — `answer()` (#31) is
        the first thing that writes one."""
        self._findings.append((tenant_id, cited_clause_ids))

    async def count_findings_citing(self, tenant_id: str, stable_id_prefix: str) -> int:
        return sum(
            1
            for tenant, cited in self._findings
            if tenant == tenant_id and any(c.startswith(stable_id_prefix) for c in cited)
        )

    async def record_version(
        self,
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str,
        valid_from: dt.date,
        supersedes: str | None,
    ) -> None:
        key = (tenant_id, instrument_id, version_id)
        if key in self._versions:
            # Re-recording would reset valid_to and silently un-retire it.
            raise ValueError(f"{instrument_id}:{version_id} is already recorded for {tenant_id}")
        self._versions[key] = (valid_from, None)

    async def end_version(
        self, *, tenant_id: str, instrument_id: str, version_id: str, valid_to: dt.date
    ) -> None:
        key = (tenant_id, instrument_id, version_id)
        if key not in self._versions:
            raise KeyError(f"{instrument_id}:{version_id} was never recorded for {tenant_id}")
        self._versions[key] = (self._versions[key][0], valid_to)

    async def version_validity(
        self, *, tenant_id: str, instrument_id: str, version_id: str
    ) -> tuple[dt.date, dt.date | None] | None:
        return self._versions.get((tenant_id, instrument_id, version_id))

    async def mark_withdrawn(
        self, *, tenant_id: str, instrument_id: str, version_id: str, withdrawn_by: str
    ) -> None:
        self.withdrawn[(tenant_id, instrument_id, version_id)] = withdrawn_by

    async def is_withdrawn(self, *, tenant_id: str, instrument_id: str, version_id: str) -> bool:
        return (tenant_id, instrument_id, version_id) in self.withdrawn

    async def mark_purged(
        self,
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str | None,
        purged_by: str,
        reason: str,
    ) -> None:
        self.purged[(tenant_id, instrument_id, version_id)] = (purged_by, reason)

    async def record_removal_audit(self, entry: RemovalAuditEntry) -> None:
        self._audit.append(entry)

    async def removal_audit(self, tenant_id: str) -> tuple[RemovalAuditEntry, ...]:
        return tuple(e for e in self._audit if e.tenant_id == tenant_id)


class FakeClauseStore(ClauseStore):
    """Clauses in a dict keyed by (tenant_id, clause_id); `source.html` and
    manifest metadata in dicts keyed by (tenant_id, instrument_id, version_id)."""

    def __init__(self) -> None:
        self._clauses: dict[tuple[str, str], ClauseRecord] = {}
        self._source_html: dict[tuple[str, str, str], str] = {}
        self._manifest_metadata: dict[tuple[str, str, str], ManifestMetadata] = {}

    async def get_clause(self, clause_id: str, *, tenant_id: str) -> ClauseRecord | None:
        return self._clauses.get((tenant_id, clause_id))

    async def put_clause(self, clause: ClauseRecord) -> None:
        self._clauses[(clause.tenant_id, clause.clause_id)] = clause

    async def get_manifest(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> dict[str, str]:
        return {
            clause.clause_path: canonical_sha256(clause.canonical_text)
            for (stored_tenant_id, _), clause in self._clauses.items()
            if stored_tenant_id == tenant_id
            and clause.instrument_id == instrument_id
            and clause.version_id == version_id
        }

    async def put_manifest(
        self,
        instrument_id: str,
        version_id: str,
        *,
        tenant_id: str,
        canonicalisation_version: int,
        source_html: str,
    ) -> None:
        key = (tenant_id, instrument_id, version_id)
        self._source_html[key] = source_html
        self._manifest_metadata[key] = ManifestMetadata(
            canonicalisation_version=canonicalisation_version,
            source_html_sha256=sha256_hex(source_html),
        )

    async def get_manifest_metadata(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> ManifestMetadata | None:
        return self._manifest_metadata.get((tenant_id, instrument_id, version_id))

    async def get_source_html(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> str | None:
        return self._source_html.get((tenant_id, instrument_id, version_id))

    async def purge(self, authorization: RemovalAuthorization) -> None:
        a = authorization
        doomed_clauses = [
            key
            for key in self._clauses
            if key[0] == a.tenant_id and key[1].startswith(a.stable_id_prefix)
        ]
        doomed_versions = [
            key
            for key in self._source_html.keys() | self._manifest_metadata.keys()
            if key[:2] == (a.tenant_id, a.instrument_id)
            and (a.version_id is None or key[2] == a.version_id)
        ]
        for key in doomed_clauses:
            del self._clauses[key]
        for key in doomed_versions:
            self._source_html.pop(key, None)
            self._manifest_metadata.pop(key, None)


class FakeReranker(Reranker):
    """Deterministic token-overlap reranker. Stands in for the cross-encoder."""

    def __init__(self) -> None:
        self.rerank_calls = 0
        """Tests use this to prove the router actually skips reranking
        (`CONTEXT.md`: Router, issue #5) rather than just returning the
        right candidate by coincidence."""

    async def rerank(
        self,
        query: str,
        documents: tuple[str, ...],
        *,
        top_k: int,
    ) -> tuple[ScoredDocument, ...]:
        self.rerank_calls += 1
        q = set(canonicalise(query).lower().split())
        scored = [
            ScoredDocument(
                index=i,
                score=(len(q & set(canonicalise(d).lower().split())) / len(q)) if q else 0.0,
            )
            for i, d in enumerate(documents)
        ]
        scored.sort(key=lambda s: (-s.score, s.index))
        return tuple(scored[:top_k])


class FakeEmbedder(Embedder):
    """Hash-bucket vectors. Deterministic, dimension-correct, meaningless."""

    def __init__(self, dimensions: int = 1536) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        out: list[tuple[float, ...]] = []
        for text in texts:
            vector = [0.0] * self._dimensions
            for token in canonicalise(text).lower().split():
                # zlib.crc32, not hash(): str hashing is salted per process,
                # which made these vectors differ from one run to the next.
                vector[zlib.crc32(token.encode()) % self._dimensions] += 1.0
            norm = sum(v * v for v in vector) ** 0.5 or 1.0
            out.append(tuple(v / norm for v in vector))
        return tuple(out)


class FakeGenerator(Generator):
    """Echoes its inputs. Never invents a citation, because it never writes one."""

    def __init__(self, reply: str = '{"answer": "fake reply", "citations": []}') -> None:
        self._reply = reply
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        *,
        system: str,
        cached_prefix: str | None,
        user: str,
        max_tokens: int,
    ) -> Generation:
        self.calls.append(
            {
                "system": system,
                "cached_prefix": cached_prefix,
                "user": user,
                "max_tokens": max_tokens,
            }
        )
        return Generation(
            text=self._reply,
            input_tokens=len(system.split()) + len(user.split()),
            output_tokens=len(self._reply.split()),
            cached_prefix_hit=cached_prefix is not None,
            stop_reason="end_turn",
        )


class FakeCitationVerifier(CanonicalCitationVerifier):
    """The real comparison: it does no I/O, so the fake *is* the real one —
    one implementation of the check that decides whether a citation is
    accepted, not two that can drift."""
