"""How an approved policy mapping changes a finding: by derivation, never by prompt
(#306, ADR-0053 F6, ADR-0059 Q7 and C1).

A claim's finding against a policy clause C carries over to the obligation O when the
tenant's admin has approved "C `implements` O". The finding derived for (claim, O) has
the verdict of the claim's finding against C, is cited to both (C's citations, which
were verified when the finding was made, and O's own clause), and points at its source
finding and at the mapping. **Nothing here asks a model anything**: that is what makes
the demonstration deterministic, the human approval being what changes the finding.

- Only an **approved `implements`** mapping derives (`PolicyMapping.derives`). A
  `partially_implements` mapping, a proposed one and a rejected one derive nothing.
- Only a source finding that has a **verdict** carries one over: a withheld, failed or
  conditional finding against C has none, and so derives nothing. A policy clause's
  finding is never conditional (a policy clause is in force or it is not).
- Only for an obligation Track A **decides applies** (`applicable`): one it cannot decide
  has a conditional finding and no verdict to carry into, and one that does not apply is
  not a finding at all (`check_findings_write`).
- **The model's own finding for (claim, O) stays** (ADR-0059 Q7: neither is dropped).
  Where the two disagree the report shows the derived one with the direct one beneath it.

**The tier is C1's, and the approval raises nothing.** The approval makes the mapping link
`asserted`, and that is all it makes so: a derived finding is as sure as its weakest input,
the claim, the policy verdict and the mapping (`Decisions.finding_tier`). So a derived
finding is `inferred` until its claim is confirmed and its source verdict accepted, exactly
as the finding it is derived from is, and **never above its claim's tier**.

Pure: `plan_derivations` is the one rule, used when a findings run makes the derived
findings and again when a report is read to say which approved mappings would change an
older assessment (ADR-0059 Q7), so the two can never disagree about what a mapping does.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Protocol

from compliance_rag.adapters.interfaces import FindingOutcome, FindingRecord, PolicyMapping
from compliance_rag.assessment.coverage import Outcome


@dataclass(frozen=True, slots=True)
class Derivation:
    """One finding an approved mapping derives: from the claim's finding against the
    mapping's policy clause, to the mapping's obligation."""

    source: FindingRecord
    """The claim's finding against the policy clause: a verdict, never itself derived."""
    mapping: PolicyMapping
    """The approved `implements` mapping the derivation goes through."""

    @property
    def claim_index(self) -> int:
        return self.source.claim_index

    @property
    def obligation_id(self) -> str:
        return self.mapping.obligation_id


def plan_derivations(
    findings: Iterable[FindingRecord],
    mappings: Iterable[PolicyMapping],
    applicable: Collection[str],
) -> tuple[Derivation, ...]:
    """The findings the mappings derive from `findings` and have **not yet** derived,
    in claim order, then by obligation and mapping. `applicable` is the obligation ids
    Track A decides apply on the assessment's date. A finding already derived from a
    source through a mapping is not planned again, so applied to a findings run's own
    output (after its derived findings are made) the plan is empty, and applied to an
    older assessment it is what a mapping approved since would add."""
    held = tuple(findings)
    derived = {(f.derived_from, f.mapping_id) for f in held if f.derived_from is not None}
    by_clause: dict[str, list[PolicyMapping]] = {}
    for mapping in mappings:
        if mapping.derives and mapping.obligation_id in applicable:
            by_clause.setdefault(mapping.policy_clause_id, []).append(mapping)
    planned = [
        Derivation(source, mapping)
        for source in held
        if source.policy_clause_id is not None
        and source.outcome is FindingOutcome.VERDICT
        and not source.derived
        for mapping in by_clause.get(source.policy_clause_id, ())
        if (source.finding_id, mapping.mapping_id) not in derived
    ]
    return tuple(
        sorted(planned, key=lambda d: (d.claim_index, d.obligation_id, d.mapping.mapping_id))
    )


class _Decided(Protocol):
    """A coverage row as the ledger keeps it (`CoverageRow`) or as Track A made it
    (`ObligationCoverage`): both say which obligation and how it was decided."""

    @property
    def obligation_id(self) -> str: ...

    @property
    def outcome(self) -> Outcome: ...


def applicable_obligations(rows: Iterable[_Decided]) -> frozenset[str]:
    """The obligation ids Track A decided apply (`Outcome.APPLICABLE`), from coverage rows."""
    return frozenset(r.obligation_id for r in rows if r.outcome is Outcome.APPLICABLE)
