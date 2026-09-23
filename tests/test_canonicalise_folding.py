"""canonicalise()'s Unicode-folding table, one assertion per character.

Every character folded here was found by the ePub format spike's full-
document Unicode audit (docs/research/legislation-gov-au-format.md,
issue #9) - this is the exact set real legislative text actually uses, not
a speculative list. The audit also found U+2022 (bullet) and U+2026
(ellipsis), but only inside Schedule 2's outline and the endnotes'
abbreviation legend - outside Phase 1's ingest scope (Schedule 1 plus
ss 6, 6C-6E) - so they are not folded here; add them when a phase actually
ingests that text.

Every character below is built with chr(), never a literal, so there is
no ambiguous-character glyph anywhere in this file's source for a reader
(or ruff's RUF001/RUF002) to mistake for something else.
"""

from __future__ import annotations

import pytest

from compliance_rag.corpus.models import canonicalise

LEFT_SINGLE_QUOTE = chr(0x2018)
RIGHT_SINGLE_QUOTE = chr(0x2019)
LEFT_DOUBLE_QUOTE = chr(0x201C)
RIGHT_DOUBLE_QUOTE = chr(0x201D)
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)
NO_BREAK_SPACE = chr(0x00A0)
NON_BREAKING_HYPHEN = chr(0x2011)

FOLD_CASES: list[tuple[str, str, str]] = [
    ("left single quote", LEFT_SINGLE_QUOTE, "'"),
    ("right single quote", RIGHT_SINGLE_QUOTE, "'"),
    ("left double quote", LEFT_DOUBLE_QUOTE, '"'),
    ("right double quote", RIGHT_DOUBLE_QUOTE, '"'),
    ("en dash", EN_DASH, "-"),
    ("em dash", EM_DASH, "-"),
    ("non-breaking space", NO_BREAK_SPACE, " "),
    ("non-breaking hyphen", NON_BREAKING_HYPHEN, "-"),
]


@pytest.mark.parametrize("name,char,expected", FOLD_CASES, ids=[case[0] for case in FOLD_CASES])
def test_character_folds_to_ascii(name: str, char: str, expected: str) -> None:
    assert canonicalise(f"a{char}b") == f"a{expected}b"


def test_non_breaking_hyphen_in_real_compound_words() -> None:
    """The exact real-text forms the spike found: cross-border, out-of-date
    and de-identified all use U+2011 in the source, not ASCII '-'
    (docs/research/legislation-gov-au-format.md).
    """
    nbh = NON_BREAKING_HYPHEN
    assert canonicalise(f"cross{nbh}border") == "cross-border"
    assert canonicalise(f"out{nbh}of{nbh}date") == "out-of-date"
    assert canonicalise(f"de{nbh}identified") == "de-identified"


def test_a_citation_quoting_an_ascii_hyphen_matches_source_using_non_breaking_hyphen() -> None:
    """The false-negative case the spike flagged: an LLM or user typing an
    ordinary hyphen must still match source text using U+2011.
    """
    assert canonicalise("cross-border") == canonicalise(f"cross{NON_BREAKING_HYPHEN}border")
