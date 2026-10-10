"""The demo's "opening at the clause" and the eval summary's arithmetic,
proven without a model (#31)."""

from __future__ import annotations

from pathlib import Path

from compliance_rag.corpus.models import PUBLIC_TENANT_ID
from compliance_rag.eval.answer_summary import summarise
from compliance_rag.eval.golden_corpus import golden_clauses
from compliance_rag.eval.harness import clause_link, law_source_html


def test_every_law_citation_opens_at_its_own_anchor_in_the_stored_html(tmp_path: Path) -> None:
    html = law_source_html()
    source = tmp_path / "source.html"
    source.write_text(html, encoding="utf-8")
    law = [c for c in golden_clauses() if c.tenant_id == PUBLIC_TENANT_ID]
    assert law
    for clause in law:
        link = clause_link(clause, source)
        assert link is not None and link.startswith(source.resolve().as_uri() + "#")
        anchor = link.split("#", 1)[1]
        assert f'id="{anchor}"' in html, clause.clause_id


def test_a_policy_citation_has_no_html_anchor(tmp_path: Path) -> None:
    policy = next(c for c in golden_clauses() if c.tenant_id != PUBLIC_TENANT_ID)
    assert clause_link(policy, tmp_path / "x.html") is None


def test_the_summary_states_the_projection_with_its_arithmetic() -> None:
    def row(verified: bool, cost: float, hit: bool, outcome: str) -> dict[str, object]:
        return {
            "schema_valid": True,
            "verified": verified,
            "tenant_id": "acme",
            "cost_usd": cost,
            "repairs": [],
            "final_citations": [{"outcome": outcome}],
            "generation": {
                "input_tokens": 7000,
                "cache_read_input_tokens": 5000 if hit else 0,
                "cache_creation_input_tokens": 0 if hit else 5000,
                "output_tokens": 400,
                "cached_prefix_hit": hit,
            },
        }

    summary = summarise(
        [
            row(True, 0.02, False, "verified"),
            row(False, 0.01, True, "quote_mismatch"),
            {"schema_valid": False},
        ]
    )
    assert summary["schema_valid"] == "2/3"
    assert summary["answers_verified"] == "1/2"
    assert summary["citations_verified"] == "1/2"
    assert summary["cache_hits_on_prefixed_requests"] == "1/2"
    assert abs(summary["mean_cost_per_query_usd"] - 0.015) < 1e-12
    assert abs(summary["projected_monthly_usd"] - 30.0) < 1e-9
    assert summary["projection_arithmetic"] == "2000 queries x $0.015000 mean = $30.00/month"
