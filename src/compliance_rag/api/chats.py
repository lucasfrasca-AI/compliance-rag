"""A member's chat history, read back (#162): `GET /v1/chats`, their chats
newest first, and `GET /v1/chats/{id}`, one chat turn by turn. The writing is
`POST /v1/answers` (`answers.py`), which records each question and what the
member was shown for it.

A chat is its member's alone, by the sign-in's uid (ADR-0036). Another
member's chat and another tenant's are not found, the same 404 a chat that
never existed gets, so a request confirms nothing about chats that aren't the
caller's. The tenant is always the configured one (`deps.py`).

A turn reads back as it was shown. The question is the member's text. An answer
is the verified text with its citations (clause id, its display form and the
version, which is what keeps it clickable once the clause is superseded), or
the reason it was withheld with the clauses whose citations blocked it, or the
error that ended the request. Never the model's draft: the ledger has no such
column (ADR-0003).

A citation is built by the answer event's builder (`citation_json`), so a chat
shows it as the answer did. A policy citation names its document by the title
recorded with its version (ADR-0049), which never changes once recorded: the
chat's titles are read once, for every turn, never once per citation.

A stored citation that can't be read (a corrupt row) stays in its place in the
list as `{"unreadable": true, "reason": ...}`,
with `clause_id`, `cite` and `version_id` when the stored id is well-formed:
the chat is still returned (200), the citation asserts nothing, and no other
part of the row is shown (#232). It carries no `incomplete`: what the member
was shown with it is lost, and the turn's `incomplete` list can't count it.

A blocked clause id that can't be read is marked the same way, in its place in
`blocked` (#234); it names no clause, since a readable id is a readable entry.

An answer turn reads back the `not_in_environment` flags it was shown with
(#252): the instruments its question named that the environment didn't hold.
A stored flag that can't be read is `{"unreadable": true}` in its place.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request

from compliance_rag.adapters.interfaces import (
    BlockedClause,
    ChatCitation,
    ChatTurn,
    Citation,
    TurnOutcome,
    UnreadableCitation,
)
from compliance_rag.api.answers import WITHHELD, citation_json, incomplete_clauses_json
from compliance_rag.api.deps import UUID4_HEX, CurrentCaller, services_of
from compliance_rag.assessment.environment import NotInEnvironment
from compliance_rag.corpus.display_form import Titles, cite, cited_policy_versions

router = APIRouter(prefix="/v1")


def _unreadable_json(c: UnreadableCitation, titles: Titles) -> dict[str, object]:
    # A corrupt stored element, in its place: marked, with the clause when it
    # could still be named, and nothing else of the row (#232, #234).
    unreadable: dict[str, object] = {"unreadable": True, "reason": c.reason.value}
    if c.clause_id is not None:
        unreadable["clause_id"] = c.clause_id
        unreadable["cite"] = cite(c.clause_id, titles)
    if c.version_id is not None:
        unreadable["version_id"] = c.version_id
    return unreadable


def _citation_json(c: Citation, titles: Titles) -> dict[str, object]:
    if isinstance(c, ChatCitation):
        return citation_json(c.clause_id, c.version_id, c.incomplete, titles)
    return _unreadable_json(c, titles)


def _blocked_json(b: BlockedClause, titles: Titles) -> dict[str, object]:
    if isinstance(b, UnreadableCitation):
        return _unreadable_json(b, titles)
    return {"clause_id": b, "cite": cite(b, titles)}


def _named(turns: Sequence[ChatTurn]) -> list[str]:
    """Every clause id the chat names, cited or blocked: whose titles it reads."""
    named: list[str] = []
    for turn in turns:
        for c in (*turn.citations, *turn.blocked):
            if isinstance(c, str):
                named.append(c)
            elif c.clause_id is not None:
                named.append(c.clause_id)
    return named


def _turn_json(turn: ChatTurn, titles: Titles) -> dict[str, object]:
    common: dict[str, object] = {
        "turn_index": turn.turn_index,
        "role": turn.role.value,
        "as_of": turn.as_of.isoformat(),
        "trace_id": turn.trace_id,
        "created_at": turn.created_at.isoformat() if turn.created_at else None,
    }
    if turn.outcome is None:  # the member's question
        return {**common, "text": turn.content}
    return {
        **common,
        "verified": turn.outcome is TurnOutcome.VERIFIED,
        "text": turn.content,
        "citations": [_citation_json(c, titles) for c in turn.citations],
        "incomplete": incomplete_clauses_json(
            ((c.clause_id, c.incomplete) for c in turn.citations if isinstance(c, ChatCitation)),
            titles,
        ),
        "blocked": [_blocked_json(b, titles) for b in turn.blocked],
        "withheld": WITHHELD.get(turn.outcome),
        "error": turn.outcome.value if turn.outcome.failed else None,
        # As recorded with the turn, the flag the member was shown (#252).
        "not_in_environment": [
            n.json() if isinstance(n, NotInEnvironment) else {"unreadable": True}
            for n in turn.not_in_environment
        ],
    }


@router.get("/chats")
async def list_chats(request: Request, who: CurrentCaller) -> dict[str, object]:
    """The member's chats, the one with the latest turn first. Each is named by
    what the member first asked."""
    chats = await services_of(request).ledger.chats(who.tenant_id, who.member.uid)
    return {
        "chats": [
            {
                "chat_id": c.chat_id,
                "first_question": c.first_question,
                "turns": c.turns,
                "started_at": c.started_at.isoformat(),
                "updated_at": c.updated_at.isoformat(),
            }
            for c in chats
        ]
    }


@router.get("/chats/{chat_id}")
async def get_chat(
    request: Request, who: CurrentCaller, chat_id: Annotated[str, Path(pattern=UUID4_HEX)]
) -> dict[str, object]:
    ledger = services_of(request).ledger
    turns = await ledger.chat_turns(who.tenant_id, who.member.uid, chat_id)
    if not turns:
        raise HTTPException(404, "no such chat")
    titles = await ledger.version_titles(who.tenant_id, cited_policy_versions(_named(turns)))
    return {"chat_id": chat_id, "turns": [_turn_json(t, titles) for t in turns]}
