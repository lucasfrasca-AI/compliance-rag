"""In-memory implementations of every interface, for tests (D-08).

These are the only implementations in the repo at Phase 0. No adapter talks to a
real service yet. The fakes are deliberately strict where the interface makes a
promise: `FakeGraphStore` rejects a traversal without a depth cap, and
`FakeRetrievalStore` refuses to return a chunk outside the Track A filter.
"""

from __future__ import annotations

import datetime as dt

from compliance_rag.adapters.interfaces import (
    Candidate,
    Channel,
    CitationVerifier,
    Embedder,
    Generation,
    Generator,
    GraphStore,
    LedgerStore,
    Obligation,
    Profile,
    Reranker,
    RetrievalResult,
    RetrievalStore,
    ScoredDocument,
)
from compliance_rag.corpus.models import Chunk, ClauseRecord, Scope, canonicalise


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


class FakeRetrievalStore(RetrievalStore):
    """Token-overlap 'dense' and 'sparse' channels with a deterministic fusion."""

    def __init__(self, chunks: tuple[Chunk, ...] = ()) -> None:
        self._chunks = chunks

    @staticmethod
    def _overlap(query: str, text: str) -> float:
        q = set(canonicalise(query).lower().split())
        t = set(canonicalise(text).lower().split())
        if not q or not t:
            return 0.0
        return len(q & t) / len(q)

    def _visible(self, chunk: Chunk, tenant_id: str, scopes: tuple[Scope, ...]) -> bool:
        if chunk.scope not in scopes:
            return False
        if chunk.scope is Scope.PUBLIC:
            return True
        return chunk.tenant_id == tenant_id

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
        pool = [
            c
            for c in self._chunks
            if self._visible(c, tenant_id, scopes)
            # D-03: Track B only ever returns chunks under Track A's obligations.
            and (not allowed or (c.obligation_id is not None and c.obligation_id in allowed))
            and (c.valid_from is None or as_of >= c.valid_from)
            and (c.valid_to is None or as_of < c.valid_to)
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
        for c in self._chunks:
            if c.clause_id != clause_id:
                continue
            if c.scope is not Scope.PUBLIC and c.tenant_id != tenant_id:
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


class FakeReranker(Reranker):
    """Deterministic token-overlap reranker. Stands in for the cross-encoder."""

    async def rerank(
        self,
        query: str,
        documents: tuple[str, ...],
        *,
        top_k: int,
    ) -> tuple[ScoredDocument, ...]:
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
                vector[hash(token) % self._dimensions] += 1.0
            norm = sum(v * v for v in vector) ** 0.5 or 1.0
            out.append(tuple(v / norm for v in vector))
        return tuple(out)


class FakeGenerator(Generator):
    """Echoes its inputs. Never invents a citation, because it never writes one."""

    def __init__(self, reply: str = "fake reply") -> None:
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


class FakeCitationVerifier(CitationVerifier):
    """The real comparison: canonical quote must be a substring of canonical text.

    Raises rather than comparing when the citation's `canonicalisation_version`
    doesn't match the clause's (issue #2) — text canonicalised under different
    rules is never comparable, however textually similar it looks.
    """

    async def verify(
        self,
        clause_id: str,
        quoted_text: str,
        *,
        clauses: tuple[ClauseRecord, ...],
        canonicalisation_version: int,
    ) -> bool:
        quote = canonicalise(quoted_text)
        if not quote:
            return False
        for clause in clauses:
            if clause.clause_id != clause_id:
                continue
            if canonicalisation_version != clause.canonicalisation_version:
                raise ValueError(
                    f"cannot verify a canonicalisation v{canonicalisation_version} citation "
                    f"against clause {clause_id!r}, canonicalised under "
                    f"v{clause.canonicalisation_version}"
                )
            return quote in clause.canonical_text
        return False
