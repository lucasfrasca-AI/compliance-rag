"""`display_form()`: render a clause path the way the profession cites it
(`CONTEXT.md`: Display form, issue #7).

A pure function of the path, never stored: storing both a path and a label
lets them drift, and the path is the authority (`CONTEXT.md`). It restores
what lowercasing throws away — a section's letter suffix is uppercased
(`s6c` -> `6C`) — and dispatches on the clause_path's own segment shape
(`sch.../app...`, `sN...`, `art...`, `annexa...`) to pick how the remaining
segments are joined: a dotted sub-clause then bracketed paragraphs for an
APP (`APP 5.2(b)`), all-bracketed for a section or an EU article
(`s 6C(1)(a)`, `Art 9(2)(b)`), all-dotted for an ISO Annex A control
(`A.8.3`).

`instrument_id` is part of the signature (issue #7) because the shape alone
won't stay sufficient forever: a future instrument could reuse an existing
shape (say, another `sN...`-style Act) while wanting a different citation
convention. It is not yet consulted — no two instruments in scope today
share a shape, so there is nothing to disambiguate — and this function
raises nothing to enforce that silently; it is simply unused until a real
collision exists to resolve.
"""

from __future__ import annotations

import re

_SECTION_RE = re.compile(r"^s(\d+)([a-z]?)$")
_ARTICLE_RE = re.compile(r"^art(\d+)$")
_SCHEDULE_APP_RE = re.compile(r"^app(\d+)$")


def _bracketed(base: str, rest: list[str]) -> str:
    return base + "".join(f"({segment})" for segment in rest)


def display_form(clause_path: str, instrument_id: str) -> str:
    """A clause path rendered for a human, dispatching on its own segment
    shape (see module docstring for why `instrument_id` is accepted but not
    yet read). Falls back to the raw path — never raises — for a
    clause_path shape no convention below recognises (issue #7)."""
    segments = clause_path.split("/")
    head, rest = segments[0], segments[1:]

    section_match = _SECTION_RE.match(head)
    if section_match is not None:
        number, letter = section_match.groups()
        return _bracketed(f"s {number}{letter.upper()}", rest)

    article_match = _ARTICLE_RE.match(head)
    if article_match is not None:
        return _bracketed(f"Art {article_match.group(1)}", rest)

    if head.startswith("sch") and rest and _SCHEDULE_APP_RE.match(rest[0]):
        app_match = _SCHEDULE_APP_RE.match(rest[0])
        assert app_match is not None
        sub_clauses = rest[1:]
        base = f"APP {app_match.group(1)}"
        if not sub_clauses:
            return base
        return _bracketed(f"{base}.{sub_clauses[0]}", sub_clauses[1:])

    if head == "annexa":
        return ".".join(("A", *rest)) if rest else "A"

    return clause_path
