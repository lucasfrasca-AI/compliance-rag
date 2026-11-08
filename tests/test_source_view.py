"""The two pages click-to-clause serves (#277): stored law, made safe without
losing a clause anchor, and a tenant's Markdown, rendered with the policy
parser's anchors. Every real fixture, not one: both law fixtures and all six
Acme policies."""

from __future__ import annotations

import html
import re

import pytest

from compliance_rag.corpus.models import clause_anchor_id
from compliance_rag.corpus.policy_parser import parse_policy_markdown
from compliance_rag.corpus.source_view import (
    TARGET_STYLE,
    render_policy_html,
    sanitise_source_html,
)
from compliance_rag.eval.golden_corpus import (
    FIXTURES,
    POLICY_INSTRUMENT_IDS,
    policy_source,
    policy_titles,
)
from compliance_rag.eval.harness import law_source_html

IDS = re.compile(r'\bid="([^"]*)"')
LAW_FIXTURES = sorted(FIXTURES.glob("privacy-act-1988-C2026C00227.*.html"))


def _text(page: str) -> str:
    return " ".join(
        html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<!--.*?-->", "", page))).split()
    )


def test_there_are_law_fixtures_to_run_on() -> None:
    assert len(LAW_FIXTURES) == 2


@pytest.mark.parametrize("source", [law_source_html(), *(p.read_text() for p in LAW_FIXTURES)])
def test_sanitising_a_real_law_source_keeps_every_anchor_and_every_word(source: str) -> None:
    page = sanitise_source_html(source)
    assert IDS.findall(page) == IDS.findall(source) and IDS.findall(source)
    assert _text(page.replace(TARGET_STYLE, "")) == _text(source)
    assert TARGET_STYLE in page


def test_sanitising_drops_what_can_run_and_nothing_else() -> None:
    page = sanitise_source_html(
        '<html><head><title>T</title></head><body><p id="clause-a" class="x" data-k="v">'
        '<a href="https://example.com/a?b=1&amp;c=2" onclick="x()">ok &amp; fine &#169;</a>'
        '<a href=" JaVa\tScRiPt:x()">bad</a><script>x()</script></p></body></html>'
    )
    assert '<p id="clause-a" class="x" data-k="v">' in page
    assert '<a href="https://example.com/a?b=1&amp;c=2">ok &amp; fine &#169;</a>' in page
    assert "<a>bad</a>" in page and "x()" not in page and "onclick" not in page


def test_an_unterminated_script_swallows_the_rest_rather_than_leaking_it() -> None:
    page = sanitise_source_html('<body><p id="a">kept</p><script>steal()</body>')
    assert "kept" in page and "steal" not in page


def test_a_script_tag_a_parser_missed_is_made_text() -> None:
    page = sanitise_source_html('<body><p title="<script>x</script>">t</p></body>')
    assert "<script" not in page.lower() and "&lt;script" in page


@pytest.mark.parametrize("filename", POLICY_INSTRUMENT_IDS)
def test_a_rendered_policy_anchors_every_clause_the_parser_makes(filename: str) -> None:
    markdown = policy_source(filename)
    clauses = parse_policy_markdown(
        markdown, instrument_id=POLICY_INSTRUMENT_IDS[filename], version_id="v1", tenant_id="acme"
    )
    page = render_policy_html(markdown, title=policy_titles()[POLICY_INSTRUMENT_IDS[filename]])
    assert clauses
    anchors = IDS.findall(page)
    assert anchors == [clause_anchor_id(c.clause_path) for c in clauses]
    assert len(set(anchors)) == len(anchors)


def test_the_documents_own_title_heading_is_not_repeated() -> None:
    markdown = policy_source("acme-privacy-policy.md")
    title = policy_titles()["acme/privacy-policy"]
    page = render_policy_html(markdown, title=title)
    assert page.count(html.escape(title)) == 2  # <title> and the one <h1>
    renamed = render_policy_html(markdown, title="A later title")
    assert "<h2>" + html.escape(title) in renamed  # kept when the recorded title differs
