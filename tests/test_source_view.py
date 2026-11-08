"""The two pages click-to-clause serves (#277): stored law, made safe without
losing a clause anchor, and a tenant's Markdown, rendered with the policy
parser's anchors. Every real fixture, not one: both law fixtures and all six
Acme policies."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

from compliance_rag.corpus.models import clause_anchor_id
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.policy_parser import parse_policy_markdown
from compliance_rag.corpus.source_view import (
    ALLOWED_TAGS,
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

IDS = re.compile(r'\bid="([^"]*)"')
LAW_FIXTURES = sorted(FIXTURES.glob("privacy-act-1988-C2026C00227.*.html"))
TITLE = "Privacy Act 1988"


def _text(page: str) -> str:
    return " ".join(
        html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<!--.*?-->", "", page))).split()
    )


def _annotated(path: Path) -> str:
    """A law fixture as the ingest stores it: the parser's anchors written in."""
    return parse_compilation(
        path.read_text(encoding="utf-8"), instrument_id="privacy-act-1988", version_id="C2026C00227"
    ).annotated_html


def _body(source: str) -> str:
    return source[source.rindex("<body") : source.rindex("</body>")]


def test_there_are_law_fixtures_to_run_on() -> None:
    assert len(LAW_FIXTURES) == 2


@pytest.mark.parametrize("path", LAW_FIXTURES, ids=lambda p: p.name)
def test_rebuilding_a_real_law_source_keeps_every_clause_anchor_and_every_word(
    path: Path,
) -> None:
    """Both real fixtures (the excerpt and the constructs cut from the full
    compilation, whose source isn't in the repository, only its hash), as the
    parser anchors them: every id, in order, and every word of the body."""
    source = _annotated(path)
    page = sanitise_source_html(source, TITLE)
    assert IDS.findall(page) == IDS.findall(source) and IDS.findall(source)
    clauses = parse_compilation(
        path.read_text(encoding="utf-8"), instrument_id="privacy-act-1988", version_id="C2026C00227"
    ).clauses
    assert len(clauses) > 20
    for clause in clauses:
        assert f'id="{clause_anchor_id(clause.clause_path)}"' in page, clause.clause_id
    shown = _text(page[page.index("<body") :])
    assert shown == _text(_body(source)[_body(source).index(">") + 1 :])
    assert TARGET_STYLE in page and f"<title>{TITLE}</title>" in page


def test_the_elements_the_real_compilation_uses_are_all_allowed() -> None:
    used = {
        t
        for path in LAW_FIXTURES
        for t in re.findall(r"<([a-z][a-z0-9]*)", _body(path.read_text(encoding="utf-8")))
    }
    # `body` is the wrapper; `img` is the one thing dropped, a picture with no text.
    assert used - {"body", "img"} == {"span", "p", "a", "div"}
    assert used - {"body", "img"} <= ALLOWED_TAGS


BYPASSES = {
    "noscript attribute": '<noscript><a title="</noscript><img src=x onerror=alert(1)>">',
    "math mglyph style": "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>",
    "svg animate href": '<svg><a><animate attributeName=href values="javascript:alert(1)" />'
    "<text>click</text></a></svg>",
    "svg set": '<svg><a><set attributeName=href to="javascript:alert(1)" /></a></svg>',
    "script": "<script>alert(1)</script>",
    "script, upper case, attributes": '<SCRIPT type="text/javascript" src="//evil.example/x.js">',
    "event handler": '<p onclick="alert(1)" onmouseover=alert(1)>x</p>',
    "javascript href": '<a href="javascript:alert(1)">x</a>',
    "obfuscated javascript href": '<a href="java&#9;script:alert(1)">x</a>',
    "data href": '<a href="data:text/html,<script>alert(1)</script>">x</a>',
    "external href": '<a href="https://evil.example/">x</a>',
    "srcdoc": '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    "iframe": '<iframe src="https://evil.example"></iframe>',
    "object and embed": '<object data="x.swf"></object><embed src="x.swf">',
    "form": '<form action="https://evil.example"><button formaction="javascript:x()">go</button>'
    "<input name=a></form>",
    "meta refresh": '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    "base": '<base href="https://evil.example/">',
    "link": '<link rel="stylesheet" href="https://evil.example/x.css">',
    "style import": "<style>@import 'https://evil.example/x.css';</style>",
    "style attribute": '<p style="background:url(https://evil.example/x)">x</p>',
    "template": "<template><script>alert(1)</script></template>",
    "image": "<img src=x onerror=alert(1)>",
    "comment": "<!--[if IE]><script>alert(1)</script><![endif]-->",
    "cdata": "<![CDATA[<script>alert(1)</script>]]>",
    "unclosed script": "<script>alert(1)",
}
DANGEROUS = re.compile(
    r"<\s*(script|style|noscript|template|iframe|object|embed|math|svg|img|form|input|button"
    r"|meta|base|link|animate|set|mglyph)\b|\son[a-z]+\s*=|javascript|data:text|srcdoc"
    r"|formaction|evil\.example|alert\(|http-equiv",
    re.IGNORECASE,
)


@pytest.mark.parametrize("name", BYPASSES)
def test_nothing_that_can_run_survives_the_rebuild(name: str) -> None:
    page = sanitise_source_html(
        f'<html><body><p id="clause-a">kept</p>{BYPASSES[name]}</body></html>', TITLE
    )
    body = page[page.index("<body>") :]
    assert not DANGEROUS.search(body), (name, body)
    assert '<p id="clause-a">kept</p>' in body


def test_the_whole_attack_list_together_is_inert() -> None:
    page = sanitise_source_html("<body>" + "".join(BYPASSES.values()) + "</body>", TITLE)
    assert not DANGEROUS.search(page[page.index("<body>") :])


def test_only_allowlisted_elements_and_attributes_are_written() -> None:
    page = sanitise_source_html(
        '<html><head><title>T</title></head><body><div id="d" class="c" lang="x" data-k="v">'
        '<p id="clause-a" class="x" style="color:red" title="t">'
        '<a href="#clause-b" onclick="x()" target="_blank">in</a> '
        '<a href="/elsewhere">out</a> <a href="#a b">spaced</a></p>'
        '<table><tr><td colspan="2" rowspan="x">c</td></tr></table>'
        "<custom-element>unknown <b>inside</b></custom-element>"
        "<span>after</span></div></body></html>",
        TITLE,
    )
    body = page[page.index("<body>") + 6 : page.index("</body>")]
    assert body == (
        '<div id="d" class="c"><p id="clause-a" class="x">'
        '<a href="#clause-b">in</a> <a>out</a> <a>spaced</a></p>'
        '<table><tr><td colspan="2">c</td></tr></table><span>after</span></div>'
    )


def test_the_output_is_balanced_whatever_the_source_is() -> None:
    page = sanitise_source_html("<body><div><p>one</div></p></span><p>two<div>three", TITLE)
    body = page[page.index("<body>") + 6 : page.index("</body>")]
    assert body == "<div><p>one</p></div><p>two<div>three</div></p>"


def test_text_is_escaped_not_passed_through() -> None:
    page = sanitise_source_html(
        "<body><p>a &lt;script&gt;x&lt;/script&gt; &amp; b &#169; c</p></body>", TITLE
    )
    assert "<p>a &lt;script&gt;x&lt;/script&gt; &amp; b \u00a9 c</p>" in page


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
