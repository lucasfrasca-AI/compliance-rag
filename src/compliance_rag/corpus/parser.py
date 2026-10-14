"""`parse_compilation()`: legislation.gov.au ePub HTML to clause records
(issue #17, ADR-0019).

Pure function over markup: no network, no storage. Findings this module
implements are all from issue #9's ePub format spike
(`docs/research/legislation-gov-au-format.md`):

- The markup is flat, non-nesting Word-export HTML — hierarchy lives
  entirely in a fixed set of CSS class names on sibling `<p>` elements, not
  in DOM nesting. A regex-based block scanner is enough; a general HTML
  parser would buy nothing this format needs.
- Section numbers are not globally unique (`CharSectno="6"` appears in the
  main body, inside Schedule 1's APP numbering, and would repeat in any
  other Schedule), so the parser tracks which container it's in — a stack,
  not a flat class -> clause_path mapping.
- Schedule 1's subclauses use a different numbering register (`11.1`, dot-
  separated) from the main body's (`(1)`, bracketed) — both are handled,
  selected by which class produced the token, not by a schedule number.
- No clause below section/Part/Schedule level carries its own `id` in the
  source. `clause_anchor_id(clause_path)` (issue #9 follow-up, ADR-0019) is
  computed and injected into each clause's opening `<p>` tag in the
  returned HTML, rather than read from the markup.
- s6's flat, unnumbered `Definition` paragraphs need a term-slug clause_path
  segment (`definition_slug`, issue #9 follow-up); a section with exactly
  one `Definition` under a subsection (s6C's `organisation`) does not —
  `CONTEXT.md`'s pre-existing `s6c/1/a` example has no `organisation`
  segment, so the singleton case folds the definition's own text into its
  enclosing subsection instead of giving it a path of its own.
- A `subsection2` block inside a Definition belongs to the definition, never to
  the subsection's chapeau (issue #178, ADR-0039). Between two paragraphs it is
  a connector — *record*'s "but does not include:" — and leads the paragraph
  that follows, keeping source order across the stored clauses; after the last
  one it closes the definition and joins the definition's own text, ahead of
  its paragraphs, as a subsection's closing words always have.
- The same holds in any other subsection (issue #186, ADR-0041): a `subsection2`
  between two paragraphs is a connector — s7(1)'s "but does not include a
  reference to an act done ... received from:" — and leads the paragraph that
  follows; one that no paragraph follows is the subsection's closing words and
  joins its text, as it always has (s8(2)). Except that a subsection with both
  keeps its connector with its closing words, which complete the connector's
  sentence (s26WF(2): "this Part does not require: (e) ...; to take steps to
  notify ..."): it is stored as it always was, until the human decides.
"""

from __future__ import annotations

import html as html_module
import re
from dataclasses import dataclass, field

from compliance_rag.corpus.models import (
    ClauseRecord,
    Scope,
    assert_unique_clause_ids,
    assert_unique_definition_slugs,
    clause_anchor_id,
    definition_slug,
)

# --------------------------------------------------------------------------
# Block scanning: the source has no nested <p> (issue #9 spike), so a
# non-greedy regex over <p ...>...</p> is a faithful, much simpler stand-in
# for a general HTML parser.
# --------------------------------------------------------------------------

_P_BLOCK_RE = re.compile(r"<p\b[^>]*>.*?</p>", re.DOTALL)
_P_OPEN_TAG_RE = re.compile(r"<p\b[^>]*>", re.DOTALL)
_CLASS_RE = re.compile(r'class="([^"]*)"')
_SPAN_WITH_ATTRS_RE = re.compile(r"<span\b([^>]*)>(.*?)</span>", re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")

# Leading-token patterns, one per class, per the spike's documented forms —
# plus inserted provisions, which take a letter suffix rather than renumbering
# their neighbours: subsection (1A), subclause 11.1A, subparagraph (iia) or
# (ivaa) (issue #97). A subsection's letter suffix is lowercased into the clause
# path (`s6e/1a`); subparagraph suffixes are lowercase in the source.
_SUBSECTION_PAREN_RE = re.compile(r"^\((\d+[A-Z]*)\)\s*")
_SUBSECTION_DOT_RE = re.compile(r"^\d+\.(\d+[A-Z]*)\s*")
_PARAGRAPH_RE = re.compile(r"^\(([a-z]+)\)\s*")
_PARAGRAPHSUB_RE = re.compile(r"^\(([ivx]+[a-z]*)\)\s*")
_PARAGRAPHSUB_SUB_RE = re.compile(r"^\(([A-Z])\)\s*")

# An APP's own heading: "11 Australian Privacy Principle 11—security of ...",
# its number repeated. Main-body sections that merely mention the APPs — s6A
# "Breach of an Australian Privacy Principle", s14 "Australian Privacy
# Principles", s15 "APP entities must comply with ..." — are not APPs, and
# filing them under `sch1/` put s15 at `sch1/app15` (issue #97).
_APP_HEADING_RE = re.compile(r"^(\w+) Australian Privacy Principle (\w+)\b")


def _strip_tags(fragment: str) -> str:
    return html_module.unescape(_ANY_TAG_RE.sub("", fragment))


def _normalise_ws(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class _Block:
    class_name: str
    inner_text: str  # tags stripped, entities unescaped, whitespace collapsed
    inner_html: str  # raw, tags intact — only Definition's term-span split needs this
    open_tag_start: int


def _scan_blocks(source: str) -> list[_Block]:
    blocks: list[_Block] = []
    for match in _P_BLOCK_RE.finditer(source):
        full = match.group(0)
        open_tag_match = _P_OPEN_TAG_RE.match(full)
        assert open_tag_match is not None
        class_match = _CLASS_RE.search(open_tag_match.group(0))
        class_name = class_match.group(1) if class_match is not None else ""
        inner = full[open_tag_match.end() : -len("</p>")]
        blocks.append(
            _Block(
                class_name=class_name,
                inner_text=_normalise_ws(_strip_tags(inner)),
                inner_html=inner,
                open_tag_start=match.start(),
            )
        )
    return blocks


def _is_bold(span_attrs: str) -> bool:
    """Word marks a defined term with an inline bold style on its spans."""
    return "font-weight:bold" in span_attrs.replace(" ", "")


def _join_term(term: str, body: str) -> str:
    """Term and body as the source reads: no space before the body's leading
    punctuation ("breach: ...", not "breach : ...")."""
    if body[:1] in {":", ",", ";", "."}:
        return f"{term}{body}"
    return f"{term} {body}".strip()


def _definition_term(block: _Block) -> tuple[str, str]:
    """The defined term and the remaining body text, for a `Definition` block
    — `CONTEXT.md`'s Definition entry requires capturing the term itself, not
    just the paragraph.

    The term is the run of leading bold spans, not the first span: Word splits
    a term at a non-breaking hyphen or space into several bold spans
    (`de` / `&#x2011;` / `identified`, `de` / `&#xa0;` / `facto` / ...), and
    taking only the first gave "de-identified" and "de facto partner" the
    same slug (issue #97)."""
    spans = _SPAN_WITH_ATTRS_RE.findall(block.inner_html)
    if not spans:
        return "", block.inner_text
    term_parts: list[str] = []
    for attrs, inner in spans:
        if not _is_bold(attrs):
            break
        term_parts.append(inner)
    if not term_parts:
        # No bold lead (not seen in C2026C00227): the first span, as before.
        term_parts, rest = [spans[0][1]], spans[1:]
    else:
        rest = spans[len(term_parts) :]
    term = _normalise_ws(html_module.unescape(_strip_tags("".join(term_parts))))
    body = _normalise_ws(html_module.unescape(_strip_tags("".join(i for _, i in rest))))
    return term, body


# --------------------------------------------------------------------------
# Tree: a subsection's children are Definition-or-paragraph nodes, whose own
# children (paragraphsub, paragraphsub-sub) nest by ordinary depth — only
# the Definition-vs-paragraph decision at the subsection's direct children
# is content-dependent (issue #17: singleton Definition merges, multiple
# Definitions each get their own term-slug segment).
# --------------------------------------------------------------------------


@dataclass
class _ParagraphNode:
    token: str
    text: str
    block_index: int
    children: list[_ParagraphNode] = field(default_factory=list["_ParagraphNode"])
    # The connector that leads this paragraph: the `subsection2` words between
    # it and the paragraph before (issues #178, #186). Kept apart from `text` so
    # that a subsection can take it back (`_SubsectionNode.settle`); the
    # clause's text is the two together (`_paragraph_text`).
    lead: str = ""

    def continue_with(self, text: str) -> None:
        """A tokenless paragraph block continues this paragraph: s6FA(a)'s
        closing "that is also personal information;", or s26WG(h)'s text
        between its subparagraphs (ii) and (iii), which still number on under
        (h) (issue #97). Like a subsection's `subsection2` tail, it joins the
        paragraph's own text after its lead-in; its subparagraphs stay
        clauses of their own."""
        self.text = f"{self.text} {text}".strip()


@dataclass
class _DefinitionNode:
    term: str
    text: str
    block_index: int
    children: list[_ParagraphNode] = field(default_factory=list[_ParagraphNode])
    # `subsection2` words met inside this definition that no clause has taken
    # yet (issue #178, ADR-0039). The next paragraph to open takes them as its
    # lead — *record*'s "but does not include:" between (b) and (d), which
    # governs (d) onward and so has to be read with them. Whatever is still
    # here when the definition ends is its closing words, and joins its own
    # text (`_definition_text`): "that carries on a credit reporting business."
    # closes the whole of *credit reporting body*, not just its last paragraph.
    unclaimed: list[str] = field(default_factory=list[str])


@dataclass
class _SubsectionNode:
    token: str
    # Document-order text of the subsection's OWN clause: plain strings
    # (chapeau, the subsection2 tail) interleaved with _DefinitionNode
    # references for a singleton Definition's lead-in — resolved at
    # emission time (`_emit_subsection`), once "singleton vs multiple
    # Definitions" is known, so the merge lands in the same order the
    # source has it rather than always at the end.
    text_parts: list[str | _DefinitionNode]
    block_index: int
    children: list[_ParagraphNode | _DefinitionNode] = field(
        default_factory=list[_ParagraphNode | _DefinitionNode]
    )
    # The same for a subsection that holds no Definition yet (issue #186,
    # ADR-0041): `subsection2` words that no clause has taken. s7(1)'s "but does
    # not include a reference to an act done ...:" between (ee) and (f) leads
    # (f); words still here when the list ends close the subsection (`settle`).
    unclaimed: list[str] = field(default_factory=list[str])
    # The paragraphs a connector leads, in document order.
    led: list[_ParagraphNode] = field(default_factory=list[_ParagraphNode])

    def settle(self) -> None:
        """The subsection's list has ended (or a Definition opens): words no
        paragraph took close it and join its own text, after what it holds, as
        closing words always have (s8(2)). A connector goes with them. Closing
        words after a connector complete its sentence — s26WF(2): "this Part
        does not require: (e) ...; (f) ...; to take steps to notify the
        individual ..." — and the connector on its paragraph with the closing
        words on the subsection would put the negation on one clause and the
        verb it governs on another (ADR-0041)."""
        if self.unclaimed:
            for paragraph in self.led:
                self.text_parts.append(paragraph.lead)
                paragraph.lead = ""
            self.text_parts.extend(self.unclaimed)
        self.led = []
        self.unclaimed = []


@dataclass
class _SectionNode:
    prefix: str
    subsections: list[_SubsectionNode] = field(default_factory=list[_SubsectionNode])


def _strip_leading_token(text: str, pattern: re.Pattern[str]) -> tuple[str, str]:
    match = pattern.match(text)
    if match is None:
        return "", text
    return match.group(1), text[match.end() :]


def _new_paragraph(
    owner: _DefinitionNode | _SubsectionNode, token: str, text: str, block_index: int
) -> _ParagraphNode:
    """A paragraph opening in `owner`: it takes the words waiting there as its
    lead, as the source reads them. With nothing waiting it has none."""
    lead = " ".join(w for w in owner.unclaimed if w)
    owner.unclaimed = []
    node = _ParagraphNode(token=token, text=text, block_index=block_index, lead=lead)
    if lead and isinstance(owner, _SubsectionNode):
        owner.led.append(node)
    return node


def _paragraph_text(node: _ParagraphNode) -> str:
    return " ".join(part for part in (node.lead, node.text) if part)


def _definition_text(definition: _DefinitionNode) -> str:
    """The definition's own clause text: the term and its lead-in, then the
    words that close it (`_DefinitionNode.unclaimed`), in that order."""
    head = _join_term(definition.term, definition.text)
    return " ".join(part for part in (head, *definition.unclaimed) if part)


def _section_prefix(title_text: str, sectno: str, current_schedule: str | None) -> str:
    local_number = sectno.strip().lower()
    heading = _APP_HEADING_RE.match(title_text)
    if heading is not None and heading.group(1) == heading.group(2):
        schedule = current_schedule or "1"
        return f"sch{schedule}/app{local_number}"
    local = f"s{local_number}"
    return f"sch{current_schedule}/{local}" if current_schedule is not None else local


def _parse_tree(blocks: list[_Block]) -> list[_SectionNode]:
    sections: list[_SectionNode] = []
    current_schedule: str | None = None
    current_section: _SectionNode | None = None
    current_subsection: _SubsectionNode | None = None
    # Explicit "currently open" pointers, one per depth below the subsection
    # — not a generic stack, because "paragraph" and "paragraphsub" are each
    # used for SIBLINGS at their own depth (e.g. (a), (b), (c) all attach to
    # the same Definition, never nesting one under another), which a stack
    # keyed only on class name can't distinguish from real nesting.
    current_definition: _DefinitionNode | None = None
    current_paragraph: _ParagraphNode | None = None
    current_paragraphsub: _ParagraphNode | None = None

    for index, block in enumerate(blocks):
        cls = block.class_name
        # Whose `subsection2` words wait for the next clause to open, and whose
        # children a paragraph joins: the open definition's, else the open
        # subsection's (issues #178, #186).
        owner = current_definition if current_definition is not None else current_subsection

        if cls == "ActHead1":
            # CharChapNo carries the schedule number as its own span text,
            # first in the block — a bare digit search is enough since it's
            # the first token in document order.
            number_match = re.search(r"\d+", block.inner_text)
            current_schedule = number_match.group(0) if number_match is not None else None
            current_section = None
            current_subsection = None
            current_definition = current_paragraph = current_paragraphsub = None
            continue

        if cls in ("ActHead2", "ActHead3", "ActHead4"):
            current_subsection = None
            current_definition = current_paragraph = current_paragraphsub = None
            continue

        if cls == "ActHead5":
            sectno_match = re.search(r"[A-Za-z0-9]+", block.inner_text)
            sectno = sectno_match.group(0) if sectno_match is not None else ""
            prefix = _section_prefix(block.inner_text, sectno, current_schedule)
            current_section = _SectionNode(prefix=prefix)
            sections.append(current_section)
            current_subsection = None
            current_definition = current_paragraph = current_paragraphsub = None
            continue

        if cls == "subsection":
            if current_section is None:
                continue
            token, rest = _strip_leading_token(block.inner_text, _SUBSECTION_PAREN_RE)
            if not token:
                token, rest = _strip_leading_token(block.inner_text, _SUBSECTION_DOT_RE)
            current_subsection = _SubsectionNode(
                token=token.lower(), text_parts=[rest], block_index=index
            )
            current_section.subsections.append(current_subsection)
            current_definition = current_paragraph = current_paragraphsub = None
            continue

        if cls == "subsection2":
            # Still part of the currently open subsection, not a new one
            # (issue #9 spike); no new clause. Inside a Definition it belongs
            # to the definition, not to the subsection's chapeau (issue #178):
            # s6(1) holds hundreds of them, and a chapeau that collected their
            # closing words read "In this Act, unless the contrary intention
            # appears: but does not include: ...". Whether it leads the next
            # paragraph (a connector: s7(1)'s "but does not include a
            # reference to an act done ...:", issue #186) or closes the list is
            # known only once the next block is, so it waits (`unclaimed`).
            if owner is not None:
                owner.unclaimed.append(block.inner_text)
            continue

        if cls == "Definition":
            if current_subsection is None:
                continue
            # Words that closed a list before this Definition keep their place
            # in the subsection's text, ahead of the Definition's lead-in.
            current_subsection.settle()
            term, body = _definition_term(block)
            node = _DefinitionNode(term=term, text=body, block_index=index)
            current_subsection.children.append(node)
            # Recorded in document order alongside the chapeau/subsection2
            # text — whether it's actually rendered here (singleton case)
            # or dropped in favour of its own clause (multi-Definition
            # case) is decided later, once every Definition in this
            # subsection is known (`_emit_subsection`).
            current_subsection.text_parts.append(node)
            current_definition = node
            current_paragraph = current_paragraphsub = None
            continue

        if cls == "paragraph":
            token, rest = _strip_leading_token(block.inner_text, _PARAGRAPH_RE)
            if not token and current_paragraph is not None:
                current_paragraph.continue_with(rest)
                continue
            if owner is None:
                continue
            node = _new_paragraph(owner, token, rest, index)
            owner.children.append(node)
            current_paragraph = node
            current_paragraphsub = None
            continue

        if cls == "paragraphsub":
            if current_paragraph is None or owner is None:
                continue
            token, rest = _strip_leading_token(block.inner_text, _PARAGRAPHSUB_RE)
            node = _new_paragraph(owner, token, rest, index)
            current_paragraph.children.append(node)
            current_paragraphsub = node
            continue

        if cls == "paragraphsub-sub":
            if current_paragraphsub is None or owner is None:
                continue
            token, rest = _strip_leading_token(block.inner_text, _PARAGRAPHSUB_SUB_RE)
            node = _new_paragraph(owner, token.lower(), rest, index)
            current_paragraphsub.children.append(node)
            continue

        # SubsectionHead, notetext/notepara/notemargin, TOC*, Table*, and
        # any other class the spike catalogued as "not operative text" —
        # skipped: no clause, no contribution to any clause's text.

    for section in sections:
        for subsection in section.subsections:
            subsection.settle()
    return sections


# --------------------------------------------------------------------------
# Emission: the tree to ClauseRecords, applying the singleton-vs-multiple
# Definition rule described in the module docstring.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _EmitContext:
    """The record-building fields every emission call needs, unchanged
    through the whole recursive walk — bundled so `_emit_paragraph` and
    `_emit_subsection` take one context argument instead of five identical
    parameters apiece, plus the two accumulators (`clauses`, `anchors`)
    they both write into."""

    instrument_id: str
    version_id: str
    tenant_id: str
    scope: Scope
    clauses: list[ClauseRecord]
    anchors: dict[int, str]

    def build(self, clause_path: str, text: str) -> ClauseRecord:
        return ClauseRecord.build(
            instrument_id=self.instrument_id,
            version_id=self.version_id,
            clause_path=clause_path,
            text=text,
            scope=self.scope,
            tenant_id=self.tenant_id,
        )


def _emit_paragraph(node: _ParagraphNode, *, parent_path: str, ctx: _EmitContext) -> None:
    path = f"{parent_path}/{node.token}"
    ctx.clauses.append(ctx.build(path, _paragraph_text(node)))
    ctx.anchors[node.block_index] = path
    for child in node.children:
        _emit_paragraph(child, parent_path=path, ctx=ctx)


def _emit_subsection(
    subsection: _SubsectionNode, *, section_prefix: str, ctx: _EmitContext
) -> None:
    # An unnumbered subsection — a section that is one sentence, like s15
    # ("An APP entity must not do an act...") or s1's short title — is the
    # section itself: its clause path is the section's (issue #97).
    subsection_path = f"{section_prefix}/{subsection.token}" if subsection.token else section_prefix
    definitions = [c for c in subsection.children if isinstance(c, _DefinitionNode)]
    bare_paragraphs = [c for c in subsection.children if isinstance(c, _ParagraphNode)]
    singleton = len(definitions) <= 1

    assert_unique_definition_slugs(d.term for d in definitions)

    # Resolve text_parts in the document order they were recorded — a
    # singleton Definition's lead-in renders in place (between the chapeau
    # and a later subsection2 tail, exactly where the source has it);
    # dropped here when there are multiple Definitions, since each of those
    # gets its own separate clause below instead.
    rendered_parts: list[str] = []
    for part in subsection.text_parts:
        if isinstance(part, _DefinitionNode):
            if singleton:
                rendered_parts.append(_definition_text(part))
        else:
            rendered_parts.append(part)
    ctx.clauses.append(ctx.build(subsection_path, " ".join(p for p in rendered_parts if p)))
    ctx.anchors[subsection.block_index] = subsection_path

    for definition in definitions:
        if singleton:
            definition_path = subsection_path
        else:
            definition_path = f"{subsection_path}/{definition_slug(definition.term)}"
            ctx.clauses.append(ctx.build(definition_path, _definition_text(definition)))
            ctx.anchors[definition.block_index] = definition_path
        for child in definition.children:
            _emit_paragraph(child, parent_path=definition_path, ctx=ctx)

    for paragraph in bare_paragraphs:
        _emit_paragraph(paragraph, parent_path=subsection_path, ctx=ctx)


@dataclass(frozen=True, slots=True)
class ParsedCompilation:
    """`parse_compilation()`'s result: every clause found, and the source
    HTML with a `clause_anchor_id` injected at each clause's position
    (`CONTEXT.md`: Position map, ADR-0019). Nothing is written anywhere —
    persisting `annotated_html` is a later ticket's concern (#19)."""

    clauses: tuple[ClauseRecord, ...]
    annotated_html: str


def parse_compilation(
    source: str,
    *,
    instrument_id: str,
    version_id: str,
    tenant_id: str = "_public",
    scope: Scope = Scope.PUBLIC,
) -> ParsedCompilation:
    """Legislation.gov.au ePub HTML to clause records (issue #17).

    Pure function: no network call, no store write. Handles the whole
    compilation — Schedule and main-body sections alike — via the container
    stack described in the module docstring; which clauses are later
    chunked and embedded is a manifest's decision (`PHASES.md`), not this
    function's.
    """
    blocks = _scan_blocks(source)
    sections = _parse_tree(blocks)

    ctx = _EmitContext(
        instrument_id=instrument_id,
        version_id=version_id,
        tenant_id=tenant_id,
        scope=scope,
        clauses=[],
        anchors={},
    )
    for section in sections:
        numbered = any(s.token for s in section.subsections)
        for subsection in section.subsections:
            if numbered and not subsection.token and not subsection.children:
                # A heading set in subsection style ("Enforceable civil
                # penalty provisions" above s80U(1)), not a provision: an
                # unnumbered subsection is the section itself only when the
                # section has no numbered ones (issue #97).
                continue
            _emit_subsection(subsection, section_prefix=section.prefix, ctx=ctx)

    assert_unique_clause_ids(c.clause_id for c in ctx.clauses)

    annotated = source
    for block_index in sorted(ctx.anchors, reverse=True):
        block = blocks[block_index]
        anchor_id = clause_anchor_id(ctx.anchors[block_index])
        insertion_point = block.open_tag_start + len("<p")
        annotated = annotated[:insertion_point] + f' id="{anchor_id}"' + annotated[insertion_point:]

    return ParsedCompilation(clauses=tuple(ctx.clauses), annotated_html=annotated)
