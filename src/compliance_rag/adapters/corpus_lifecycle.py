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

Publishing takes a version's clauses, not its chunks (#233, ADR-0048): the
clauses are what the clause store holds and what the verifier and the cached
prefix read, and the chunks are derived from them here (`chunk_clauses`, one
per clause), so a chunk's text is its clause's canonical text by
construction. The stores are written in one order, each step idempotent, so a
publish that stopped part-way is finished by `publish_version` (nothing in
retrieval yet) or `complete_version` (some of it there) and then
`record_published_version`:

1. the clause store — the clauses it lacks, then the manifest's source and
   canonicalisation version — the authority the derived stores come from;
2. retrieval — the chunks;
3. the ledger — the version's record, the commit point: until it is written
   the verifier blocks every citation of the version and the cached prefix
   doesn't list it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from compliance_rag import telemetry
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
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import (
    NO_END_DATE,
    PUBLIC_TENANT_ID,
    Chunk,
    ClauseRecord,
    DocumentClass,
    Scope,
    assert_unique_clause_ids,
    canonical_sha256,
    check_document_class,
    check_user_owner,
    instrument_tenant_prefix,
    parse_stable_id,
    sha256_hex,
    validate_title,
)
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry

_CONCURRENT_READS = 16
"""Clause-store reads in flight at once while a publish compares what is
stored with what it was given: one object per clause."""


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
    tenant's instrument — checked before any store is touched.

    A user-scoped record names its owner, and a version has one owner
    (ADR-0036). The models refuse an ownerless user-scoped record when they are
    built, but `model_copy` skips validation, so the rule is checked again here
    where a store is about to hold the record. One owner per version because a
    version is retired, withdrawn and purged as a unit, by tenant and
    instrument and never by owner: a version spanning two people's private
    additions would let either one's removal reach the other's."""
    target = (instrument_id, version_id, tenant_id)
    for chunk in chunks:
        chunk_instrument, chunk_version, _ = parse_stable_id(chunk.clause_id)
        if (chunk_instrument, chunk_version, chunk.tenant_id) != target:
            raise ValueError(f"chunk {chunk.chunk_id!r} is not in {instrument_id}:{version_id}")
        _check_owner(chunk.chunk_id, chunk.scope, chunk.user_id)
    for clause in clauses:
        if (clause.instrument_id, clause.version_id, clause.tenant_id) != target:
            raise ValueError(f"clause {clause.clause_id!r} is not in {instrument_id}:{version_id}")
        _check_owner(clause.clause_id, clause.scope, clause.user_id)
    owners = {(r.scope, r.user_id) for r in (*chunks, *clauses)}
    if len(owners) > 1:
        raise ValueError(
            f"{instrument_id}:{version_id} spans more than one scope or owner: a version is one "
            "document with one owner"
        )


def _check_owner(what: str, scope: Scope, user_id: str | None) -> None:
    try:
        check_user_owner(scope, user_id)
    except ValueError as refused:
        raise ValueError(f"{what!r}: {refused}") from None


def _check_class_scope(
    document_class: DocumentClass, chunks: tuple[Chunk, ...], clauses: tuple[ClauseRecord, ...]
) -> None:
    """An internal policy is the cached prefix of every member's answer (D-11),
    so it can't hold one person's private additions (ADR-0036)."""
    if document_class is DocumentClass.INTERNAL_POLICY and any(
        r.scope is Scope.USER for r in (*chunks, *clauses)
    ):
        raise ValueError(
            "an internal policy is read by every member of the tenant in the cached prefix, so "
            "it can't hold user-scoped (private) text"
        )


def _in_force_from(chunks: tuple[Chunk, ...], effective_from: dt.date) -> tuple[Chunk, ...]:
    return tuple(
        c.model_copy(update={"valid_from": effective_from, "valid_to": NO_END_DATE}) for c in chunks
    )


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
        clauses: tuple[ClauseRecord, ...],
        *,
        count_tokens: Callable[[str], int],
        source_html: str,
        instrument_id: str,
        tenant_id: str,
        version_id: str,
        effective_from: dt.date,
        supersedes: str | None,
        document_class: DocumentClass,
        title: str,
    ) -> None:
        """Publish a new version: its clauses' canonical text and its manifest
        to the clause store, the chunks derived from them (`count_tokens` is
        the reranker tokenizer's count, as `chunk_clauses` takes it) to
        retrieval, in force from `effective_from`, and its record to the
        ledger, retiring `supersedes` from the same date. The old version
        stays where it is — that is what keeps an issued assessment
        verifiable. `source_html` is the document the clauses were parsed
        from, stored beside them (`ClauseStore.put_manifest`).

        `document_class` is the instrument's (#148): it decides whether the
        version joins the tenant's cached policy prefix (D-11). A class that
        disagrees with the instrument's is refused before anything is written.

        `title` is the document's, as the publisher gives it: the front
        matter's `title:` for an operator-registered fixture, a request field
        for an upload. Recorded with the version, it is what a policy citation
        names the document by (ADR-0049). An empty or multi-line one is
        refused before anything is written.

        Refused once retrieval holds any of the version: that is
        `complete_version`'s to finish, or a replace in place. Before then a
        re-run finishes one that stopped in the clause store (module
        docstring), writing nothing the clause store already holds."""
        if supersedes == version_id:
            raise ValueError("a version cannot supersede itself — that is replace-in-place")
        validate_title(title)
        await self._check_class(tenant_id, instrument_id, document_class)
        chunks = self._admitted(
            clauses,
            count_tokens=count_tokens,
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=version_id,
            document_class=document_class,
        )
        if await self._retrieval.version_exists(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        ):
            raise ValueError(
                f"{instrument_id}:{version_id} is already published; a new upload is a new "
                "version_id, and changing this one in place is replace_in_place"
            )
        await self._store_clauses(
            clauses,
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=version_id,
            source_html=source_html,
        )
        await self._retrieval.upsert_chunks(_in_force_from(chunks, effective_from))
        await self._record(
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=version_id,
            effective_from=effective_from,
            supersedes=supersedes,
            document_class=document_class,
            title=title,
        )

    async def complete_version(
        self,
        clauses: tuple[ClauseRecord, ...],
        *,
        count_tokens: Callable[[str], int],
        source_html: str,
        instrument_id: str,
        tenant_id: str,
        version_id: str,
        effective_from: dt.date,
        document_class: DocumentClass,
    ) -> int:
        """Finish a publish that stopped part-way (#144): bring the clause
        store up to `clauses` and `source_html` as `publish_version` does,
        then upsert the chunks of `clauses` retrieval doesn't hold yet, and
        return how many. Refused unless retrieval holds part of the version
        and every chunk it holds is one of `clauses`, so nothing stored is
        written over, in either store: that would be a replace in place. The
        same guardrails as `publish_version`. The ledger isn't touched;
        `record_published_version` records the finished version. Idempotent:
        a second call writes nothing."""
        await self._check_class(tenant_id, instrument_id, document_class)
        chunks = self._admitted(
            clauses,
            count_tokens=count_tokens,
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=version_id,
            document_class=document_class,
        )
        stored = await self._retrieval.version_chunk_ids(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        )
        if not stored:
            raise ValueError(f"{instrument_id}:{version_id} isn't stored; publish it instead")
        foreign = stored - {c.chunk_id for c in chunks}
        if foreign:
            raise ValueError(
                f"{instrument_id}:{version_id} stores {len(foreign)} chunks not among those "
                f"given (e.g. {min(foreign)}): a different clause set, not a partial one"
            )
        await self._store_clauses(
            clauses,
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=version_id,
            source_html=source_html,
        )
        missing = tuple(c for c in chunks if c.chunk_id not in stored)
        await self._retrieval.upsert_chunks(_in_force_from(missing, effective_from))
        return len(missing)

    async def record_published_version(
        self,
        *,
        instrument_id: str,
        tenant_id: str,
        version_id: str,
        effective_from: dt.date,
        supersedes: str | None,
        document_class: DocumentClass,
        title: str,
    ) -> None:
        """Record in the ledger a version whose chunks are already stored: a
        ledger that came after the vectors (#144, #174), or a publish whose
        ledger write never happened. The same class checks as
        `publish_version`; refused if retrieval holds none of the version, or
        the clause store doesn't hold it complete (its manifest's source not
        written): the record is what makes a version citable and puts a
        policy in the cached prefix, and both read the clause store. `title`
        as `publish_version` takes it (ADR-0049)."""
        validate_title(title)
        await self._check_class(tenant_id, instrument_id, document_class)
        if not await self._retrieval.version_exists(
            instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        ):
            raise ValueError(f"{instrument_id}:{version_id} isn't stored, so there is no record")
        if (
            await self._clauses.get_manifest_metadata(
                instrument_id, version_id, tenant_id=tenant_id
            )
            is None
        ):
            raise ValueError(
                f"{instrument_id}:{version_id}: the clause store doesn't hold it complete, so its "
                "citations couldn't verify; finish it with complete_version first"
            )
        await self._record(
            instrument_id=instrument_id,
            tenant_id=tenant_id,
            version_id=version_id,
            effective_from=effective_from,
            supersedes=supersedes,
            document_class=document_class,
            title=title,
        )

    async def _check_class(
        self, tenant_id: str, instrument_id: str, document_class: DocumentClass
    ) -> None:
        check_document_class(tenant_id, document_class)
        recorded = await self._ledger.document_class(
            tenant_id=tenant_id, instrument_id=instrument_id
        )
        if recorded is not None and recorded is not document_class:
            raise ValueError(f"{instrument_id} is {recorded.value}, not {document_class.value}")

    def _admitted(
        self,
        clauses: tuple[ClauseRecord, ...],
        *,
        count_tokens: Callable[[str], int],
        instrument_id: str,
        tenant_id: str,
        version_id: str,
        document_class: DocumentClass,
    ) -> tuple[Chunk, ...]:
        """The version's chunks, derived from its clauses once they are
        checked: one version of one instrument with one owner, each clause
        once, under one canonicalisation (the manifest records one), and
        their scope allowed for the class (ADR-0036). Marked synthetic from
        the registry (never the caller, #78; a clause carries no such flag)
        and allowed by the residency gate (ADR-0023), which counts an
        unregistered clause as real as it does an unregistered chunk."""
        if not clauses:
            raise ValueError(
                f"{instrument_id}:{version_id} has no clauses: a version is published with them"
            )
        _check_payload(
            (), clauses, instrument_id=instrument_id, tenant_id=tenant_id, version_id=version_id
        )
        assert_unique_clause_ids(c.clause_id for c in clauses)
        if len({c.canonicalisation_version for c in clauses}) > 1:
            raise ValueError(
                f"{instrument_id}:{version_id} mixes canonicalisation versions: one parse, one "
                "version"
            )
        _check_class_scope(document_class, (), clauses)
        chunks = self._synthetic_registry.mark(chunk_clauses(clauses, count_tokens=count_tokens))
        assert_upload_allowed(
            chunks,
            generation_region=self._generation_region,
            real_clause_ids=self._synthetic_registry.real_clauses(clauses),
        )
        return chunks

    async def _store_clauses(
        self,
        clauses: tuple[ClauseRecord, ...],
        *,
        instrument_id: str,
        tenant_id: str,
        version_id: str,
        source_html: str,
    ) -> int:
        """Bring the clause store up to this version: write the clauses it
        lacks (`put_clauses`, which lists them on the manifest first), then
        the manifest's source and canonicalisation version, which mark the
        version complete there, as ingest does. Returns how many clauses were
        written.

        Writes nothing it already holds, and never writes over anything: what
        the store holds of the version must be part of what was given,
        identical, with the same source once that is written. Anything else
        is refused before any write — a different document under a stored
        version_id is a new version, or a replace in place. So library law
        that ingest stored (`vector_load`) is checked and left as it is, and
        a publish that stopped part-way is finished by running it again."""
        with telemetry.span(
            "corpus_lifecycle.store_clauses",
            **{
                "lifecycle.version": f"{instrument_id}:{version_id}",
                "lifecycle.clauses": len(clauses),
            },
        ) as span:
            held = await self._clauses.get_manifest(instrument_id, version_id, tenant_id=tenant_id)
            given = {c.clause_path: c for c in clauses}
            foreign = sorted(held.keys() - given.keys())
            if foreign:
                raise ValueError(
                    f"{instrument_id}:{version_id}: the clause store holds {len(foreign)} clauses "
                    f"not among those given (e.g. {foreign[0]}): a different clause set, not a "
                    "partial one"
                )
            # The manifest names a held path's text even where its object isn't
            # written yet, or is another member's private clause, which reads as
            # absent (ADR-0036).
            manifest_differs = sorted(
                p for p, sha in held.items() if sha != canonical_sha256(given[p].canonical_text)
            )
            if manifest_differs:
                raise ValueError(
                    f"{instrument_id}:{version_id}: the clause store's manifest lists "
                    f"{manifest_differs[0]} with different text: changing a stored clause is "
                    "replace_in_place"
                )
            metadata = await self._clauses.get_manifest_metadata(
                instrument_id, version_id, tenant_id=tenant_id
            )
            canonicalisation_version = clauses[0].canonicalisation_version
            if metadata is not None and (
                metadata.source_html_sha256,
                metadata.canonicalisation_version,
            ) != (sha256_hex(source_html), canonicalisation_version):
                raise ValueError(
                    f"{instrument_id}:{version_id} is stored from a different source, or under "
                    "another canonicalisation: a different document is a new version_id"
                )
            stored = await self._read_clauses(clauses)
            object_differs = sorted(c.clause_id for c, s in stored if s is not None and s != c)
            if object_differs:
                raise ValueError(
                    f"the clause store holds {object_differs[0]} differently from the clause "
                    "given: changing a stored clause is replace_in_place"
                )
            missing = tuple(c for c, s in stored if s is None)
            unreadable = sorted(
                c.clause_id for c in missing if c.scope is Scope.USER and c.clause_path in held
            )
            if unreadable:
                # Owner-only reads (ADR-0036) can't tell this owner's unwritten
                # object from another member's private clause on the same path,
                # so a private clause is never written onto a listed path.
                raise ValueError(
                    f"{unreadable[0]} is listed for this version but its owner can't read it: "
                    "publish the private addition under a new version_id"
                )
            if missing and metadata is not None:
                raise ValueError(
                    f"{instrument_id}:{version_id} is complete in the clause store without "
                    f"{missing[0].clause_id}: a complete version's clause set is fixed"
                )
            if missing:
                await self._clauses.put_clauses(missing)
            if metadata is None:
                await self._clauses.put_manifest(
                    instrument_id,
                    version_id,
                    tenant_id=tenant_id,
                    canonicalisation_version=canonicalisation_version,
                    source_html=source_html,
                )
            span.set_attribute("lifecycle.clauses_written", len(missing))
            span.set_attribute("lifecycle.manifest_written", metadata is None)
            return len(missing)

    async def _read_clauses(
        self, clauses: tuple[ClauseRecord, ...]
    ) -> list[tuple[ClauseRecord, ClauseRecord | None]]:
        """Each clause beside what the clause store holds at its id, read as
        its owner (ADR-0036)."""
        reads = asyncio.Semaphore(_CONCURRENT_READS)

        async def read(clause: ClauseRecord) -> ClauseRecord | None:
            async with reads:
                return await self._clauses.get_clause(
                    clause.clause_id, tenant_id=clause.tenant_id, user_id=clause.user_id
                )

        return list(zip(clauses, await asyncio.gather(*map(read, clauses)), strict=True))

    async def _record(
        self,
        *,
        instrument_id: str,
        tenant_id: str,
        version_id: str,
        effective_from: dt.date,
        supersedes: str | None,
        document_class: DocumentClass,
        title: str,
    ) -> None:
        # The ledger is the record the verifier trusts (ADR-0021 mechanism
        # 3): written alongside the payload dates, never derived from them.
        await self._ledger.record_version(
            tenant_id=tenant_id,
            instrument_id=instrument_id,
            version_id=version_id,
            valid_from=effective_from,
            supersedes=supersedes,
            document_class=document_class,
            title=title,
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
        recorded = await self._ledger.document_class(
            tenant_id=tenant_id, instrument_id=instrument_id
        )
        if recorded is not None:
            # With no class recorded the instrument is no policy, so it is not
            # in the cached prefix (the ledger lists the prefix by class).
            _check_class_scope(recorded, chunks, clauses)
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
