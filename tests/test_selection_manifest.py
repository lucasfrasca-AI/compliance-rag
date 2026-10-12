"""The selection manifest: which clauses of a pinned compilation are in
scope for ingest (issue #19).

`corpus/mappings/README.md`'s four-field front-matter convention minus
`source_clause_ids` — a selection manifest declares clause paths in scope
prospectively, it isn't derived from clauses that already exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compliance_rag.corpus.models import ClauseRecord
from compliance_rag.corpus.selection_manifest import (
    SelectionManifest,
    parse_selection_manifest,
)

INSTRUMENT_ID = "privacy-act-1988"
VERSION_ID = "C2026C00227"


def _clause(clause_path: str) -> ClauseRecord:
    return ClauseRecord.build(
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        clause_path=clause_path,
        text=f"text for {clause_path}",
    )


# --------------------------------------------------------------------------
# parse_selection_manifest
# --------------------------------------------------------------------------


def test_parses_required_front_matter_and_prefixes() -> None:
    source = """
author: jane.doe@example.com
reviewed_by: sam.smith@example.com
date: 2026-09-24
prefixes:
  - sch1/
  - s6/
"""
    manifest = parse_selection_manifest(source)
    assert manifest.author == "jane.doe@example.com"
    assert manifest.reviewed_by == "sam.smith@example.com"
    assert manifest.date == "2026-09-24"
    assert manifest.prefixes == ("sch1/", "s6/")


@pytest.mark.parametrize("missing_field", ["author", "reviewed_by", "date", "prefixes"])
def test_a_missing_required_field_fails_loudly(missing_field: str) -> None:
    fields = {
        "author": "jane.doe@example.com",
        "reviewed_by": "sam.smith@example.com",
        "date": "2026-09-24",
        "prefixes": ["sch1/"],
    }
    del fields[missing_field]
    lines = [f"{k}: {v}" for k, v in fields.items() if k != "prefixes"]
    if "prefixes" in fields:
        lines.append("prefixes:\n  - sch1/")
    source = "\n".join(lines)
    with pytest.raises(ValueError, match=missing_field):
        parse_selection_manifest(source)


def test_author_and_reviewed_by_may_be_the_same_person() -> None:
    """Single-author review: a human may review their own file — a solo
    builder has no one else to name (`corpus/mappings/README.md`:
    "Review"). A placeholder that can never resolve would be worse than an
    honest single name."""
    source = """
author: jane.doe@example.com
reviewed_by: jane.doe@example.com
date: 2026-09-24
prefixes:
  - sch1/
"""
    manifest = parse_selection_manifest(source)
    assert manifest.author == manifest.reviewed_by == "jane.doe@example.com"


def test_empty_prefix_list_fails_loudly() -> None:
    source = """
author: jane.doe@example.com
reviewed_by: sam.smith@example.com
date: 2026-09-24
prefixes: []
"""
    with pytest.raises(ValueError, match="prefix"):
        parse_selection_manifest(source)


def test_a_non_mapping_document_fails_loudly() -> None:
    with pytest.raises(ValueError, match="mapping"):
        parse_selection_manifest("- just\n- a\n- list\n")


# --------------------------------------------------------------------------
# SelectionManifest.select
# --------------------------------------------------------------------------


def test_select_matches_a_whole_schedule_prefix() -> None:
    manifest = SelectionManifest(
        author="a", reviewed_by="b", date="2026-09-24", prefixes=("sch1/",)
    )
    clauses = (_clause("sch1/app11/1"), _clause("sch1/app12/1"), _clause("s6/1/organisation"))
    selected = manifest.select(clauses)
    assert {c.clause_path for c in selected} == {"sch1/app11/1", "sch1/app12/1"}


def test_a_trailing_slash_prefix_does_not_match_a_different_section_sharing_the_digit() -> None:
    """`s6/` must not match `s6c/1/a` — the whole point of Phase 1 scoping
    ss 6, 6C, 6D, 6E as four separate prefixes rather than one `s6` prefix.
    """
    manifest = SelectionManifest(author="a", reviewed_by="b", date="2026-09-24", prefixes=("s6/",))
    clauses = (_clause("s6/1/organisation"), _clause("s6c/1/a"))
    selected = manifest.select(clauses)
    assert {c.clause_path for c in selected} == {"s6/1/organisation"}


def test_multiple_prefixes_union() -> None:
    manifest = SelectionManifest(
        author="a",
        reviewed_by="b",
        date="2026-09-24",
        prefixes=("sch1/", "s6/", "s6c/", "s6d/", "s6e/"),
    )
    clauses = (
        _clause("sch1/app11/1"),
        _clause("s6/1/organisation"),
        _clause("s6c/1/a"),
        _clause("s6d/1"),
        _clause("s6e/1"),
        _clause("sch2/1"),
    )
    selected = manifest.select(clauses)
    assert {c.clause_path for c in selected} == {
        "sch1/app11/1",
        "s6/1/organisation",
        "s6c/1/a",
        "s6d/1",
        "s6e/1",
    }


def test_a_prefix_matching_nothing_fails_loudly() -> None:
    """A stale or mistyped prefix in a hand-reviewed manifest.yaml must be
    caught immediately, not discovered later as a silently missing clause.
    """
    manifest = SelectionManifest(
        author="a", reviewed_by="b", date="2026-09-24", prefixes=("sch1/", "s99/")
    )
    clauses = (_clause("sch1/app11/1"),)
    with pytest.raises(ValueError, match="s99/"):
        manifest.select(clauses)


def test_select_result_is_sorted_by_clause_path() -> None:
    manifest = SelectionManifest(
        author="a", reviewed_by="b", date="2026-09-24", prefixes=("sch1/",)
    )
    clauses = (_clause("sch1/app12/1"), _clause("sch1/app11/1"))
    selected = manifest.select(clauses)
    assert [c.clause_path for c in selected] == ["sch1/app11/1", "sch1/app12/1"]


def test_a_model_may_draft_a_manifest_but_never_review_one() -> None:
    """`corpus/mappings/README.md`: Review — the same rule as the golden set."""
    SelectionManifest(
        author="model: Claude Opus 5.5",
        reviewed_by="Piero Frasca",
        date="2026-10-08",
        prefixes=("s6/",),
    )
    with pytest.raises(ValueError, match="never a model"):
        SelectionManifest(
            author="Piero Frasca",
            reviewed_by="model: Claude Opus 5.5",
            date="2026-10-08",
            prefixes=("s6/",),
        )


def test_a_section_prefix_does_not_match_its_inserted_neighbour() -> None:
    """`s6d/` must not take `s6da/` (annual turnover), nor `s6e/` take
    `s6ea/` (opt-in): inserted sections share their neighbour's digits and
    letter (issue #98)."""
    manifest = SelectionManifest(
        author="a", reviewed_by="b", date="2026-10-11", prefixes=("s6d/", "s6e/")
    )
    clauses = (_clause("s6d/1"), _clause("s6da/1/a"), _clause("s6e/1a"), _clause("s6ea/1"))
    assert [c.clause_path for c in manifest.select(clauses)] == ["s6d/1", "s6e/1a"]


def test_the_checked_in_manifest_selects_the_small_business_exemption() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "corpus/instruments/privacy-act-1988/C2026C00227/manifest.yaml"
    )
    manifest = parse_selection_manifest(path.read_text(encoding="utf-8"))
    assert {"s6c/", "s6d/", "s6da/", "s6e/", "s6ea/"} <= set(manifest.prefixes)
