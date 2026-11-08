"""Give a tenant in a fresh ledger what its members' questions need (#293): its
profile, its subscription to the Privacy Act, its evidence (`DEMO_PROFILE`, the
applicability set's GA-01 facts, for Acme) and its six synthetic policies,
published through `CorpusLifecycle` into the clause bucket, Qdrant and the
ledger, so Ask's cached policy prefix and policy citations work.

Run `scripts/migrate_ledger.py` and then `scripts/load_compilation_vectors.py`
first: the ledger refuses a subscription to law it doesn't hold, and this script
refuses, before writing, if the ledger doesn't hold the Act yet. Idempotent: a
tenant that already holds exactly this seed is left as it is. A tenant that holds
anything the seed would contradict (another profile, other evidence, a different
policy set, a policy stored differently) is refused, naming each reason, with nothing written to any
store (`corpus/tenant_seed.py`). The policies are synthetic only because the
checked-in registry vouches for them (ADR-0026); this script cannot say so.

Reads DATABASE_URL, QDRANT_URL / QDRANT_API_KEY / QDRANT_COLLECTION and
CORPUS_BUCKET through `Settings` (the bucket may be given as `--bucket`), and
never prints them. Writes to the stores those name. The chunks are embedded
with `gemini-embedding-001` in australia-southeast1 (the six policies: well
under a cent).

    uv run python -m scripts.seed_tenant --tenant acme [--bucket NAME] [--dry-run]

`--dry-run` reads all three stores and says what a run would write, or why it
would be refused, without writing. The Qdrant collection must already exist.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from collections.abc import Callable

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.interfaces import ClauseStore, LedgerStore, RetrievalAdmin
from compliance_rag.adapters.postgres_ledger_store import LedgerUnreachable, PostgresLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.vertex_embedder import VertexEmbedder
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.corpus.tenant_seed import SeedRefused, TenantSeed, apply_seed, plan_seed
from compliance_rag.eval.acme_seed import SEEDS
from compliance_rag.settings import Settings


async def run_seed(
    seed: TenantSeed,
    *,
    ledger: LedgerStore,
    retrieval: RetrievalAdmin,
    clauses: ClauseStore,
    lifecycle: CorpusLifecycle,
    today: dt.date,
    dry_run: bool = False,
    count_tokens: Callable[[str], int] = count_reranker_tokens,
) -> int:
    """Seed (or, `dry_run`, only plan) over the stores given, say what was
    done, and return the exit code: 0, or 1 for a seed the tenant's stores
    refuse. The stores are the caller's, so a test runs this on local ones."""
    try:
        if dry_run:
            plan = await plan_seed(
                seed,
                ledger=ledger,
                retrieval=retrieval,
                clauses=clauses,
                count_tokens=count_tokens,
                today=today,
            )
            print(f"{seed.tenant_id} (dry run): {plan.describe()}")
        else:
            plan = await apply_seed(
                seed,
                ledger=ledger,
                lifecycle=lifecycle,
                retrieval=retrieval,
                clauses=clauses,
                count_tokens=count_tokens,
                today=today,
            )
            print(f"{seed.tenant_id}: {plan.describe()}")
    except SeedRefused as refused:
        print(f"seed_tenant: refused, nothing was written to any store ({len(refused.reasons)}):")
        for reason in refused.reasons:
            print(f"  - {reason}")
        return 1
    return 0


async def main(tenant: str, *, bucket: str | None, dry_run: bool) -> int:
    if tenant not in SEEDS:
        print(f"seed_tenant: no seed for {tenant!r}; seeds: {', '.join(sorted(SEEDS))}")
        return 2
    seed = SEEDS[tenant]()
    settings = Settings()
    bucket = bucket or settings.corpus_bucket
    if not bucket:
        print("seed_tenant: no corpus bucket (--bucket or CORPUS_BUCKET)")
        return 2
    ledger = PostgresLedgerStore.from_settings(settings)
    try:
        await ledger.ping()
    except LedgerUnreachable as exc:
        print(f"seed_tenant: {exc}, so nothing was written")
        return 2
    clause_store = GCSClauseStore.for_bucket(bucket, project=settings.gcp_project_id or None)
    embedder = VertexEmbedder.from_settings(settings)
    retrieval = QdrantRetrievalStore.from_settings(settings, embedder=embedder, ledger=ledger)
    if not dry_run:
        await retrieval.ensure_ready()
    code = await run_seed(
        seed,
        ledger=ledger,
        retrieval=retrieval,
        clauses=clause_store,
        lifecycle=CorpusLifecycle(
            ledger=ledger,
            retrieval=retrieval,
            clauses=clause_store,
            generation_region=settings.vertex_claude_region,
            synthetic_registry=SyntheticRegistry.load(),
        ),
        today=dt.date.today(),
        dry_run=dry_run,
    )
    if code == 0 and not dry_run:
        print(f"embedded {embedder.tokens_embedded} tokens, ${embedder.cost_usd:.4f}")
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--tenant", required=True, help=f"one of: {', '.join(sorted(SEEDS))}")
    parser.add_argument("--bucket", default=None, help="the corpus bucket (default: CORPUS_BUCKET)")
    parser.add_argument("--dry-run", action="store_true", help="read, report, and write nothing")
    arguments = parser.parse_args()
    raise SystemExit(
        asyncio.run(main(arguments.tenant, bucket=arguments.bucket, dry_run=arguments.dry_run))
    )
