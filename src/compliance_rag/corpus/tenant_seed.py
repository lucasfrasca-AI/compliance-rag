"""Give a tenant in a fresh ledger what its members' questions need (#293): its
profile, its subscriptions to library law, its dated evidence and its policies,
published. `eval/acme_seed.py` says what Acme is given; this module is how any
seed is applied.

Every write goes through `LedgerStore` and `CorpusLifecycle`, so a policy is
chunked, marked synthetic by the registry alone (ADR-0026) and gated for
residency (ADR-0023) as an upload is, and no store is written to directly. The
lifecycle is driven by `load_version_vectors`, which finishes a publish that
stopped part-way, so a run that died after three policies is finished by
running it again.

**Idempotent and refusing, never overwriting.** `plan_seed` reads and writes
nothing. It finds what is missing, and refuses, with every reason, when the
tenant already holds something this seed would contradict:

- a profile that isn't the seed's;
- evidence the seed doesn't carry, or the seed's own id recorded with other
  content (evidence is append-only, so it can't be corrected here);
- a policy the seed doesn't carry in force for the tenant, or one of the seed's
  recorded, stored or published differently: another title or start date,
  chunks or clauses that aren't the ones given, another source.

`apply_seed` runs the plan first, so a refused seed has written nothing, to any
store. A tenant that already holds exactly the seed gets an empty plan and no
write. The law version record is not the seed's job: it is
`scripts/load_compilation_vectors.py`'s, and it comes first, because the ledger
refuses a subscription to a library instrument it doesn't hold. A seed whose
subscription names one the ledger lacks is refused with that said, before any
write, rather than failing after the profile was written.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from compliance_rag import telemetry
from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.interfaces import ClauseStore, LedgerStore, Profile, RetrievalAdmin
from compliance_rag.assessment.evidence import Evidence
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    ClauseRecord,
    DocumentClass,
    canonical_sha256,
    sha256_hex,
    validate_tenant_id,
)
from compliance_rag.corpus.vector_load import load_version_vectors

_CONCURRENT_READS = 16


class SeedRefused(RuntimeError):
    """The tenant holds something the seed contradicts. Nothing was written."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


@dataclass(frozen=True, slots=True)
class SeedPolicy:
    """One policy, one version: its clauses and the source they were parsed
    from, stored beside them (`ClauseStore.put_manifest`), and the title the
    publisher gives it (ADR-0049)."""

    instrument_id: str
    version_id: str
    title: str
    clauses: tuple[ClauseRecord, ...]
    source: str


@dataclass(frozen=True, slots=True)
class TenantSeed:
    tenant_id: str
    profile: Profile
    subscriptions: tuple[str, ...]
    """Library instrument ids."""
    evidence: tuple[Evidence, ...]
    policies: tuple[SeedPolicy, ...]
    effective_from: dt.date
    """When the policies come into force."""
    subscribed_by: str

    def __post_init__(self) -> None:
        validate_tenant_id(self.tenant_id)
        if self.profile.tenant_id != self.tenant_id:
            raise ValueError("the seed's profile is another tenant's")
        if any(e.tenant_id != self.tenant_id for e in self.evidence):
            raise ValueError("the seed carries another tenant's evidence")
        ids = [e.evidence_id for e in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("the seed carries one evidence id twice")
        versions = [(p.instrument_id, p.version_id) for p in self.policies]
        if len(versions) != len(set(versions)):
            raise ValueError("the seed carries one policy version twice")
        for policy in self.policies:
            if not policy.clauses:
                raise ValueError(f"{policy.instrument_id} has no clauses")
            for clause in policy.clauses:
                if (clause.tenant_id, clause.instrument_id, clause.version_id) != (
                    self.tenant_id,
                    policy.instrument_id,
                    policy.version_id,
                ):
                    raise ValueError(f"{clause.clause_id} isn't in {policy.instrument_id}")


@dataclass(frozen=True, slots=True)
class SeedPlan:
    """What a seed would write to a tenant as it stands; empty when the tenant
    already holds exactly the seed."""

    profile: bool = False
    subscriptions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    """Evidence ids."""
    policies: tuple[str, ...] = ()
    """`instrument_id:version_id` of each policy not yet fully published."""

    @property
    def is_empty(self) -> bool:
        return not (self.profile or self.subscriptions or self.evidence or self.policies)

    def describe(self) -> str:
        if self.is_empty:
            return "nothing to write: the tenant already holds exactly this seed"
        parts = [
            "the profile" if self.profile else "",
            f"{len(self.subscriptions)} subscription(s): {', '.join(self.subscriptions)}"
            if self.subscriptions
            else "",
            f"{len(self.evidence)} evidence row(s)" if self.evidence else "",
            f"{len(self.policies)} policy version(s): {', '.join(self.policies)}"
            if self.policies
            else "",
        ]
        return "; ".join(p for p in parts if p)


def _same_evidence(a: Evidence, b: Evidence) -> bool:
    """Whether two rows are one fact: everything the seed says. Not
    `recorded_at`, which the ledger sets."""
    return (
        a.tenant_id,
        a.evidence_id,
        a.field,
        a.value,
        a.period,
        a.provenance,
        a.source,
        a.asserted_by,
        a.supersedes,
        a.trace_id,
        a.document_id,
    ) == (
        b.tenant_id,
        b.evidence_id,
        b.field,
        b.value,
        b.period,
        b.provenance,
        b.source,
        b.asserted_by,
        b.supersedes,
        b.trace_id,
        b.document_id,
    )


async def _plan_policy(
    policy: SeedPolicy,
    seed: TenantSeed,
    *,
    ledger: LedgerStore,
    retrieval: RetrievalAdmin,
    clauses: ClauseStore,
    count_tokens: Callable[[str], int],
    refusals: list[str],
) -> bool:
    """Whether the policy still has something to publish, and any way it
    contradicts what is stored, by reading all three stores."""
    name = f"{policy.instrument_id}:{policy.version_id}"
    tenant_id = seed.tenant_id
    expected = frozenset(
        c.chunk_id for c in chunk_clauses(policy.clauses, count_tokens=count_tokens)
    )

    stored = await retrieval.version_chunk_ids(
        instrument_id=policy.instrument_id, tenant_id=tenant_id, version_id=policy.version_id
    )
    if stored - expected:
        refusals.append(
            f"{name}: retrieval holds {len(stored - expected)} chunk(s) that aren't this "
            f"policy's (e.g. {min(stored - expected)})"
        )

    recorded = await ledger.version_validity(
        tenant_id=tenant_id, instrument_id=policy.instrument_id, version_id=policy.version_id
    )
    if recorded is not None:
        if recorded[0] != seed.effective_from:
            refusals.append(
                f"{name}: the ledger has it in force from {recorded[0]}, not {seed.effective_from}"
            )
        recorded_class = await ledger.document_class(
            tenant_id=tenant_id, instrument_id=policy.instrument_id
        )
        if recorded_class is not DocumentClass.INTERNAL_POLICY:
            refusals.append(f"{name}: the ledger has it as {recorded_class}, not a policy")
        titles = await ledger.version_titles(tenant_id, [(policy.instrument_id, policy.version_id)])
        title = titles.get((policy.instrument_id, policy.version_id))
        if title != policy.title:
            refusals.append(f"{name}: the ledger titles it {title!r}, not {policy.title!r}")

    manifest = await clauses.get_manifest(
        policy.instrument_id, policy.version_id, tenant_id=tenant_id
    )
    given = {c.clause_path: c for c in policy.clauses}
    if manifest.keys() - given.keys():
        refusals.append(
            f"{name}: the clause store lists {len(manifest.keys() - given.keys())} clause(s) the "
            f"policy hasn't (e.g. {min(manifest.keys() - given.keys())})"
        )
    changed = sorted(
        p
        for p, sha in manifest.items()
        if p in given and sha != canonical_sha256(given[p].canonical_text)
    )
    if changed:
        refusals.append(f"{name}: the clause store has other text at {changed[0]}")
    metadata = await clauses.get_manifest_metadata(
        policy.instrument_id, policy.version_id, tenant_id=tenant_id
    )
    if metadata is not None and (
        metadata.source_html_sha256,
        metadata.canonicalisation_version,
    ) != (sha256_hex(policy.source), policy.clauses[0].canonicalisation_version):
        refusals.append(f"{name}: the clause store holds it from another source")
    reads = asyncio.Semaphore(_CONCURRENT_READS)

    async def read(clause: ClauseRecord) -> ClauseRecord | None:
        async with reads:
            return await clauses.get_clause(clause.clause_id, tenant_id=tenant_id)

    objects = await asyncio.gather(*(read(c) for c in policy.clauses))
    differing = sorted(
        c.clause_id
        for c, o in zip(policy.clauses, objects, strict=True)
        if o is not None and o != c
    )
    if differing:
        refusals.append(f"{name}: the clause store holds {differing[0]} differently")

    complete = stored == expected and recorded is not None and metadata is not None
    return not complete


async def plan_seed(
    seed: TenantSeed,
    *,
    ledger: LedgerStore,
    retrieval: RetrievalAdmin,
    clauses: ClauseStore,
    count_tokens: Callable[[str], int],
    today: dt.date,
) -> SeedPlan:
    """What `apply_seed` would write, found by reading only. Raises
    `SeedRefused`, naming every reason, if the tenant holds anything the seed
    contradicts (module docstring)."""
    tenant_id = seed.tenant_id
    refusals: list[str] = []
    with telemetry.span("tenant_seed.plan", **{"seed.tenant_id": tenant_id}):
        profile = await ledger.get_profile(tenant_id)
        if profile is not None and profile != seed.profile:
            refusals.append("the tenant has a profile other than the seed's")

        held = await ledger.subscribed_instruments(tenant_id)
        missing_subscriptions = tuple(i for i in seed.subscriptions if i not in held)
        if missing_subscriptions:
            library = await ledger.environment_titles(PUBLIC_TENANT_ID, as_of=today)
            absent = [i for i in missing_subscriptions if i not in library]
            if absent:
                refusals.append(
                    f"the ledger holds no version of {', '.join(absent)} in force on {today}, "
                    "and a tenant can only subscribe to library law it holds: run "
                    "scripts/load_compilation_vectors.py against this ledger first"
                )

        recorded_evidence = {e.evidence_id: e for e in await ledger.evidence(tenant_id)}
        given_evidence = {e.evidence_id: e for e in seed.evidence}
        extra = sorted(recorded_evidence.keys() - given_evidence.keys())
        if extra:
            refusals.append(
                f"the tenant holds {len(extra)} evidence row(s) the seed doesn't carry "
                f"(e.g. {extra[0]})"
            )
        for evidence_id in sorted(recorded_evidence.keys() & given_evidence.keys()):
            if not _same_evidence(recorded_evidence[evidence_id], given_evidence[evidence_id]):
                refusals.append(
                    f"evidence {evidence_id} is recorded with other content, and evidence is "
                    "append-only"
                )
        missing_evidence = tuple(
            e.evidence_id for e in seed.evidence if e.evidence_id not in recorded_evidence
        )

        given_versions = {(p.instrument_id, p.version_id) for p in seed.policies}
        try:
            in_force = await ledger.policy_versions(
                tenant_id, as_of=max(today, seed.effective_from)
            )
        except ValueError as unclassified:
            refusals.append(str(unclassified))
            in_force = ()
        foreign = sorted(
            f"{p.instrument_id}:{p.version_id}"
            for p in in_force
            if (p.instrument_id, p.version_id) not in given_versions
        )
        if foreign:
            refusals.append(
                f"the tenant has {len(foreign)} policy version(s) in force the seed doesn't "
                f"carry (e.g. {foreign[0]}): a different policy set"
            )

        unpublished: list[str] = []
        for policy in seed.policies:
            if await _plan_policy(
                policy,
                seed,
                ledger=ledger,
                retrieval=retrieval,
                clauses=clauses,
                count_tokens=count_tokens,
                refusals=refusals,
            ):
                unpublished.append(f"{policy.instrument_id}:{policy.version_id}")

        if refusals:
            raise SeedRefused(refusals)
        return SeedPlan(
            profile=profile is None,
            subscriptions=missing_subscriptions,
            evidence=missing_evidence,
            policies=tuple(unpublished),
        )


async def apply_seed(
    seed: TenantSeed,
    *,
    ledger: LedgerStore,
    lifecycle: CorpusLifecycle,
    retrieval: RetrievalAdmin,
    clauses: ClauseStore,
    count_tokens: Callable[[str], int],
    today: dt.date,
) -> SeedPlan:
    """Plan, then write what the plan says. `lifecycle` must be the one over
    this `ledger`, `retrieval` and `clauses`. Returns what was written.
    A refused seed (`SeedRefused`) has written nothing."""
    plan = await plan_seed(
        seed,
        ledger=ledger,
        retrieval=retrieval,
        clauses=clauses,
        count_tokens=count_tokens,
        today=today,
    )
    with telemetry.span("tenant_seed.apply", **{"seed.tenant_id": seed.tenant_id}) as span:
        if plan.profile:
            await ledger.put_profile(seed.profile)
        for instrument_id in plan.subscriptions:
            await ledger.subscribe(
                tenant_id=seed.tenant_id,
                instrument_id=instrument_id,
                subscribed_by=seed.subscribed_by,
            )
        for evidence in seed.evidence:
            if evidence.evidence_id in plan.evidence:
                await ledger.record_evidence(evidence)
        for policy in seed.policies:
            if f"{policy.instrument_id}:{policy.version_id}" not in plan.policies:
                continue
            await load_version_vectors(
                policy.clauses,
                source_html=policy.source,
                count_tokens=count_tokens,
                lifecycle=lifecycle,
                retrieval=retrieval,
                ledger=ledger,
                instrument_id=policy.instrument_id,
                version_id=policy.version_id,
                tenant_id=seed.tenant_id,
                effective_from=seed.effective_from,
                document_class=DocumentClass.INTERNAL_POLICY,
                title=policy.title,
            )
        span.set_attribute("seed.policies_published", len(plan.policies))
        return plan
