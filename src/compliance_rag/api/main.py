"""Uvicorn entrypoint, in factory mode so importing this module builds
nothing: `uvicorn --factory compliance_rag.api.main:build_app` (#146). Builds
the API from `Settings` with the real adapters, and telemetry configured (the
one sink, D-16):
- the Firebase verifier;
- the Postgres ledger;
- Track A over the deployed graph;
- the corpus bucket;
- `answer()` over Qdrant, the reranker and Claude on Vertex.

Tests build it with `create_app` and fakes instead.

Two things run at startup, before anything is served:
- the graph's connectivity check;
- the generator, built through `VertexClaudeGenerator.create`, so the
  residency gate (ADR-0023) runs first. That gate asks whether the vector
  store holds real tenant chunks.

Before either, the signed `incomplete.yaml` and `part_year.yaml` files are
read (#211, ADR-0047, ADR-0052):
one still pending review refuses the build, as the mapping loader refuses an
unsigned mapping (D-01), rather than failing the first answer that cites it.

The inputs that store never sees are checked per request (ADR-0032): a
member's question against the tester list (`api/answers.py`), and the CAG
prefix and any clause to repair, read from the bucket, against the synthetic
registry (`TenantTextGate`, built in here).
"""

from __future__ import annotations

from fastapi import FastAPI

from compliance_rag import telemetry
from compliance_rag.adapters.firebase_auth import FirebaseTokenVerifier, GoogleSigningKeys
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import Generation, Generator, RetrievalAdmin
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.adapters.postgres_ledger_store import PostgresLedgerStore
from compliance_rag.api.app import create_app
from compliance_rag.api.deps import ApiServices
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.incomplete_reading import committed_incomplete_readings
from compliance_rag.graph.track_a import GraphTrackA
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import Settings, get_settings
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier


class GeneratorAtStartup(Generator):
    """The Claude generator, built at startup rather than at import: its
    constructor is async, because the residency gate asks the store."""

    def __init__(self, settings: Settings, *, corpus: RetrievalAdmin) -> None:
        self._settings = settings
        self._corpus = corpus
        self._generator: Generator | None = None

    async def start(self) -> None:
        from compliance_rag.adapters.vertex_claude_generator import VertexClaudeGenerator

        self._generator = await VertexClaudeGenerator.create(self._settings, corpus=self._corpus)

    async def generate(
        self, *, system: str, cached_prefix: str | None, user: str, max_tokens: int
    ) -> Generation:
        if self._generator is None:
            raise RuntimeError("the generator starts with the app; it hasn't")
        return await self._generator.generate(
            system=system, cached_prefix=cached_prefix, user=user, max_tokens=max_tokens
        )


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    verifier = FirebaseTokenVerifier(
        project_id=settings.firebase_project_id,
        members=settings.members,
        sign_in_providers=settings.sign_in_providers,
        keys=GoogleSigningKeys(),
    )
    telemetry.configure(settings)
    committed_incomplete_readings()  # refuses an unsigned reading file (D-01)
    graph = Neo4jGraph.open(settings)
    ledger = PostgresLedgerStore.from_settings(settings)
    clauses = GCSClauseStore.for_bucket(settings.corpus_bucket)
    retrieval = RetrievalPipeline.from_settings(settings, ledger=ledger)
    store = retrieval.retrieval_store
    if not isinstance(store, RetrievalAdmin):
        raise TypeError("the residency gate needs a store that can say what it holds")
    generator = GeneratorAtStartup(settings, corpus=store)
    return create_app(
        ApiServices(
            settings=settings,
            verifier=verifier,
            ledger=ledger,
            graph=GraphTrackA(graph),
            clauses=clauses,
            pipeline=AssessmentPipeline(
                retrieval,
                generator,
                Verifier(clauses, ledger, CanonicalCitationVerifier()),
                ledger,
                tenant_text=TenantTextGate.from_settings(settings),
            ),
            checks=(graph.verify, generator.start),
            closers=(graph.aclose,),
        )
    )
