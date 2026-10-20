"""Revise the ingested compilation from a fresh parse of its pinned source
(#178 part 2, ADR-0042): the one sanctioned way stored library law changes.

Without `--apply` it writes nothing. It parses the pinned source, compares it
with what the corpus bucket holds, clause by clause, and prints the plan (each
changed clause's id, its old and new opening words), the number of findings
citing those clauses, where the latest revision stands in each store, and the
exact acknowledgement an `--apply` needs:

    uv run python -m scripts.revise_compilation \\
        --bucket compliance-rag-508904-corpus [--html path/to/document_1.html] [--show-text]

`--apply --mode correct` applies a parse correction in place: the bucket's
clause objects and manifest hashes (with the revision's record on the
manifest), the graph's Clause nodes (with a trail entry on each) under a
recorded corpus load, and the changed chunks in Qdrant, re-embedded. A clause
the parse marks incomplete, or no longer does (ADR-0047), is a change too: its
state is set in each store, with no re-embedding when its text is the same.
Only when the acknowledgement names exactly the changed clauses:

    uv run python -m scripts.revise_compilation --bucket compliance-rag-508904-corpus \\
        --html path/to/document_1.html --apply --mode correct \\
        --acknowledge CLAUSE_ID [CLAUSE_ID ...] --by NAME --reason WHY \\
        --acknowledge-findings N

`--apply --finish` brings every store up to an unfinished revision (one that
stopped part-way); it needs no acknowledgement, because it writes only what
the revision recorded. `--mode amend` (a new compilation, a new version) is
built and tested in `CorpusRevision.amend`, but there is no second compilation
to wire it to yet (Phase 4, `AMEND_ISSUE`).

The findings count is the configured ledger's (DATABASE_URL). No ledger is
deployed yet (#174); `--no-ledger` says so, and the count is then 0, which
must still be acknowledged. It is refused if the ledger is reachable.

Reads the bucket (`--bucket`, or `CORPUS_BUCKET`), Neo4j, Qdrant and (to
embed, on `--apply`) Vertex in australia-southeast1 through `Settings`, and
never prints a URL, a key or a password. Writes to existing resources only;
creates none. Never run in CI.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from compliance_rag.adapters.corpus_revision import CorpusRevision, PlannedRevision, RevisionStatus
from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.git_source_versions import UncommittedSource, resolve_source_version
from compliance_rag.adapters.interfaces import Embedder, LedgerStore, SourceKind, SourceVersion
from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.adapters.postgres_ledger_store import LedgerUnreachable, PostgresLedgerStore
from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
from compliance_rag.adapters.vertex_embedder import VertexEmbedder
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.revision import (
    RevisionAcknowledgement,
    RevisionDrift,
    RevisionPlan,
    RevisionRefused,
    described,
    opening_words,
)
from compliance_rag.corpus.selection_manifest import parse_selection_manifest
from compliance_rag.settings import Settings
from scripts.ingest_compilation import (
    INSTRUMENT_ID,
    MANIFEST,
    SOURCE_SHA256,
    VERSION_ID,
    pinned_source,
)
from scripts.load_graph_structure import EFFECTIVE, INSTRUMENT_SINCE

REPO = Path(__file__).resolve().parents[1]
AMEND_ISSUE = "#198"
"""What `--mode amend` still needs: a second pinned compilation and its
reviewed selection, the operator wiring, and the mappings re-grounded on the
new version's clauses (ADR-0028)."""


class _NoEmbedding(Embedder):
    """A dry run reads and never embeds: an embedding call here is a bug."""

    def __init__(self, dimensions: int) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        raise AssertionError("a dry run embedded something")


def render_plan(plan: RevisionPlan, *, show_text: bool = False) -> str:
    """The plan as a person reads it: one row per changed clause, with its
    old and new opening words (from just before the first difference, when
    they open alike), then what is added or removed."""
    lines = [
        f"{plan.instrument_id}:{plan.stored_version_id} -> {plan.version_id} ({plan.mode.value}): "
        f"{len(plan.changed)} changed, {len(plan.added)} added, {len(plan.removed)} removed, "
        f"{plan.unchanged} unchanged",
        f"source sha256 {plan.source_sha256}; anchored source.html "
        + (
            "identical to the stored one"
            if plan.source_html_sha256 == plan.stored_source_html_sha256
            else f"DIFFERS ({plan.source_html_sha256} vs {plan.stored_source_html_sha256})"
        ),
    ]
    if plan.changed:
        rows = [("clause id", "old (opening words)", "new (opening words)")]
        rows += [(c.new_clause_id, *opening_words(c.old_text, c.new_text)) for c in plan.changed]
        widths = [max(len(r[i]) for r in rows) for i in range(2)]
        lines.append("")
        for row in rows:
            lines.append(f"{row[0]:<{widths[0]}} | {row[1]:<{widths[1]}} | {row[2]}")
    restated = [c for c in plan.changed if c.incompleteness_changed]
    if restated:
        lines.append("")
        lines.append("incompleteness (ADR-0047), stored -> parsed:")
        for change in restated:
            text = "text changes too" if change.text_changed else "text unchanged"
            lines.append(
                f"  {change.new_clause_id}: {described(change.old_incomplete)} -> "
                f"{described(change.new_incomplete)} ({text})"
            )
    for clause in plan.added:
        lines.append(f"added   {clause.clause_id}: {clause.canonical_text[:80]}")
    for removed in plan.removed:
        lines.append(f"removed {removed.clause_id}")
    if show_text:
        for change in plan.changed:
            lines += [
                "",
                change.new_clause_id,
                f"  old: {change.old_text}",
                f"  new: {change.new_text}",
            ]
    return "\n".join(lines)


def render_status(status: RevisionStatus) -> str:
    stores = (
        f"graph {'holds' if status.graph_holds_version else 'does not hold'} the version"
        + (" (a corpus load is OPEN)" if status.corpus_load_open else "")
        + (" (an abandoned corpus load blocks assessments)" if status.corpus_load_blocked else "")
        + f"; Qdrant {'holds' if status.retrieval_holds_version else 'does not hold'} it"
    )
    record = status.record
    if record is None:
        return f"no revision on record; {stores}"
    state = "UNFINISHED" if status.unfinished else "finished"
    lines = [
        f"latest revision {record.revision_id} ({record.mode.value}, {record.at}, by "
        f"{record.by}: {record.reason}): {state}; {stores}"
    ]
    if status.unfinished:
        lines.append(f"  bucket objects behind: {list(status.objects_behind)}")
        lines.append(f"  graph nodes behind:    {list(status.graph_behind)}")
        lines.append(f"  Qdrant chunks behind:  {list(status.chunks_behind)}")
        if status.graph_load_pending:
            lines.append(
                "  a corpus load is open or abandoned: abandon an open one that died "
                "(load_graph_structure --abandon), and the finish records the load"
            )
        lines.append("  finish it with --apply --finish")
    return "\n".join(lines)


def acknowledgement_line(plan: RevisionPlan, findings: int) -> str:
    ids = " ".join(sorted(plan.acknowledgeable))
    return (
        f"--apply --mode {plan.mode.value} --acknowledge {ids} --by NAME --reason WHY "
        f"--acknowledge-findings {findings}"
    )


async def _ledger(settings: Settings, *, no_ledger: bool) -> tuple[LedgerStore, str]:
    """The configured ledger, checked reachable; or, with `--no-ledger`, an
    empty one: no ledger is deployed (#174), so no finding exists to count.
    Refused if the configured ledger answers: then its count is the count."""
    try:
        ledger = PostgresLedgerStore.from_settings(settings)
        await ledger.ping()
    except (ValueError, LedgerUnreachable) as unreachable:
        if no_ledger:
            return FakeLedgerStore(), "none deployed (#174): 0 findings can cite these clauses"
        raise SystemExit(
            f"{unreachable}, so the findings citing these clauses can't be counted and nothing "
            "was written. Start it, or pass --no-ledger if no ledger is deployed yet (#174)."
        ) from None
    if no_ledger:
        raise SystemExit(
            f"--no-ledger, but the ledger at {ledger.location} answers: its findings are the "
            "count to acknowledge. Run without --no-ledger."
        )
    return ledger, ledger.location


async def main(arguments: argparse.Namespace) -> None:
    settings = Settings()
    if arguments.mode == "amend":
        raise SystemExit(
            "--mode amend is built and tested (CorpusRevision.amend) but there is no second "
            f"compilation of {INSTRUMENT_ID} pinned to amend to: Phase 4, {AMEND_ISSUE}"
        )
    applying = bool(arguments.apply)
    if applying and not arguments.finish and arguments.mode != "correct":
        raise SystemExit("--apply needs --mode correct (or --finish)")
    corpus_version: SourceVersion | None = None
    if applying:
        # Refused before anything is read: the content would be in no commit.
        try:
            corpus_version = resolve_source_version(SourceKind.CORPUS, repo=REPO)
        except UncommittedSource as refused:
            raise SystemExit(f"nothing written: {refused}") from None
    bucket = arguments.bucket or settings.corpus_bucket
    if not bucket:
        raise SystemExit("no corpus bucket: set CORPUS_BUCKET, or pass --bucket")
    selection = parse_selection_manifest(MANIFEST.read_text(encoding="utf-8"))
    source = pinned_source(arguments.html)
    ledger, ledger_location = await _ledger(settings, no_ledger=arguments.no_ledger)
    embedder: Embedder = (
        VertexEmbedder.from_settings(settings)
        if applying
        else _NoEmbedding(settings.vertex_embedding_dims)
    )
    retrieval = QdrantRetrievalStore.from_settings(settings, embedder=embedder, ledger=ledger)
    async with Neo4jGraph.from_settings(settings) as graph:
        revision = CorpusRevision(
            clauses=GCSClauseStore.for_bucket(bucket),
            graph=graph,
            retrieval=retrieval,
            ledger=ledger,
            count_tokens=count_reranker_tokens,
        )
        try:
            planned = await revision.plan(
                source, instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, selection=selection
            )
            await _report_or_apply(revision, planned, arguments, corpus_version, ledger_location)
        except (RevisionRefused, RevisionDrift) as refused:
            raise SystemExit(f"refused, nothing written by this step: {refused}") from None


async def _report_or_apply(
    revision: CorpusRevision,
    planned: PlannedRevision,
    arguments: argparse.Namespace,
    corpus_version: SourceVersion | None,
    ledger_location: str,
) -> None:
    plan = planned.plan
    findings = await revision.findings_citing(plan)
    status = await revision.status(planned)
    print(render_plan(plan, show_text=arguments.show_text))
    print(f"findings citing the changed clauses: {findings} (ledger: {ledger_location})")
    print(render_status(status))
    if not arguments.apply:
        if not plan.empty:
            print("\nto apply it:\n" + acknowledgement_line(plan, findings))
        return
    assert corpus_version is not None  # resolved before anything was read
    if plan.empty and not status.unfinished:
        # A re-run after the correction: nothing differs and nothing is behind.
        print("\nnothing to change: every store holds the parse")
        return
    print(f"\ncorpus version (the commit that parsed the new text): {corpus_version.described}")
    if arguments.finish:
        report = await revision.finish(
            planned,
            corpus_version=corpus_version,
            instrument_since=INSTRUMENT_SINCE,
            effective=EFFECTIVE,
        )
        if report is None:
            print("nothing to finish: every store holds the latest revision")
            return
    else:
        report = await revision.correct(
            planned,
            RevisionAcknowledgement(
                clause_ids=frozenset(arguments.acknowledge),
                by=arguments.by,
                reason=arguments.reason,
                findings_citing=arguments.acknowledge_findings,
            ),
            pinned_source_sha256=SOURCE_SHA256,
            corpus_version=corpus_version,
            instrument_since=INSTRUMENT_SINCE,
            effective=EFFECTIVE,
        )
    record = report.record
    print(
        f"revision {record.revision_id} ({record.mode.value}) applied at {record.at} by "
        f"{record.by}: {len(record.clauses)} clause(s) on the bucket manifest; graph: "
        f"{report.graph_nodes_corrected} node(s) corrected"
        + (", corpus load recorded" if report.graph_loaded else ", not loaded (no version)")
        + f"; Qdrant: {report.chunks_revised} chunk(s) revised (re-embedded only where the "
        "text changed)"
    )
    print(render_status(await revision.status(planned)))


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument(
        "--bucket", default=None, help="the corpus bucket; CORPUS_BUCKET (Settings) if not given"
    )
    parser.add_argument("--html", type=Path, default=None)
    parser.add_argument("--show-text", action="store_true", help="print both texts in full")
    parser.add_argument("--apply", action="store_true", help="write; without it nothing is")
    parser.add_argument("--mode", choices=("correct", "amend"), default=None)
    parser.add_argument(
        "--finish", action="store_true", help="bring every store up to an unfinished revision"
    )
    parser.add_argument("--acknowledge", nargs="*", default=[], metavar="CLAUSE_ID")
    parser.add_argument("--by", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--acknowledge-findings", type=int, default=None, metavar="N")
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        help="no ledger is deployed yet (#174): count 0 findings; refused if one answers",
    )
    arguments = parser.parse_args(argv)
    if arguments.finish and (
        arguments.acknowledge or arguments.by or arguments.reason or arguments.mode
    ):
        parser.error(
            "--finish takes no acknowledgement, --by, --reason or --mode: it writes only what "
            "the unfinished revision recorded"
        )
    if (
        arguments.apply
        and arguments.mode == "correct"
        and (
            not arguments.acknowledge
            or not arguments.by.strip()
            or not arguments.reason.strip()
            or arguments.acknowledge_findings is None
        )
    ):
        parser.error(
            "--apply --mode correct needs --acknowledge CLAUSE_ID..., --by, --reason and "
            "--acknowledge-findings N, exactly as the dry run prints them"
        )
    return arguments


if __name__ == "__main__":
    asyncio.run(main(parse_arguments()))
