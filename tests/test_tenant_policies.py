"""A tenant's policy corpus, `answer()`'s cached prefix (#148, D-11): its
in-force internal policies from the ledger, clause by clause in document
order, from the clause store. Run over the golden corpus, where Acme's six
synthetic policies are its internal policies.
"""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag.adapters.fakes import FakeClauseStore
from compliance_rag.assessment.policies import document_order, tenant_policies
from compliance_rag.assessment.prompt import SourceClause
from compliance_rag.corpus.models import ClauseRecord, DocumentClass, Scope
from compliance_rag.eval.golden_corpus import POLICY_TENANT_ID, golden_clauses
from compliance_rag.eval.harness import GOLDEN_VALID_FROM, golden_clause_store, golden_ledger

AS_OF = GOLDEN_VALID_FROM + dt.timedelta(days=30)


def _acme_policies() -> list[SourceClause]:
    """What the live answer eval sends as the prefix (`run_answer_eval`),
    grouped by instrument: the order `policy_versions` lists them in, with
    document order kept within each."""
    acme = [c for c in golden_clauses() if c.tenant_id == POLICY_TENANT_ID]
    acme.sort(key=lambda c: c.instrument_id)  # stable: document order within each
    return [SourceClause(c.clause_id, c.canonical_text) for c in acme]


async def test_the_prefix_is_the_tenants_policies_in_document_order() -> None:
    ledger, clauses = await golden_ledger(), await golden_clause_store()
    prefix = await tenant_policies(ledger, clauses, tenant_id=POLICY_TENANT_ID, as_of=AS_OF)
    assert list(prefix) == _acme_policies()
    assert len({c.clause_id.split(":")[0] for c in prefix}) == 6  # the six Acme policies


async def test_the_same_day_gives_the_same_prefix_byte_for_byte() -> None:
    ledger, clauses = await golden_ledger(), await golden_clause_store()
    first = await tenant_policies(ledger, clauses, tenant_id=POLICY_TENANT_ID, as_of=AS_OF)
    again = await tenant_policies(ledger, clauses, tenant_id=POLICY_TENANT_ID, as_of=AS_OF)
    assert first == again


async def test_law_withdrawn_policies_and_external_uploads_are_not_in_it() -> None:
    ledger, clauses = await golden_ledger(), await golden_clause_store()
    withdrawn = _acme_policies()[0].clause_id.split(":")
    await ledger.mark_withdrawn(
        tenant_id=POLICY_TENANT_ID,
        instrument_id=withdrawn[0],
        version_id=withdrawn[1],
        withdrawn_by="u",
    )
    await ledger.record_version(
        tenant_id=POLICY_TENANT_ID,
        instrument_id="acme/iso-27001-extract",
        version_id="v1",
        valid_from=GOLDEN_VALID_FROM,
        supersedes=None,
        document_class=DocumentClass.EXTERNAL_UPLOAD,
    )
    prefix = await tenant_policies(ledger, clauses, tenant_id=POLICY_TENANT_ID, as_of=AS_OF)
    instruments = {c.clause_id.split(":")[0] for c in prefix}
    assert withdrawn[0] not in instruments and "acme/iso-27001-extract" not in instruments
    assert not any(c.clause_id.startswith("privacy-act-1988:") for c in prefix)
    assert len(instruments) == 5


async def test_before_any_policy_is_in_force_the_prefix_is_empty() -> None:
    ledger, clauses = await golden_ledger(), await golden_clause_store()
    before = GOLDEN_VALID_FROM - dt.timedelta(days=1)
    assert await tenant_policies(ledger, clauses, tenant_id=POLICY_TENANT_ID, as_of=before) == ()


async def test_document_order_is_numeric_whatever_order_the_manifest_comes_in() -> None:
    """`GCSClauseStore` writes its manifest sorted as strings: `10/1` before
    `9/1`. A policy with ten sections must still read in its own order."""
    ledger = await golden_ledger()
    policy = _acme_policies()[0].clause_id.split(":")[0]
    version = _acme_policies()[0].clause_id.split(":")[1]

    class StringSortedManifests(FakeClauseStore):
        async def get_manifest(
            self, instrument_id: str, version_id: str, *, tenant_id: str
        ) -> dict[str, str]:
            manifest = await super().get_manifest(instrument_id, version_id, tenant_id=tenant_id)
            return dict(sorted(manifest.items()))

    store = StringSortedManifests()
    for clause in golden_clauses():
        if clause.instrument_id != policy:
            await store.put_clause(clause)
    for section in (9, 10, 11, 2):
        await store.put_clause(
            ClauseRecord.build(
                instrument_id=policy,
                version_id=version,
                clause_path=f"{section}/1",
                text=f"Section {section}.",
                scope=Scope.TENANT,
                tenant_id=POLICY_TENANT_ID,
            )
        )
    prefix = await tenant_policies(ledger, store, tenant_id=POLICY_TENANT_ID, as_of=AS_OF)
    sections = [c.clause_id.split(":")[2] for c in prefix if c.clause_id.startswith(policy + ":")]
    assert sections == ["2/1", "9/1", "10/1", "11/1"]
    assert sorted(["10/1", "9/1", "2/1", "app10/1", "app9/1"], key=document_order) == [
        "2/1",
        "9/1",
        "10/1",
        "app10/1",
        "app9/1",
    ]


async def test_a_clause_missing_from_the_store_is_an_error_not_a_gap() -> None:
    lost = _acme_policies()[0].clause_id

    class LosingOne(FakeClauseStore):
        async def get_clause(
            self, clause_id: str, *, tenant_id: str, user_id: str | None = None
        ) -> ClauseRecord | None:
            if clause_id == lost:
                return None
            return await super().get_clause(clause_id, tenant_id=tenant_id, user_id=user_id)

    store = LosingOne()
    for clause in golden_clauses():
        await store.put_clause(clause)
    with pytest.raises(LookupError, match="not stored"):
        await tenant_policies(await golden_ledger(), store, tenant_id=POLICY_TENANT_ID, as_of=AS_OF)
