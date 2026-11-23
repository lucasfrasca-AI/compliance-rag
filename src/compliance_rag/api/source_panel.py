"""The cards of the Sources panel (#354, ADR-0053 I1 and I2): the one citation panel the
chat page and every check page share (`templates/_sources_panel.html`, `static/sources.js`).

A card is one cited clause: its **title** (a law clause: the instrument's recorded title and the
clause's display form; a policy clause: its document's title and number, which `cite` already
is, ADR-0049), its version, the clause's own stored text as the excerpt (D-02, never the
model's quote) and the link that opens it in its source (`sources.source_href`). This module
makes the cards for the two kinds of page; the template draws them and the script opens them.

- `chat_cards`: the verified citations of a chat's answers, each clause once, in the order it
  was first cited.
- `report_cards`: the cards a findings report already holds (`check_report._sources`), given
  their title.

Nothing here is HTML. Every value reaches the page through the templates' autoescaping.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
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

Card = dict[str, object]


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
    source route. An unreadable stored one asserts nothing, so it has no card."""
    if not isinstance(citation, dict):
        return None
    shown = cast("dict[str, object]", citation)
    href = shown.get("href")
    if shown.get("unreadable") or not isinstance(href, str) or not href.startswith(SOURCE_PREFIX):
        return None
    return shown


async def chat_cards(
    services: ApiServices, *, user_id: str, turns: Sequence[Mapping[str, object]]
) -> list[Card]:
    """The cards of a chat (`chats.read_chat`'s turns): every readable citation of a verified
    answer, each clause once, in the order it was first cited. A withheld or failed turn cites
    nothing the member was shown, so it adds none."""
    cited: dict[str, Mapping[str, object]] = {}
    for turn in turns:
        if turn.get("role") == "user" or not turn.get("verified"):
            continue
        for citation in cast("Sequence[object]", turn.get("citations") or ()):
            shown = _readable(citation)
            if shown is not None:
                cited.setdefault(str(shown["clause_id"]), shown)
    if not cited:
        return []
    texts = await canonical_texts(services, cited, user_id=user_id)
    law = await _law_titles(services, cited)
    return [
        {
            "id": f"src-{n}",
            "clause_id": clause_id,
            "cite": str(c["cite"]),
            "title": _title(clause_id, str(c["cite"]), law),
            "version_id": str(c["version_id"]),
            "href": str(c["href"]),
            "text": texts.get(clause_id),
            "quotes": [],
            "incomplete": c.get("incomplete") or [],
        }
        for n, (clause_id, c) in enumerate(cited.items(), start=1)
    ]
