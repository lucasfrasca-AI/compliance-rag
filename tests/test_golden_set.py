"""`corpus/golden/queries.yaml` (issue #27): it parses against the schema, and
every label is a stable ID that resolves against the parsed fixtures — the law
excerpt and the six synthetic policies — not merely one that is well-formed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Scope
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.policy_parser import parse_policy
from compliance_rag.eval.golden import Category, GoldenSet, load_golden_set

FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "research" / "fixtures"

# The instrument ids the golden set's policy labels use (agreed for #27; the
# first two match tests/test_policy_parser.py).
POLICY_INSTRUMENT_IDS = {
    "acme-privacy-policy.md": "acme/privacy-policy",
    "acme-vendor-cross-border-transfer-policy.md": "acme/vendor-policy",
    "acme-data-security-retention-policy.md": "acme/data-security-policy",
    "acme-marketing-communications-policy.md": "acme/marketing-policy",
    "acme-access-correction-procedure.md": "acme/access-correction-procedure",
    "acme-employee-data-handling-policy.md": "acme/employee-policy",
}


@pytest.fixture(scope="module")
def golden() -> GoldenSet:
    return load_golden_set()


@pytest.fixture(scope="module")
def corpus_clause_ids() -> frozenset[str]:
    html = (FIXTURES / "privacy-act-1988-C2026C00227.excerpt.html").read_text(encoding="utf-8")
    law = parse_compilation(
        html,
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
        tenant_id=PUBLIC_TENANT_ID,
        scope=Scope.PUBLIC,
    ).clauses
    ids = {c.clause_id for c in law}
    for filename, instrument_id in POLICY_INSTRUMENT_IDS.items():
        text = (FIXTURES / "synthetic-policies" / filename).read_text(encoding="utf-8")
        ids |= {
            c.clause_id
            for c in parse_policy(
                text, instrument_id=instrument_id, version_id="v1", tenant_id="acme"
            )
        }
    return frozenset(ids)


def test_every_label_resolves_against_the_parsed_fixtures(
    golden: GoldenSet, corpus_clause_ids: frozenset[str]
) -> None:
    unresolved = [
        (q.id, clause_id)
        for q in golden.queries
        for clause_id in q.expected_clause_ids
        if clause_id not in corpus_clause_ids
    ]
    assert unresolved == []


def test_every_category_is_represented(golden: GoldenSet) -> None:
    assert {q.category for q in golden.queries} == set(Category)


def test_the_labels_are_signed_by_a_person_not_the_drafting_model(golden: GoldenSet) -> None:
    """D-01: the model drafted, a person reviewed. The recall gate (#28)
    refuses an unsigned file, so this is what makes these labels count."""
    assert golden.is_signed_off
    assert golden.author.startswith("model:")
    assert not golden.reviewed_by.startswith("model:")


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        (
            {
                "category": "exact_id",
                "query": "privacy-act-1988:C1:s6/1",
                "expected_clause_ids": ["privacy-act-1988:C1:s6/2"],
            },
            "is its one expected clause id",
        ),
        (
            {
                "category": "paraphrase",
                "expected_clause_ids": ["privacy-act-1988:C1:s6/1", "privacy-act-1988:C1:s6/2"],
            },
            "only cross_clause",
        ),
        (
            {"category": "cross_clause", "expected_clause_ids": ["privacy-act-1988:C1:s6/1"]},
            "at least two",
        ),
        (
            {"tenant_id": "acme", "expected_clause_ids": ["privacy-act-1988:C1:s6/1"]},
            "environment",
        ),
    ],
    ids=["exact-id-mismatch", "paraphrase-many", "cross-clause-one", "crosses-scope"],
)
def test_the_schema_refuses_a_malformed_entry(entry: dict[str, object], problem: str) -> None:
    base: dict[str, object] = {
        "id": "GQ-01",
        "category": "paraphrase",
        "query": "q",
        "tenant_id": "_public",
        "as_of": "2026-10-01",
        "expected_clause_ids": ["privacy-act-1988:C1:s6/1"],
    }
    document = {"author": "a", "reviewed_by": "b", "date": "d", "queries": [base | entry]}
    with pytest.raises(ValueError, match=problem):
        GoldenSet.model_validate(document)


def _document(**front_matter: str) -> dict[str, object]:
    raw = yaml.safe_load(
        Path(__file__).resolve().parents[1].joinpath("corpus/golden/queries.yaml").read_text()
    )
    return raw | front_matter


def test_a_model_never_reviews_its_own_or_any_draft() -> None:
    with pytest.raises(ValueError, match="never a model"):
        GoldenSet.model_validate(_document(reviewed_by="model: Claude Opus 5.5"))


def test_a_signed_file_carries_a_real_review_date() -> None:
    with pytest.raises(ValueError):
        GoldenSet.model_validate(_document(date="last week"))
