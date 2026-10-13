"""The `synthetic` flag is operator-controlled, not self-attested (issue #78).

The build-phase residency gate (ADR-0023) trusts the flag, so an upload must
not be able to set it. Through the real write path (`CorpusLifecycle`), with
generation offshore as in the build phase: a chunk claiming synthetic without
being in the operator's registry is refused before anything is written, and a
chunk's flag is whatever the registry says, whatever the caller sent.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.generation_residency import GenerationResidencyError
from compliance_rag.corpus.ingest import ingest_compilation
from compliance_rag.corpus.models import Chunk, ClauseRecord, DocumentClass, Scope, parse_stable_id
from compliance_rag.corpus.synthetic_registry import (
    SyntheticClaimRefused,
    SyntheticRegistry,
)
from compliance_rag.eval.golden_corpus import (
    golden_chunks,
    synthetic_fixture_chunks,
    synthetic_fixture_clauses,
)
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION


def _chunk(text: str, *, synthetic: bool, tenant_id: str = "acme") -> Chunk:
    instrument = f"{tenant_id}/privacy-policy"
    return Chunk(
        chunk_id=f"{instrument}:v2:1/1",
        clause_id=f"{instrument}:v2:1/1",
        text=text,
        scope=Scope.TENANT,
        tenant_id=tenant_id,
        synthetic=synthetic,
    )


def _lifecycle() -> tuple[CorpusLifecycle, FakeRetrievalStore]:
    ledger = FakeLedgerStore()
    store = FakeRetrievalStore(ledger=ledger)
    lifecycle = CorpusLifecycle(
        ledger=ledger,
        retrieval=store,
        clauses=FakeClauseStore(),
        generation_region=BUILD_PHASE_OFFSHORE_REGION,
        synthetic_registry=SyntheticRegistry.load(),
    )
    return lifecycle, store


async def _upload(lifecycle: CorpusLifecycle, chunk: Chunk) -> None:
    await lifecycle.publish_version(
        (chunk,),
        instrument_id=parse_stable_id(chunk.clause_id)[0],
        tenant_id=chunk.tenant_id,
        version_id="v2",
        effective_from=dt.date(2026, 10, 1),
        supersedes=None,
        document_class=DocumentClass.INTERNAL_POLICY,
    )


async def test_an_upload_cannot_mark_its_own_content_synthetic() -> None:
    lifecycle, store = _lifecycle()
    real = _chunk("Acme sells customer lists to partners each quarter.", synthetic=True)
    with pytest.raises(SyntheticClaimRefused, match="cannot mark its own content synthetic"):
        await _upload(lifecycle, real)
    assert store.stored_chunk_ids == frozenset()  # refused before any write
    assert not await store.holds_real_tenant_data()


async def test_a_claim_is_refused_on_replace_in_place_too() -> None:
    lifecycle, store = _lifecycle()
    fixture = synthetic_fixture_chunks()[0]
    await _upload(lifecycle, _chunk(fixture.text, synthetic=False))
    swapped = _chunk("Acme sells customer lists to partners each quarter.", synthetic=True)
    clause = ClauseRecord.build(
        instrument_id="acme/privacy-policy",
        version_id="v2",
        clause_path="1/1",
        text=swapped.text,
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    with pytest.raises(SyntheticClaimRefused):
        await lifecycle.replace_in_place(
            (swapped,),
            (clause,),
            tenant_id="acme",
            instrument_id="acme/privacy-policy",
            version_id="v2",
            source_html="<p/>",
            actor="u1",
            reason="typo",
            acknowledged_findings_citing=0,
        )
    assert not await store.holds_real_tenant_data()


async def test_registered_fixture_text_is_synthetic_whatever_the_caller_sent() -> None:
    lifecycle, store = _lifecycle()
    fixture = synthetic_fixture_chunks()[0]
    await _upload(lifecycle, _chunk(fixture.text, synthetic=False))
    assert not await store.holds_real_tenant_data()


async def test_registered_text_under_another_tenant_is_real() -> None:
    """The registry vouches for a tenant's fixture, not for the words: the same
    text uploaded as another tenant's document is that tenant's real data."""
    lifecycle, _ = _lifecycle()
    fixture = synthetic_fixture_chunks()[0]
    with pytest.raises(GenerationResidencyError, match="real tenant chunk"):
        await _upload(lifecycle, _chunk(fixture.text, synthetic=False, tenant_id="beta"))
    with pytest.raises(SyntheticClaimRefused):
        await _upload(lifecycle, _chunk(fixture.text, synthetic=True, tenant_id="beta"))


async def test_an_unclaimed_unregistered_upload_is_real_and_closes_the_gate() -> None:
    lifecycle, _ = _lifecycle()
    with pytest.raises(GenerationResidencyError, match="real tenant chunk"):
        await _upload(lifecycle, _chunk("Acme keeps payroll for 7 years.", synthetic=False))


def test_the_registry_matches_the_fixtures() -> None:
    """Editing a fixture, the parser or the chunker without re-registering
    (scripts/register_synthetic_fixtures.py, by PR) fails here."""
    expected = SyntheticRegistry.of(
        synthetic_fixture_chunks(), synthetic_fixture_clauses(), source=""
    )
    assert SyntheticRegistry.load().keys == expected.keys


def test_every_golden_tenant_chunk_is_synthetic_by_the_registry() -> None:
    tenant = [c for c in golden_chunks() if c.scope is Scope.TENANT]
    assert tenant and all(c.synthetic for c in tenant)


def test_the_registry_round_trips(tmp_path: Path) -> None:
    registry = SyntheticRegistry.of((_chunk("t", synthetic=False),), source="s")
    registry.save(tmp_path / "r.json")
    assert SyntheticRegistry.load(tmp_path / "r.json") == SyntheticRegistry(
        keys=frozenset(registry.keys), source="s"
    )


async def test_registered_chunks_cannot_carry_real_clauses_through_replace_in_place() -> None:
    """Clause text reaches the generator by itself (a repair prompt carries the
    clause's canonical text), so registered chunks paired with an unregistered
    clause are a real upload: refused offshore, before any write."""
    lifecycle, _ = _lifecycle()
    fixture = synthetic_fixture_chunks()[0]
    disguise = _chunk(fixture.text, synthetic=False)
    await _upload(lifecycle, disguise)
    real_clause = ClauseRecord.build(
        instrument_id="acme/privacy-policy",
        version_id="v2",
        clause_path="1/1",
        text="Acme sells customer lists to partners each quarter.",
        scope=Scope.TENANT,
        tenant_id="acme",
    )
    with pytest.raises(GenerationResidencyError, match="acme/privacy-policy:v2:1/1"):
        await lifecycle.replace_in_place(
            (disguise,),
            (real_clause,),
            tenant_id="acme",
            instrument_id="acme/privacy-policy",
            version_id="v2",
            source_html="<p/>",
            actor="u1",
            reason="typo",
            acknowledged_findings_citing=0,
        )


async def test_registered_clauses_pass_replace_in_place() -> None:
    lifecycle, store = _lifecycle()
    clause = synthetic_fixture_clauses()[0]
    chunk = next(c for c in synthetic_fixture_chunks() if c.clause_id == clause.clause_id)
    instrument, version, _ = parse_stable_id(clause.clause_id)
    await lifecycle.publish_version(
        (chunk,),
        instrument_id=instrument,
        tenant_id="acme",
        version_id=version,
        effective_from=dt.date(2026, 10, 1),
        supersedes=None,
        document_class=DocumentClass.INTERNAL_POLICY,
    )
    await lifecycle.replace_in_place(
        (chunk,),
        (clause,),
        tenant_id="acme",
        instrument_id=instrument,
        version_id=version,
        source_html="<p/>",
        actor="u1",
        reason="typo",
        acknowledged_findings_citing=0,
    )
    assert not await store.holds_real_tenant_data()


WRITERS = {
    "src/compliance_rag/adapters/corpus_lifecycle.py",  # the write path: registry + gate
    "src/compliance_rag/corpus/ingest.py",  # library law only (refuses tenant scope)
    "src/compliance_rag/eval/harness.py",  # operator: the golden corpus
    "src/compliance_rag/eval/live.py",  # operator: live eval and injection probes
    "src/compliance_rag/adapters/interfaces.py",  # ClauseStore.put_clauses' default delegates
}


def test_only_known_paths_write_to_the_corpus_stores() -> None:
    """The stores take `synthetic` as given, so a new caller of
    `upsert_chunks` or `put_clause` — a Phase 3 upload handler writing
    directly — would bypass the registry. Adding one fails here until it is
    reviewed and either routed through `CorpusLifecycle` or listed."""
    root = Path(__file__).resolve().parents[1]
    callers = {
        str(path.relative_to(root))
        for path in (root / "src").rglob("*.py")
        if re.search(r"\.(upsert_chunks|put_clauses?)\(", path.read_text(encoding="utf-8"))
    }
    assert callers == WRITERS


async def test_compilation_ingest_refuses_a_tenant_document() -> None:
    with pytest.raises(ValueError, match="library law only"):
        await ingest_compilation(
            "<html/>",
            instrument_id="acme/policy",
            version_id="v1",
            tenant_id="acme",
            scope=Scope.TENANT,
            selection=None,  # pyright: ignore[reportArgumentType] — refused before use
            store=FakeClauseStore(),
        )
