"""What Acme, the synthetic tenant, is given in a ledger (#293): the one place
both `scripts/seed_tenant.py` and the local demo server
read it from, so the demo's in-memory ledger and a deployed one hold the same
facts.

- a profile, and a subscription to the Privacy Act (the retrieval filter drops
  all law for a tenant without one);
- `DEMO_PROFILE`'s facts as evidence: the signed applicability set's base Acme
  profile, GA-01;
- the six synthetic policies, version `v1`, each under the title its front
  matter gives, with the Markdown the clauses were parsed from. Whether any of
  it is synthetic is the registry's to say (ADR-0026), not this module's.
"""

from __future__ import annotations

from collections import defaultdict

from compliance_rag.adapters.interfaces import Profile
from compliance_rag.assessment.evidence import Evidence
from compliance_rag.corpus.models import ClauseRecord
from compliance_rag.corpus.tenant_seed import SeedPolicy, TenantSeed
from compliance_rag.eval.applicability import ApplicabilityCase, load_applicability_set
from compliance_rag.eval.golden_corpus import (
    LAW_INSTRUMENT_ID,
    POLICY_INSTRUMENT_IDS,
    POLICY_TENANT_ID,
    POLICY_VERSION_ID,
    policy_source,
    policy_titles,
    synthetic_fixture_clauses,
)
from compliance_rag.eval.harness import GOLDEN_VALID_FROM

DEMO_PROFILE = "GA-01"
"""The applicability set's base Acme profile: every APP applies but the two
that bind only agencies."""
SEEDED_BY = "scripts/seed_tenant.py"


def demo_profile() -> ApplicabilityCase:
    return next(c for c in load_applicability_set().cases if c.id == DEMO_PROFILE)


def acme_evidence() -> tuple[Evidence, ...]:
    """`DEMO_PROFILE`'s facts. The trace id is the run's, set when they are
    recorded (`record_evidence_in_span`)."""
    return demo_profile().evidence()


def acme_policies() -> tuple[SeedPolicy, ...]:
    """The six synthetic policies, each with its own clauses and the Markdown
    they were parsed from, in the order the fixtures are listed."""
    clauses: defaultdict[str, list[ClauseRecord]] = defaultdict(list)
    for clause in synthetic_fixture_clauses():
        clauses[clause.instrument_id].append(clause)
    titles = policy_titles()
    return tuple(
        SeedPolicy(
            instrument_id=instrument_id,
            version_id=POLICY_VERSION_ID,
            title=titles[instrument_id],
            clauses=tuple(clauses[instrument_id]),
            source=policy_source(filename),
        )
        for filename, instrument_id in POLICY_INSTRUMENT_IDS.items()
    )


def acme_seed() -> TenantSeed:
    return TenantSeed(
        tenant_id=POLICY_TENANT_ID,
        profile=Profile(POLICY_TENANT_ID),
        subscriptions=(LAW_INSTRUMENT_ID,),
        evidence=acme_evidence(),
        policies=acme_policies(),
        effective_from=GOLDEN_VALID_FROM,
        subscribed_by=SEEDED_BY,
    )


SEEDS = {POLICY_TENANT_ID: acme_seed}
"""The tenants `scripts/seed_tenant.py` can seed, by tenant id."""
