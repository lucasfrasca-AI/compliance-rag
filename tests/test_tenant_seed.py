"""Seeding a tenant into a fresh ledger (#293), over the in-memory fakes.

The same code runs against real Postgres and Qdrant in
`tests/test_tenant_seed_end_to_end.py`; here it is proved to be idempotent and
to refuse without writing, which a fake can show exactly: every write method of
all three stores is counted, so "nothing written" is a count of zero.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from scripts.seed_tenant import run_seed

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.interfaces import Profile
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    ClauseRecord,
    DocumentClass,
    Scope,
    sha256_hex,
)
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.corpus.tenant_seed import (
    SeedPlan,
    SeedPolicy,
    SeedRefused,
    TenantSeed,
    apply_seed,
    plan_seed,
)
from compliance_rag.eval.acme_seed import acme_seed, demo_profile
from compliance_rag.eval.golden_corpus import (
    LAW_INSTRUMENT_ID,
    LAW_TITLE,
    LAW_VERSION_ID,
    POLICY_INSTRUMENT_IDS,
    POLICY_TENANT_ID,
    POLICY_VERSION_ID,
)
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION

TODAY = dt.date(2026, 11, 8)
SEED = acme_seed()
POLICY_IDS = tuple(POLICY_INSTRUMENT_IDS.values())

LEDGER_WRITES = (
    "put_profile",
    "subscribe",
    "record_evidence",
    "record_version",
    "end_version",
    "mark_withdrawn",
    "mark_purged",
    "record_removal_audit",
)
RETRIEVAL_WRITES = (
    "upsert_chunks",
    "retire_version",
    "withdraw_version",
    "delete_chunks",
    "revise_chunks",
)
CLAUSE_WRITES = ("put_clause", "put_clauses", "put_manifest", "purge", "record_revision")


@dataclasses.dataclass
class Stores:
    ledger: FakeLedgerStore
    retrieval: FakeRetrievalStore
    clauses: FakeClauseStore
    lifecycle: CorpusLifecycle
    writes: list[str]

    def watch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """From here on, every write to any store is noted by name."""
        for owner, names in (
            (self.ledger, LEDGER_WRITES),
            (self.retrieval, RETRIEVAL_WRITES),
            (self.clauses, CLAUSE_WRITES),
        ):
            for name in names:
                monkeypatch.setattr(owner, name, self._noting(name, getattr(owner, name)))

    def _noting(
        self,
        name: str,
        method: Callable[..., Awaitable[Any]],  # Any: the stores' write signatures differ
    ) -> Callable[..., Awaitable[Any]]:  # Any: as above
        async def noted(*args: Any, **kwargs: Any) -> Any:  # Any: forwards every signature
            self.writes.append(name)
            return await method(*args, **kwargs)

        return noted

    async def fingerprint(self, seed: TenantSeed) -> dict[str, object]:
        """Everything the seed writes, as the stores report it."""
        tenant = seed.tenant_id
        return {
            "profile": await self.ledger.get_profile(tenant),
            "subscriptions": await self.ledger.subscribed_instruments(tenant),
            "evidence": [
                dataclasses.replace(e, recorded_at=None, trace_id=None)
                for e in await self.ledger.evidence(tenant)
            ],
            "policies": await self.ledger.policy_versions(tenant, as_of=TODAY),
            "titles": await self.ledger.version_titles(
                tenant, [(p.instrument_id, p.version_id) for p in seed.policies]
            ),
            "chunks": {
                p.instrument_id: await self.retrieval.version_chunk_ids(
                    instrument_id=p.instrument_id, tenant_id=tenant, version_id=p.version_id
                )
                for p in seed.policies
            },
            "manifests": {
                p.instrument_id: await self.clauses.get_manifest(
                    p.instrument_id, p.version_id, tenant_id=tenant
                )
                for p in seed.policies
            },
        }


async def fresh_stores(*, library_loaded: bool = True) -> Stores:
    """Empty stores, as a fresh ledger is after `migrate_ledger` and, unless
    `library_loaded` is False, `load_compilation_vectors` (which records the
    Act's version in the ledger). Generation is offshore, as in the build phase,
    so only text the registry vouches for as synthetic can be published."""
    ledger = FakeLedgerStore()
    if library_loaded:
        await ledger.record_version(
            tenant_id=PUBLIC_TENANT_ID,
            instrument_id=LAW_INSTRUMENT_ID,
            version_id=LAW_VERSION_ID,
            valid_from=dt.date(2026, 6, 4),
            supersedes=None,
            document_class=DocumentClass.LIBRARY_LAW,
            title=LAW_TITLE,
        )
    retrieval = FakeRetrievalStore(ledger=ledger)
    clauses = FakeClauseStore()
    lifecycle = CorpusLifecycle(
        ledger=ledger,
        retrieval=retrieval,
        clauses=clauses,
        generation_region=BUILD_PHASE_OFFSHORE_REGION,
        synthetic_registry=SyntheticRegistry.load(),
    )
    return Stores(ledger, retrieval, clauses, lifecycle, writes=[])


async def seed_into(stores: Stores, seed: TenantSeed = SEED) -> SeedPlan:
    return await apply_seed(
        seed,
        ledger=stores.ledger,
        lifecycle=stores.lifecycle,
        retrieval=stores.retrieval,
        clauses=stores.clauses,
        count_tokens=count_reranker_tokens,
        today=TODAY,
    )


async def plan_of(stores: Stores, seed: TenantSeed = SEED) -> SeedPlan:
    return await plan_seed(
        seed,
        ledger=stores.ledger,
        lifecycle=stores.lifecycle,
        retrieval=stores.retrieval,
        clauses=stores.clauses,
        count_tokens=count_reranker_tokens,
        today=TODAY,
    )


def with_policies(seed: TenantSeed, policies: tuple[SeedPolicy, ...]) -> TenantSeed:
    return dataclasses.replace(seed, policies=policies)


def reworded(policy: SeedPolicy) -> SeedPolicy:
    """The same policy with the words of its first clause changed (and the
    source with them), as a different version of the document would be."""
    first, *rest = policy.clauses
    edited = ClauseRecord.build(
        instrument_id=first.instrument_id,
        version_id=first.version_id,
        clause_path=first.clause_path,
        text=first.text + " Acme also keeps them for ever.",
        scope=Scope.TENANT,
        tenant_id=first.tenant_id,
    )
    return dataclasses.replace(
        policy, clauses=(edited, *rest), source=policy.source + "\nAcme also keeps them for ever."
    )


# ---- the seed itself ------------------------------------------------------------------


def test_the_acme_seed_is_the_six_policies_and_the_demos_facts() -> None:
    assert [p.instrument_id for p in SEED.policies] == list(POLICY_IDS) and len(POLICY_IDS) == 6
    assert all(p.version_id == POLICY_VERSION_ID and p.source and p.title for p in SEED.policies)
    assert all(p.clauses for p in SEED.policies)
    assert SEED.subscriptions == (LAW_INSTRUMENT_ID,)
    assert {(e.field, e.value, e.period) for e in SEED.evidence} == {
        (f.field, f.value, f.period) for f in demo_profile().evidence()
    }
    assert {e.trace_id for e in SEED.evidence} == {None}  # the run's, set when recorded


def test_a_seed_cannot_carry_another_tenants_evidence() -> None:
    other = dataclasses.replace(SEED.evidence[0], tenant_id="beta")
    with pytest.raises(ValueError, match="another tenant's evidence"):
        dataclasses.replace(SEED, evidence=(other, *SEED.evidence[1:]))


async def test_a_fresh_ledger_gets_the_profile_the_subscription_the_evidence_and_six_policies() -> (
    None
):
    stores = await fresh_stores()

    written = await seed_into(stores)

    assert written.profile and written.subscriptions == (LAW_INSTRUMENT_ID,)
    assert len(written.evidence) == len(SEED.evidence) > 0
    assert len(written.policies) == 6
    ledger = stores.ledger
    assert await ledger.get_profile(POLICY_TENANT_ID) == Profile(POLICY_TENANT_ID)
    assert await ledger.subscribed_instruments(POLICY_TENANT_ID) == {LAW_INSTRUMENT_ID}
    assert {e.evidence_id for e in await ledger.evidence(POLICY_TENANT_ID)} == {
        e.evidence_id for e in SEED.evidence
    }
    in_force = await ledger.policy_versions(POLICY_TENANT_ID, as_of=TODAY)
    assert [p.instrument_id for p in in_force] == sorted(POLICY_IDS)
    for policy in SEED.policies:
        assert await ledger.version_titles(
            POLICY_TENANT_ID, [(policy.instrument_id, policy.version_id)]
        ) == {(policy.instrument_id, policy.version_id): policy.title}
        manifest = await stores.clauses.get_manifest(
            policy.instrument_id, policy.version_id, tenant_id=POLICY_TENANT_ID
        )
        assert set(manifest) == {c.clause_path for c in policy.clauses}
        metadata = await stores.clauses.get_manifest_metadata(
            policy.instrument_id, policy.version_id, tenant_id=POLICY_TENANT_ID
        )
        assert metadata is not None and metadata.source_html_sha256 == sha256_hex(policy.source)
        assert await stores.retrieval.version_chunk_ids(
            instrument_id=policy.instrument_id,
            tenant_id=POLICY_TENANT_ID,
            version_id=policy.version_id,
        )


async def test_policies_are_synthetic_only_because_the_registry_says_so() -> None:
    """Published with generation offshore, which refuses anything the registry
    doesn't vouch for: the six went through, and the store holds no real data."""
    stores = await fresh_stores()
    await seed_into(stores)
    assert not await stores.retrieval.holds_real_customer_data()


async def test_a_second_run_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    stores = await fresh_stores()
    await seed_into(stores)
    before = await stores.fingerprint(SEED)
    stores.watch(monkeypatch)

    again = await seed_into(stores)

    assert again.is_empty and "already holds exactly this seed" in again.describe()
    assert stores.writes == []
    assert await stores.fingerprint(SEED) == before


async def test_a_run_that_stopped_part_way_is_finished_by_running_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = await fresh_stores()
    await seed_into(complete)

    stopped = await fresh_stores()
    recorded = stopped.ledger.record_version
    versions = 0

    async def dies_on_the_fourth_policy(**kwargs: Any) -> None:  # Any: forwards the signature
        nonlocal versions
        if kwargs["tenant_id"] == POLICY_TENANT_ID:
            versions += 1
            if versions == 4:
                raise ConnectionError("the ledger went away")
        await recorded(**kwargs)

    with monkeypatch.context() as broken:
        broken.setattr(stopped.ledger, "record_version", dies_on_the_fourth_policy)
        with pytest.raises(ConnectionError):
            await seed_into(stopped)
    assert len(await stopped.ledger.policy_versions(POLICY_TENANT_ID, as_of=TODAY)) == 3

    finished = await seed_into(stopped)

    assert len(finished.policies) == 3  # the three left, not the six
    assert await stopped.fingerprint(SEED) == await complete.fingerprint(SEED)
    assert (await plan_of(stopped)).is_empty


# ---- refusing, with nothing written ----------------------------------------------------


def _renamed(seed: TenantSeed) -> TenantSeed:
    """The last policy replaced by another document, `acme/other-policy` (which
    needs its own clauses, since a clause carries its instrument)."""
    donor = seed.policies[0]
    moved = tuple(
        ClauseRecord.build(
            instrument_id="acme/other-policy",
            version_id=c.version_id,
            clause_path=c.clause_path,
            text=c.text,
            scope=Scope.TENANT,
            tenant_id=c.tenant_id,
        )
        for c in donor.clauses
    )
    other = dataclasses.replace(donor, instrument_id="acme/other-policy", clauses=moved)
    return with_policies(seed, (*seed.policies[:-1], other))


def _seventh(seed: TenantSeed) -> TenantSeed:
    """The seed and a seventh policy, `acme/other-policy`."""
    return with_policies(seed, (*seed.policies, _renamed(seed).policies[-1]))


DIFFERENT_POLICY_SETS: dict[str, Callable[[TenantSeed], TenantSeed]] = {
    "one policy more": _seventh,
    "one policy fewer": lambda s: with_policies(s, s.policies[:-1]),
    "one policy replaced by another": _renamed,
    "one policy reworded": lambda s: with_policies(
        s, (*s.policies[:2], reworded(s.policies[2]), *s.policies[3:])
    ),
    "one policy retitled": lambda s: with_policies(
        s, (dataclasses.replace(s.policies[0], title="Another title"), *s.policies[1:])
    ),
    "one policy from another source": lambda s: with_policies(
        s, (dataclasses.replace(s.policies[0], source="another source"), *s.policies[1:])
    ),
}


@pytest.mark.parametrize("kind", DIFFERENT_POLICY_SETS)
async def test_a_different_policy_set_is_refused_before_any_write(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = await fresh_stores()
    await seed_into(stores)
    before = await stores.fingerprint(SEED)
    stores.watch(monkeypatch)
    different = DIFFERENT_POLICY_SETS[kind](SEED)

    with pytest.raises(SeedRefused) as refused:
        await seed_into(stores, different)

    assert refused.value.reasons
    assert stores.writes == []
    assert await stores.fingerprint(SEED) == before


async def test_a_seed_with_a_bad_policy_late_in_it_writes_none_of_the_earlier_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal comes from the plan, over every policy, so a good policy
    that comes before the bad one is not published first."""
    stores = await fresh_stores()
    await seed_into(stores, with_policies(SEED, SEED.policies[3:4]))  # one policy held
    stores.watch(monkeypatch)
    # Five policies not yet stored, then the held one reworded.
    mixed = with_policies(SEED, (*SEED.policies[:3], reworded(SEED.policies[3])))

    with pytest.raises(SeedRefused, match=r"holds .* differently|other text"):
        await seed_into(stores, mixed)

    assert stores.writes == []
    assert len(await stores.ledger.policy_versions(POLICY_TENANT_ID, as_of=TODAY)) == 1


async def test_a_policy_the_registry_does_not_vouch_for_is_refused_by_the_plan_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offshore, the lifecycle refuses text the registry doesn't call synthetic.
    It would do so at the last policy, after five were published; the plan asks
    it first, so none are."""
    stores = await fresh_stores()
    stores.watch(monkeypatch)
    real = with_policies(SEED, (*SEED.policies[:-1], reworded(SEED.policies[-1])))

    with pytest.raises(SeedRefused, match="the lifecycle would refuse it"):
        await seed_into(stores, real)

    assert stores.writes == []
    assert await stores.ledger.get_profile(POLICY_TENANT_ID) is None


async def test_a_title_the_lifecycle_would_refuse_is_refused_by_the_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = await fresh_stores()
    stores.watch(monkeypatch)
    untitled = dataclasses.replace(SEED.policies[-1], title="two\nlines")

    with pytest.raises(SeedRefused, match="the lifecycle would refuse it"):
        await seed_into(stores, with_policies(SEED, (*SEED.policies[:-1], untitled)))

    assert stores.writes == []


async def test_the_evidence_carries_the_trace_id_of_the_span_the_seed_ran_in(
    span_exporter: InMemorySpanExporter,
) -> None:
    """The seed is a script, so its span is its request: the trace id on every
    row it writes is that span's, not one made up for it."""
    stores = await fresh_stores()
    await seed_into(stores)

    apply_spans = [s for s in span_exporter.get_finished_spans() if s.name == "tenant_seed.apply"]
    assert len(apply_spans) == 1 and apply_spans[0].context is not None
    trace_id = format(apply_spans[0].context.trace_id, "032x")
    recorded = await stores.ledger.evidence(POLICY_TENANT_ID)
    assert len(recorded) == len(SEED.evidence) > 0
    assert {e.trace_id for e in recorded} == {trace_id} and trace_id.strip("0")


async def test_a_profile_the_tenant_already_has_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = await fresh_stores()
    await stores.ledger.put_profile(Profile(POLICY_TENANT_ID, sectors=("K",)))
    stores.watch(monkeypatch)
    with pytest.raises(SeedRefused, match="profile other than the seed's"):
        await seed_into(stores)
    assert stores.writes == []


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"value": "something else"}, "recorded with other content"),
        ({"evidence_id": "someone-elses-fact"}, r"evidence row\(s\) the seed doesn't carry"),
    ],
    ids=["same id, other content", "an extra row"],
)
async def test_evidence_the_seed_would_contradict_is_refused_before_any_write(
    changes: dict[str, str], reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = await fresh_stores()
    await stores.ledger.put_profile(SEED.profile)
    await stores.ledger.record_evidence(
        dataclasses.replace(SEED.evidence[0], trace_id="a" * 32, **changes)
    )
    stores.watch(monkeypatch)

    with pytest.raises(SeedRefused, match=reason):
        await seed_into(stores)

    assert stores.writes == []
    assert await stores.ledger.subscribed_instruments(POLICY_TENANT_ID) == frozenset()


async def test_a_ledger_without_the_act_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ledger's subscription needs the library instrument to exist, so
    the seed says to load the law first instead of failing after the profile."""
    stores = await fresh_stores(library_loaded=False)
    stores.watch(monkeypatch)

    with pytest.raises(SeedRefused, match="load_compilation_vectors"):
        await seed_into(stores)

    assert stores.writes == []


# ---- the script's face ------------------------------------------------------------------


async def _run(stores: Stores, seed: TenantSeed = SEED, *, dry_run: bool = False) -> int:
    return await run_seed(
        seed,
        ledger=stores.ledger,
        retrieval=stores.retrieval,
        clauses=stores.clauses,
        lifecycle=stores.lifecycle,
        today=TODAY,
        dry_run=dry_run,
    )


async def test_run_seed_says_what_it_did_and_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    stores = await fresh_stores()

    assert await _run(stores) == 0
    assert "6 policy version(s)" in capsys.readouterr().out
    assert await _run(stores) == 0
    assert "already holds exactly this seed" in capsys.readouterr().out


async def test_run_seed_dry_run_writes_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = await fresh_stores()
    stores.watch(monkeypatch)

    code = await _run(stores, dry_run=True)

    assert code == 0 and stores.writes == []
    assert "dry run" in capsys.readouterr().out


async def test_run_seed_names_every_reason_for_a_refusal_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stores = await fresh_stores()
    await seed_into(stores)

    code = await _run(stores, with_policies(SEED, SEED.policies[:-1]))

    out = capsys.readouterr().out
    assert code == 1 and "refused, nothing was written to any store" in out
    assert "a different policy set" in out
