"""Versioning and removal across the corpus stores (ADR-0021, D-18).

Version and retire, for everything: a new version is published next to the
old one and the old one gets `valid_to`; nothing is replaced in place or
deleted on update. Removal is three explicit states — unsubscribe (ledger
only, ADR-0020), withdraw, purge — plus replace-in-place as a "typo fix, not a
policy change" escape hatch.

The guardrails live here, in the adapter, so no UI can bypass them: library
law is refused outright; a purge or replace returns the number of findings
citing the target before acting and refuses unless the caller acknowledges
exactly that number; every outcome is audited. This class is the only issuer
of the `RemovalAuthorization` the store-level deletes require.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from compliance_rag.adapters.generation_residency import (
    assert_upload_allowed,
    validate_generation_region,
)
from compliance_rag.adapters.interfaces import (
    ClauseStore,
    LedgerStore,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
    RetrievalAdmin,
)
from compliance_rag.adapters.removal_authorization import (
    RemovalAuthorization,
    _issue,  # pyright: ignore[reportPrivateUsage] — the one sanctioned issuer
    stable_id_prefix,
)
from compliance_rag.corpus.models import (
    NO_END_DATE,
    PUBLIC_TENANT_ID,
    Chunk,
    ClauseRecord,
    instrument_tenant_prefix,
    parse_stable_id,
)
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry


class RemovalRefused(PermissionError):
    """A removal the guardrails refused. The refusal is already audited."""


@dataclass(frozen=True, slots=True)
class RemovalPreview:
    """What a caller must see before a purge or replace-in-place acts."""

    tenant_id: str
    instrument_id: str
    version_id: str | None
    findings_citing: int


def _check_payload(
    chunks: tuple[Chunk, ...],
    clauses: tuple[ClauseRecord, ...],
    *,
    instrument_id: str,
    tenant_id: str,
    version_id: str,
) -> None:
    """Every chunk and clause belongs to exactly this version of this
    tenant's instrument — checked before any store is touched."""
    target = (instrument_id, version_id, tenant_id)
    for chunk in chunks:
        chunk_instrument, chunk_version, _ = parse_stable_id(chunk.clause_id)
        if (chunk_instrument, chunk_version, chunk.tenant_id) != target:
            raise ValueError(f"chunk {chunk.chunk_id!r} is not in {instrument_id}:{version_id}")
    for clause in clauses:
        if (clause.instrument_id, clause.version_id, clause.tenant_id) != target:
            raise ValueError(f"clause {clause.clause_id!r} is not in {instrument_id}:{version_id}")


def _tenant_owns(tenant_id: str, instrument_id: str) -> bool:
    return tenant_id != PUBLIC_TENANT_ID and instrument_tenant_prefix(instrument_id) == tenant_id


class CorpusLifecycle:
    def __init__(
        self,
        *,
        ledger: LedgerStore,
        retrieval: RetrievalAdmin,
        clauses: ClauseStore,
        generation_region: str,
        synthetic_registry: SyntheticRegistry,
        new_audit_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        """`generation_region` is where answers about this corpus are
        generated: while it is offshore, real tenant data is refused at the
        door (ADR-0023). `synthetic_registry` is the operator's registry of synthetic
        content: every chunk's `synthetic` flag is derived from it, never
        taken from the caller (issue #78). Both required, so no caller
        forgets them."""
        self._generation_region = validate_generation_region(generation_region)
        self._synthetic_registry = synthetic_registry
        self._ledger = ledger
        self._retrieval = retrieval
        self._clauses = clauses
        self._new_audit_id = new_audit_id

    async def publish_version(
        self,
        chunks: tuple[Chunk, ...],
        *,
        instrument_id: str,
        tenant_id: str,
        version_id: str,
        effective_from: dt.date,
        supersedes: str | None,
    ) -> None:
        """Upsert a new version's chunks, in force from `effective_from`, and
        retire `supersedes` from the same date. The old chunks stay where
        they are — that is what keeps an issued assessment verifiable."""
        _check_payload(
            chunks, (), instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        )
        chunks = self._synthetic_registry.mark(chunks)
        assert_upload_allowed(chunks, generation_region=self._generation_region)
        if supersedes == version_id:
            raise ValueError("a version cannot supersede itself — that is replace-in-place")
        if await self._retrieval.version_exists(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        ):
            raise ValueError(
                f"{instrument_id}:{version_id} is already published; a new upload is a new "
                "version_id, and changing this one in place is replace_in_place"
            )
        await self._retrieval.upsert_chunks(
            tuple(
                c.model_copy(update={"valid_from": effective_from, "valid_to": NO_END_DATE})
                for c in chunks
            )
        )
        # The ledger is the record the verifier trusts (ADR-0021 mechanism
        # 3): written alongside the payload dates, never derived from them.
        await self._ledger.record_version(
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            valid_from=effective_from,
            supersedes=supersedes,
        )
        if supersedes is not None:
            # Ledger first: a crash between the two leaves the verifier
            # blocking a version retrieval still returns (fails closed), never
            # accepting one retrieval has already retired.
            await self._ledger.end_version(
                tenant_id=tenant_id,
                instrument_id=instrument_id,
                version_id=supersedes,
                valid_to=effective_from,
            )
            await self._retrieval.retire_version(
                instrument_id=instrument_id,
                tenant_id=tenant_id,
                version_id=supersedes,
                valid_to=effective_from,
            )

    async def preview_removal(
        self, *, tenant_id: str, instrument_id: str, version_id: str | None
    ) -> RemovalPreview:
        return RemovalPreview(
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            findings_citing=await self._ledger.count_findings_citing(
                tenant_id, stable_id_prefix(instrument_id, version_id)
            ),
        )

    async def withdraw(
        self, *, tenant_id: str, instrument_id: str, version_id: str, actor: str, reason: str
    ) -> None:
        """What "delete my policy" does by default: out of retrieval at every
        `as_of`, text and vectors kept, so prior citations still verify."""
        authorization = await self._authorize(
            RemovalAction.WITHDRAW,
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            actor=actor,
            reason=reason,
            findings_citing=None,
        )
        async with self._audited(authorization, RemovalAction.WITHDRAW, actor, reason, None):
            await self._retrieval.withdraw_version(authorization)
            await self._ledger.mark_withdrawn(
                tenant_id=tenant_id,
                instrument_id=instrument_id,
                version_id=version_id,
                withdrawn_by=actor,
            )

    async def purge(
        self,
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str | None,
        actor: str,
        reason: str,
        acknowledged_findings_citing: int,
    ) -> None:
        """Actually delete a tenant-owned instrument (or one version): its
        chunks and every stored object version. The ledger keeps the version
        row with a purged marker. `acknowledged_findings_citing` must equal
        what `preview_removal` returns now — a finding cited in between
        forces a fresh look."""
        count = await self._ledger.count_findings_citing(
            tenant_id, stable_id_prefix(instrument_id, version_id)
        )
        authorization = await self._authorize(
            RemovalAction.PURGE,
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            actor=actor,
            reason=reason,
            findings_citing=count,
            acknowledged=acknowledged_findings_citing,
        )
        async with self._audited(authorization, RemovalAction.PURGE, actor, reason, count):
            await self._retrieval.delete_chunks(authorization)
            await self._clauses.purge(authorization)
            await self._ledger.mark_purged(
                tenant_id=tenant_id,
                instrument_id=instrument_id,
                version_id=version_id,
                purged_by=actor,
                reason=reason,
            )

    async def replace_in_place(
        self,
        chunks: tuple[Chunk, ...],
        clauses: tuple[ClauseRecord, ...],
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str,
        source_html: str,
        actor: str,
        reason: str,
        acknowledged_findings_citing: int,
    ) -> None:
        """A typo fix, not a policy change: the version's stored text and
        chunks are swapped under the same `version_id`. Prior citations to
        this version may stop verifying — hence the same findings
        acknowledgement as purge. Never for library law."""
        if not chunks or not clauses:
            raise ValueError("replace_in_place needs the full replacement: chunks and clauses")
        _check_payload(
            chunks, clauses, instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        )
        chunks = self._synthetic_registry.mark(chunks)
        assert_upload_allowed(
            chunks,
            generation_region=self._generation_region,
            real_clause_ids=self._synthetic_registry.real_clauses(clauses),
        )
        count = await self._ledger.count_findings_citing(
            tenant_id, stable_id_prefix(instrument_id, version_id)
        )
        withdrawn = await self._ledger.is_withdrawn(
            tenant_id=tenant_id, instrument_id=instrument_id, version_id=version_id
        )
        authorization = await self._authorize(
            RemovalAction.REPLACE_IN_PLACE,
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            actor=actor,
            reason=reason,
            findings_citing=count,
            acknowledged=acknowledged_findings_citing,
            target_withdrawn=withdrawn,
        )
        async with self._audited(
            authorization, RemovalAction.REPLACE_IN_PLACE, actor, reason, count
        ):
            await self._retrieval.delete_chunks(authorization)
            await self._clauses.purge(authorization)
            for clause in clauses:
                await self._clauses.put_clause(clause)
            await self._clauses.put_manifest(
                instrument_id,
                version_id,
                tenant_id=tenant_id,
                canonicalisation_version=clauses[0].canonicalisation_version,
                source_html=source_html,
            )
            await self._retrieval.upsert_chunks(chunks)

    async def _authorize(
        self,
        action: RemovalAction,
        *,
        tenant_id: str,
        instrument_id: str,
        version_id: str | None,
        actor: str,
        reason: str,
        findings_citing: int | None,
        acknowledged: int | None = None,
        target_withdrawn: bool = False,
    ) -> RemovalAuthorization:
        refusal: str | None = None
        if not _tenant_owns(tenant_id, instrument_id):
            refusal = (
                f"{instrument_id!r} is library law or another tenant's document; "
                "a tenant unsubscribes from library law, it never removes it"
            )
        elif target_withdrawn:
            refusal = (
                "this version is withdrawn; replacing it in place would put it back into "
                "retrieval — publish a new version instead"
            )
        elif findings_citing is not None and acknowledged != findings_citing:
            refusal = (
                f"{findings_citing} finding(s) cite this target and their citations will "
                f"stop verifying; acknowledged {acknowledged}"
            )
        entry = RemovalAuditEntry(
            audit_id=self._new_audit_id(),
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            action=action,
            outcome=RemovalOutcome.REFUSED if refusal else RemovalOutcome.AUTHORIZED,
            actor=actor,
            reason=reason,
            findings_citing=findings_citing,
            detail=refusal or "",
        )
        await self._ledger.record_removal_audit(entry)
        if refusal is not None:
            raise RemovalRefused(refusal)
        return _issue(
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            audit_id=entry.audit_id,
        )

    @asynccontextmanager
    async def _audited(
        self,
        authorization: RemovalAuthorization,
        action: RemovalAction,
        actor: str,
        reason: str,
        findings_citing: int | None,
    ) -> AsyncGenerator[None]:
        """Audit `completed` or `failed` for an authorised removal, whichever
        happens — the outcome is recorded even when the removal raises."""

        async def record(outcome: RemovalOutcome, detail: str) -> None:
            await self._ledger.record_removal_audit(
                RemovalAuditEntry(
                    audit_id=self._new_audit_id(),
                    tenant_id=authorization.tenant_id,
                    instrument_id=authorization.instrument_id,
                    version_id=authorization.version_id,
                    action=action,
                    outcome=outcome,
                    actor=actor,
                    reason=reason,
                    findings_citing=findings_citing,
                    detail=f"authorized by {authorization.audit_id}{detail}",
                )
            )

        try:
            yield
        except BaseException as exc:
            await record(RemovalOutcome.FAILED, f"; {exc!r}")
            raise
        await record(RemovalOutcome.COMPLETED, "")
