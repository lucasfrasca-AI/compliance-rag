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
- **Each answer request.** `assert_request_allowed` refuses a question from a
  member the operator hasn't registered as a build-phase tester, and a policy
  prefix holding a clause the synthetic registry doesn't vouch for
  (ADR-0032). Neither passes through the store the startup gate asks.

The `global` endpoint, and every region not listed here, is refused
outright: it routes requests anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet

from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.settings import GENERATION_REGIONS, REGION


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


def assert_request_allowed(
    *,
    generation_region: str,
    member_email: str,
    testers: AbstractSet[str],
    real_prefix_clause_ids: Sequence[str] = (),
) -> None:
    """The two inputs of an answer that never pass through the stores the
    startup gate asks (ADR-0032):
    - **the question.** A member can type anything, so it counts as real
      unless the operator registered the member as a build-phase tester
      (`testers`, compared case-insensitively);
    - **the policy prefix,** read from the bucket. `real_prefix_clause_ids`
      are the clauses in it the synthetic registry doesn't vouch for.
    Checked per request, before the model is called. Sydney checks neither."""
    if not is_offshore(generation_region):
        return
    if member_email.casefold() not in {t.casefold() for t in testers}:
        raise GenerationResidencyError(
            f"generation runs in {generation_region}, outside Australia, during the build "
            "phase, so only a registered build-phase tester's question is sent there: anyone "
            "else's may hold real customer data (ADR-0023, ADR-0032)"
        )
    if real_prefix_clause_ids:
        raise GenerationResidencyError(
            f"generation runs in {generation_region}, outside Australia, during the build "
            f"phase, and {len(real_prefix_clause_ids)} policy clause(s) in this tenant's prefix "
            f"(first: {real_prefix_clause_ids[0]!r}) aren't registered synthetic, so they're "
            "real data (ADR-0026, ADR-0032)"
        )
