"""`display_form()`: a clause path rendered the way the profession cites it
(`CONTEXT.md`: Display form, issue #7), and `cite()`, the one form every
surface shows (ADR-0049, #239).

Library law's is a pure function of the path (plus the instrument id, needed
to pick the citation convention — `annexa/8/3` and `art9/2/b` are rendered by
different rules even though both are three segments). A policy clause's names
its document by the title the ledger records with its version. Never stored:
storing both a form and what it is made from lets them drift.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from compliance_rag.assessment.coverage import ObligationCoverage
from compliance_rag.corpus.display_form import NO_TITLES, cite, cited_policy_versions, display_form
from compliance_rag.corpus.models import Citation, ClauseRecord, validate_title
from compliance_rag.corpus.policy_parser import parse_policy
from compliance_rag.eval.golden_corpus import (
    LAW_FIXTURE,
    LAW_TITLE,
    POLICY_INSTRUMENT_IDS,
    front_matter_title,
    gate_law_clauses,
    golden_clauses,
    policy_titles,
)
from compliance_rag.graph.mappings import Obligation

PRIVACY_ACT = "privacy-act-1988"
EU_AI_ACT = "eu-ai-act"
ISO_27001 = "iso-27001"

TABLE_CASES: list[tuple[str, str, str]] = [
    (PRIVACY_ACT, "sch1/app11/1", "APP 11.1"),
    (PRIVACY_ACT, "sch1/app5/2/b", "APP 5.2(b)"),
    (PRIVACY_ACT, "s6c/1/a", "s 6C(1)(a)"),
    (PRIVACY_ACT, "s6/1", "s 6(1)"),
    (PRIVACY_ACT, "s6da/1", "s 6DA(1)"),
    (PRIVACY_ACT, "s6ea/3/b", "s 6EA(3)(b)"),
    (PRIVACY_ACT, "s6e/1a", "s 6E(1A)"),
    (PRIVACY_ACT, "s6d/4/a/ii", "s 6D(4)(a)(ii)"),
    (PRIVACY_ACT, "s6/1/app-entity", 's 6(1) "app-entity"'),
    (PRIVACY_ACT, "s6/1/de-identified", 's 6(1) "de-identified"'),
    (PRIVACY_ACT, "s6/1/acc", 's 6(1) "acc"'),
    (PRIVACY_ACT, "s6/1/agency/c/ii", 's 6(1) "agency" (c)(ii)'),
    (PRIVACY_ACT, "s6/1/agency/c/iia", 's 6(1) "agency" (c)(iia)'),
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


def test_every_definition_in_the_constructs_fixture_reads_as_a_definition() -> None:
    """The paragraph-or-slug rule against real paths: every s 6(1) definition
    the parser emits from the constructs fixture renders as a quoted term, and
    every paragraph below one as a bracket."""
    from pathlib import Path

    from compliance_rag.corpus.parser import parse_compilation

    fixture = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "research"
        / "fixtures"
        / "privacy-act-1988-C2026C00227.constructs.html"
    )
    parsed = parse_compilation(
        fixture.read_text(encoding="utf-8"), instrument_id=PRIVACY_ACT, version_id="C2026C00227"
    )
    definitions = [c.clause_path for c in parsed.clauses if c.clause_path.startswith("s6/1/")]
    assert definitions
    for path in definitions:
        slug, *paragraphs = path.split("/")[2:]
        rendered = display_form(path, PRIVACY_ACT)
        assert f'"{slug}"' in rendered, rendered
        assert all(f"({p})" in rendered for p in paragraphs), rendered


# ---- a policy clause's form: its document's recorded title and its number (#239)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "docs/research/fixtures"


def _policy_fixtures() -> list[Path]:
    return sorted((FIXTURES_DIR / "synthetic-policies").glob("*.md"))


def test_every_synthetic_policy_fixture_is_tested_not_a_sample() -> None:
    """CLAUDE.md: a fixture not exercised is a fixture that isn't testing
    anything. The glob below is the set; this pins that it is all six and
    that each has the instrument id the golden set records it under."""
    assert {p.name for p in _policy_fixtures()} == set(POLICY_INSTRUMENT_IDS)
    assert len(_policy_fixtures()) == 6


@pytest.mark.parametrize("fixture", _policy_fixtures(), ids=lambda p: p.stem)
def test_a_policy_clause_is_cited_by_its_documents_title_and_its_number(fixture: Path) -> None:
    """Every clause of every synthetic policy: "{title}, {N}.{M}", the title as
    recorded (the front matter's, which the document's `#` heading repeats)
    and the number as the document writes it. Deterministic: the same ids
    and titles give the same form, which is all any surface computes it
    from (ADR-0049)."""
    source = fixture.read_text(encoding="utf-8")
    instrument_id = POLICY_INSTRUMENT_IDS[fixture.name]
    title = front_matter_title(source)
    assert f"\n# {title}\n" in source  # the document's own heading names it so
    clauses = parse_policy(source, instrument_id=instrument_id, version_id="v1", tenant_id="acme")
    assert clauses
    titles = {(instrument_id, "v1"): title}
    written = {line.split(" ", 1)[0] for line in source.splitlines() if line[:1].isdigit()}
    for clause in clauses:
        section, paragraph = clause.clause_path.split("/")
        number = f"{section}.{paragraph}"
        assert number in written  # "3.1" is how the document numbers it
        assert cite(clause.clause_id, titles) == f"{title}, {number}"
        assert cite(clause.clause_id, dict(titles)) == cite(clause.clause_id, titles)


def test_the_acme_policies_read_as_their_titles() -> None:
    titles = {(i, "v1"): t for i, t in policy_titles().items()}
    assert cite("acme/data-security-policy:v1:3/1", titles) == (
        "Acme Pty Ltd — Data Security and Retention Policy (Internal), 3.1"
    )
    assert cite("acme/privacy-policy:v1:6/2", titles) == "Acme Pty Ltd — Privacy Policy, 6.2"


def test_a_title_is_read_per_version_and_never_from_another_version_or_tenant() -> None:
    titles = {("acme/privacy-policy", "v1"): "Acme Privacy Policy"}
    assert cite("acme/privacy-policy:v1:3/1", titles) == "Acme Privacy Policy, 3.1"
    # Not recorded for this version: the number alone, never a title made up
    # from the instrument id.
    assert cite("acme/privacy-policy:v2:3/1", titles) == "3.1"
    assert cite("other/privacy-policy:v1:3/1", titles) == "3.1"


def test_a_law_shaped_path_in_a_tenants_document_keeps_its_law_form_after_the_title() -> None:
    titles = {("acme/supplied-act", "v1"): "A supplied Act"}
    assert cite("acme/supplied-act:v1:s6c/1/a", titles) == "A supplied Act, s 6C(1)(a)"


@pytest.mark.parametrize(
    "clauses",
    [golden_clauses, gate_law_clauses],
    ids=["golden-excerpt", "whole-compilation"],
)
def test_library_law_is_cited_as_before_whatever_titles_are_read(
    clauses: Callable[[], tuple[ClauseRecord, ...]],
) -> None:
    """Library law's form is `display_form` alone ("APP 11.1", "s 6(1)"):
    a title recorded for its version, even one read by mistake, never
    reaches it. Every clause of the whole compilation, not a sample."""
    law = [c for c in clauses() if c.tenant_id == "_public"]
    assert law
    titles = {(c.instrument_id, c.version_id): "Privacy Act 1988" for c in law}
    for clause in law:
        expected = display_form(clause.clause_path, clause.instrument_id)
        assert cite(clause.clause_id, titles) == expected == cite(clause.clause_id, NO_TITLES)
    assert cite("privacy-act-1988:C2026C00227:sch1/app11/1", titles) == "APP 11.1"
    assert cite("privacy-act-1988:C2026C00227:s6/1", titles) == "s 6(1)"


def test_only_policy_versions_have_titles_to_read() -> None:
    assert cited_policy_versions(
        [
            "acme/privacy-policy:v1:3/1",
            "acme/privacy-policy:v1:3/2",
            "acme/vendor-policy:v1:1/1",
            "privacy-act-1988:C2026C00227:sch1/app11/1",
        ]
    ) == {("acme/privacy-policy", "v1"), ("acme/vendor-policy", "v1")}


def test_the_law_title_recorded_is_the_compilations_own() -> None:
    """The title the operator records the Act under is the one in its source's
    `<title>`, for the excerpt the golden ledger records and the compilation
    the load script does."""
    from scripts.ingest_compilation import TITLE

    excerpt = LAW_FIXTURE.read_text(encoding="utf-8")
    assert f"<title>{LAW_TITLE}</title>" in excerpt
    assert TITLE == LAW_TITLE == "Privacy Act 1988"


@pytest.mark.parametrize(
    "source",
    [
        "# No front matter\n\n## 1. A\n\n1.1 Text.\n",
        "---\nauthor: x\n---\n# Untitled\n",
        "---\ntitle: '  '\n---\n# Blank\n",
    ],
)
def test_a_fixture_without_a_front_matter_title_is_refused_not_guessed(source: str) -> None:
    with pytest.raises(ValueError, match="no title"):
        front_matter_title(source)


@pytest.mark.parametrize("title", ["", "   ", "Acme\nPolicy", "Acme\u2028Policy", "Acme\tPolicy"])
def test_a_title_is_one_non_empty_line(title: str) -> None:
    with pytest.raises(ValueError, match="title"):
        validate_title(title)


def test_a_title_may_hold_any_printable_text() -> None:
    for title in ('Acme "Customer" Policy [v2]', "Política de Privacidad — Acme", "A] excepts x"):
        assert validate_title(title) == title
