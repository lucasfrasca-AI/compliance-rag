"""Where generation may run, and the gate that stops it running offshore the
moment it would matter (ADR-0023, D-16).

Gemini 3.5 Flash is not served from `australia-southeast1`. During the build
phase — while the corpus is public law plus synthetic data — generation runs
on `gemini-3.5-flash` at the US multi-region endpoint (`us`, ADR-0023,
ADR-0057, which replaced Claude's ADR-0024). Everything else (stores,
telemetry) stays in Sydney unconditionally.

The exception is closed by a gate, not a note, at both ends:

- **Startup and every request.** `assert_generation_allowed` refuses an
  offshore generation region if the retrieval store holds any chunk that
  isn't public law and isn't marked synthetic (tenant or user scope, #166).
  `GeminiGenerator` asks before building a client and again before
  every offshore request, so data written later — by another process, or a
  path that skips `CorpusLifecycle` — still closes it.
- **Ingest.** `assert_upload_allowed` refuses to store a non-public chunk
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
    """Generation would process real customer text outside Australia, or run
    on an endpoint outside the allowed regions."""


def validate_generation_region(region: str) -> str:
    """Only Sydney, or the US multi-region during the build phase. `global`
    and every other region are refused outright."""
    if region not in GENERATION_REGIONS:
        raise GenerationResidencyError(
            f"generation region {region!r} refused: only {sorted(GENERATION_REGIONS)} "
            "are allowed, and never the global endpoint (ADR-0023, ADR-0057)"
        )
    return region


def is_offshore(region: str) -> bool:
    return validate_generation_region(region) != REGION


def is_real_customer_data(chunk: Chunk) -> bool:
    """Not public law, and not marked synthetic: a tenant's documents and one
    person's private additions (`user` scope) alike (#166). Unmarked counts
    as real."""
    return chunk.scope is not Scope.PUBLIC and not chunk.synthetic


def assert_upload_allowed(
    chunks: Iterable[Chunk], *, generation_region: str, real_clause_ids: Iterable[str] = ()
) -> None:
    """`real_clause_ids`: tenant clauses written alongside the chunks that the
    synthetic registry doesn't vouch for (ADR-0026) — real data as much as an
    unmarked chunk is."""
    if not is_offshore(generation_region):
        return
    real = [c.chunk_id for c in chunks if is_real_customer_data(c)] + list(real_clause_ids)
    if real:
        raise GenerationResidencyError(
            f"refusing to store {len(real)} chunk(s) of real customer data "
            f"(first: {real[0]!r}) while generation runs in {generation_region}, outside "
            "Australia. Real customer data "
            "needs AU-resident generation first (ADR-0023; ~$2.2-3k/month via Provisioned "
            "Throughput, SERVICES.md)"
        )


def assert_generation_allowed(*, generation_region: str, holds_real_customer_data: bool) -> None:
    if is_offshore(generation_region) and holds_real_customer_data:
        raise GenerationResidencyError(
            f"refusing to start the generator in {generation_region}: the corpus holds real "
            "(non-public, non-synthetic) data, and generation outside Australia is "
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
    if not _is_tester(asker, testers):
        raise GenerationResidencyError(
            f"generation runs in {generation_region}, outside Australia, during the build "
            "phase, so only a registered build-phase tester's question is sent there: anyone "
            "else's may hold real customer data (ADR-0023, ADR-0032)"
        )


def _is_tester(member: str, testers: AbstractSet[str]) -> bool:
    return member.strip().lower() in {t.strip().lower() for t in testers}


def assert_submission_allowed(
    *, generation_region: str, submitter: str, testers: AbstractSet[str]
) -> None:
    """A document a member submits to be checked is typed or pasted fresh, as a
    question is, and its claims are what the model reads (#297). Offshore, it
    counts as real unless the operator registered its submitter as a build-phase
    tester (ADR-0032), so a non-tester is refused before anything is parsed or
    written, a document byte-identical to a registered fixture included: the
    registry vouches for a fixture, and the tester rule is what lets a person
    send anything at all. `submitter` is the member's email, compared as
    `assert_question_allowed` compares it. Sydney checks nothing."""
    if not is_offshore(generation_region):
        return
    if not _is_tester(submitter, testers):
        raise GenerationResidencyError(
            f"generation runs in {generation_region}, outside Australia, during the build "
            "phase, so only a registered build-phase tester may submit a document to be "
            "checked: anyone else's may hold real customer data (ADR-0023, ADR-0032)"
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
        return cls(settings.generation.region, SyntheticRegistry.load())

    def check_document(self, raw: bytes) -> None:
        """Offshore, a submitted document is synthetic only if its raw bytes are
        a registered submitted fixture's (#297, ADR-0026); this is the
        whole-document half, and `check` over its passages the other. Anything
        else (an excerpt, a reordering, one byte changed) is real data."""
        if is_offshore(self.generation_region) and not self.registry.vouches_for_document(raw):
            raise GenerationResidencyError(
                f"generation runs in {self.generation_region}, outside Australia, during the "
                "build phase, and this submitted document isn't, byte for byte, a registered "
                "synthetic fixture, so it's real data (ADR-0026, ADR-0032)"
            )

    def check_policy_document(self, raw: bytes) -> None:
        """Offshore, a policy an admin uploads is synthetic only if its raw bytes
        are a registered policy fixture's (#303, ADR-0026), as `check_document`
        says for a submission; the chunks and clauses it yields are checked as
        `CorpusLifecycle.publish_version` checks every publish. An excerpt, a
        reordering or one byte changed is real data."""
        if is_offshore(self.generation_region) and not self.registry.vouches_for_policy_document(
            raw
        ):
            raise GenerationResidencyError(
                f"generation runs in {self.generation_region}, outside Australia, during the "
                "build phase, and this policy isn't, byte for byte, a registered synthetic "
                "fixture, so it's real data (ADR-0026, ADR-0032)"
            )

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
