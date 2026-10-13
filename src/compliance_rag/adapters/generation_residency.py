"""Where generation may run, and the gate that stops it running offshore the
moment it would matter (ADR-0023, D-16).

Claude on Vertex is not served from `australia-southeast1`. During the build
phase — while the corpus is public law plus synthetic data — generation runs
on `claude-sonnet-5` at the US multi-region endpoint (`us`, ADR-0024). Everything else
(stores, telemetry) stays in Sydney unconditionally.

The exception is closed by a gate, not a note, at both ends:

- **Startup and every request.** `assert_generation_allowed` refuses an
  offshore generation region if the retrieval store holds any tenant-scoped
  chunk not marked synthetic. `VertexClaudeGenerator` asks before building a
  client and again before every offshore request, so data written later — by
  another process, or a path that skips `CorpusLifecycle` — still closes it.
- **Ingest.** `assert_upload_allowed` refuses to store a tenant-scoped chunk
  that is not marked synthetic while generation is offshore, so the first
  real policy upload fails loudly instead of being sent offshore on its first
  question. `CorpusLifecycle` calls it before touching any store.
- **Each answer request** (ADR-0032), for what never passes through the
  store the startup gate asks. `assert_question_allowed` refuses a question
  from a member the operator hasn't registered as a build-phase tester (the
  API). `TenantTextGate` refuses tenant text read from the bucket, the CAG
  prefix or a clause to repair, that the synthetic registry doesn't vouch for
  (`AssessmentPipeline`, which can't be built without one).

The `global` endpoint, and every region not listed here, is refused
outright: it routes requests anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.settings import GENERATION_REGIONS, REGION, Settings


class GenerationResidencyError(RuntimeError):
    """Generation would process real tenant text outside Australia, or run
    on an endpoint outside the allowed regions."""


def validate_generation_region(region: str) -> str:
    """Only Sydney, or the US multi-region during the build phase. `global`
    and every other region are refused outright."""
    if region not in GENERATION_REGIONS:
        raise GenerationResidencyError(
            f"generation region {region!r} refused: only {sorted(GENERATION_REGIONS)} "
            "are allowed, and never the global endpoint (ADR-0023, ADR-0024)"
        )
    return region


def is_offshore(region: str) -> bool:
    return validate_generation_region(region) != REGION


def is_real_tenant_data(chunk: Chunk) -> bool:
    """Tenant-scoped and not marked synthetic. Unmarked counts as real."""
    return chunk.scope is Scope.TENANT and not chunk.synthetic


def assert_upload_allowed(
    chunks: Iterable[Chunk], *, generation_region: str, real_clause_ids: Iterable[str] = ()
) -> None:
    """`real_clause_ids`: tenant clauses written alongside the chunks that the
    synthetic registry doesn't vouch for (ADR-0026) — real data as much as an
    unmarked chunk is."""
    if not is_offshore(generation_region):
        return
    real = [c.chunk_id for c in chunks if is_real_tenant_data(c)] + list(real_clause_ids)
    if real:
        raise GenerationResidencyError(
            f"refusing to store {len(real)} real tenant chunk(s) (first: {real[0]!r}) while "
            f"generation runs in {generation_region}, outside Australia. Real customer data "
            "needs AU-resident generation first (ADR-0023; ~$2.2-3k/month via Provisioned "
            "Throughput, SERVICES.md)"
        )


def assert_generation_allowed(*, generation_region: str, holds_real_tenant_data: bool) -> None:
    if is_offshore(generation_region) and holds_real_tenant_data:
        raise GenerationResidencyError(
            f"refusing to start the generator in {generation_region}: the corpus holds real "
            "(tenant-scoped, non-synthetic) data, and generation outside Australia is "
            "allowed only while it holds public law and synthetic data (ADR-0023)"
        )


def assert_question_allowed(
    *, generation_region: str, asker: str, testers: AbstractSet[str]
) -> None:
    """A member's question is typed fresh, so no registry can vouch for it:
    offshore, it counts as real unless the operator registered its asker as a
    build-phase tester (ADR-0032). `asker` is the member's email; `testers`
    are compared lowercased, as the sign-in verifier compares members. Sydney
    checks nothing."""
    if not is_offshore(generation_region):
        return
    if asker.strip().lower() not in {t.strip().lower() for t in testers}:
        raise GenerationResidencyError(
            f"generation runs in {generation_region}, outside Australia, during the build "
            "phase, so only a registered build-phase tester's question is sent there: anyone "
            "else's may hold real customer data (ADR-0023, ADR-0032)"
        )


@dataclass(frozen=True, slots=True)
class TenantTextGate:
    """Offshore, tenant text that reaches the model by a path the store gate
    can't see must be registered synthetic (ADR-0026, ADR-0032): the CAG
    prefix, and a clause read from the bucket to repair a citation. Both come
    from `ClauseStore`, never from the vector store `assert_generation_allowed`
    asks. A required part of `AssessmentPipeline`, so no caller of `answer()`
    skips it."""

    generation_region: str
    registry: SyntheticRegistry

    def __post_init__(self) -> None:
        validate_generation_region(self.generation_region)

    @classmethod
    def from_settings(cls, settings: Settings) -> TenantTextGate:
        return cls(settings.vertex_claude_region, SyntheticRegistry.load())

    def check(self, tenant_id: str, texts: Iterable[tuple[str, str]], *, what: str) -> None:
        """`texts`: `(clause id, text)` pairs owned by `tenant_id`, about to be
        sent to the model as `what`."""
        if not is_offshore(self.generation_region):
            return
        real = self.registry.unregistered(tenant_id, texts)
        if real:
            raise GenerationResidencyError(
                f"generation runs in {self.generation_region}, outside Australia, during the "
                f"build phase, and {len(real)} {what} (first: {real[0]!r}) isn't registered "
                "synthetic, so it's real data (ADR-0026, ADR-0032)"
            )
