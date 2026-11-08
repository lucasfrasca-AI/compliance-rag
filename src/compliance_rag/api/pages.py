"""The Ask pages (#281, ADR-0053 decisions 14-17 and 19): server-rendered by
this service with Jinja2 and htmx, every asset served from `/static`, under the
one CSP (`sign_in.CONTENT_SECURITY_POLICY`).

- `GET /chats`: where a member lands (the sign-in page links to it; the root
  `/` is deliberately no page, the throwaway demo owns it): the member's chats,
  newest first, and a link to a new one.
- `GET /chats/new` and `GET /chats/{id}`: a chat. Its turns are read back from
  the ledger exactly as `GET /v1/chats/{id}` serves them (`chats.read_chat`),
  and the question form streams `POST /v1/answers` from the one served script
  (`static/ask.js`), with the session cookie.
- `GET /chats/{id}/turns`: the turns alone, as an HTML fragment. After an
  answer the script has htmx swap this in, so every outcome a member sees is
  rendered here, by the server, from what the ledger recorded: the page never
  builds HTML from the stream.

**Nothing is HTML but what the templates say.** Autoescaping is on for every
template, `StrictUndefined` makes a misspelt field an error rather than empty
text, and nothing is marked safe: a question or an answer containing markup
reaches the browser as text. No template holds a script, an event handler or a
style attribute (the CSP would refuse them anyway); `tests/test_api_pages.py`
scans for all three.

**What the stream knows that the ledger doesn't.** A request a quota refusal or
an over-cap policy corpus ended is recorded as `internal` (#251, ADR-0054),
the answer that couldn't be completed. The `error` event names them, and the
script passes that to the fragment as `refine`, which re-labels the chat's last
turn if (and only if) it was recorded as `internal`. It carries a kind and two
numbers, never text: a forged `refine` changes how the member's own last failed
turn is worded, and nothing else. Read back later, such a turn is the generic
`internal` one.

**Signing in.** A page request without a session redirects (303) to `/signin`.
A signed-in non-member gets a 403 page, as a redirect would only loop. The
fragment is fetched by script, so it answers 401 as JSON, which the script
turns into the same redirect.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from compliance_rag.api.chats import read_chat
from compliance_rag.api.deps import (
    SYDNEY,
    UUID4_HEX,
    Caller,
    CurrentCaller,
    current_caller,
    current_member,
    services_of,
)
from compliance_rag.api.sign_in import CONTENT_SECURITY_POLICY

TEMPLATE_DIR = Path(__file__).parent / "templates"

PAGE_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    # A member's own chats: never stored by a cache or the back-forward cache.
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
"""On every page response, a redirect and an error page included."""

HEADING_CHARS = 80
_CHAT_ID = re.compile(UUID4_HEX)


def _sydney(value: dt.datetime) -> str:
    return value.astimezone(SYDNEY).strftime("%-d %b %Y, %H:%M")


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        # On for every template, whatever its name: member and model text is
        # data, never markup. Nothing is marked safe (the tests scan for it).
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["sydney"] = _sydney  # pyright: ignore[reportUnknownMemberType] — Jinja's filter table is untyped
    return env


ENV = _environment()

router = APIRouter()
_bearer = HTTPBearer(auto_error=False)


class SignInRequired(Exception):
    """A page was asked for without a session."""


class PageError(Exception):
    """A page that can't be shown, with the words to show for it."""

    def __init__(self, status: int, title: str, message: str) -> None:
        super().__init__(title)
        self.status = status
        self.title = title
        self.message = message


def render(name: str, *, status: int = 200, **context: object) -> HTMLResponse:
    return HTMLResponse(
        ENV.get_template(name).render(**context), status_code=status, headers=PAGE_HEADERS
    )


def sign_in_required(_: Request, __: Exception) -> Response:
    return RedirectResponse("/signin", status_code=303, headers=PAGE_HEADERS)


def page_error(_: Request, error: Exception) -> Response:
    assert isinstance(error, PageError)
    return render("message.html", status=error.status, title=error.title, message=error.message)


async def page_caller(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> Caller:
    """The signed-in member, as `current_caller` has it; a page asks the
    unauthenticated to sign in rather than showing them JSON."""
    try:
        member = await current_member(request, credentials)
    except HTTPException as refused:
        if refused.status_code == 401:
            raise SignInRequired from None
        if refused.status_code == 403:
            raise PageError(
                403, "Not a member", "This account is not a member of this workspace."
            ) from None
        raise PageError(503, "Try again shortly", "Sign-in can't be checked right now.") from None
    return await current_caller(request, member)


PageCaller = Annotated[Caller, Depends(page_caller)]


def _no_such_chat() -> PageError:
    return PageError(404, "No such chat", "This chat doesn't exist, or it isn't yours.")


def _heading(turns: list[dict[str, object]]) -> str:
    first = next((t for t in turns if t["role"] == "user"), None)
    if first is None:
        return "New chat"
    text = " ".join(str(first["text"]).split())
    return text if len(text) <= HEADING_CHARS else text[: HEADING_CHARS - 1].rstrip() + "…"


Refinement = Literal["generation_unavailable", "policy_corpus_too_large"]


def refined(
    turns: list[dict[str, object]],
    refinement: Refinement | None,
    estimated_tokens: int | None,
    cap_tokens: int | None,
) -> list[dict[str, object]]:
    """The last turn re-labelled with what the `error` event said, when it was
    recorded as `internal` (the only recording those two share). Anything else
    is left exactly as the ledger has it."""
    if refinement is None or not turns:
        return turns
    last = turns[-1]
    if last["role"] != "assistant" or last["error"] != "internal":
        return turns
    if refinement == "policy_corpus_too_large":
        if estimated_tokens is None or cap_tokens is None:
            return turns
        last = {**last, "estimated_tokens": estimated_tokens, "cap_tokens": cap_tokens}
    return [*turns[:-1], {**last, "error": refinement}]


@router.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, who: PageCaller) -> HTMLResponse:
    chats = await services_of(request).ledger.chats(who.tenant_id, who.member.uid)
    return render("chats.html", chats=chats, email=who.member.email)


@router.get("/chats/new", response_class=HTMLResponse)
async def new_chat_page(_: PageCaller) -> HTMLResponse:
    return render("chat.html", chat_id=None, heading="New chat", turns=[])


@router.get("/chats/{chat_id}", response_class=HTMLResponse)
async def chat_page(request: Request, who: PageCaller, chat_id: str) -> HTMLResponse:
    turns = (
        await read_chat(services_of(request).ledger, who.tenant_id, who.member.uid, chat_id)
        if _CHAT_ID.match(chat_id)
        else None
    )
    if turns is None:
        raise _no_such_chat()
    return render("chat.html", chat_id=chat_id, heading=_heading(turns), turns=turns)


@router.get("/chats/{chat_id}/turns", response_class=HTMLResponse)
async def turns_fragment(
    request: Request,
    who: CurrentCaller,
    chat_id: str,
    refine: Refinement | None = None,
    estimated_tokens: Annotated[int | None, Query(ge=0, le=10**9)] = None,
    cap_tokens: Annotated[int | None, Query(ge=0, le=10**9)] = None,
) -> HTMLResponse:
    turns = (
        await read_chat(services_of(request).ledger, who.tenant_id, who.member.uid, chat_id)
        if _CHAT_ID.match(chat_id)
        else None
    )
    if turns is None:
        raise HTTPException(404, "no such chat")
    return render("_turns.html", turns=refined(turns, refine, estimated_tokens, cap_tokens))
