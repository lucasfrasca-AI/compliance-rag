"""Clause-level anchors for click-to-clause (`CONTEXT.md` **Position map**).

The ePub format spike (issue #9, docs/research/legislation-gov-au-format.md)
found stable ids only at section/Part/Schedule level in the source markup —
nothing at clause_path granularity. `clause_anchor_id()` is what the parser
writes into the stored HTML at ingest time, and what citation rendering
recomputes later: derived from `clause_path` alone, so it needs no extra
stored field and is never invented at render time.
"""

from __future__ import annotations

import pytest

from compliance_rag.corpus.models import clause_anchor_id


@pytest.mark.parametrize(
    "clause_path,expected",
    [
        ("sch1/app11/1", "clause-sch1-app11-1"),
        ("s6c/1/a", "clause-s6c-1-a"),
        ("art9/2/b", "clause-art9-2-b"),
    ],
)
def test_anchor_id_is_a_slug_of_the_clause_path(clause_path: str, expected: str) -> None:
    assert clause_anchor_id(clause_path) == expected


def test_anchor_id_handles_a_hyphenated_segment() -> None:
    """A defined-term slug (`CONTEXT.md`: Definition) may itself contain
    '-', e.g. "access-seeker"."""
    assert clause_anchor_id("s6/1/access-seeker") == "clause-s6-1-access--seeker"


def test_anchor_id_is_deterministic() -> None:
    assert clause_anchor_id("sch1/app11/1") == clause_anchor_id("sch1/app11/1")


@pytest.mark.parametrize(
    "path_a,path_b",
    [
        ("s6/1", "s6c/1"),
        # A real segment boundary vs. a '-' that's part of one segment's own
        # text — the exact collision a bare "/".join(...).replace("-") would
        # produce (both would naively become "clause-s6-1-access-seeker").
        ("s6/1/access-seeker", "s6/1-access-seeker"),
        ("s6/1/access-seeker", "s6/1-access/seeker"),
    ],
    ids=["different-instruments", "term-slug-vs-fake-boundary", "term-slug-vs-split-elsewhere"],
)
def test_different_clause_paths_never_collide(path_a: str, path_b: str) -> None:
    assert clause_anchor_id(path_a) != clause_anchor_id(path_b)
