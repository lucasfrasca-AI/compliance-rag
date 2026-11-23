"""The cards of the Sources panel (#354, ADR-0053 I1 and I2): the one citation panel the chat page
and every check page share (`templates/_sources_panel.html`, `static/sources.js`).

A card is one cited clause: its **title** (a law clause: the instrument's recorded title and the
clause's display form; a policy clause: its document's title and number, which `cite` already
is, ADR-0049), its version, the clause's own stored text as the excerpt (D-02, never the
model's quote) and the link that opens it in its source (`sources.source_href`). This module
makes the cards for the two kinds of page; the template draws them and the script opens them.

- `chat_panel`: the cards of a chat's verified citations, each clause once, and the title of
  every clause the chat cites (the citation links carry it, so any of them opens the panel with
  its name). Cards are made for the chat's latest `CHAT_CARD_ANSWERS` answers: an older answer's
  citation link still opens its source in the panel, it just has no card of its own. After an
  answer (`only_last`), the cards are the new answer's alone, and the text of no other clause
  is read.
- `report_cards`: the cards a findings report already holds (`check_report._sources`), given
  their title.

**A link the panel opens is this service's own source route and nothing else** (`is_source_path`,
the same rule `static/source_href.js` applies in the browser): the browser resolves dot segments
before it asks, so `/v1/sources/../../logout` is `/logout`.

Nothing here is HTML. Every value reaches the page through the templates' autoescaping.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from compliance_rag.api.deps import ApiServices
from compliance_rag.corpus.display_form import Titles
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    instrument_owner,
    instrument_tenant_prefix,
    parse_stable_id,
)

SOURCE_PREFIX = "/v1/sources/"
"""Where every card's link points: this service's own source route."""

CHAT_CARD_ANSWERS = 10
"""A chat's panel lists cards for the citations of this many of its latest answers."""

Card = dict[str, object]

_ENCODED_DOT = re.compile(r"%2e", re.IGNORECASE)
_ENCODED_SLASH = re.compile(r"%(2f|5c)", re.IGNORECASE)
_UNSAFE = re.compile(r"[\\\x00-\x1f\x7f]")


def is_source_path(href: str) -> bool:
    """Whether `href` is a path under the source route, as `static/source_href.js` reads it:
    the part before any `?` or `#`, segment by segment, every segment after the prefix
    non-empty and neither `.` nor `..` in any spelling (`%2e` included), no slash or
    backslash hidden by percent-encoding, and no backslash or control character anywhere."""
    if _UNSAFE.search(href):
        return False
    path = re.split(r"[?#]", href, maxsplit=1)[0]
    if not path.startswith(SOURCE_PREFIX) or _ENCODED_SLASH.search(path):
        return False
    return all(
        segment != "" and _ENCODED_DOT.sub(".", segment) not in {".", ".."}
        for segment in path[len(SOURCE_PREFIX) :].split("/")
    )


async def canonical_texts(
    services: ApiServices, clause_ids: Iterable[str], *, user_id: str | None = None
) -> dict[str, str]:
    """The stored canonical text of each clause, read once each: what a reader is shown for a
    citation (D-02), never the model's quote. A clause the store no longer holds (purged), or
    holds for another member alone (`user_id`, ADR-0036), is left out."""

    async def read(clause_id: str) -> tuple[str, str | None]:
        owner = instrument_owner(parse_stable_id(clause_id)[0])
        clause = await services.clauses.get_clause(clause_id, tenant_id=owner, user_id=user_id)
        return clause_id, None if clause is None else clause.canonical_text

    ids = sorted(set(clause_ids))
    return {c: t for c, t in await asyncio.gather(*(read(i) for i in ids)) if t}


async def _law_titles(services: ApiServices, clause_ids: Iterable[str]) -> Titles:
    """The recorded titles of the library instruments the clauses are in: a law clause's
    display form (`s 6D(4)(a)`) does not name its instrument, so the card does."""
    versions: set[tuple[str, str]] = set()
    for clause_id in clause_ids:
        instrument_id, version_id, _ = parse_stable_id(clause_id)
        if instrument_tenant_prefix(instrument_id) is None:
            versions.add((instrument_id, version_id))
    if not versions:
        return {}
    return await services.ledger.version_titles(PUBLIC_TENANT_ID, versions)


def _title(clause_id: str, shown: str, law_titles: Titles) -> str:
    instrument_id, version_id, _ = parse_stable_id(clause_id)
    if instrument_tenant_prefix(instrument_id) is not None:
        return shown  # a policy's cite is already its document's title and number
    name = law_titles.get((instrument_id, version_id))
    return f"{name} — {shown}" if name else shown


async def report_cards(services: ApiServices, cards: Sequence[Mapping[str, object]]) -> list[Card]:
    """A report's cards (`check_report._sources`), each given its `title`. The report's own
    cards are not changed: the JSON the API serves is the same."""
    law = await _law_titles(services, (str(c["clause_id"]) for c in cards))
    return [{**c, "title": _title(str(c["clause_id"]), str(c["cite"]), law)} for c in cards]


def _readable(citation: object) -> Mapping[str, object] | None:
    """A citation that can be shown: one that names its clause and opens at this service's
    source route (`is_source_path`). An unreadable stored one asserts nothing, so it has no
    card."""
    if not isinstance(citation, dict):
        return None
    shown = cast("dict[str, object]", citation)
    href = shown.get("href")
    if shown.get("unreadable") or not isinstance(href, str) or not is_source_path(href):
        return None
    return shown


@dataclass(frozen=True, slots=True)
class ChatPanel:
    """What a chat's panel is made of: the cards to draw, and the title of every clause the
    chat cites, by clause id."""

    cards: list[Card]
    titles: dict[str, str]


def _answers(turns: Sequence[Mapping[str, object]]) -> list[list[Mapping[str, object]]]:
    """The readable citations of each verified answer, in order."""
    return [
        [c for c in map(_readable, cast("Sequence[object]", t.get("citations") or ())) if c]
        for t in turns
        if t.get("role") != "user" and t.get("verified")
    ]


async def chat_panel(
    services: ApiServices,
    *,
    user_id: str,
    turns: Sequence[Mapping[str, object]],
    only_last: bool = False,
) -> ChatPanel:
    """The panel of a chat (`chats.read_chat`'s turns): a card for each clause cited by the
    chat's latest `CHAT_CARD_ANSWERS` verified answers, in the order it was first cited, and
    every cited clause's title. A card's id counts the chat's distinct clauses in order of
    first citation, so it is the same whenever it is made.

    With `only_last`, the cards are those of the last answer alone, and of its clauses none
    that the answers before it (within the same window) already have: they are what the page
    adds to the cards it has. The text of no other clause is read. A last turn that is not a
    verified answer adds none."""
    answers = _answers(turns)
    ordinal: dict[str, int] = {}
    shown: dict[str, Mapping[str, object]] = {}
    for answer in answers:
        for citation in answer:
            clause_id = str(citation["clause_id"])
            ordinal.setdefault(clause_id, len(ordinal) + 1)
            shown.setdefault(clause_id, citation)
    if not answers:
        return ChatPanel([], {})
    law = await _law_titles(services, shown)
    titles = {c: _title(c, str(s["cite"]), law) for c, s in shown.items()}
    last_is_answer = (
        bool(turns) and bool(turns[-1].get("verified")) and turns[-1].get("role") != "user"
    )
    if only_last:
        before = {str(c["clause_id"]) for a in answers[:-1][-CHAT_CARD_ANSWERS:] for c in a}
        wanted = [] if not last_is_answer else [str(c["clause_id"]) for c in answers[-1]]
        wanted = [c for c in dict.fromkeys(wanted) if c not in before]
    else:
        window = {str(c["clause_id"]) for a in answers[-CHAT_CARD_ANSWERS:] for c in a}
        wanted = [c for c in ordinal if c in window]
    texts = await canonical_texts(services, wanted, user_id=user_id)
    # A clause whose text this member can't be given (another member's private clause, or
    # one purged) has no card: it is not shown what it can't be given.
    cards: list[Card] = [
        {
            "id": f"src-{ordinal[clause_id]}",
            "clause_id": clause_id,
            "cite": str(shown[clause_id]["cite"]),
            "title": titles[clause_id],
            "version_id": str(shown[clause_id]["version_id"]),
            "href": str(shown[clause_id]["href"]),
            "text": texts[clause_id],
            "quotes": [],
            "incomplete": shown[clause_id].get("incomplete") or [],
        }
        for clause_id in wanted
        if clause_id in texts
    ]
    return ChatPanel(cards, titles)
