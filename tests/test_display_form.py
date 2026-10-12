"""`display_form()`: a clause path rendered the way the profession cites it
(`CONTEXT.md`: Display form, issue #7).

Pure function of the path (plus the instrument id, needed to pick the
citation convention — `annexa/8/3` and `art9/2/b` are rendered by different
rules even though both are three segments). Never stored: storing both a
path and a label lets them drift, and the path is the authority.
"""

from __future__ import annotations

import pytest

from compliance_rag.assessment.coverage import ObligationCoverage
from compliance_rag.corpus.display_form import display_form
from compliance_rag.corpus.models import Citation, ClauseRecord
from compliance_rag.graph.mappings import Obligation

PRIVACY_ACT = "privacy-act-1988"
EU_AI_ACT = "eu-ai-act"
ISO_27001 = "iso-27001"

TABLE_CASES: list[tuple[str, str, str]] = [
    (PRIVACY_ACT, "sch1/app11/1", "APP 11.1"),
    (PRIVACY_ACT, "sch1/app5/2/b", "APP 5.2(b)"),
    (PRIVACY_ACT, "s6c/1/a", "s 6C(1)(a)"),
    (PRIVACY_ACT, "s6/1", "s 6(1)"),
    (EU_AI_ACT, "art9/2/b", "Art 9(2)(b)"),
    (ISO_27001, "annexa/8/3", "A.8.3"),
]


@pytest.mark.parametrize(
    "instrument_id,clause_path,expected",
    TABLE_CASES,
    ids=[c[1] for c in TABLE_CASES],
)
def test_display_form_table(instrument_id: str, clause_path: str, expected: str) -> None:
    assert display_form(clause_path, instrument_id) == expected


def test_a_path_with_no_sub_clause() -> None:
    assert display_form("s6", PRIVACY_ACT) == "s 6"
    assert display_form("art9", EU_AI_ACT) == "Art 9"
    assert display_form("annexa/8", ISO_27001) == "A.8"


def test_a_four_level_path() -> None:
    assert display_form("s6c/1/a/i", PRIVACY_ACT) == "s 6C(1)(a)(i)"


def test_an_unknown_convention_falls_back_to_something_legible_rather_than_raising() -> None:
    result = display_form("reg4/2", "unknown-instrument")
    assert result  # never raises, never empty
    assert "reg4" in result and "2" in result


def test_nothing_stores_a_display_form() -> None:
    """CONTEXT.md's Display form entry: storing both a path and a label lets
    them drift, so no model field may hold one."""
    for model in (ClauseRecord, Citation, Obligation, ObligationCoverage):
        field_names = getattr(model, "model_fields", None) or {
            f.name
            for f in model.__dataclass_fields__.values()  # type: ignore[attr-defined]
        }
        assert "display_form" not in field_names
        assert "display_text" not in field_names
