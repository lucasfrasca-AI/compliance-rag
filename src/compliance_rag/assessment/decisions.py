"""What a check's decisions amount to: the state of each claim and verdict, and the tier of
each finding (#301, ADR-0053 F5, ADR-0059 Q3 and C1, D-06).

A claim is `inferred` and is never updated; a finding is `inferred` and is never updated
(ADR-0034). What a member decided sits beside them as append-only rows
(`CheckDecision`), and **this is where they are read**: the current state of a claim or of
a verdict is its latest decision, and a finding's tier is computed here, when it is read,
never stored.

**One tier per finding, the weakest of its inputs** (F5):
- the claim is `asserted` once it is confirmed, and `inferred` otherwise, including when it
  was confirmed and then rejected (latest wins);
- the verdict is `asserted` only when a person has accepted it (latest wins: an acceptance
  withdrawn is `inferred` again), and only a finding that has a verdict can be accepted;
- the finding is `asserted` only when **both** are. A claim confirmation alone leaves the
  finding `inferred`, and so does an acceptance alone.

**A finding derived from an approved mapping** (#306, ADR-0059 C1, `mapping_derivation.py`) has
three inputs, and is as sure as the weakest: its claim; the verdict of the policy finding it
carries over, accepted when a person accepts *that* finding (a derived finding's own verdict is
not accepted separately, the store refuses it); and the mapping, `asserted` by the admin's
approval and no higher. The approval makes only the mapping link certain: it lifts neither
the claim nor the verdict, so a derived finding is never above its claim's tier.

A claim confirmation is not evidence (CONTEXT.md): it creates no evidence row and no
coverage changes. Nothing here reads or writes either. A rejected claim's findings leave
the report (`check_report`), which still names who rejected it.

Pure: it takes the rows and answers, so the report, which reads the check's decisions in
one query, applies it to every claim and finding without another read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from compliance_rag.adapters.interfaces import (
    CheckDecision,
    DecisionKind,
    FindingOutcome,
    FindingRecord,
)
from compliance_rag.assessment.evidence import EvidenceProvenance


MAPPING_TIER = EvidenceProvenance.ASSERTED
"""What an approved mapping is: the admin said so. It is the one input of a derived finding that
an approval raises, and it is the strongest a tier can be, so it never lifts the finding."""


def weakest(*tiers: EvidenceProvenance) -> EvidenceProvenance:
    """The tier of a finding from its inputs' (F5, D-06): `asserted` only when every input is."""
    return (
        EvidenceProvenance.ASSERTED
        if all(t is EvidenceProvenance.ASSERTED for t in tiers)
        else EvidenceProvenance.INFERRED
    )


@dataclass(frozen=True, slots=True)
class Decisions:
    """The latest decision about each claim and each finding of one check."""

    claims: Mapping[int, CheckDecision]
    """By claim number: the latest of its confirmations and rejections."""
    verdicts: Mapping[str, CheckDecision]
    """By finding id: the latest of its acceptances and withdrawals."""

    @classmethod
    def of(cls, rows: Iterable[CheckDecision]) -> Decisions:
        """The state from a check's decisions, oldest first (`LedgerStore.check_decisions`):
        a later row replaces an earlier one about the same claim or finding."""
        claims: dict[int, CheckDecision] = {}
        verdicts: dict[str, CheckDecision] = {}
        for row in rows:
            if row.kind.of_claim:
                assert row.claim_index is not None  # `check_decision_write` and the database
                claims[row.claim_index] = row
            else:
                assert row.finding_id is not None
                verdicts[row.finding_id] = row
        return cls(claims, verdicts)

    def confirmed(self, claim_index: int) -> CheckDecision | None:
        """The confirmation that stands for the claim, or None."""
        latest = self.claims.get(claim_index)
        return latest if latest and latest.kind is DecisionKind.CLAIM_CONFIRMED else None

    def rejected(self, claim_index: int) -> CheckDecision | None:
        """The rejection that stands for the claim, or None."""
        latest = self.claims.get(claim_index)
        return latest if latest and latest.kind is DecisionKind.CLAIM_REJECTED else None

    def accepted(self, finding_id: str) -> CheckDecision | None:
        """The acceptance that stands for the finding's verdict, or None."""
        latest = self.verdicts.get(finding_id)
        return latest if latest and latest.kind is DecisionKind.VERDICT_ACCEPTED else None

    def claim_tier(self, claim_index: int) -> EvidenceProvenance:
        """`asserted` once the claim is confirmed (and not rejected since), else `inferred`."""
        confirmed = self.confirmed(claim_index) is not None
        return EvidenceProvenance.ASSERTED if confirmed else EvidenceProvenance.INFERRED

    def verdict_tier(self, finding: FindingRecord) -> EvidenceProvenance:
        """`asserted` only for a finding that has a verdict and whose verdict a person has
        accepted (and not withdrawn since). A withheld, failed or conditional finding has no
        verdict to accept, so it is always `inferred`."""
        has_verdict = finding.outcome is FindingOutcome.VERDICT  # the one predicate
        # a derived finding's verdict is its source's, so it is the source's acceptance
        # that counts (#306)
        accepted_id = finding.derived_from or finding.finding_id
        accepted = has_verdict and self.accepted(accepted_id) is not None
        return EvidenceProvenance.ASSERTED if accepted else EvidenceProvenance.INFERRED

    def finding_tier(self, finding: FindingRecord) -> EvidenceProvenance:
        """The finding's one tier: the weakest of its inputs (F5, ADR-0059 C1, D-06): its
        claim's, its verdict's and, for a finding derived from an approved mapping, the
        mapping's (`MAPPING_TIER`). `asserted` only when the claim is confirmed **and** the
        verdict is accepted, and a derived finding is no more than its claim."""
        inputs = [self.claim_tier(finding.claim_index), self.verdict_tier(finding)]
        if finding.derived:
            inputs.append(MAPPING_TIER)
        return weakest(*inputs)
