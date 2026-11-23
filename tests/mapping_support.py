"""What the policy-mapping tests share (#305): Acme's six synthetic policies in the ledger
and the clause store, Track A over the signed mappings (so the obligation digest is the
real one), a proposer over a scripted model, and the replies a model might give: an honest
one and the ones a compromised or careless one would. Nothing here calls a model.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace

from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import Generation, ReplyKind
from compliance_rag.assessment.evidence import Facts
from compliance_rag.assessment.mapping_proposals import MappingProposer, Proposed
from compliance_rag.assessment.obligation_digest import ObligationDigest
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.assessment.prompt import SourceClause
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.harness import golden_clause_store
from compliance_rag.settings import REGION
from compliance_rag.telemetry import current_trace_id, span
from tests.claims_support import Prompt, ScriptedGenerator
from tests.findings_support import (
    DAY,
    TENANT,
    evidence_of,
    golden_world_ledger,
    graph_over_mappings,
)

MODEL = "gemini-3.5-flash"
CONFIGURED_MODEL = "the-configured-model"

SECURITY = ("acme/data-security-policy", "v1")
"""The policy version proposed for."""
SEC_3_1 = "acme/data-security-policy:v1:3/1"
SEC_3_2 = "acme/data-security-policy:v1:3/2"
SEC_4_1 = "acme/data-security-policy:v1:4/1"
OTHER_POLICY = "acme/privacy-policy:v1:3/2"
"""A clause in the prefix that is another policy's, not the version's."""

APP11_1 = "OB-PA-APP11-1"
APP11_2 = "OB-PA-APP11-2"
APP8_1 = "OB-PA-APP8-1"

RATIONALE = "Section 3.1 requires the controls this obligation asks for."


def item(
    obligation: str,
    clause: str,
    kind: str = "implements",
    rationale: str = RATIONALE,
) -> dict[str, object]:
    """One proposal as the model replies it."""
    return {
        "obligation_id": obligation,
        "policy_clause_id": clause,
        "kind": kind,
        "rationale": rationale,
    }


def reply(*items: dict[str, object]) -> str:
    return json.dumps({"proposals": list(items)})


def honest(seen: Prompt) -> str:
    """What an honest model replies: one clause implements APP 11.1, another partly does."""
    del seen
    return reply(
        item(APP11_1, SEC_3_1),
        item(APP11_2, SEC_3_2, "partially_implements", "Section 3.2 covers retention only."),
    )


class NamedModel(ScriptedGenerator):
    """A scripted model that names itself and has a cost, as a real adapter's
    `Generation` does."""

    def __init__(
        self, script: str | Callable[[Prompt], str] = honest, *, model: str = MODEL
    ) -> None:
        super().__init__(script)
        self._model = model

    async def generate(
        self,
        *,
        system: str,
        cached_prefix: str | None,
        user: str,
        max_tokens: int,
        reply: ReplyKind = ReplyKind.ANSWER,
    ) -> Generation:
        made = await super().generate(
            system=system,
            cached_prefix=cached_prefix,
            user=user,
            max_tokens=max_tokens,
            reply=reply,
        )
        return replace(made, model=self._model, cost_usd=0.0123)


@dataclass
class ProposerWorld:
    ledger: FakeLedgerStore
    policies: tuple[SourceClause, ...]
    digest: ObligationDigest
    model: ScriptedGenerator
    proposer: MappingProposer

    async def propose(
        self,
        version: tuple[str, str] = SECURITY,
        *,
        policies: tuple[SourceClause, ...] | None = None,
    ) -> Proposed:
        """One run as the endpoint makes it: reserved under the request's trace, then proposed."""
        with span("test.request"):
            held_by = current_trace_id()
            await self.ledger.reserve_mapping_run(TENANT, *version, trace_id=held_by)
            return await self.proposer.propose(
                tenant_id=TENANT,
                instrument_id=version[0],
                version_id=version[1],
                digest=self.digest,
                policies=self.policies if policies is None else policies,
            )


async def proposer_world(
    model: ScriptedGenerator | None = None, *, region: str = REGION
) -> ProposerWorld:
    ledger = await golden_world_ledger(evidence_of("GA-05"))
    clauses = await golden_clause_store()
    coverage = await graph_over_mappings().coverage(
        Facts.of(await ledger.evidence(TENANT)), tenant_id=TENANT, as_of=DAY
    )
    digest = ObligationDigest.of(coverage)
    assert {APP11_1, APP11_2, APP8_1} <= digest.ids, "the fixture's obligations are in the digest"
    policies = await tenant_policies(ledger, clauses, tenant_id=TENANT, as_of=DAY)
    scripted = model or NamedModel()
    return ProposerWorld(
        ledger,
        policies,
        digest,
        scripted,
        MappingProposer(
            scripted,
            ledger,
            tenant_text=TenantTextGate(region, SyntheticRegistry.load()),
            model=CONFIGURED_MODEL,
        ),
    )
