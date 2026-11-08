"""A fresh ledger, by script, to an Acme member's answer (#293).

Real Postgres (a scratch database) and a real Qdrant server, nothing in the
cloud: `LedgerMigrator` builds the schema, the law is loaded the way
`scripts/load_compilation_vectors.py` loads it (the same `load_version_vectors`
over the same ingest output, held in a fake bucket), `scripts.seed_tenant`'s
`run_seed` seeds Acme, and then `answer()` is asked as Acme with a scripted
stand-in for the model. It must retrieve Privacy Act clauses and cite an Acme
policy from the cached prefix, and both citations must verify against what the
ledger recorded.

Dense vectors are the checked-in real `gemini-embedding-001` ones
(`CachedEmbedder`), so no embedding call is made. Runs in CI, where Postgres
and Qdrant are service containers and `REQUIRE_LEDGER_DB` /
`REQUIRE_RECALL_GATE` make a missing one fail instead of skip.
"""

from __future__ import annotations

import datetime as dt
import os
import re

import pytest
from scripts.ingest_compilation import INSTRUMENT_ID, VERSION_ID
from scripts.load_compilation_vectors import load_law
from scripts.seed_tenant import run_seed

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeReranker
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import Generation, Generator, LawScope
from compliance_rag.adapters.ledger_migrations import LedgerMigrator
from compliance_rag.adapters.postgres_ledger_store import PostgresLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.assessment.evidence import Facts
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.corpus.display_form import cite
from compliance_rag.corpus.models import PUBLIC_TENANT_ID
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.corpus.tenant_seed import TenantSeed
from compliance_rag.eval.acme_seed import acme_seed
from compliance_rag.eval.applicability import load_applicability_set
from compliance_rag.eval.embedding_cache import CachedEmbedder, EmbeddingCache
from compliance_rag.eval.golden import load_golden_set
from compliance_rag.eval.golden_corpus import (
    LAW_INSTRUMENT_ID,
    POLICY_TENANT_ID,
    gate_law_clauses,
    policy_titles,
)
from compliance_rag.graph.mappings import load_mapping_set
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.conftest import qdrant_reachable
from tests.ledger_db import needs_ledger_db, scratch_database
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for
from tests.test_tenant_seed import with_policies

QDRANT_URL = os.environ.get("QDRANT_URL", "")
_needs_qdrant = pytest.mark.skipif(
    not os.environ.get("REQUIRE_RECALL_GATE") and not qdrant_reachable(QDRANT_URL),
    reason="needs a real Qdrant (QDRANT_URL)",
)

AS_OF = dt.date(2026, 10, 12)
QUESTION = "What must a company do to keep personal data safe from being misused or lost?"
"""Golden query GQ-09, so its vector is in the checked-in cache."""
_FENCE = re.compile(
    r"\[(?P<boundary>[0-9a-f]{24}) clause (?P<clause_id>\S+)[^\n]*\]\n"
    r"(?P<text>.*?)\n\[(?P=boundary) end\]",
    re.DOTALL,
)
"""The fence `assessment/prompt.py` writes around each clause."""


class QuotesTheFirstClauseOfEach(Generator):
    """A scripted stand-in for the model: cites the first clause fenced in the
    question's context and the first in the cached policy prefix, each by its
    opening words, verbatim. Cites nothing if the prompt format changes, which
    fails the test's assertions."""

    async def generate(
        self, *, system: str, cached_prefix: str | None, user: str, max_tokens: int
    ) -> Generation:
        import json

        cited = [
            {"clause_id": m["clause_id"], "quote": " ".join(m["text"].split()[:8])}
            for source in (user, cached_prefix or "")
            for m in list(_FENCE.finditer(source))[:1]
        ]
        return Generation(
            text=json.dumps({"answer": "Scripted.", "citations": cited}),
            input_tokens=0,
            output_tokens=0,
            stop_reason="end_turn",
            model="scripted-test-generator",
        )


@needs_ledger_db
@_needs_qdrant
async def test_migrate_load_law_seed_then_an_acme_member_is_answered_from_law_and_a_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (LAW_INSTRUMENT_ID, VERSION_ID) == (INSTRUMENT_ID, "C2026C00227")
    seed = acme_seed()
    law = gate_law_clauses()
    with scratch_database() as dsn:
        await LedgerMigrator(dsn).migrate()
        ledger = PostgresLedgerStore(dsn)
        # What scripts/ingest_compilation.py leaves in the bucket.
        bucket = FakeClauseStore()
        await bucket.put_clauses(law)
        await bucket.put_manifest(
            INSTRUMENT_ID,
            VERSION_ID,
            tenant_id=PUBLIC_TENANT_ID,
            canonicalisation_version=law[0].canonicalisation_version,
            source_html="<html><title>Privacy Act 1988</title></html>",
        )
        async with QdrantRetrievalStore.ephemeral(
            QDRANT_URL,
            embedder=CachedEmbedder(EmbeddingCache.load()),
            ledger=ledger,
            prefix="seed293",
        ) as retrieval:
            lifecycle = CorpusLifecycle(
                ledger=ledger,
                retrieval=retrieval,
                clauses=bucket,
                generation_region=BUILD_PHASE_OFFSHORE_REGION,
                synthetic_registry=SyntheticRegistry.load(),
            )

            async def seeded_with(this: TenantSeed) -> int:
                return await run_seed(
                    this,
                    ledger=ledger,
                    retrieval=retrieval,
                    clauses=bucket,
                    lifecycle=lifecycle,
                    today=AS_OF,
                )

            # Before the Act is in the ledger the seed is refused, not half done.
            assert await seeded_with(seed) == 1
            assert "load_compilation_vectors" in capsys.readouterr().out
            assert await ledger.get_profile(POLICY_TENANT_ID) is None

            # scripts/load_compilation_vectors.py's own entry, over these stores.
            _, loaded = await load_law(
                clause_store=bucket, ledger=ledger, retrieval=retrieval, lifecycle=lifecycle
            )
            assert loaded.recorded_in_ledger and loaded.written == loaded.chunks > 0

            assert await seeded_with(seed) == 0
            assert "6 policy version(s)" in capsys.readouterr().out
            seeded = await _held(ledger)
            # A re-run changes nothing; a different policy set is refused.
            assert await seeded_with(seed) == 0
            assert "already holds exactly this seed" in capsys.readouterr().out
            fewer = with_policies(seed, seed.policies[:-1])
            assert await seeded_with(fewer) == 1
            assert "a different policy set" in capsys.readouterr().out
            assert await _held(ledger) == seeded

            # An Acme member asks. Track A reads the ledger's evidence, not the case's.
            labels = load_applicability_set()
            graph = fake_for(load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), labels)
            scope = LawScope.of(
                await graph.coverage(
                    Facts.of(await ledger.evidence(POLICY_TENANT_ID)),
                    tenant_id=POLICY_TENANT_ID,
                    as_of=AS_OF,
                )
            )
            policies = await tenant_policies(
                ledger, bucket, tenant_id=POLICY_TENANT_ID, as_of=AS_OF
            )
            assert len(policies) == sum(len(p.clauses) for p in seed.policies)
            pipeline = AssessmentPipeline(
                RetrievalPipeline(retrieval, FakeReranker()),
                QuotesTheFirstClauseOfEach(),
                Verifier(bucket, ledger, CanonicalCitationVerifier()),
                ledger,
                tenant_text=TenantTextGate("us", SyntheticRegistry.load()),
            )

            answer = await pipeline.answer(
                QUESTION, POLICY_TENANT_ID, AS_OF, law=scope, policies=policies
            )

            retrieved = {c.clause_id for c in answer.retrieval.delivered()}
            assert any(c.startswith(f"{INSTRUMENT_ID}:") for c in retrieved), retrieved
            assert not retrieved & {p.clause_id for p in policies}  # the prefix carries those
            assert answer.verified and not answer.blocked
            cited = [c.clause.clause_id for c in answer.citations if c.clause is not None]
            assert len(cited) == 2
            law_cited, policy_cited = cited
            assert law_cited.startswith(f"{INSTRUMENT_ID}:")
            assert policy_cited in {p.clause_id for p in policies}
            assert cite(policy_cited, answer.titles).startswith(
                tuple(f"{t}, " for t in policy_titles().values())
            )

            # Without the subscription the filter drops all law for Acme.
            await ledger.unsubscribe(tenant_id=POLICY_TENANT_ID, instrument_id=INSTRUMENT_ID)
            without = await pipeline.retrieval.retrieve(
                QUESTION, POLICY_TENANT_ID, AS_OF, law=scope
            )
            assert not any(
                c.clause_id.startswith(f"{INSTRUMENT_ID}:") for c in without.ranked_candidates()
            )


async def _held(ledger: PostgresLedgerStore) -> dict[str, object]:
    return {
        "profile": await ledger.get_profile(POLICY_TENANT_ID),
        "subscriptions": await ledger.subscribed_instruments(POLICY_TENANT_ID),
        "evidence": [
            (e.evidence_id, e.field, e.value, e.recorded_at)
            for e in await ledger.evidence(POLICY_TENANT_ID)
        ],
        "policies": await ledger.policy_versions(POLICY_TENANT_ID, as_of=AS_OF),
    }


def test_the_golden_question_is_in_the_embedding_cache() -> None:
    """The end-to-end test embeds nothing: its question is a cached golden query."""
    assert QUESTION in {q.query for q in load_golden_set().queries}
    assert not EmbeddingCache.load().missing((QUESTION,))
