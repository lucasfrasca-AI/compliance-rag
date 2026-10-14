"""Corpus revision across the stores (#178 part 2, ADR-0042): the one path
by which stored library law changes after it is ingested.

`plan` reads what the bucket holds for an instrument version, parses the
source afresh and returns the plan (`corpus/revision.py`): which clauses the
parse changes, adds and removes. Nothing is written.

`correct` applies a plan of the same source to the same version, in place:
refused unless the source is the version's pinned bytes, the anchors are
unchanged, the clause set is the stored one, the acknowledgement names
exactly the changed clauses with who and why, and the findings count citing
them is acknowledged. The graph and Qdrant are checked to agree with the
bucket outside the changed clauses before anything is written. Then, in this
order:

1. **the bucket** — the manifest, with the new hashes and the revision's
   record in one write (the commit point); then the clause objects;
2. **the graph** — under a recorded corpus load (ADR-0034): each changed
   Clause node's `canonical_sha256` and its `corrections` trail, then the
   version's whole structure written again, which is idempotent and refuses
   any node that disagrees with the parse, so the load it records is true;
3. **Qdrant** — the changed chunks re-embedded and written over themselves.

The bucket goes first because the bucket is authoritative and the graph and
Qdrant are derived from it (CONTEXT.md: Derived store): once the record is on
the manifest, the revision has happened, and every later step brings a
derived store up to it. A run that stops after the commit leaves the revision
unfinished; `status` says which stores are behind, and `finish` brings them up
to the recorded revision without a new acknowledgement, because it decides
nothing: it writes the text whose hash the record already holds.

`amend` applies a plan of a new compilation as version and retire (ADR-0021),
through the existing paths: ingest the new version into the bucket and record
the revision on its manifest, load its structure into the graph superseding
the old version, publish its vectors retiring the old version's
(`load_version_vectors`).

This class is the only issuer of the `RevisionAuthorization` the store-level
revision writes require.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from compliance_rag import telemetry
from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.graph_schema import NodeRef
from compliance_rag.adapters.interfaces import (
    ClauseStore,
    GraphAdmin,
    LedgerStore,
    RetrievalAdmin,
    SourceVersion,
)
from compliance_rag.adapters.revision_authorization import (
    RevisionAuthorization,
    _issue,  # pyright: ignore[reportPrivateUsage] — the one sanctioned issuer
)
from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.ingest import ingest_compilation
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    ClauseRecord,
    DocumentClass,
    Scope,
    canonical_sha256,
    stable_id,
)
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.revision import (
    RevisionAcknowledgement,
    RevisionDrift,
    RevisionMode,
    RevisionPlan,
    RevisionRecord,
    RevisionRefused,
    changed_paths,
    check_acknowledgement,
    check_amendment,
    check_correction,
    plan_revision,
    record_of,
)
from compliance_rag.corpus.selection_manifest import SelectionManifest
from compliance_rag.corpus.vector_load import load_version_vectors
from compliance_rag.graph.loader import (
    CheckedVersion,
    check_instrument_version,
    recorded_load,
    version_ref,
    write_instrument_version,
)


@dataclass(frozen=True, slots=True)
class Planned:
    """A plan with what applying it needs: the fresh parse it came from."""

    plan: RevisionPlan
    clauses: tuple[ClauseRecord, ...]
    """The fresh parse, as the selection keeps it."""
    source_html: str
    selection: SelectionManifest


@dataclass(frozen=True, slots=True)
class RevisionStatus:
    """Where the latest revision of a version stands in each store."""

    record: RevisionRecord | None
    """The latest revision on the version's manifest, if any."""
    objects_behind: tuple[str, ...] = ()
    """Clause ids whose bucket object isn't the recorded text yet."""
    graph_behind: tuple[str, ...] = ()
    """Clause ids whose graph node isn't at the recorded hash yet."""
    chunks_behind: tuple[str, ...] = ()
    """Chunk ids whose Qdrant text isn't the recorded text yet."""
    graph_holds_version: bool = False
    retrieval_holds_version: bool = False
    corpus_load_open: bool = False
    """A corpus load is open in the graph: nothing is written to it until it
    finishes or is abandoned (ADR-0034)."""
    corpus_load_blocked: bool = False
    """An abandoned corpus load blocks assessments until a load finishes; a
    revision's graph step is such a load."""

    @property
    def unfinished(self) -> bool:
        return bool(self.objects_behind or self.graph_behind or self.chunks_behind)


@dataclass(frozen=True, slots=True)
class RevisionReport:
    """What one `correct`, `finish` or `amend` call did."""

    record: RevisionRecord
    graph_nodes_corrected: int
    graph_loaded: bool
    """Whether a corpus load was recorded in the graph."""
    chunks_revised: int


def _clause_ref(clause_id: str) -> NodeRef:
    return NodeRef("Clause", clause_id)


class CorpusRevision:
    def __init__(
        self,
        *,
        clauses: ClauseStore,
        graph: GraphAdmin,
        retrieval: RetrievalAdmin,
        ledger: LedgerStore,
        count_tokens: Callable[[str], int],
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        new_revision_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        """`retrieval` and `ledger` are the stores the deployment answers
        from; `count_tokens` is the reranker tokenizer's count, which every
        chunk is cut to (CONTEXT.md: Chunk)."""
        self._clauses = clauses
        self._graph = graph
        self._retrieval = retrieval
        self._ledger = ledger
        self._count_tokens = count_tokens
        self._clock = clock
        self._new_revision_id = new_revision_id

    # ---- the plan: reads only ------------------------------------------------

    async def plan(
        self,
        source_html: str,
        *,
        instrument_id: str,
        version_id: str,
        selection: SelectionManifest,
        stored_version_id: str | None = None,
    ) -> Planned:
        """Parse `source_html` as `version_id`, keep what `selection` keeps,
        and compare it with what the bucket holds for `stored_version_id`
        (`version_id` itself for a correction, the version an amendment
        retires otherwise). Reads the bucket; writes nothing anywhere."""
        stored_version = stored_version_id or version_id
        with telemetry.span(
            "corpus_revision.plan",
            **{
                "revision.version": f"{instrument_id}:{version_id}",
                "revision.against": stored_version,
            },
        ) as span:
            parsed = parse_compilation(
                source_html, instrument_id=instrument_id, version_id=version_id
            )
            fresh = selection.select(parsed.clauses)
            metadata = await self._clauses.get_manifest_metadata(
                instrument_id, stored_version, tenant_id=PUBLIC_TENANT_ID
            )
            if metadata is None:
                raise RevisionRefused(
                    f"{instrument_id}:{stored_version} has no completed ingest: nothing stored "
                    "to revise (ingest_compilation writes a version the first time)"
                )
            manifest = await self._clauses.get_manifest(
                instrument_id, stored_version, tenant_id=PUBLIC_TENANT_ID
            )
            texts: dict[str, str] = {}
            for path in changed_paths(fresh, manifest):
                stored = await self._clauses.get_clause(
                    stable_id(instrument_id, stored_version, path), tenant_id=PUBLIC_TENANT_ID
                )
                if stored is None:
                    raise RevisionDrift(
                        f"{instrument_id}:{stored_version}:{path} is in the manifest but not stored"
                    )
                texts[path] = stored.canonical_text
            plan = plan_revision(
                fresh,
                source_html=source_html,
                annotated_html=parsed.annotated_html,
                stored_version_id=stored_version,
                stored_manifest=manifest,
                stored_texts=texts,
                stored_source_html_sha256=metadata.source_html_sha256,
            )
            span.set_attribute("revision.mode", plan.mode.value)
            span.set_attribute("revision.changed", [c.new_clause_id for c in plan.changed])
            span.set_attribute("revision.added", [c.clause_id for c in plan.added])
            span.set_attribute("revision.removed", [r.clause_id for r in plan.removed])
            return Planned(plan, fresh, source_html, selection)

    async def findings_citing(self, plan: RevisionPlan) -> int:
        """The number an acknowledgement of `plan` must carry."""
        return await self._ledger.count_findings_citing_clauses(plan.cited_before)

    async def status(self, planned: Planned) -> RevisionStatus:
        """Where the version's latest revision stands in each store: which
        clauses each store still holds otherwise. Reads only."""
        plan = planned.plan
        instrument_id, version_id = plan.instrument_id, plan.version_id
        with telemetry.span(
            "corpus_revision.status", **{"revision.version": f"{instrument_id}:{version_id}"}
        ) as span:
            records = await self._clauses.revisions(
                instrument_id, version_id, tenant_id=PUBLIC_TENANT_ID
            )
            in_graph = bool(await self._graph.stored([version_ref(instrument_id, version_id)]))
            loaded = (await self._graph.loaded_sources()).corpus
            texts = await self._retrieval.stored_chunk_texts(
                instrument_id=instrument_id, tenant_id=PUBLIC_TENANT_ID, version_id=version_id
            )
            record = records[-1] if records else None
            objects: list[str] = []
            nodes: list[str] = []
            chunks: list[str] = []
            if record is not None and record.mode is RevisionMode.CORRECT:
                ids = {record.clause_id(c.clause_path): c.sha256 for c in record.corrected}
                stored_nodes = await self._graph.stored([_clause_ref(i) for i in ids])
                for clause_id, sha in sorted(ids.items()):
                    stored = await self._clauses.get_clause(clause_id, tenant_id=PUBLIC_TENANT_ID)
                    if stored is None or canonical_sha256(stored.canonical_text) != sha:
                        objects.append(clause_id)
                    node = stored_nodes.get(_clause_ref(clause_id))
                    if in_graph and (
                        node is None or node.properties.get("canonical_sha256") != sha
                    ):
                        nodes.append(clause_id)
                    text = texts.get(clause_id)
                    if texts and (text is None or canonical_sha256(text) != sha):
                        chunks.append(clause_id)
            status = RevisionStatus(
                record=record,
                objects_behind=tuple(objects),
                graph_behind=tuple(nodes),
                chunks_behind=tuple(chunks),
                graph_holds_version=in_graph,
                retrieval_holds_version=bool(texts),
                corpus_load_open=loaded.loading,
                corpus_load_blocked=loaded.blocked_by_abandoned,
            )
            span.set_attribute("revision.latest", record.revision_id if record else "none")
            span.set_attribute("revision.objects_behind", list(objects))
            span.set_attribute("revision.graph_behind", list(nodes))
            span.set_attribute("revision.chunks_behind", list(chunks))
            return status

    # ---- correct: same source, same version, in place ------------------------

    async def correct(
        self,
        planned: Planned,
        acknowledgement: RevisionAcknowledgement,
        *,
        pinned_source_sha256: str,
        corpus_version: SourceVersion,
        instrument_since: dt.date,
        effective: dt.date,
    ) -> RevisionReport:
        """Apply a parse correction (module docstring). Every refusal comes
        before the first write. `corpus_version` is the commit the fresh parse
        came from (ADR-0034); `instrument_since` and `effective` are the
        version's own dates in the graph, which a correction doesn't change."""
        plan = planned.plan
        with telemetry.span(
            "corpus_revision.correct",
            **{
                "revision.version": f"{plan.instrument_id}:{plan.version_id}",
                "revision.by": acknowledgement.by,
            },
        ) as span:
            check_correction(plan, pinned_source_sha256=pinned_source_sha256)
            status = await self.status(planned)
            if status.unfinished:
                raise RevisionRefused(self._unfinished(status))
            findings = await self.findings_citing(plan)
            span.set_attribute("revision.findings_citing", findings)
            check_acknowledgement(plan, acknowledgement, findings_citing=findings)
            record = record_of(
                plan,
                acknowledgement,
                revision_id=self._new_revision_id(),
                at=self._clock(),
                corpus_sha=corpus_version.sha,
                corpus_tag=corpus_version.tag,
            )
            await self._preflight(planned, record, status)
            span.set_attribute("revision.id", record.revision_id)
            return await self._apply(
                _issue(record),
                planned,
                status,
                corpus_version=corpus_version,
                instrument_since=instrument_since,
                effective=effective,
            )

    async def finish(
        self,
        planned: Planned,
        *,
        corpus_version: SourceVersion,
        instrument_since: dt.date,
        effective: dt.date,
    ) -> RevisionReport | None:
        """Bring every store up to the version's latest revision, if one is
        behind; None if none is. No acknowledgement: the revision is on record
        with its hashes, who and why, and this writes nothing else. Refused
        unless this checkout's parse reproduces the recorded text from the
        recorded source bytes."""
        plan = planned.plan
        with telemetry.span(
            "corpus_revision.finish",
            **{"revision.version": f"{plan.instrument_id}:{plan.version_id}"},
        ) as span:
            status = await self.status(planned)
            record = status.record
            if record is None or not status.unfinished:
                span.set_attribute("revision.unfinished", False)
                return None
            span.set_attribute("revision.id", record.revision_id)
            if plan.source_sha256 != record.source_sha256:
                raise RevisionRefused(
                    f"revision {record.revision_id} was parsed from source sha256 "
                    f"{record.source_sha256}, not {plan.source_sha256}"
                )
            fresh = {c.clause_path: c for c in planned.clauses}
            for path, sha in record.written.items():
                clause = fresh.get(path)
                if clause is None or canonical_sha256(clause.canonical_text) != sha:
                    raise RevisionRefused(
                        f"this checkout's parse doesn't give {record.clause_id(path)} the text "
                        f"revision {record.revision_id} records: finish it from the code it was "
                        f"made with (corpus commit {record.corpus_sha})"
                    )
            await self._preflight(planned, record, status)
            return await self._apply(
                _issue(record),
                planned,
                status,
                corpus_version=corpus_version,
                instrument_since=instrument_since,
                effective=effective,
            )

    @staticmethod
    def _unfinished(status: RevisionStatus) -> str:
        record = status.record
        assert record is not None  # unfinished implies a record
        return (
            f"revision {record.revision_id} ({record.by}, {record.at}: {record.reason}) is "
            f"unfinished — bucket objects behind {list(status.objects_behind)}, graph behind "
            f"{list(status.graph_behind)}, Qdrant behind {list(status.chunks_behind)}. Finish it "
            "first (it needs no acknowledgement: it writes only what it recorded)"
        )

    async def _preflight(
        self, planned: Planned, record: RevisionRecord, status: RevisionStatus
    ) -> None:
        """Before the first write: each derived store holding the version
        agrees with the parse outside the revision, and holds each revised
        clause at its previous or its new hash; and no corpus load is open."""
        if status.graph_holds_version:
            if status.corpus_load_open:
                raise RevisionRefused(
                    "a corpus load is open in the graph: wait for it, or abandon it if it failed "
                    "(load_graph_structure --abandon, #153), then revise"
                )
            await self._check_graph(planned, record)
        if status.retrieval_holds_version:
            await self._check_retrieval(planned, record)

    async def _check_graph(self, planned: Planned, record: RevisionRecord) -> None:
        stored = await self._graph.stored([_clause_ref(c.clause_id) for c in planned.clauses])
        for clause in planned.clauses:
            node = stored.get(_clause_ref(clause.clause_id))
            have = None if node is None else node.properties.get("canonical_sha256")
            _check_derived("graph", clause, None if have is None else str(have), record)

    async def _check_retrieval(self, planned: Planned, record: RevisionRecord) -> None:
        plan = planned.plan
        texts = await self._retrieval.stored_chunk_texts(
            instrument_id=plan.instrument_id, tenant_id=PUBLIC_TENANT_ID, version_id=plan.version_id
        )
        foreign = set(texts) - {c.clause_id for c in planned.clauses}
        if foreign:
            raise RevisionDrift(
                f"Qdrant holds {len(foreign)} chunk(s) of {plan.instrument_id}:{plan.version_id} "
                f"the parse doesn't have (e.g. {min(foreign)}): a different clause set"
            )
        for clause in planned.clauses:
            text = texts.get(clause.clause_id)
            have = None if text is None else canonical_sha256(text)
            _check_derived("Qdrant", clause, have, record)

    async def _apply(
        self,
        authorization: RevisionAuthorization,
        planned: Planned,
        status: RevisionStatus,
        *,
        corpus_version: SourceVersion,
        instrument_since: dt.date,
        effective: dt.date,
    ) -> RevisionReport:
        record = authorization.record
        revised = [c for c in planned.clauses if c.clause_path in record.written]
        with telemetry.span(
            "corpus_revision.apply",
            **{
                "revision.id": record.revision_id,
                "revision.clauses": [c.clause_id for c in revised],
            },
        ) as span:
            # 1. The bucket: the record and the hashes (the commit), then the text.
            await self._clauses.record_revision(authorization, revised)
            # 2. The graph, as a recorded corpus load (ADR-0034).
            corrected, loaded = 0, False
            if status.graph_holds_version and (
                status.record is None
                or status.record.revision_id != record.revision_id
                or status.graph_behind
                or status.corpus_load_blocked
            ):
                check = partial(self._graph_check, planned, record, effective)
                async with recorded_load(self._graph, corpus_version, check=check) as checked:
                    corrected = await self._graph.revise_clauses(authorization)
                    await write_instrument_version(
                        self._graph,
                        planned.clauses,
                        checked,
                        instrument_since=instrument_since,
                        effective=effective,
                        supersedes=None,
                    )
                loaded = True
            # 3. Qdrant: the chunks that don't hold their new text yet.
            chunks_revised = 0
            if status.retrieval_holds_version:
                plan = planned.plan
                texts = await self._retrieval.stored_chunk_texts(
                    instrument_id=plan.instrument_id,
                    tenant_id=PUBLIC_TENANT_ID,
                    version_id=plan.version_id,
                )
                behind = [c for c in revised if texts.get(c.clause_id) != c.canonical_text]
                chunks = chunk_clauses(behind, count_tokens=self._count_tokens)
                await self._retrieval.revise_chunks(authorization, chunks)
                chunks_revised = len(chunks)
            span.set_attribute("revision.graph_nodes_corrected", corrected)
            span.set_attribute("revision.graph_loaded", loaded)
            span.set_attribute("revision.chunks_revised", chunks_revised)
            return RevisionReport(record, corrected, loaded, chunks_revised)

    async def _graph_check(
        self, planned: Planned, record: RevisionRecord, effective: dt.date
    ) -> CheckedVersion:
        """`recorded_load`'s check, run before the load is recorded and again
        under its hold: the graph still agrees (`_check_graph`), and the
        version is loadable as it stands (`check_instrument_version`)."""
        await self._check_graph(planned, record)
        return await check_instrument_version(
            self._graph, planned.clauses, effective=effective, supersedes=None
        )

    # ---- amend: a new compilation, version and retire ------------------------

    async def amend(
        self,
        planned: Planned,
        acknowledgement: RevisionAcknowledgement,
        *,
        pinned_source_sha256: str,
        corpus_version: SourceVersion,
        lifecycle: CorpusLifecycle,
        instrument_since: dt.date,
        effective: dt.date,
    ) -> RevisionReport:
        """Publish the plan's new version and retire the stored one from
        `effective` (ADR-0021), through the existing paths: `ingest_compilation`
        (then the revision's record on the new version's manifest),
        `write_instrument_version` superseding the old version under a
        recorded load, and `load_version_vectors` through `lifecycle`, which
        retires the old version's chunks and ends it in the ledger. Every
        step is idempotent, so re-running the same call finishes one that
        stopped part-way; a record already on the new version is reused."""
        plan = planned.plan
        old = plan.stored_version_id
        with telemetry.span(
            "corpus_revision.amend",
            **{
                "revision.version": f"{plan.instrument_id}:{plan.version_id}",
                "revision.supersedes": old,
                "revision.by": acknowledgement.by,
            },
        ) as span:
            check_amendment(plan, pinned_source_sha256=pinned_source_sha256)
            findings = await self.findings_citing(plan)
            check_acknowledgement(plan, acknowledgement, findings_citing=findings)
            record = record_of(
                plan,
                acknowledgement,
                revision_id=self._new_revision_id(),
                at=self._clock(),
                corpus_sha=corpus_version.sha,
                corpus_tag=corpus_version.tag,
            )
            existing = [
                r
                for r in await self._clauses.revisions(
                    plan.instrument_id, plan.version_id, tenant_id=PUBLIC_TENANT_ID
                )
                if r.mode is RevisionMode.AMEND
            ]
            if existing:
                if (existing[-1].supersedes, existing[-1].clauses) != (old, record.clauses):
                    raise RevisionDrift(
                        f"{plan.instrument_id}:{plan.version_id} already records a different "
                        f"amendment ({existing[-1].revision_id})"
                    )
                record = existing[-1]  # a re-run finishes the amendment on record
            span.set_attribute("revision.id", record.revision_id)
            authorization = _issue(record)
            # 1. The bucket: the new version, then its record.
            await ingest_compilation(
                planned.source_html,
                instrument_id=plan.instrument_id,
                version_id=plan.version_id,
                tenant_id=PUBLIC_TENANT_ID,
                scope=Scope.PUBLIC,
                selection=planned.selection,
                store=self._clauses,
            )
            await self._clauses.record_revision(authorization, ())
            # 2. The graph: the new version supersedes the old, and its clauses.
            check = partial(
                check_instrument_version,
                self._graph,
                planned.clauses,
                effective=effective,
                supersedes=old,
            )
            async with recorded_load(self._graph, corpus_version, check=check) as checked:
                await write_instrument_version(
                    self._graph,
                    planned.clauses,
                    checked,
                    instrument_since=instrument_since,
                    effective=effective,
                    supersedes=old,
                )
            # 3. Qdrant and the ledger: publish the new version, retire the old.
            load = await load_version_vectors(
                planned.clauses,
                count_tokens=self._count_tokens,
                lifecycle=lifecycle,
                retrieval=self._retrieval,
                ledger=self._ledger,
                instrument_id=plan.instrument_id,
                version_id=plan.version_id,
                tenant_id=PUBLIC_TENANT_ID,
                effective_from=effective,
                document_class=DocumentClass.LIBRARY_LAW,
                supersedes=old,
            )
            return RevisionReport(record, 0, True, load.written)


def _check_derived(
    store: str, clause: ClauseRecord, have: str | None, record: RevisionRecord
) -> None:
    """A derived store's hash for one clause is the parse's, or, for a clause
    the revision writes, its previous hash (not yet revised)."""
    new = canonical_sha256(clause.canonical_text)
    if have == new:
        return
    revised: Mapping[str, str | None] = {c.clause_path: c.previous_sha256 for c in record.corrected}
    if clause.clause_path in revised and have == revised[clause.clause_path]:
        return
    if have is None:
        raise RevisionDrift(
            f"the {store} holds the version but not {clause.clause_id}: finish that load first"
        )
    raise RevisionDrift(
        f"the {store} holds {clause.clause_id} at hash {have}, which is neither the parse's "
        f"({new}) nor the stored text the revision replaces: it disagrees with the bucket, and "
        "a person looks before anything is written"
    )
