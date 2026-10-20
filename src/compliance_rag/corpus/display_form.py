"""`display_form()`: render a clause path the way the profession cites it
(`CONTEXT.md`: Display form, issue #7).

A pure function of the path, never stored: storing both a path and a label
lets them drift, and the path is the authority (`CONTEXT.md`). It restores
what lowercasing throws away — a section's letter suffix is uppercased
(`s6c` -> `6C`, `s6da` -> `6DA`) — and names a defined term by its slug
(`s6/1/app-entity` -> `s 6(1) "app-entity"`) — and dispatches on the clause_path's own segment shape
(`sch.../app...`, `sN...`, `art...`, `annexa...`) to pick how the remaining
segments are joined: a dotted sub-clause then bracketed paragraphs for an
APP (`APP 5.2(b)`), all-bracketed for a section or an EU article
(`s 6C(1)(a)`, `Art 9(2)(b)`), all-dotted for an ISO Annex A control
(`A.8.3`).

A **policy** clause (a tenant-prefixed instrument's, ADR-0049) is cited by
its document's recorded title and its numbering as the document writes it:
`acme/privacy-policy:v1:3/1` -> "Acme Pty Ltd — Privacy Policy, 3.1". That
is not a function of the path alone — the title is recorded in the ledger,
with the version — so `cite()` takes the titles a request has read, once,
for every citation it shows. Library law's form never reads them.

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
from collections.abc import Iterable, Mapping
from types import MappingProxyType

from compliance_rag.corpus.models import CLAUSE_PATH_RE, instrument_tenant_prefix, parse_stable_id
from compliance_rag.corpus.models import PARAGRAPH_SEGMENT_RE as _PARAGRAPH_RE

Titles = Mapping[tuple[str, str], str]
"""`(instrument_id, version_id)` to the title the ledger records with that
version (`LedgerStore.version_titles`, ADR-0049)."""

NO_TITLES: Titles = MappingProxyType({})
"""For a caller that cites only library law, whose form names no title: an
obligation and its questions rest on law clauses (ADR-0028)."""

_SECTION_RE = re.compile(r"^s(\d+)([a-z]{0,2})\Z")
_ARTICLE_RE = re.compile(r"^art(\d+)\Z")
_SCHEDULE_APP_RE = re.compile(r"^app(\d+)\Z")


_INSERTED_RE = re.compile(r"^(\d+)([a-z]+)\Z")
_NUMBER_RE = re.compile(r"[0-9]+")


def _cased(segment: str) -> str:
    """An inserted subsection keeps its capital: `1a` -> `1A` (s 6E(1A));
    a paragraph letter stays lower case."""
    match = _INSERTED_RE.match(segment)
    return f"{match.group(1)}{match.group(2).upper()}" if match else segment


def _bracketed(base: str, rest: list[str]) -> str:
    return base + "".join(f"({_cased(segment)})" for segment in rest)


def display_form(clause_path: str, instrument_id: str) -> str:
    """A clause path rendered for a human, dispatching on its own segment
    shape (see module docstring for why `instrument_id` is accepted but not
    yet read). Falls back to the raw path — never raises — for a
    clause_path shape no convention below recognises (issue #7), and for
    anything that isn't a well-formed clause path at all: a trailing newline
    or a space in any segment is shown as it is, never rendered as clean
    (#240)."""
    if not CLAUSE_PATH_RE.match(clause_path):
        return clause_path
    segments = clause_path.split("/")
    head, rest = segments[0], segments[1:]

    section_match = _SECTION_RE.match(head)
    if section_match is not None:
        number, letter = section_match.groups()
        paragraphs: list[str] = []
        for n, segment in enumerate(rest):
            if not _PARAGRAPH_RE.match(segment):
                # s6/1/app-entity: the definition of "app entity" in s 6(1),
                # then any paragraphs of that definition.
                # The slug, verbatim: it can't say where capitals or real
                # hyphens were ("APP entity" and "de-identified" both slug
                # with a hyphen), so it is shown as the slug it is.
                defined = f'{_bracketed(f"s {number}{letter.upper()}", paragraphs)} "{segment}"'
                following = rest[n + 1 :]
                return _bracketed(f"{defined} ", following) if following else defined
            paragraphs.append(segment)
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


def numbering(clause_path: str, instrument_id: str) -> str:
    """A policy clause's number as the document writes it: `3/1` -> "3.1"
    (`policy_parser`'s `## N.` section and `N.M` paragraph). A path of
    another shape — a law-shaped one in a document a tenant supplied — is
    rendered as `display_form` renders it."""
    segments = clause_path.split("/")
    if all(_NUMBER_RE.fullmatch(segment) for segment in segments):
        return ".".join(segments)
    return display_form(clause_path, instrument_id)


def cite(clause_id: str, titles: Titles) -> str:
    """A stable ID as a reader cites it: the one form the answer event, a chat
    read back and the model's fence all show (ADR-0049).

    - Library law: `display_form`, unchanged — "s 6D(4)(b)", "APP 11.1".
    - A policy clause: "{title}, {numbering}", the title the ledger records
      for its version: "Acme Pty Ltd — Privacy Policy, 3.1". A comma, as a
      pinpoint follows the document it is in; no "§", which the policies
      never write. A version with no recorded title — only a citation that
      didn't verify can name one — is its numbering alone, never a title
      made up from the instrument id.
    """
    instrument_id, version_id, clause_path = parse_stable_id(clause_id)
    if instrument_tenant_prefix(instrument_id) is None:
        return display_form(clause_path, instrument_id)
    number = numbering(clause_path, instrument_id)
    title = titles.get((instrument_id, version_id))
    return number if title is None else f"{title}, {number}"


def cited_policy_versions(clause_ids: Iterable[str]) -> frozenset[tuple[str, str]]:
    """The policy versions among these clause ids: what a request reads the
    titles of, in one `version_titles` call. Library law needs none."""
    found: set[tuple[str, str]] = set()
    for clause_id in clause_ids:
        instrument_id, version_id, _ = parse_stable_id(clause_id)
        if instrument_tenant_prefix(instrument_id) is not None:
            found.add((instrument_id, version_id))
    return frozenset(found)
