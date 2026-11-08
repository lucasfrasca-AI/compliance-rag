"""A stored source as a page a browser opens at a clause (I2, ADR-0053; the
anchors are ADR-0019's).

Two kinds of stored source, one anchor scheme: every clause a citation can
name has an element with `id="{clause_anchor_id(clause_path)}"`.

- **Law** is third-party markup, stored with the parser's synthesized anchors
  written in (`ClauseStore.get_source_html`). `sanitise_source_html` serves it
  as it is, minus anything that can run: scripts, event handlers,
  `javascript:` URLs, frames, objects and the like. Every `id` is kept, so the
  anchors survive. This is a second line of defence: the response's CSP
  (`api/sources.py`) is the first, and neither is relied on alone.
- **A tenant's document** is stored as the Markdown its publisher uploaded
  (ADR-0048). `render_policy_html` builds the page from it with the policy
  parser's own line grammar, so a clause is anchored exactly where the parser
  made one: `## N.` opens a section and `N.M ` starts clause `N/M`. Every
  character of the source goes through `html.escape`, so the page holds no
  markup the document did not write as text, whatever the document says.

Both are pure: no store, no network.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from html.parser import HTMLParser

from compliance_rag.corpus.models import clause_anchor_id
from compliance_rag.corpus.policy_parser import HEADING_RE, SUBSECTION_RE, strip_front_matter

TARGET_STYLE = (
    "<style>:target{background:#fff1b8;outline:3px solid #d4a017;scroll-margin-top:4rem}</style>"
)
"""Marks the clause a link opened at. Added to a page; a stored object is
never changed."""

# ---- law: third-party markup, minus whatever can run ---------------------------------

_DROPPED_TAGS = frozenset(
    {"script", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "link"}
)
"""Elements that load or run something. Their own markup is dropped; `script`'s
content too (it is code). A `style` element stays: it is a stylesheet, and the
CSP allows inline styles only."""
_DROPPED_META = frozenset({"refresh", "set-cookie", "content-security-policy"})
_URL_ATTRIBUTES = frozenset({"href", "src", "xlink:href", "action", "formaction", "srcdoc", "data"})
_DANGEROUS_URL_RE = re.compile(r"^(?:javascript|vbscript|data\s*:\s*text/html)", re.IGNORECASE)
_URL_NOISE_RE = re.compile(r"[\x00-\x20\x7f]+")
_SCRIPT_OPEN_RE = re.compile(r"<(\s*/?\s*script)", re.IGNORECASE)


def _is_dropped_attribute(name: str, value: str | None) -> bool:
    lowered = name.lower()
    if lowered.startswith("on"):
        return True
    if lowered in _URL_ATTRIBUTES and value is not None:
        # A browser ignores whitespace and control characters inside a scheme
        # ("java\tscript:"), so the check does too.
        return _DANGEROUS_URL_RE.match(_URL_NOISE_RE.sub("", value)) is not None
    return False


class _Sanitiser(HTMLParser):
    """Re-emits a document token by token. A start tag with nothing to drop is
    written back exactly as it was (`get_starttag_text`), so ids, classes and
    every other attribute are untouched."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._in_script = False
        self.styled = False

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool) -> None:
        if tag in _DROPPED_TAGS:
            self._in_script = tag == "script" and not closed
            return
        if tag == "meta" and any(
            n.lower() == "http-equiv" and (v or "").strip().lower() in _DROPPED_META
            for n, v in attrs
        ):
            return  # a page that redirects itself, or sets a cookie
        kept = [(n, v) for n, v in attrs if not _is_dropped_attribute(n, v)]
        if len(kept) == len(attrs):
            self.parts.append(self.get_starttag_text() or "")
            return
        rendered = "".join(
            f" {n}" if v is None else f' {n}="{html.escape(v, quote=True)}"' for n, v in kept
        )
        self.parts.append(f"<{tag}{rendered}{' /' if closed else ''}>")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_TAGS:
            self._in_script = False
            return
        if tag == "head" and not self.styled:
            self.parts.append(TARGET_STYLE)
            self.styled = True
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._in_script:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._in_script:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._in_script:
            self.parts.append(f"&#{name};")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    # Comments, processing instructions and CDATA sections are dropped: a
    # conditional comment is markup some browsers read, and none carries a clause.


@lru_cache(maxsize=4)
def sanitise_source_html(source: str) -> str:
    """A stored law source, safe to serve as a page, with the clause the link
    opens at marked (`TARGET_STYLE`). Cached by the source itself, so a source
    revised in place (ADR-0042) is read afresh and an unchanged one is parsed
    once."""
    sanitiser = _Sanitiser()
    sanitiser.feed(source)
    sanitiser.close()
    page = "".join(sanitiser.parts)
    if not sanitiser.styled:
        page = TARGET_STYLE + page
    # A parser can disagree with a browser about where a tag starts. Whatever
    # `<script` is left is made text, which can't run.
    return _SCRIPT_OPEN_RE.sub(r"&lt;\1", page)


# ---- a tenant's document: Markdown, escaped, anchored as the parser anchors ----------

_HEADING_LEVEL_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")

_STYLE = """
body{font:16px/1.55 system-ui,sans-serif;margin:0 auto;max-width:46rem;padding:1.5rem 1rem}
h1{font-size:1.5rem}
h2,h3,h4{margin-top:1.8rem}
.clause{display:flex;gap:.75rem;margin:.9rem 0;padding:.25rem .5rem}
.clause-number{flex:none;font-variant-numeric:tabular-nums;color:#555}
.clause-body p,.clause-body ul{margin:.4rem 0}
:target{background:#fff1b8;outline:3px solid #d4a017;scroll-margin-top:4rem}
"""


def _body(lines: list[str]) -> str:
    """Paragraphs and bullet lists, escaped. A bullet's indented lines continue
    it; a blank line ends a paragraph, not a list."""
    out: list[str] = []
    paragraph: list[str] = []
    items: list[list[str]] = []

    def end_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{html.escape(' '.join(paragraph))}</p>")
            paragraph.clear()

    def end_list() -> None:
        if items:
            lis = "".join(f"<li>{html.escape(' '.join(item))}</li>" for item in items)
            out.append(f"<ul>{lis}</ul>")
            items.clear()

    for line in lines:
        if not line.strip():
            end_paragraph()
            continue
        bullet = _BULLET_RE.match(line.strip())
        if bullet is not None:
            end_paragraph()
            items.append([bullet.group(1)])
        elif line[0].isspace() and items:
            items[-1].append(line.strip())
        else:
            end_list()
            paragraph.append(line.strip())
    end_paragraph()
    end_list()
    return "".join(out)


def render_policy_html(markdown: str, *, title: str) -> str:
    """A tenant's stored Markdown as a page, named by the `title` the ledger
    records with its version (ADR-0049). Each clause the policy parser would
    make (`## N.` then `N.M text`) is an element with
    `id="{clause_anchor_id('N/M')}"`; the document's own `#` title is left out
    when it only repeats the recorded one."""
    blocks: list[str] = []
    prose: list[str] = []
    clause: tuple[str, list[str]] | None = None
    section: str | None = None

    def flush() -> None:
        nonlocal clause
        if clause is not None:
            number, lines = clause
            path = number.replace(".", "/")
            blocks.append(
                f'<div class="clause" id="{clause_anchor_id(path)}">'
                f'<span class="clause-number">{html.escape(number)}</span>'
                f'<div class="clause-body">{_body(lines)}</div></div>'
            )
            clause = None
        if prose:
            blocks.append(_body(prose))
            prose.clear()

    for line in strip_front_matter(markdown).splitlines():
        if (heading := HEADING_RE.match(line)) is not None:
            section = heading.group(1)
        elif (sub := SUBSECTION_RE.match(line)) is not None and sub.group(1) == section:
            flush()
            clause = (f"{sub.group(1)}.{sub.group(2)}", [line[sub.end() :]])
            continue
        if (level := _HEADING_LEVEL_RE.match(line)) is not None:
            flush()
            text = level.group(2)
            if len(level.group(1)) == 1 and text == title:
                continue
            tag = f"h{min(len(level.group(1)) + 1, 6)}"
            blocks.append(f"<{tag}>{html.escape(text)}</{tag}>")
        elif clause is not None:
            clause[1].append(line)
        else:
            prose.append(line)
    flush()
    heading = html.escape(title)
    return (
        '<!doctype html><html lang="en-AU"><head><meta charset="utf-8">'
        f"<title>{heading}</title><style>{_STYLE}</style></head>"
        f"<body><main><h1>{heading}</h1>{''.join(blocks)}</main></body></html>"
    )
