"""Load the ingested compilation into the deployed Qdrant collection (#144):
the clauses `scripts/ingest_compilation.py` wrote to the corpus bucket, read
back, chunked with the real reranker tokenizer, embedded with
`gemini-embedding-001` in australia-southeast1, and published through
`CorpusLifecycle` (ADR-0021), which records the version in the ledger too.

Idempotent (`corpus/vector_load.py`): a re-run with every chunk stored changes
nothing, or only records the version in a ledger that hasn't got it yet. A
partly stored version is refused, not written over.

The ledger is the one `Settings` configures (DATABASE_URL). If it can't be
reached the load stops before writing anything, unless `--defer-ledger` says
to publish the vectors now and leave the ledger record to a later run against
a reachable ledger. Until then the verifier blocks every citation of the
version (ADR-0021 mechanism 3): it fails closed.

Reads QDRANT_URL / QDRANT_API_KEY / QDRANT_COLLECTION through `Settings` and
never prints the URL or the key. Writes points to an existing collection on an
approved service; creates no resource. About 779 embedding calls, under a cent.

    uv run python -m scripts.load_compilation_vectors \\
        --bucket compliance-rag-508904-corpus [--defer-ledger]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from urllib.parse import urlparse

import psycopg

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.interfaces import LedgerStore
from compliance_rag.adapters.postgres_ledger_store import PostgresLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.vertex_embedder import VertexEmbedder
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, DocumentClass
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.corpus.vector_load import load_version_vectors, read_version_clauses
from compliance_rag.settings import Settings
from scripts.ingest_compilation import INSTRUMENT_ID, START, VERSION_ID

EFFECTIVE = dt.date.fromisoformat(START)
"""In force from the compilation's start date, as the graph has it
(`scripts/load_graph_structure.py`)."""
QDRANT_FREE_TIER_BYTES = 1024**3


async def _ledger(settings: Settings, *, defer: bool) -> tuple[LedgerStore, str]:
    """The configured ledger, checked reachable before anything is written,
    or an in-memory one, discarded at exit, when the record is deferred. Names
    where it is, never who it connects as."""
    if defer:
        return FakeLedgerStore(), ""
    dsn = urlparse(settings.database_url.get_secret_value())
    where = f"{dsn.hostname}:{dsn.port or 5432}{dsn.path}"
    ledger = PostgresLedgerStore.from_settings(settings)
    try:
        await ledger.version_validity(
            tenant_id=PUBLIC_TENANT_ID, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID
        )
    except psycopg.OperationalError as exc:
        raise SystemExit(
            f"the configured ledger ({where}) can't be reached, so nothing was written. "
            "Start it, or pass --defer-ledger to publish the vectors now and record the "
            f"version on a later run. ({type(exc).__name__})"
        ) from exc
    return ledger, where


async def main(bucket: str, *, defer_ledger: bool) -> None:
    settings = Settings()
    clause_store = GCSClauseStore.for_bucket(bucket)
    clauses = await read_version_clauses(
        clause_store, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, tenant_id=PUBLIC_TENANT_ID
    )
    ledger, ledger_where = await _ledger(settings, defer=defer_ledger)
    embedder = VertexEmbedder.from_settings(settings)
    retrieval = QdrantRetrievalStore.from_settings(settings, embedder=embedder, ledger=ledger)
    await retrieval.ensure_ready()
    lifecycle = CorpusLifecycle(
        ledger=ledger,
        retrieval=retrieval,
        clauses=clause_store,
        generation_region=settings.vertex_claude_region,
        synthetic_registry=SyntheticRegistry.load(),
    )
    load = await load_version_vectors(
        clauses,
        count_tokens=count_reranker_tokens,
        lifecycle=lifecycle,
        retrieval=retrieval,
        ledger=ledger,
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        effective_from=EFFECTIVE,
        document_class=DocumentClass.LIBRARY_LAW,
    )
    stored = await retrieval.count_version_chunks(
        instrument_id=INSTRUMENT_ID, tenant_id=PUBLIC_TENANT_ID, version_id=VERSION_ID
    )
    print(
        f"{INSTRUMENT_ID}:{VERSION_ID} in {settings.qdrant_collection}: {len(clauses)} clauses, "
        f"{load.chunks} chunks; stored before {load.stored_before}, after {stored} "
        f"({'matches' if stored == load.chunks else 'DOES NOT MATCH'} the chunk count)"
    )
    print(
        "vectors: "
        + ("embedded and upserted by this run" if load.published else "already stored, unchanged")
        + f"; {embedder.tokens_embedded} tokens embedded, ${embedder.cost_usd:.4f}"
    )
    # An estimate: dense float32 vectors plus the stored text. HNSW links,
    # sparse vectors and payload indexes come on top.
    approx = stored * embedder.dimensions * 4 + sum(len(c.text.encode()) for c in clauses)
    print(
        f"size: ~{approx / 1024**2:.1f} MB of dense vectors and text, "
        f"~{100 * approx / QDRANT_FREE_TIER_BYTES:.2f}% of the 1 GB Free tier (estimate)"
    )
    if defer_ledger:
        print(
            "ledger: deferred, so the version is NOT recorded in any ledger. Until a run "
            "against a reachable ledger records it, the verifier blocks every citation of it"
        )
    else:
        recorded = "recorded by this run" if load.recorded_in_ledger else "already recorded"
        print(f"ledger ({ledger_where}): {recorded}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument(
        "--defer-ledger",
        action="store_true",
        help="publish the vectors without a reachable ledger; a later run records the version",
    )
    arguments = parser.parse_args()
    asyncio.run(main(arguments.bucket, defer_ledger=arguments.defer_ledger))
