"""`retrieve()`: Track B end to end, from query to reranked candidates (issue #16).

Track A's scope arrives as a `LawScope` (D-03, ADR-0030), which every call
requires: library law only from the clauses Track A cites for the tenant on
`as_of` (`LawScope.of(coverage)`), or `LawScope.unscoped(reason)` for a
question asked with no tenant profile — named, never an empty filter.

`RetrievalPipeline.from_settings` is the real composition (issue #25): the
Qdrant store, the Vertex embedder and the reranker client, with no LLM
anywhere in the path.

The reranker's top k are delivered whole (ADR-0045): each ranked library-law
clause with the clauses of its provision that introduce it (its chapeaux) and
that it introduces (its paragraphs), fetched by containment and never ranked.
`RetrievalTrace.delivered()` is what generation is given.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass

from opentelemetry.trace import Span

from compliance_rag.adapters.interfaces import (
    NO_PREFIX,
    Candidate,
    Channel,
    LawScope,
    LedgerStore,
    PolicyCorpus,
    Reranker,
    RetrievalStore,
    ScoredDocument,
)
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    Scope,
    clause_ancestors,
    in_same_provision,
    instrument_owner,
    parse_stable_id,
)
from compliance_rag.retrieval.router import is_clause_id_query
from compliance_rag.settings import Settings
from compliance_rag.telemetry import span

RERANK_CANDIDATES = 20
"""Fused candidates handed to the cross-encoder (ADR-0022, revising D-04's
"~50"). Measured on the golden set (#32): 20 keeps every recall@10 exit bar
(cross-clause 0.896 vs 0.938 at 50) at about a third of the warm Cloud Run
latency (6.4 s median vs 16.1 s)."""


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Every stage of `retrieve()`, kept separate rather than collapsed into a
    top-k list — so a later swap of one fake for a real adapter (the reranker
    service, Qdrant) is attributable to that adapter, not the composition
    (issue #16).
    """

    per_channel: dict[Channel, tuple[Candidate, ...]]
    fused: tuple[Candidate, ...]
    reranked: tuple[ScoredDocument, ...]
    """The reranker's own output — `ScoredDocument.index` indexes into
    `fused`, exactly as `Reranker.rerank()` promises."""
    chapeaux: tuple[Candidate, ...] = ()
    """Library-law clauses containing a ranked one in its provision: the words
    that introduce it (APP 11.1 for a ranked APP 11.1(b); "agency means:" for
    a ranked paragraph of that definition). Fetched by id, never ranked
    (ADR-0045)."""
    paragraphs: tuple[Candidate, ...] = ()
    """Library-law clauses inside a ranked one in its provision: the rest of
    its text (APP 11.1(a) and (b) for a ranked APP 11.1). Fetched by
    containment, never ranked (ADR-0045)."""

    def ranked_candidates(self) -> tuple[Candidate, ...]:
        """`fused`, reordered (and trimmed) by rerank score: the reranker's
        top k, before each is delivered whole (`delivered`)."""
        return tuple(self.fused[doc.index] for doc in self.reranked)

    def delivered(self) -> tuple[Candidate, ...]:
        """What generation is given: the ranked candidates in rank order, each
        with the rest of its provision around it, its chapeaux before it
        (outermost first) and its paragraphs after it (in clause order, a
        ranked one among them), each clause once (ADR-0045). Never a fragment
        of the law: a paragraph without the words that introduce it, or a
        chapeau without its list. Adds to the ranked top k, never cuts it."""
        ranked = self.ranked_candidates()
        pool = (*ranked, *self.chapeaux, *self.paragraphs)
        delivered: dict[str, Candidate] = {}
        for candidate in ranked:
            ancestors = clause_ancestors(candidate.clause_id)
            before = sorted(
                (c for c in pool if in_same_provision(c.clause_id, candidate.clause_id)),
                key=lambda c: ancestors.index(c.clause_id),
            )
            after = sorted(
                (c for c in pool if in_same_provision(candidate.clause_id, c.clause_id)),
                key=lambda c: c.clause_id,
            )
            for clause in (*before, candidate, *after):
                delivered.setdefault(clause.clause_id, clause)
        return tuple(delivered.values())

    def top_candidate(self) -> Candidate | None:
        """The single best candidate after reranking, or `None` if nothing
        was retrieved."""
        ranked = self.ranked_candidates()
        return ranked[0] if ranked else None


def _scored(candidates: tuple[Candidate, ...]) -> list[str]:
    return [f"{c.chunk_id}:{c.score:.4f}" for c in candidates]


class RetrievalPipeline:
    """Composes exactly one `RetrievalStore` and one `Reranker` (D-08)."""

    def __init__(self, retrieval_store: RetrievalStore, reranker: Reranker) -> None:
        self.retrieval_store = retrieval_store
        self.reranker = reranker

    @classmethod
    def from_settings(cls, settings: Settings, *, ledger: LedgerStore) -> RetrievalPipeline:
        """The real adapters behind the same `retrieve()` the fakes proved
        (issue #25). The reranker endpoint's residency is asserted when its
        client is built (`build_reranker_http_client`, D-15)."""
        # Imported here, not at module level: these pull in qdrant-client,
        # fastembed and the Vertex SDK, which nothing that only composes
        # fakes (every unit test) should pay for.
        from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
        from compliance_rag.adapters.reranker_client import RerankerClient
        from compliance_rag.adapters.reranker_http_client import build_reranker_http_client
        from compliance_rag.adapters.vertex_embedder import VertexEmbedder

        # Residency first: a reranker endpoint outside Sydney is refused
        # before any cloud client is constructed (D-15).
        reranker = RerankerClient(
            build_reranker_http_client(
                settings.reranker_url, timeout_seconds=settings.reranker_timeout_seconds
            )
        )
        store = QdrantRetrievalStore.from_settings(
            settings, embedder=VertexEmbedder.from_settings(settings), ledger=ledger
        )
        return cls(store, reranker)

    async def retrieve(
        self,
        query: str,
        tenant_id: str,
        as_of: dt.date,
        *,
        law: LawScope,
        scopes: tuple[Scope, ...] = (Scope.PUBLIC, Scope.TENANT),
        limit: int = RERANK_CANDIDATES,
        top_k: int = 10,
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> RetrievalTrace:
        """Both channels, fused, then reranked (`CONTEXT.md`: Fusion, Rerank).
        `policy_corpus`: the tenant clauses already sent as the CAG prefix,
        never retrieved again (ADR-0033). `user_id`: the signed-in member
        asking; a user-scoped chunk comes back to its owner alone, and to no
        one when this is `None` (ADR-0036)."""
        # The law scope is recorded on this span by the store's filter.
        with span("retrieve", query=query, tenant_id=tenant_id, as_of=as_of.isoformat()) as current:
            result = await self.retrieval_store.search(
                query,
                law=law,
                tenant_id=tenant_id,
                scopes=scopes,
                as_of=as_of,
                limit=limit,
                policy_corpus=policy_corpus,
                user_id=user_id,
            )
            # Both retrieval channels are logged before fusion (CLAUDE.md).
            for channel, candidates in result.per_channel.items():
                current.set_attribute(f"retrieval.channel.{channel.value}", _scored(candidates))
            current.set_attribute("retrieval.fused", _scored(result.fused))

            reranked = await self.reranker.rerank(
                query,
                tuple(c.text for c in result.fused),
                top_k=top_k,
            )
            current.set_attribute(
                "retrieval.rerank_scores", [f"{doc.index}:{doc.score:.4f}" for doc in reranked]
            )
            ranked = tuple(result.fused[doc.index] for doc in reranked)
            chapeaux, paragraphs = await self._whole(
                ranked, tenant_id, as_of, law=law, policy_corpus=policy_corpus, current=current
            )

            return RetrievalTrace(
                per_channel=result.per_channel,
                fused=result.fused,
                reranked=reranked,
                chapeaux=chapeaux,
                paragraphs=paragraphs,
            )

    async def route(
        self,
        query: str,
        tenant_id: str,
        as_of: dt.date,
        *,
        law: LawScope,
        scopes: tuple[Scope, ...] = (Scope.PUBLIC, Scope.TENANT),
        limit: int = RERANK_CANDIDATES,
        top_k: int = 10,
        policy_corpus: PolicyCorpus = NO_PREFIX,
        user_id: str | None = None,
    ) -> RetrievalTrace:
        """The router's fixed decision (`CONTEXT.md`: Router, D-04, D-12):
        a clause-ID query goes straight to `lookup_clause` on the keyword
        channel and never reaches the reranker; anything else runs the full
        `retrieve()` pipeline unchanged. Both paths are bound by `scopes` and
        `user_id` alike: an exact lookup is a channel too, so it returns a
        clause of a scope the caller didn't ask for, or a user's private
        addition to anyone but its owner, no more than a search does
        (ADR-0036).
        """
        if not is_clause_id_query(query):
            return await self.retrieve(
                query,
                tenant_id,
                as_of,
                law=law,
                scopes=scopes,
                limit=limit,
                top_k=top_k,
                policy_corpus=policy_corpus,
                user_id=user_id,
            )

        with span(
            "retrieve.router_hit", query=query, tenant_id=tenant_id, as_of=as_of.isoformat()
        ) as current:
            hit = await self.retrieval_store.lookup_clause(
                query.strip(),
                law=law,
                tenant_id=tenant_id,
                scopes=scopes,
                as_of=as_of,
                policy_corpus=policy_corpus,
                user_id=user_id,
            )
            fused = (hit,) if hit is not None else ()
            current.set_attribute("retrieval.channel.keyword", _scored(fused))
            current.set_attribute("retrieval.fused", _scored(fused))
            reranked = tuple(ScoredDocument(index=i, score=c.score) for i, c in enumerate(fused))
            # Delivered whole, like a ranked clause: the lookup is exact, but
            # the clause it finds is still part of a provision (ADR-0045).
            chapeaux, paragraphs = await self._whole(
                fused, tenant_id, as_of, law=law, policy_corpus=policy_corpus, current=current
            )
            return RetrievalTrace(
                per_channel={Channel.KEYWORD: fused},
                fused=fused,
                reranked=reranked,
                chapeaux=chapeaux,
                paragraphs=paragraphs,
            )

    async def _whole(
        self,
        ranked: tuple[Candidate, ...],
        tenant_id: str,
        as_of: dt.date,
        *,
        law: LawScope,
        policy_corpus: PolicyCorpus,
        current: Span,
    ) -> tuple[tuple[Candidate, ...], tuple[Candidate, ...]]:
        """The chapeaux and paragraphs that make each ranked library-law
        clause whole (ADR-0045): the clauses of its provision that contain it
        or are inside it, through every filter a lookup applies (the law
        scope, subscriptions, validity), so nothing comes back that the
        ranked clause's own channel couldn't have returned. Library law only:
        a tenant's documents are parsed flat. Never ranked, so the reranker's
        order and its top k are what they were. Both are recorded on `current`,
        the span of the retrieval they complete."""
        ranked_ids = {c.clause_id for c in ranked}
        law_ids = tuple(
            dict.fromkeys(
                c.clause_id
                for c in ranked
                if instrument_owner(parse_stable_id(c.clause_id)[0]) == PUBLIC_TENANT_ID
            )
        )
        if not law_ids:
            current.set_attribute("retrieval.chapeaux", [])
            current.set_attribute("retrieval.paragraphs", [])
            return (), ()
        # One read for both directions; which of them share a ranked
        # clause's provision is decided here, not by the store.
        lineage = await self.retrieval_store.lookup_lineage(
            law_ids,
            law=law,
            tenant_id=tenant_id,
            scopes=(Scope.PUBLIC,),
            as_of=as_of,
            policy_corpus=policy_corpus,
        )
        unranked = [c for c in lineage if c.clause_id not in ranked_ids]
        paragraphs = tuple(
            c for c in unranked if any(in_same_provision(r, c.clause_id) for r in law_ids)
        )
        held = {c.clause_id for c in paragraphs}
        chapeaux = tuple(
            c
            for c in unranked
            if c.clause_id not in held and any(in_same_provision(c.clause_id, r) for r in law_ids)
        )
        current.set_attribute("retrieval.chapeaux", [c.clause_id for c in chapeaux])
        current.set_attribute("retrieval.paragraphs", [c.clause_id for c in paragraphs])
        return chapeaux, paragraphs

    async def fetch(
        self,
        clause_ids: tuple[str, ...],
        tenant_id: str,
        as_of: dt.date,
        *,
        law: LawScope,
        policy_corpus: PolicyCorpus = NO_PREFIX,
    ) -> tuple[Candidate, ...]:
        """Clauses by exact id on the keyword channel, through every filter a
        lookup applies (the law scope, subscriptions, validity, ADR-0033);
        the ones not found are left out. For ids Track A names — an
        obligation's exceptions and qualifiers and the clauses inside them
        (#164, #182, ADR-0037, ADR-0044) — never for an id a model or a
        retrieved text supplied. Public scope only: each is library law, so
        no tenant or private text can come back under its id."""
        with span("retrieve.fetch", tenant_id=tenant_id, as_of=as_of.isoformat()) as current:
            current.set_attribute("retrieval.fetch.clause_ids", list(clause_ids))
            found = await asyncio.gather(
                *(
                    self.retrieval_store.lookup_clause(
                        clause_id,
                        law=law,
                        tenant_id=tenant_id,
                        scopes=(Scope.PUBLIC,),
                        as_of=as_of,
                        policy_corpus=policy_corpus,
                    )
                    for clause_id in clause_ids
                )
            )
            hits = tuple(hit for hit in found if hit is not None)
            current.set_attribute("retrieval.channel.keyword", _scored(hits))
            return hits
