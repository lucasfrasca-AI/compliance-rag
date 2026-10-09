"""The build-phase residency gate on ingest (ADR-0023): while generation runs
offshore, a real tenant upload fails loudly at the door instead of being sent
to Singapore on its first question. Both ways, through the real write path
(`CorpusLifecycle`)."""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    validate_generation_region,
)
from compliance_rag.corpus.models import Chunk, Scope

POLICY = "acme/retention-policy"


def _chunk(*, synthetic: bool, scope: Scope = Scope.TENANT) -> Chunk:
    return Chunk(
        chunk_id=f"{POLICY}:v1:1/1",
        clause_id=f"{POLICY}:v1:1/1",
        text="Accounts are kept seven years.",
        scope=scope,
        tenant_id="acme",
        synthetic=synthetic,
    )


def _lifecycle(region: str) -> tuple[CorpusLifecycle, FakeRetrievalStore]:
    ledger = FakeLedgerStore()
    store = FakeRetrievalStore(ledger=ledger)
    return (
        CorpusLifecycle(
            ledger=ledger, retrieval=store, clauses=FakeClauseStore(), generation_region=region
        ),
        store,
    )


async def _publish(lifecycle: CorpusLifecycle, chunk: Chunk) -> None:
    await lifecycle.publish_version(
        (chunk,),
        instrument_id=POLICY,
        tenant_id="acme",
        version_id="v1",
        effective_from=dt.date(2026, 1, 1),
        supersedes=None,
    )


async def test_the_first_real_policy_upload_fails_while_generation_is_offshore() -> None:
    lifecycle, store = _lifecycle("asia-southeast1")
    with pytest.raises(GenerationResidencyError, match="real tenant chunk"):
        await _publish(lifecycle, _chunk(synthetic=False))
    assert not await store.holds_real_tenant_data()  # refused before any write
    assert store.stored_chunk_ids == frozenset()


async def test_a_synthetic_policy_is_accepted_while_generation_is_offshore() -> None:
    lifecycle, store = _lifecycle("asia-southeast1")
    await _publish(lifecycle, _chunk(synthetic=True))
    assert store.stored_chunk_ids == frozenset({f"{POLICY}:v1:1/1"})
    assert not await store.holds_real_tenant_data()


async def test_a_real_policy_is_accepted_once_generation_is_in_sydney() -> None:
    lifecycle, store = _lifecycle("australia-southeast1")
    await _publish(lifecycle, _chunk(synthetic=False))
    assert await store.holds_real_tenant_data()


def test_an_unmarked_chunk_counts_as_real() -> None:
    """Fail-closed default: forgetting the flag closes the gate."""
    assert Chunk(chunk_id="c", clause_id="acme/p:v1:1", text="t").synthetic is False


@pytest.mark.parametrize("region", ["global", "us-central1", "asia-northeast1", ""])
def test_no_other_generation_region_is_accepted(region: str) -> None:
    with pytest.raises(GenerationResidencyError):
        validate_generation_region(region)
    with pytest.raises(GenerationResidencyError):
        _lifecycle(region)


def test_the_golden_corpus_marks_every_tenant_chunk_synthetic() -> None:
    """The six Acme policies are invented; if this marking were dropped, the
    recall gate's corpus would close offshore generation for everyone."""
    from compliance_rag.eval.golden_corpus import golden_chunks

    tenant = [c for c in golden_chunks() if c.scope is Scope.TENANT]
    assert tenant and all(c.synthetic for c in tenant)
