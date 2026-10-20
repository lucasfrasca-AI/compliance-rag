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
shows it as the answer did. A stored citation that can't be read (a corrupt
row) stays in its place in the list as `{"unreadable": true, "reason": ...}`,
with `clause_id`, `cite` and `version_id` when the stored id is well-formed:
the chat is still returned (200), the citation asserts nothing, and no other
part of the row is shown (#232). It carries no `incomplete`: what the member
was shown with it is lost, and the turn's `incomplete` list can't count it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request

from compliance_rag.adapters.interfaces import ChatCitation, ChatTurn, Citation, TurnOutcome
from compliance_rag.api.answers import WITHHELD, citation_json, incomplete_clauses_json
from compliance_rag.api.deps import UUID4_HEX, CurrentCaller, services_of
from compliance_rag.assessment.coverage_report import cite

router = APIRouter(prefix="/v1")


def _citation_json(c: Citation) -> dict[str, object]:
    if isinstance(c, ChatCitation):
        return citation_json(c.clause_id, c.version_id, c.incomplete)
    # A corrupt stored citation, in its place: marked, with the clause when it
    # could still be named, and nothing else of the row (#232).
    unreadable: dict[str, object] = {"unreadable": True, "reason": c.reason.value}
    if c.clause_id is not None:
        unreadable["clause_id"] = c.clause_id
        unreadable["cite"] = cite(c.clause_id)
    if c.version_id is not None:
        unreadable["version_id"] = c.version_id
    return unreadable


def _turn_json(turn: ChatTurn) -> dict[str, object]:
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
        "citations": [_citation_json(c) for c in turn.citations],
        "incomplete": incomplete_clauses_json(
            (c.clause_id, c.incomplete) for c in turn.citations if isinstance(c, ChatCitation)
        ),
        "blocked": [{"clause_id": b, "cite": cite(b)} for b in turn.blocked],
        "withheld": WITHHELD.get(turn.outcome),
        "error": turn.outcome.value if turn.outcome.failed else None,
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
    turns = await services_of(request).ledger.chat_turns(who.tenant_id, who.member.uid, chat_id)
    if not turns:
        raise HTTPException(404, "no such chat")
    return {"chat_id": chat_id, "turns": [_turn_json(t) for t in turns]}
