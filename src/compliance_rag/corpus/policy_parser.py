"""Synthetic company policies to clause records, Markdown and PDF (issue #20).

Click-to-clause is two mechanisms and they are never joined (`CONTEXT.md`:
Position map): law resolves to a synthesized anchor in stored HTML (issue
#17, ADR-0019), an uploaded PDF to page and character offsets. Deriving
canonical text from extracted PDF text would make the verifier fail against
its own corpus (D-02) — PDF extraction fabricates whitespace a source
document never had. So `parse_policy_markdown()` is the only source of
`text`/`canonical_text`; `attach_pdf_positions()` only ever adds
`page`/`char_start`/`char_end` metadata on top of the same Markdown-derived
records, never re-derives their text.

Markdown structure: a document title (`#`, not citable on its own), numbered
`## N. Heading` sections (containers, not citable — matches #17's rule that
a section heading alone isn't a clause), and `N.M ` numbered paragraphs,
which are the clauses, at `clause_path` `{N}/{M}`. Verified against the real
synthetic fixtures (issue #11, `docs/research/fixtures/synthetic-policies/`).
"""

from __future__ import annotations

import re

import pymupdf

from compliance_rag.corpus.models import ClauseRecord, Scope

_FRONT_MATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_HEADING_RE = re.compile(r"^##\s+(\d+)\.\s")
_SUBSECTION_RE = re.compile(r"^(\d+)\.(\d+)\s+")


def _strip_front_matter(source: str) -> str:
    return _FRONT_MATTER_RE.sub("", source, count=1)


def parse_policy_markdown(
    source: str,
    *,
    instrument_id: str,
    version_id: str,
    tenant_id: str,
) -> tuple[ClauseRecord, ...]:
    """A policy's Markdown source to clause records.

    `Scope.TENANT` with the given `tenant_id`: `ClauseRecord`'s own
    validator already rejects `PUBLIC_TENANT_ID` for a non-public scope
    (`CONTEXT.md`: Tenant id) and any `instrument_id` tenant-prefix
    mismatch (issue #4) — this function relies on those checks rather than
    re-validating.
    """
    lines = _strip_front_matter(source).splitlines()

    clauses: list[ClauseRecord] = []
    current_section: str | None = None
    current_subsection: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_subsection, current_lines
        if current_subsection is not None:
            text = " ".join(" ".join(current_lines).split())
            if text:
                clauses.append(
                    ClauseRecord.build(
                        instrument_id=instrument_id,
                        version_id=version_id,
                        clause_path=f"{current_section}/{current_subsection}",
                        text=text,
                        scope=Scope.TENANT,
                        tenant_id=tenant_id,
                    )
                )
        current_subsection = None
        current_lines = []

    for line in lines:
        heading_match = _HEADING_RE.match(line)
        if heading_match is not None:
            flush()
            current_section = heading_match.group(1)
            continue

        subsection_match = _SUBSECTION_RE.match(line) if current_section is not None else None
        if subsection_match is not None:
            line_section = subsection_match.group(1)
            if line_section != current_section:
                raise ValueError(
                    f"numbered line {line.strip()!r} claims section {line_section!r}, but "
                    f"the enclosing heading is section {current_section!r} — mis-numbered "
                    "source, not something to silently resolve in the heading's favour"
                )
            flush()
            current_subsection = subsection_match.group(2)
            current_lines = [line[subsection_match.end() :]]
            continue

        # A blank line does NOT close the current item — a numbered item's
        # own content can include a blank-line-separated sub-list (e.g. a
        # bulleted list of vendors under a "the following:" lead-in); only a
        # new heading or a new numbered line is a real boundary. Blank
        # lines just join the accumulated lines like any other line; the
        # final `" ".join(" ".join(current_lines).split())` in flush()
        # collapses all whitespace runs regardless, so an included blank
        # line contributes nothing but is never silently dropped.
        if current_subsection is not None:
            current_lines.append(line)

    flush()
    return tuple(clauses)


def _fuzzy_pattern(text: str) -> re.Pattern[str]:
    """A regex matching `text` with any whitespace run standing in for the
    single spaces `parse_policy_markdown` already normalised — a PDF's line
    wraps rarely land where Markdown's did, but the words are the same.

    Requiring at least one whitespace character (`\\s+`, not `\\s*`) between
    every token pair is safe even across a page break, not just within a
    page: `page.get_text()` is documented by PyMuPDF to always terminate in
    `\\n`, so concatenating page texts (`attach_pdf_positions`) never glues
    two words together with zero separator, even if the source PDF wrapped
    a sentence exactly at the page boundary — confirmed against the real
    fixture's own such case (issue #11's privacy policy, clause 3/2), not
    merely assumed."""
    tokens = text.split()
    return re.compile(r"\s+".join(re.escape(token) for token in tokens))


def attach_pdf_positions(
    clauses: tuple[ClauseRecord, ...], pdf_bytes: bytes
) -> tuple[ClauseRecord, ...]:
    """Locate each clause's already-parsed text inside a PDF rendition of
    the same document, and attach `page`/`char_start`/`char_end` — never
    text or canonical_text, which stay exactly what Markdown produced.

    Raises if a clause's text can't be found in the PDF at all: a missing
    match means the PDF isn't actually a rendition of the same document,
    which should fail loudly rather than silently ship an unpositioned or
    wrongly positioned clause.

    Searched against the whole document's concatenated text, not page by
    page: a real clause can straddle a page break (confirmed against the
    #11 fixture — a wrapped sentence split exactly where a page ends), and
    a per-page search would fail to find it at all. `page` is the page the
    match *starts* on; `char_start`/`char_end` are offsets from that page's
    own start, so a cross-page clause's `char_end` can exceed that single
    page's text length by design — reconstructing it means reading from
    `page` onward, not truncating at the page boundary.
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        # get_text() is typed to return list | str | dict depending on the
        # "output" argument; called with no argument it always returns str.
        # pymupdf's own stubs don't narrow that, hence the ignore.
        page_texts: list[str] = [
            str(page.get_text())  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
            for page in doc
        ]
    finally:
        doc.close()

    page_start_offsets: list[int] = []
    running = 0
    for page_text in page_texts:
        page_start_offsets.append(running)
        running += len(page_text)
    full_text = "".join(page_texts)

    updated: list[ClauseRecord] = []
    for clause in clauses:
        pattern = _fuzzy_pattern(clause.text)
        match = pattern.search(full_text)
        if match is None:
            raise ValueError(f"clause {clause.clause_path!r} not found in PDF text")
        page_index = max(i for i, start in enumerate(page_start_offsets) if start <= match.start())
        page_offset = page_start_offsets[page_index]
        updated.append(
            clause.model_copy(
                update={
                    "page": page_index + 1,
                    "char_start": match.start() - page_offset,
                    "char_end": match.end() - page_offset,
                }
            )
        )
    return tuple(updated)


def parse_policy(
    markdown_source: str,
    *,
    instrument_id: str,
    version_id: str,
    tenant_id: str,
    pdf_bytes: bytes | None = None,
) -> tuple[ClauseRecord, ...]:
    """`parse_policy_markdown()`, plus `attach_pdf_positions()` when a PDF
    companion is given. The single entry point issue #20 asks for: with or
    without `pdf_bytes`, canonical text is identical either way."""
    clauses = parse_policy_markdown(
        markdown_source,
        instrument_id=instrument_id,
        version_id=version_id,
        tenant_id=tenant_id,
    )
    if pdf_bytes is not None:
        clauses = attach_pdf_positions(clauses, pdf_bytes)
    return clauses
