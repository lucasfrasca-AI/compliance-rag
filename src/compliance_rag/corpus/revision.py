"""A corpus revision: what a fresh parse of an instrument version's source
changes in what is stored, and the record of applying it (#178 part 2,
ADR-0042).

Pure: nothing here reads or writes a store. `CorpusRevision`
(`adapters/corpus_revision.py`) reads what is stored, asks this module for the
plan, checks the acknowledgement against it, and only then writes.

Two modes share the plan, the acknowledgement and the record:

- **correct** — the same source, the same `version_id`: a parse correction.
  The law did not change, only how it was parsed, so the stored text is
  corrected in place, clause by clause (ADR-0042).
- **amend** — a new compilation under a new `version_id`: version and retire
  (ADR-0021). The plan compares the new version's clauses with the old
  version's, path by path.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    ClauseRecord,
    canonical_sha256,
    instrument_tenant_prefix,
    sha256_hex,
    stable_id,
)


class RevisionMode(StrEnum):
    CORRECT = "correct"
    """Same source, same version: a parse correction, applied in place."""
    AMEND = "amend"
    """A new compilation: a new version, and the old one retired."""


class RevisionRefused(PermissionError):
    """A revision refused before any store was written."""


class RevisionDrift(RuntimeError):
    """What is stored disagrees with itself, or with the revision on record, in
    a way no revision explains. Nothing was written: a person looks."""


@dataclass(frozen=True, slots=True)
class ClauseChange:
    """One clause whose canonical text the fresh parse changes."""

    clause_path: str
    old_clause_id: str
    new_clause_id: str
    """The same id as `old_clause_id` for a correction; the new version's for
    an amendment."""
    old_sha256: str
    new_sha256: str
    old_text: str
    new_text: str
    """Both canonical."""


@dataclass(frozen=True, slots=True)
class RemovedClause:
    clause_path: str
    clause_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RevisionPlan:
    """What a fresh parse changes in a stored instrument version, clause by
    clause. `stored_version_id` is the version compared against;
    `version_id` the version being written: the same for a correction."""

    instrument_id: str
    stored_version_id: str
    version_id: str
    source_sha256: str
    """sha256 of the source bytes parsed (UTF-8): what a pin is checked against."""
    source_html_sha256: str
    """sha256 of the anchored HTML the fresh parse produced."""
    stored_source_html_sha256: str
    """sha256 of the anchored HTML stored for `stored_version_id`."""
    changed: tuple[ClauseChange, ...]
    added: tuple[ClauseRecord, ...]
    removed: tuple[RemovedClause, ...]
    unchanged: int

    @property
    def mode(self) -> RevisionMode:
        return (
            RevisionMode.CORRECT
            if self.version_id == self.stored_version_id
            else RevisionMode.AMEND
        )

    @property
    def empty(self) -> bool:
        return not (self.changed or self.added or self.removed)

    @property
    def acknowledgeable(self) -> frozenset[str]:
        """The clause ids an acknowledgement must name, exactly: every clause
        the revision writes (changed and added, under the version written) and
        every clause it retires (removed, under the stored version)."""
        return frozenset(
            [c.new_clause_id for c in self.changed]
            + [c.clause_id for c in self.added]
            + [r.clause_id for r in self.removed]
        )

    @property
    def cited_before(self) -> frozenset[str]:
        """The stored clause ids an issued citation may name whose text this
        revision changes or retires: what the findings count is over."""
        return frozenset(
            [c.old_clause_id for c in self.changed] + [r.clause_id for r in self.removed]
        )


def plan_revision(
    fresh: Sequence[ClauseRecord],
    *,
    source_html: str,
    annotated_html: str,
    stored_version_id: str,
    stored_manifest: Mapping[str, str],
    stored_texts: Mapping[str, str],
    stored_source_html_sha256: str,
) -> RevisionPlan:
    """The plan, from a fresh parse (the clauses a selection keeps, of one
    version) and what is stored for `stored_version_id`: its manifest (path to
    canonical hash) and the stored canonical text of every path whose hash
    differs (`changed_paths`). A stored text that doesn't hash to its own
    manifest entry is `RevisionDrift`: the bucket disagrees with itself."""
    if not fresh:
        raise ValueError("a revision of a version with no clauses")
    instrument_id, version_id = fresh[0].instrument_id, fresh[0].version_id
    for clause in fresh:
        if (clause.instrument_id, clause.version_id) != (instrument_id, version_id):
            raise ValueError(f"{clause.clause_id} is not in {instrument_id}:{version_id}")
        if clause.tenant_id != PUBLIC_TENANT_ID or instrument_tenant_prefix(instrument_id):
            raise ValueError(
                f"{clause.clause_id}: a corpus revision is of library law; a tenant's document "
                "is versioned, withdrawn or replaced through CorpusLifecycle (ADR-0021)"
            )
    by_path = {c.clause_path: c for c in fresh}
    if len(by_path) != len(fresh):
        raise ValueError("the fresh parse names one clause path twice")
    changed: list[ClauseChange] = []
    for path in changed_paths(fresh, stored_manifest):
        stored_text = stored_texts.get(path)
        if stored_text is None:
            raise RevisionDrift(f"{path} is in the manifest but its stored text wasn't read")
        if canonical_sha256(stored_text) != stored_manifest[path]:
            raise RevisionDrift(
                f"{instrument_id}:{stored_version_id}:{path}: the stored clause doesn't hash to "
                "its manifest entry — the bucket disagrees with itself (an unfinished revision "
                "is reported by its status, not here)"
            )
        clause = by_path[path]
        changed.append(
            ClauseChange(
                clause_path=path,
                old_clause_id=stable_id(instrument_id, stored_version_id, path),
                new_clause_id=clause.clause_id,
                old_sha256=stored_manifest[path],
                new_sha256=canonical_sha256(clause.canonical_text),
                old_text=stored_text,
                new_text=clause.canonical_text,
            )
        )
    added = tuple(c for c in fresh if c.clause_path not in stored_manifest)
    removed = tuple(
        RemovedClause(path, stable_id(instrument_id, stored_version_id, path), sha)
        for path, sha in sorted(stored_manifest.items())
        if path not in by_path
    )
    return RevisionPlan(
        instrument_id=instrument_id,
        stored_version_id=stored_version_id,
        version_id=version_id,
        source_sha256=sha256_hex(source_html),
        source_html_sha256=sha256_hex(annotated_html),
        stored_source_html_sha256=stored_source_html_sha256,
        changed=tuple(changed),
        added=added,
        removed=removed,
        unchanged=len(fresh) - len(changed) - len(added),
    )


def changed_paths(fresh: Sequence[ClauseRecord], stored_manifest: Mapping[str, str]) -> list[str]:
    """The paths both hold whose canonical hash differs, in parse order: the
    stored texts a plan needs read."""
    return [
        c.clause_path
        for c in fresh
        if c.clause_path in stored_manifest
        and stored_manifest[c.clause_path] != canonical_sha256(c.canonical_text)
    ]


# --------------------------------------------------------------------------
# The acknowledgement
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RevisionAcknowledgement:
    """A person's go-ahead for one revision: exactly which clauses, who, why,
    and the number of findings citing them they have seen (ADR-0042, as
    `acknowledged_additions` and ADR-0021's findings count)."""

    clause_ids: frozenset[str]
    by: str
    reason: str
    findings_citing: int


def check_acknowledgement(
    plan: RevisionPlan, acknowledgement: RevisionAcknowledgement, *, findings_citing: int
) -> None:
    """Refused unless the acknowledgement names exactly the plan's clauses
    (`RevisionPlan.acknowledgeable`), says who and why, and acknowledges
    exactly the number of findings citing them now."""
    if plan.empty:
        raise RevisionRefused(
            f"{plan.instrument_id}:{plan.stored_version_id}: the fresh parse changes nothing"
        )
    if not (acknowledgement.by.strip() and acknowledgement.reason.strip()):
        raise RevisionRefused("a revision names who authorised it and why")
    wanted, named = plan.acknowledgeable, acknowledgement.clause_ids
    if named != wanted:
        missing, extra = sorted(wanted - named), sorted(named - wanted)
        raise RevisionRefused(
            "a revision is applied only when its clauses are acknowledged exactly: "
            f"missing {missing}, not in the plan {extra}"
        )
    if acknowledgement.findings_citing != findings_citing:
        raise RevisionRefused(
            f"{findings_citing} finding(s) cite the clauses this revision changes or retires; "
            f"acknowledged {acknowledgement.findings_citing}"
        )


def check_correction(plan: RevisionPlan, *, pinned_source_sha256: str) -> None:
    """A correction is of the same law: the version's own pinned source,
    parsed to the same anchored HTML, over the same clause set. Anything else
    is refused, before the acknowledgement is looked at."""
    where = f"{plan.instrument_id}:{plan.version_id}"
    if plan.mode is not RevisionMode.CORRECT:
        raise RevisionRefused(
            f"a correction keeps the version: {plan.version_id} is not {plan.stored_version_id}"
        )
    if plan.source_sha256 != pinned_source_sha256:
        raise RevisionRefused(
            f"{where}: the source parsed (sha256 {plan.source_sha256}) is not the version's "
            f"pinned source ({pinned_source_sha256}). A correction proves the law didn't change "
            "by being parsed from the same bytes; different bytes are a new compilation (amend)"
        )
    if plan.source_html_sha256 != plan.stored_source_html_sha256:
        raise RevisionRefused(
            f"{where}: the fresh parse's anchored source.html differs from the stored one, so "
            "anchors moved. A correction changes clause text only"
        )
    if plan.removed:
        raise RevisionRefused(
            f"{where}: the fresh parse loses {[r.clause_id for r in plan.removed]}. A clause id "
            "is what a citation carries, so a correction never removes one; a provision the law "
            "repealed is a new compilation (amend)"
        )
    if plan.added:
        raise RevisionRefused(
            f"{where}: the fresh parse adds {[c.clause_id for c in plan.added]}. A correction is "
            "of the clause set already stored. A reviewed selection that grew is ingest's "
            "acknowledged-additions route, once the stored text matches the parse; a parser that "
            "finds new clauses in the same source is not a text correction"
        )


def check_amendment(plan: RevisionPlan, *, pinned_source_sha256: str) -> None:
    """An amendment is a different version, parsed from its own pinned source."""
    if plan.mode is not RevisionMode.AMEND:
        raise RevisionRefused(
            f"an amendment is a new version: {plan.version_id} is the stored version itself"
        )
    if plan.source_sha256 != pinned_source_sha256:
        raise RevisionRefused(
            f"{plan.instrument_id}:{plan.version_id}: the source parsed (sha256 "
            f"{plan.source_sha256}) is not the one pinned for it ({pinned_source_sha256})"
        )


# --------------------------------------------------------------------------
# The record (audit)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RevisedClause:
    """One clause of a revision on record: its hash before and after. `None`
    before is an added clause; `None` after a removed one (amend only)."""

    clause_path: str
    previous_sha256: str | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    """The durable record of one revision, kept on the manifest of the version
    it writes (and, for a correction, on each Clause node's trail): which
    clauses, their hashes before and after, who, why, when, from which source
    bytes and which commit of the code (ADR-0042)."""

    revision_id: str
    mode: RevisionMode
    instrument_id: str
    version_id: str
    supersedes: str | None
    """The version an amendment retires; None for a correction."""
    at: str
    """UTC, ISO 8601."""
    by: str
    reason: str
    source_sha256: str
    corpus_sha: str
    """The commit of the code and selection that parsed the new text (ADR-0034)."""
    corpus_tag: str | None
    findings_citing: int
    clauses: tuple[RevisedClause, ...] = field(default=())

    def __post_init__(self) -> None:
        if not (self.by.strip() and self.reason.strip()):
            raise ValueError("a revision on record names who and why")
        paths = [c.clause_path for c in self.clauses]
        if len(paths) != len(set(paths)) or not paths:
            raise ValueError("a revision on record names each of its clauses once")
        if (self.supersedes is None) != (self.mode is RevisionMode.CORRECT):
            raise ValueError("an amendment names the version it supersedes; a correction doesn't")
        if self.mode is RevisionMode.CORRECT and any(
            c.previous_sha256 is None or c.sha256 is None for c in self.clauses
        ):
            raise ValueError("a correction neither adds nor removes a clause")

    def clause_id(self, clause_path: str) -> str:
        return stable_id(self.instrument_id, self.version_id, clause_path)

    @property
    def written(self) -> dict[str, str]:
        """Every path the revision leaves stored in this version, with its hash."""
        return {c.clause_path: c.sha256 for c in self.clauses if c.sha256 is not None}

    @property
    def corrected(self) -> tuple[RevisedClause, ...]:
        """The clauses whose text changed in place: both hashes, one version."""
        if self.mode is not RevisionMode.CORRECT:
            return ()
        return self.clauses

    def to_json(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "mode": self.mode.value,
            "instrument_id": self.instrument_id,
            "version_id": self.version_id,
            "supersedes": self.supersedes,
            "at": self.at,
            "by": self.by,
            "reason": self.reason,
            "source_sha256": self.source_sha256,
            "corpus_sha": self.corpus_sha,
            "corpus_tag": self.corpus_tag,
            "findings_citing": self.findings_citing,
            "clauses": {
                c.clause_path: {"previous_sha256": c.previous_sha256, "sha256": c.sha256}
                for c in self.clauses
            },
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> RevisionRecord:
        # The manifest's own JSON: each field is read as the type `to_json` wrote.
        clauses = cast("dict[str, dict[str, str | None]]", data["clauses"])
        return cls(
            revision_id=str(data["revision_id"]),
            mode=RevisionMode(str(data["mode"])),
            instrument_id=str(data["instrument_id"]),
            version_id=str(data["version_id"]),
            supersedes=cast("str | None", data["supersedes"]),
            at=str(data["at"]),
            by=str(data["by"]),
            reason=str(data["reason"]),
            source_sha256=str(data["source_sha256"]),
            corpus_sha=str(data["corpus_sha"]),
            corpus_tag=cast("str | None", data["corpus_tag"]),
            findings_citing=int(cast("int", data["findings_citing"])),
            clauses=tuple(
                RevisedClause(path, entry["previous_sha256"], entry["sha256"])
                for path, entry in clauses.items()
            ),
        )

    def trail_entry(self, previous_sha256: str) -> str:
        """What a corrected Clause node's `corrections` trail gains: the same
        shape as ADR-0031's (when, by, why, the previous value), plus the
        revision it belongs to and the source bytes it was parsed from."""
        return json.dumps(
            {
                "at": self.at,
                "by": self.by,
                "reason": self.reason,
                "revision_id": self.revision_id,
                "mode": self.mode.value,
                "source_sha256": self.source_sha256,
                "previous": {"canonical_sha256": previous_sha256},
            },
            sort_keys=True,
        )


def check_revision_hashes(record: RevisionRecord, held: Mapping[str, str]) -> list[str]:
    """The paths whose stored hash (`held`, a manifest) a revision moves from
    its previous hash to its new one. A path already at the new hash is done.
    Anything else is `RevisionDrift`, before a write: a path at a hash the
    record doesn't name, a corrected path missing, a path an amendment adds
    not yet stored, or one it removes still listed."""
    moving: list[str] = []
    where = f"{record.instrument_id}:{record.version_id}"
    for entry in record.clauses:
        have = held.get(entry.clause_path)
        if entry.sha256 is None:
            if have is not None:
                raise RevisionDrift(f"{where}:{entry.clause_path} is retired but still stored")
            continue
        if have == entry.sha256:
            continue
        if record.mode is RevisionMode.CORRECT and have == entry.previous_sha256:
            moving.append(entry.clause_path)
            continue
        raise RevisionDrift(
            f"{where}:{entry.clause_path} is stored with hash {have}, which is neither the "
            f"previous ({entry.previous_sha256}) nor the new ({entry.sha256}) hash revision "
            f"{record.revision_id} records"
        )
    return moving


def check_revision_recordable(
    record: RevisionRecord, moving: Sequence[str], *, on_record: bool
) -> None:
    """A correction that moves no hash and isn't on record yet changes nothing
    stored: its clauses already hold the new hashes, written by another
    revision. Refused, so a manifest never records a revision that didn't
    happen. (An amendment's hashes are written by its ingest, so it moves
    none; a correction already on record is being finished.)"""
    if record.mode is RevisionMode.CORRECT and not moving and not on_record:
        raise RevisionDrift(
            f"revision {record.revision_id} changes nothing stored: "
            f"{record.instrument_id}:{record.version_id} already holds its new hashes"
        )


def record_of(
    plan: RevisionPlan,
    acknowledgement: RevisionAcknowledgement,
    *,
    revision_id: str,
    at: dt.datetime,
    corpus_sha: str,
    corpus_tag: str | None,
) -> RevisionRecord:
    """The record of applying `plan` under `acknowledgement` (already checked)."""
    clauses = (
        [RevisedClause(c.clause_path, c.old_sha256, c.new_sha256) for c in plan.changed]
        + [
            RevisedClause(c.clause_path, None, canonical_sha256(c.canonical_text))
            for c in plan.added
        ]
        + [RevisedClause(r.clause_path, r.sha256, None) for r in plan.removed]
    )
    return RevisionRecord(
        revision_id=revision_id,
        mode=plan.mode,
        instrument_id=plan.instrument_id,
        version_id=plan.version_id,
        supersedes=None if plan.mode is RevisionMode.CORRECT else plan.stored_version_id,
        at=at.astimezone(dt.UTC).isoformat(),
        by=acknowledgement.by,
        reason=acknowledgement.reason,
        source_sha256=plan.source_sha256,
        corpus_sha=corpus_sha,
        corpus_tag=corpus_tag,
        findings_citing=acknowledgement.findings_citing,
        clauses=tuple(clauses),
    )


# --------------------------------------------------------------------------
# For a person reading the plan
# --------------------------------------------------------------------------


def opening_words(old: str, new: str, *, words: int = 10) -> tuple[str, str]:
    """The opening words of two texts, for a table a person reads. When the
    two open alike, each is shown from a little before the first word that
    differs, so the row shows the change rather than two identical openings."""
    old_words, new_words = old.split(), new.split()
    common = 0
    for a, b in zip(old_words, new_words, strict=False):
        if a != b:
            break
        common += 1
    start = 0 if common < words else common - 3

    def show(text_words: list[str]) -> str:
        shown = " ".join(text_words[start : start + words])
        prefix = "… " if start else ""
        suffix = " …" if start + words < len(text_words) else ""
        return f"{prefix}{shown}{suffix}" if shown else "(ends)"

    return show(old_words), show(new_words)
