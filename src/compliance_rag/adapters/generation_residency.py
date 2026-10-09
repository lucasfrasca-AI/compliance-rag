"""Where generation may run, and the gate that stops it running offshore the
moment it would matter (ADR-0023, D-16).

Claude on Vertex is not served from `australia-southeast1`. During the build
phase — while the corpus is public law plus synthetic data — generation runs
in `asia-southeast1`, the nearest region that serves it. Everything else
(stores, telemetry) stays in Sydney unconditionally.

The exception is closed by a gate, not a note, at both ends:

- **Startup.** `assert_generation_allowed` refuses an offshore generation
  region if the retrieval store holds any tenant-scoped chunk not marked
  synthetic. `VertexClaudeGenerator.create` calls it before building a client.
- **Ingest.** `assert_upload_allowed` refuses to store a tenant-scoped chunk
  that is not marked synthetic while generation is offshore, so the first
  real policy upload fails loudly instead of being sent offshore on its first
  question. `CorpusLifecycle` calls it before touching any store.

The `global` endpoint, and every region not listed here, is refused
outright: it routes requests anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable

from compliance_rag.corpus.models import Chunk, Scope
from compliance_rag.settings import GENERATION_REGIONS, REGION


class GenerationResidencyError(RuntimeError):
    """Generation would process real tenant text outside Australia, or run
    on an endpoint outside the allowed regions."""


def validate_generation_region(region: str) -> str:
    """Only Sydney, or Singapore during the build phase. `global` and every
    other region are refused outright."""
    if region not in GENERATION_REGIONS:
        raise GenerationResidencyError(
            f"generation region {region!r} refused: only {sorted(GENERATION_REGIONS)} "
            "are allowed, and never the global endpoint (ADR-0023)"
        )
    return region


def is_offshore(region: str) -> bool:
    return validate_generation_region(region) != REGION


def is_real_tenant_data(chunk: Chunk) -> bool:
    """Tenant-scoped and not marked synthetic. Unmarked counts as real."""
    return chunk.scope is Scope.TENANT and not chunk.synthetic


def assert_upload_allowed(chunks: Iterable[Chunk], *, generation_region: str) -> None:
    if not is_offshore(generation_region):
        return
    real = [c.chunk_id for c in chunks if is_real_tenant_data(c)]
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
