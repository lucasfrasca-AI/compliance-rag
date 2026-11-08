"""A stored source as a page a browser opens at a clause (I2, ADR-0053; the
anchors are ADR-0019's).

Two kinds of stored source, one anchor scheme: every clause a citation can
name has an element with `id="{clause_anchor_id(clause_path)}"`.

- **Law** is third-party markup, stored with the parser's synthesized anchors
  written in (`ClauseStore.get_source_html`). `sanitise_source_html` does not
  filter it, it **rebuilds** it: a page is written from scratch out of the
  stored document's text and its allowlisted structure. Only the elements in
  `ALLOWED_TAGS` are written, each with only `id`, `class`, `colspan`,
  `rowspan` and, on an `a`, a same-document `#fragment` `href`. Every other
  element is dropped with everything inside it (script, style, noscript,
  template, iframe, object, embed, math, svg, img, forms, and any element not
  named), and text is escaped on the way out. Nothing the source says is
  passed through as markup, so a parser that reads a source differently from
  a browser (mutation XSS: `<noscript>`, `<math>`/`<mglyph>`, `<svg><animate>`)
  can't leave something behind: there is nothing to leave.
- **A tenant's document** is stored as the Markdown its publisher uploaded
  (ADR-0048). `render_policy_html` builds the page from it with the policy
  parser's own line grammar, so a clause is anchored exactly where the parser
  made one: `## N.` opens a section and `N.M ` starts clause `N/M`. Every
  character of the source goes through `html.escape`.

The two layers: this module's output is already inert, and the route
(`api/sources.py`) serves it under a CSP that would stop script even if it
weren't. Neither is relied on alone. The allowlist loses presentation (the
compilation's inline `style` attributes and its stylesheet), not text or
anchors.

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

# ---- law: third-party markup, rebuilt from an allowlist ------------------------------

ALLOWED_TAGS = frozenset(
    {"p", "div", "span", "a", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"}
    | {"ul", "ol", "li", "em", "strong", "b", "i", "u", "sup", "sub", "br", "hr", "blockquote"}
    | {"h1", "h2", "h3", "h4", "h5", "h6"}
)
"""What the stored compilation's body is made of: `span`, `p`, `a` and `div`
(the real fixtures use nothing else), and the structural and text elements a
longer Act's tables, lists and notes need. Everything else is dropped."""
_VOID_TAGS = frozenset({"br", "hr"})
_TRANSPARENT_TAGS = frozenset({"html", "body"})
"""The document's own wrapper: dropped, its children kept."""
_VOID_UNKNOWN = frozenset(
    {"img", "input", "link", "meta", "base", "embed", "source", "track", "wbr", "area", "col"}
    | {"param", "keygen", "basefont", "frame"}
)
"""Elements with no end tag: dropped alone, so nothing after them is skipped."""
_PLAIN_ATTRIBUTES = frozenset({"id", "class"})
_SPAN_ATTRIBUTES = frozenset({"colspan", "rowspan"})
_FRAGMENT_RE = re.compile(r"#[A-Za-z0-9_.:-]*\Z")
_SPAN_VALUE_RE = re.compile(r"[0-9]{1,3}\Z")
_PAGE_STYLE = (
    "<style>body{font:16px/1.5 system-ui,sans-serif;margin:0 auto;max-width:60rem;"
    "padding:1rem}table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:.2rem .4rem}"
    "</style>"
)


def _kept_attributes(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    kept: list[str] = []
    for name, value in attrs:
        name = name.lower()
        if value is None:
            continue
        if name in _PLAIN_ATTRIBUTES or (
            name in _SPAN_ATTRIBUTES and _SPAN_VALUE_RE.match(value) is not None
        ):
            kept.append(f' {name}="{html.escape(value, quote=True)}"')
        elif tag == "a" and name == "href" and _FRAGMENT_RE.match(value) is not None:
            kept.append(f' href="{html.escape(value, quote=True)}"')
    return "".join(kept)


class _Rebuilder(HTMLParser):
    """Writes a new page from a stored document's token stream. It keeps a
    stack of the allowlisted elements it has opened, so what it writes is
    balanced whatever the source was, and a closing tag the source has with no
    opening one closes nothing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open: list[str] = []
        self._skipping: str | None = None
        self._skip_depth = 0

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool) -> None:
        if self._skipping is not None:
            if tag == self._skipping and not closed:
                self._skip_depth += 1
            return
        if tag in _TRANSPARENT_TAGS or tag in _VOID_UNKNOWN:
            return
        if tag not in ALLOWED_TAGS:
            if not closed:  # dropped with everything inside it
                self._skipping, self._skip_depth = tag, 1
            return
        self.parts.append(f"<{tag}{_kept_attributes(tag, attrs)}>")
        if tag not in _VOID_TAGS:
            if closed:
                self.parts.append(f"</{tag}>")
            else:
                self._open.append(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        if self._skipping is not None:
            if tag == self._skipping:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skipping = None
            return
        if tag in self._open:
            while self._open:
                closing = self._open.pop()
                self.parts.append(f"</{closing}>")
                if closing == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._skipping is None:
            self.parts.append(html.escape(data, quote=False))

    # Comments, declarations, processing instructions and CDATA sections are
    # never written.

    def finish(self) -> str:
        self.close()
        self.parts.extend(f"</{tag}>" for tag in reversed(self._open))
        self._open.clear()
        return "".join(self.parts)


@lru_cache(maxsize=4)
def sanitise_source_html(source: str, title: str) -> str:
    """A stored law source as a page that holds only its text, its allowlisted
    structure and its ids (see the module docstring), named `title`, with the
    clause a link opens at marked (`TARGET_STYLE`). Cached by the source
    itself, so a source revised in place (ADR-0042) is read afresh and an
    unchanged one is rebuilt once."""
    rebuilder = _Rebuilder()
    rebuilder.feed(source)
    body = rebuilder.finish()
    heading = html.escape(title)
    return (
        '<!doctype html><html lang="en-AU"><head><meta charset="utf-8">'
        f"<title>{heading}</title>{_PAGE_STYLE}{TARGET_STYLE}</head><body>{body}</body></html>"
    )


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
