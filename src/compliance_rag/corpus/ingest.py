"""Ingest: a pinned compilation plus a selection manifest, written to Cloud
Storage through `ClauseStore` (issue #19, `PHASES.md`: Cloud Storage layout).

`ingest_compilation` is the only place in the codebase that decides *whether*
to write — `ClauseStore` implementations (the real `GCSClauseStore`, the fake)
just do what they're told. Nothing here talks to Cloud Storage directly, only
through the interface (`CLAUDE.md`: "No Cypher, Qdrant, Vertex or SQL calls
outside adapters/" — the same discipline extended to the one other external
store this codebase writes to).

The drift check is the auditability mechanism replacing a hand-reviewed
literal clause-path enumeration: `manifest.yaml` declares scope as prefixes,
and a re-ingest whose prefixes now expand to a different set — or whose
already-stored clause content no longer matches a fresh parse of the same
pinned compilation — stops before writing anything and waits for a human
(`CLAUDE.md` corpus rules: "canonical text never edited after write"). The
human's path for a changed text is a corpus revision (`corpus/revision.py`,
`scripts/revise_compilation.py`, ADR-0042), never a re-ingest.
"""

from __future__ import annotations

from dataclasses import dataclass

from compliance_rag.adapters.interfaces import ClauseStore
from compliance_rag.corpus.models import Scope, canonical_sha256, sha256_hex
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.selection_manifest import SelectionManifest


class IngestDriftError(RuntimeError):
    """A re-ingest's expanded clause set, or an already-stored clause's
    content, no longer matches what's on record for this instrument
    version. Raised before any write — a human must look, not the ingest
    run itself."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What one `ingest_compilation` call did."""

    instrument_id: str
    version_id: str
    clauses_written: int
    source_html_sha256: str
    first_ingest: bool
    """True only when this instrument version had no stored manifest at
    all before this run — distinct from a no-drift re-run, which still
    writes (identically) but is not a first ingest."""


def _diff_manifests(old: dict[str, str], new: dict[str, str]) -> str | None:
    """A human-readable description of the difference, or `None` if the
    two manifests agree exactly on both membership and content."""
    added = sorted(new.keys() - old.keys())
    removed = sorted(old.keys() - new.keys())
    changed = sorted(path for path in new.keys() & old.keys() if new[path] != old[path])
    if not (added or removed or changed):
        return None
    parts: list[str] = []
    if added:
        parts.append(f"added={added!r}")
    if removed:
        parts.append(f"removed={removed!r}")
    if changed:
        parts.append(f"changed_content={changed!r}")
    return ", ".join(parts)


async def ingest_compilation(
    source_html: str,
    *,
    instrument_id: str,
    version_id: str,
    tenant_id: str,
    scope: Scope,
    selection: SelectionManifest,
    store: ClauseStore,
    acknowledged_additions: frozenset[str] = frozenset(),
) -> IngestResult:
    """Parse `source_html`, select the clauses `selection` puts in scope,
    and write them — unless doing so would silently change what's already
    stored for this instrument version, in which case raise
    `IngestDriftError` and write nothing.

    Idempotent on stable ID: re-running with the same `source_html` and
    `selection` re-writes identical content, which `ClauseStore`
    implementations must treat as a no-op in effect (same bytes, same key).

    A completed version's selection can grow only by acknowledgement: when a
    reviewed manifest adds sections (s15, say), the run still stops — unless
    `acknowledged_additions` names exactly the clause paths being added, and
    every already-stored clause is unchanged. Then only the additions are
    written. A removal or an edit is never acknowledgeable (issue #19's
    guarantee; the acknowledgement mirrors ADR-0021's findings-citing count).

    Library law only: a tenant document is written through `CorpusLifecycle`,
    where the residency gate and the synthetic registry apply (ADR-0023,
    ADR-0026) — this path checks neither.
    """
    if scope is not Scope.PUBLIC:
        raise ValueError(
            "compilation ingest writes library law only; tenant documents go through "
            "CorpusLifecycle (ADR-0026)"
        )
    parsed = parse_compilation(
        source_html,
        instrument_id=instrument_id,
        version_id=version_id,
        tenant_id=tenant_id,
        scope=scope,
    )
    in_scope = selection.select(parsed.clauses)

    # A single parse_compilation() call stamps every clause with the same
    # CANONICALISATION_VERSION (corpus/models.py); this reads that value off
    # the clauses themselves rather than importing the constant directly, so
    # it stays correct if a future ingest path ever mixes clauses from more
    # than one parse.
    canonicalisation_version = in_scope[0].canonicalisation_version

    new_manifest = {
        clause.clause_path: canonical_sha256(clause.canonical_text) for clause in in_scope
    }
    old_manifest = await store.get_manifest(instrument_id, version_id, tenant_id=tenant_id)
    completed = (
        await store.get_manifest_metadata(instrument_id, version_id, tenant_id=tenant_id)
        is not None
    )
    first_ingest = not completed

    source_html_sha256 = sha256_hex(parsed.annotated_html)
    if completed:
        # A clause newly marked incomplete, or no longer so, changes the
        # stored corpus as an edit does (ADR-0047): a corpus revision.
        held_incomplete = await store.get_incomplete(instrument_id, version_id, tenant_id=tenant_id)
        restated = sorted(
            c.clause_path
            for c in in_scope
            if c.clause_path in old_manifest
            and held_incomplete.get(c.clause_path, ()) != c.incomplete
        )
        if restated:
            raise IngestDriftError(
                f"{instrument_id}:{version_id} re-ingest does not match the stored manifest — "
                f"the parse marks {restated!r} incomplete differently from what is stored; "
                "a changed clause state is a corpus revision (scripts/revise_compilation.py, "
                "ADR-0042, ADR-0047), never a re-ingest"
            )
        diff = _diff_manifests(old_manifest, new_manifest)
        added = frozenset(new_manifest) - frozenset(old_manifest)
        unchanged = (
            _diff_manifests(
                old_manifest, {p: new_manifest[p] for p in old_manifest if p in new_manifest}
            )
            is None
        )
        additions_only = added and unchanged and frozenset(old_manifest) <= frozenset(new_manifest)
        if diff is not None and additions_only and added == acknowledged_additions:
            await store.put_clauses(tuple(c for c in in_scope if c.clause_path in added))
            return IngestResult(
                instrument_id=instrument_id,
                version_id=version_id,
                clauses_written=len(added),
                source_html_sha256=source_html_sha256,
                first_ingest=False,
            )
        if diff is not None:
            hint = (
                f"; to add them, acknowledge exactly {sorted(added)!r}"
                if additions_only
                else "; a changed or removed clause is a corpus revision "
                "(scripts/revise_compilation.py, ADR-0042), never a re-ingest"
            )
            raise IngestDriftError(
                f"{instrument_id}:{version_id} re-ingest does not match the stored "
                f"manifest — canonical text must never be edited after write "
                f"({diff}){hint}"
            )
        # Complete and unchanged: write nothing. The bucket is versioned, so
        # re-uploading identical objects would leave a noncurrent generation
        # of each one on every run, forever (library law is never purged).
        return IngestResult(
            instrument_id=instrument_id,
            version_id=version_id,
            clauses_written=0,
            source_html_sha256=source_html_sha256,
            first_ingest=False,
        )
    elif old_manifest:
        # A first ingest that stopped before `put_manifest` (its last step):
        # it may resume only if everything already stored is part of this
        # selection, byte-identical. Anything else is drift (issue #99).
        partial = {p: h for p, h in new_manifest.items() if p in old_manifest}
        diff = _diff_manifests(old_manifest, partial)
        if diff is not None:
            raise IngestDriftError(
                f"{instrument_id}:{version_id} interrupted first ingest does not match "
                f"this selection — resuming would mix two sources ({diff})"
            )

    await store.put_clauses(in_scope)

    await store.put_manifest(
        instrument_id,
        version_id,
        tenant_id=tenant_id,
        canonicalisation_version=canonicalisation_version,
        source_html=parsed.annotated_html,
    )

    return IngestResult(
        instrument_id=instrument_id,
        version_id=version_id,
        clauses_written=len(in_scope),
        source_html_sha256=source_html_sha256,
        first_ingest=first_ingest,
    )
