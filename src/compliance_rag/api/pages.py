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

**The page shows what the ledger recorded, and nothing the browser says.** The
fragment takes no parameter. Every outcome, a refused generation
(`generation_unavailable`, with its reason) and an over-cap policy corpus
(`policy_corpus_too_large`, with the estimate and the cap) included, is a turn
outcome the ledger holds (#289, ledger 0019), so the specific card is the same
live and after a reload.

**Signing in.** A page request without a session redirects (303) to `/signin`.
A signed-in non-member gets a 403 page, as a redirect would only loop. The
fragment is fetched by script, so it answers 401 as JSON, which the script
turns into the same redirect.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from compliance_rag.api.chats import read_chat
from compliance_rag.api.deps import (
    SYDNEY,
    UUID4_HEX,
    Caller,
    CurrentCaller,
    current_caller,
    current_member,
    is_admin,
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
"""On every response under `/chats` and `/admin`, a redirect, an error page, a
fragment and its JSON refusals included (`PageHeaders`)."""

PAGE_PREFIXES = ("/chats", "/admin")

HEADING_CHARS = 80
_CHAT_ID = re.compile(UUID4_HEX)


def is_chat_id(text: str) -> bool:
    """Whether `text` is the form of a chat id, all of it: `fullmatch`, so a
    trailing newline (which `$` alone lets through) is not one."""
    return _CHAT_ID.fullmatch(text) is not None


class PageHeaders:
    """Adds `PAGE_HEADERS` to every response for a path under `/chats` or
    `/admin` (`PAGE_PREFIXES`), whatever
    made it: a page, a redirect, the fragment, or a refusal from a dependency
    or the validator (401, 404, 405, 422), which would otherwise carry none.
    Pure ASGI, so no route can forget it; a header a response already has is
    left as it is."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        if scope["type"] != "http" or not any(
            path == prefix or path.startswith(f"{prefix}/") for prefix in PAGE_PREFIXES
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in PAGE_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


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


def render(name: str, *, status: int = 200, admin: bool = False, **context: object) -> HTMLResponse:
    """`admin` is whether the page's reader is an admin: it decides only whether
    the nav links the admin panel. It is never what lets anyone in; the panel's
    own routes check the role on every request."""
    return HTMLResponse(
        ENV.get_template(name).render(is_admin=admin, **context),
        status_code=status,
        headers=PAGE_HEADERS,
    )


def sign_in_required(_: Request, __: Exception) -> Response:
    return RedirectResponse("/signin", status_code=303, headers=PAGE_HEADERS)


def page_error(_: Request, error: Exception) -> Response:
    if not isinstance(error, PageError):  # registered for PageError alone
        raise error
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


def reader_is_admin(request: Request, who: Caller) -> bool:
    return is_admin(services_of(request).settings, who.member.email)


def _no_such_chat() -> PageError:
    return PageError(404, "No such chat", "This chat doesn't exist, or it isn't yours.")


def _heading(turns: list[dict[str, object]]) -> str:
    first = next((t for t in turns if t["role"] == "user"), None)
    if first is None:
        return "New chat"
    text = " ".join(str(first["text"]).split())
    return text if len(text) <= HEADING_CHARS else text[: HEADING_CHARS - 1].rstrip() + "…"


@router.get("/chats", response_class=HTMLResponse)
async def chats_page(request: Request, who: PageCaller) -> HTMLResponse:
    chats = await services_of(request).ledger.chats(who.tenant_id, who.member.uid)
    return render(
        "chats.html", admin=reader_is_admin(request, who), chats=chats, email=who.member.email
    )


@router.get("/chats/new", response_class=HTMLResponse)
async def new_chat_page(request: Request, who: PageCaller) -> HTMLResponse:
    return render(
        "chat.html",
        admin=reader_is_admin(request, who),
        chat_id=None,
        heading="New chat",
        turns=[],
    )


@router.get("/chats/{chat_id}", response_class=HTMLResponse)
async def chat_page(request: Request, who: PageCaller, chat_id: str) -> HTMLResponse:
    turns = (
        await read_chat(services_of(request).ledger, who.tenant_id, who.member.uid, chat_id)
        if is_chat_id(chat_id)
        else None
    )
    if turns is None:
        raise _no_such_chat()
    return render(
        "chat.html",
        admin=reader_is_admin(request, who),
        chat_id=chat_id,
        heading=_heading(turns),
        turns=turns,
    )


@router.get("/chats/{chat_id}/turns", response_class=HTMLResponse)
async def turns_fragment(
    request: Request,
    who: CurrentCaller,
    chat_id: str,
) -> HTMLResponse:
    turns = (
        await read_chat(services_of(request).ledger, who.tenant_id, who.member.uid, chat_id)
        if is_chat_id(chat_id)
        else None
    )
    if turns is None:
        raise HTTPException(404, "no such chat")
    return render("_turns.html", turns=turns)
