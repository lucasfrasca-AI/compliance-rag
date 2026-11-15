"""A submitted document to passages with character offsets (#296, ADR-0053 U2).

A submitted document is the subject of a check, not a source: it is never
chunked and never retrieved, so it is cut into *passages* (`CONTEXT.md`), the
units a claim is anchored to and a finding opens the document at, and nothing
else is derived from it. Markdown and plain text only; a PDF's positions are
#307's and DOCX waits for a customer to name it (U2).

**A passage is verbatim.** `source[char_start:char_end] == text`, in Python
characters (code points, not bytes), whatever the line endings. Unlike a
policy's clause (`policy_parser`, which joins a clause's lines), nothing is
reflowed, so the offsets are the position map and `canonical_text` is the only
derived field (D-02). Every non-blank character of the document is in some
passage, in order, none twice: the residency gate checks what the document
says, and a claim can't be anchored to text no passage holds.

**Cutting.** A blank line ends a passage, outside a fenced code block. For
Markdown, an ATX heading line (`#` to `######`) is a passage of its own and
names the passages after it (`heading`), and a fenced block (```` ``` ```` or
`~~~`) is one passage whatever blank lines it holds, to its closing fence or
the document's end. Plain text has paragraphs and nothing else: `#` and a fence
are only characters. Lines end at `\\n`; a `\\r` before it is trailing
whitespace, so Windows and Unix endings give the same passages.

A passage's `clause_path` is `p1`, `p2`, ... in document order. The segmenter
is a function of its text: no link, instruction or code in the document is
read as anything but characters.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from compliance_rag.corpus.models import ClauseRecord, Scope

_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(?P<title>.*?))?(?:[ \t]+#+)?[ \t]*\r?$")
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")


MAX_PASSAGES = 1_000
"""Passages in one submitted document. Each is a clause record in the clause
store (an object each) and is read when the document is drawn, so the count is
bounded as the bytes are; `CorpusLifecycle.submit_document` refuses more before
it writes anything, so every caller is bound by it (#297). The API's byte limit
alone would allow some 70,000."""


class EmptyDocument(ValueError):
    """A submitted document with no words, so no passage."""


class TooManyPassages(ValueError):
    """A submitted document cut into more than `MAX_PASSAGES` passages."""

    def __init__(self, count: int) -> None:
        super().__init__(f"the document has {count:,} passages; the limit is {MAX_PASSAGES:,}")
        self.count = count


class SubmittedFormat(StrEnum):
    MARKDOWN = "markdown"
    TEXT = "text"


_BY_EXTENSION = {
    ".md": SubmittedFormat.MARKDOWN,
    ".markdown": SubmittedFormat.MARKDOWN,
    ".txt": SubmittedFormat.TEXT,
}


def format_of_filename(filename: str) -> SubmittedFormat:
    """The format a submitted file's name says it is; anything but Markdown or
    plain text is refused (U2): a PDF waits for #307, DOCX for a customer who
    names it."""
    suffix = PurePath(filename).suffix.lower()
    fmt = _BY_EXTENSION.get(suffix)
    if fmt is None:
        raise ValueError(
            f"{filename!r}: a submitted document is Markdown or plain text (.md, .markdown, .txt)"
        )
    return fmt


def passage_number(clause_path: str) -> int:
    """`12` for `p12`: a passage's place in its document. `p10` sorts before
    `p2` as text, so every reader that lists passages in document order sorts
    by this."""
    if not re.fullmatch(r"p[1-9][0-9]*", clause_path):
        raise ValueError(f"{clause_path!r} is not a passage path (p1, p2, ...)")
    return int(clause_path[1:])


@dataclass(frozen=True, slots=True)
class _Passage:
    start: int
    end: int
    heading: str | None


def _passages(source: str, fmt: SubmittedFormat) -> Iterator[_Passage]:
    markdown = fmt is SubmittedFormat.MARKDOWN
    heading: str | None = None
    start: int | None = None  # first non-blank character of the block being read
    end = 0  # end of the last non-blank line of that block
    fence: str | None = None  # the opening fence's run, while inside one
    offset = 0
    for line in source.split("\n"):
        line_start, offset = offset, offset + len(line) + 1
        blank = not line.strip()
        stripped_end = line_start + len(line.rstrip())
        first = line_start + (len(line) - len(line.lstrip()))

        if fence is not None:
            # Inside a fenced block everything belongs to it, blank lines too.
            assert start is not None
            end = stripped_end if not blank else end
            closing = _FENCE_RE.match(line)
            if (
                closing is not None
                and closing["fence"][0] == fence[0]
                and len(closing["fence"]) >= len(fence)
                and not line.strip().strip(fence[0])
            ):
                yield _Passage(start, end, heading)
                start, fence = None, None
            continue

        if blank:
            if start is not None:
                yield _Passage(start, end, heading)
                start = None
            continue

        if markdown:
            opening = _FENCE_RE.match(line)
            title = _HEADING_RE.match(line)
            if title is not None:
                if start is not None:
                    yield _Passage(start, end, heading)
                heading = (title["title"] or "").strip()
                yield _Passage(first, stripped_end, heading)
                start = None
                continue
            if opening is not None:
                if start is not None:
                    yield _Passage(start, end, heading)
                start, end, fence = first, stripped_end, opening["fence"]
                continue

        if start is None:
            start = first
        end = stripped_end
    if start is not None:
        yield _Passage(start, end, heading)


def segment_submitted_document(
    source: str,
    *,
    document_format: SubmittedFormat,
    instrument_id: str,
    version_id: str,
    tenant_id: str,
    user_id: str,
) -> tuple[ClauseRecord, ...]:
    """`source`'s passages as clause records: private to the member who
    submitted it, `Scope.USER` with their `user_id` (ADR-0036), so the clause
    store gives them to nobody else. `ClauseRecord` refuses a tenant that isn't
    the instrument id's. A document with nothing in it is refused."""
    passages = tuple(_passages(source, document_format))
    if not passages:
        raise EmptyDocument("a submitted document with no passage: it has no words")
    return tuple(
        ClauseRecord.build(
            instrument_id=instrument_id,
            version_id=version_id,
            clause_path=f"p{n}",
            heading=(p.heading or None),
            text=source[p.start : p.end],
            scope=Scope.USER,
            tenant_id=tenant_id,
            user_id=user_id,
            char_start=p.start,
            char_end=p.end,
        )
        for n, p in enumerate(passages, start=1)
    )
