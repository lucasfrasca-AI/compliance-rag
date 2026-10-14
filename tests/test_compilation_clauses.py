"""The recall gate's law corpus is the parser's output for the whole
compilation's selected clauses, committed (issue #180).

The raw compilation is never committed, so CI can't re-parse it. What CI can
check: the file is what the export script renders (no hand edits to its form),
its selection is the reviewed manifest's, every clause is in canonical form,
and every clause a checked-in fixture also holds — the excerpt's 75 and the
constructs fixture's 100 — is byte-for-byte that fixture's fresh parse.
`PRIVACY_ACT_COMPILATION_HTML`, pointed at the pinned source, adds the full
check: a fresh parse renders to the committed file exactly.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from pathlib import Path

import pytest
from scripts.ingest_compilation import INSTRUMENT_ID, SOURCE_SHA256, VERSION_ID

from compliance_rag.corpus.models import PUBLIC_TENANT_ID, ClauseRecord, canonicalise
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.eval.compilation_clauses import (
    COMPILATION_CLAUSES_PATH,
    CompilationClauses,
    load_compilation_clauses,
    render_compilation_clauses,
    select_compilation_clauses,
    selection_manifest,
)
from compliance_rag.eval.golden_corpus import gate_law_clauses, golden_clauses

FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "research" / "fixtures"


@pytest.fixture(scope="module")
def committed() -> CompilationClauses:
    return load_compilation_clauses()


def test_the_file_is_the_pinned_compilations_selected_clauses(
    committed: CompilationClauses,
) -> None:
    """779 clauses: Schedule 1, ss 6, 6C, 6D, 6DA, 6E, 6EA and s15, the counts
    the full-compilation parser test pins (test_parser_full_compilation.py)."""
    assert (committed.instrument_id, committed.version_id) == (INSTRUMENT_ID, VERSION_ID)
    assert committed.source_sha256 == SOURCE_SHA256
    assert committed.selection == selection_manifest().prefixes
    counts = Counter(c.clause_path.split("/")[0] for c in committed.clauses)
    assert counts == {
        "s6": 451,
        "s6c": 33,
        "s6d": 27,
        "s6da": 9,
        "s6e": 14,
        "s6ea": 8,
        "sch1": 236,
        "s15": 1,
    }
    assert len(committed.clauses) == 779


def test_the_manifest_selects_every_clause_and_nothing_else(
    committed: CompilationClauses,
) -> None:
    """Re-selecting the file's clauses through today's manifest keeps all of
    them, and every prefix still matches one (a removed or mistyped prefix
    fails here, before the gate measures a corpus the manifest doesn't
    describe). An added prefix changes `selection`, caught above."""
    assert selection_manifest().select(committed.clauses) == committed.clauses


def test_the_file_is_exactly_what_the_export_script_renders(
    committed: CompilationClauses,
) -> None:
    """No hand edit to its form: re-rendering what it holds reproduces it."""
    rendered = render_compilation_clauses(
        committed.clauses, source_sha256=committed.source_sha256, selection=committed.selection
    )
    assert rendered == COMPILATION_CLAUSES_PATH.read_text(encoding="utf-8")


def test_every_clauses_canonical_text_is_its_texts_canonical_form(
    committed: CompilationClauses,
) -> None:
    """A hand edit to one of the two would show here, on that clause."""
    assert [
        c.clause_path for c in committed.clauses if c.canonical_text != canonicalise(c.text)
    ] == []
    assert {(c.scope.value, c.tenant_id) for c in committed.clauses} == {
        ("public", PUBLIC_TENANT_ID)
    }


COMPILATION_FIXTURES = sorted(FIXTURES.glob("privacy-act-1988-C2026C00227.*.html"))
"""Every checked-in cut of the pinned compilation (CLAUDE.md: a parser is tested
against every available real fixture), found by name so a new one is covered."""

OVERLAP = {"excerpt": 75, "constructs": 100}
"""How many selected clauses each fixture holds. A new fixture fails until its
count is recorded here, so it can't join without being looked at."""


@pytest.mark.parametrize("fixture", COMPILATION_FIXTURES, ids=lambda p: p.name.split(".")[1])
def test_every_clause_a_fixture_also_holds_is_its_fresh_parse(
    committed: CompilationClauses, fixture: Path
) -> None:
    """Every fixture is cut verbatim from the pinned compilation, so where it
    holds a selected clause, today's parser must produce the committed record
    exactly. A parser change that moves one of these ~170 clauses fails in CI,
    not only where the full source is at hand."""
    parsed = parse_compilation(
        fixture.read_text(encoding="utf-8"), instrument_id=INSTRUMENT_ID, version_id=VERSION_ID
    )
    fresh: dict[str, ClauseRecord] = {c.clause_path: c for c in parsed.clauses}
    shared = [c for c in committed.clauses if c.clause_path in fresh]
    assert len(shared) == OVERLAP[fixture.name.split(".")[1]]
    assert [c.clause_path for c in shared if fresh[c.clause_path] != c] == []


def test_every_fixture_found_has_its_overlap_recorded() -> None:
    """And the glob found them: an empty parametrize list would skip silently."""
    assert {p.name.split(".")[1] for p in COMPILATION_FIXTURES} == set(OVERLAP)


def test_the_excerpt_is_a_subset_of_the_gate_corpus() -> None:
    """Same ids, same text: the excerpt's tests and the gate measure the same
    clauses, the gate with the other 704 as distractors."""
    gate = {c.clause_id: c for c in gate_law_clauses()}
    excerpt = [c for c in golden_clauses() if c.tenant_id == PUBLIC_TENANT_ID]
    assert len(excerpt) == 75
    assert [c.clause_id for c in excerpt if gate.get(c.clause_id) != c] == []


@pytest.mark.skipif(
    not os.environ.get("PRIVACY_ACT_COMPILATION_HTML"),
    reason="PRIVACY_ACT_COMPILATION_HTML not set — the full 2.3 MB compilation",
)
def test_a_fresh_parse_of_the_pinned_compilation_is_the_committed_file() -> None:
    """The file is the parser's output, not an edit of it. After a parser or
    manifest change this fails until `scripts/export_compilation_clauses.py`
    is re-run (then refresh the embedding cache and re-baseline the gate)."""
    raw = Path(os.environ["PRIVACY_ACT_COMPILATION_HTML"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_SHA256, "a different compilation file"
    selection = selection_manifest()
    fresh = select_compilation_clauses(
        raw.decode("utf-8"), instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, selection=selection
    )
    rendered = render_compilation_clauses(
        fresh, source_sha256=SOURCE_SHA256, selection=selection.prefixes
    )
    assert rendered == COMPILATION_CLAUSES_PATH.read_text(encoding="utf-8")
