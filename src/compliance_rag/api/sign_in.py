"""Sign in and out for server-rendered pages (#280): the sign-in page, and the
two routes that start and end a session (`session.py` has the cookie).

The page loads only files this service serves (`/static`, with the Firebase Auth
SDK vendored under `static/vendor/firebase`, version and hashes in its README),
so it has one origin and one CSP. The only other hosts it may reach are
Firebase's own sign-in endpoints, named in `CONTENT_SECURITY_POLICY`, and, for
Google sign-in by popup, Google's gapi script and the project's auth domain
(`sign_in_content_security_policy`, this page only).
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from compliance_rag.adapters.interfaces import Member
from compliance_rag.api.deps import bearer_member, services_of
from compliance_rag.api.session import COOKIE_NAME, SessionCodec

STATIC_DIR = Path(__file__).parent / "static"

_SHARED_DIRECTIVES: tuple[tuple[str, str], ...] = (
    ("default-src", "'none'"),
    ("script-src", "'self'"),
    ("style-src", "'self'"),
    ("img-src", "'self'"),
    # The Ask pages' citation panel frames a source (`/v1/sources`, which
    # is sandboxed by its own CSP); nothing else may be framed.
    ("frame-src", "'self'"),
    (
        "connect-src",
        "'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com",
    ),
    ("form-action", "'self'"),
    ("base-uri", "'none'"),
    ("frame-ancestors", "'none'"),
)

CONTENT_SECURITY_POLICY = "; ".join(f"{name} {value}" for name, value in _SHARED_DIRECTIVES)
"""The one policy for the Ask pages and every error response under them: scripts,
styles and images from this origin only, no inline script or style, and Firebase's
two token endpoints as the only other hosts a page may call. The sign-in page alone
has a wider one (`sign_in_content_security_policy`)."""

GOOGLE_GAPI_HOST = "https://apis.google.com"
"""Added to the sign-in page's `script-src` and nowhere else. Firebase Auth's popup
flow (`browserPopupRedirectResolver`) injects a `<script>` for `<host>/js/api.js`
(`gapiScript` in the vendored `firebase-auth.js`), and gapi loads its own modules
from the same host. It is Google's script, not ours, and Google serves it
unversioned, so it cannot be pinned by hash. The human decided on 2026-11-08 to
allow it (#291)."""

_HOSTNAME = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*(?::\d{1,5})?"
)


def sign_in_content_security_policy(auth_domain: str) -> str:
    """The sign-in page's policy: `CONTENT_SECURITY_POLICY` plus two named
    additions, each only for Google sign-in by popup (#291).

    - `script-src` gains `GOOGLE_GAPI_HOST` (why, there).
    - `frame-src` gains the project's auth domain: Firebase frames
      `https://<authDomain>/__/auth/iframe` (`getIframeUrl` in the vendored SDK)
      to receive the popup's result.
    - `connect-src` gains nothing: the SDK's own calls go to
      `identitytoolkit.googleapis.com` and `securetoken.googleapis.com`, which the
      shared policy already names; the popup is a separate browsing context with
      its own policy, and gapi talks to the frame by `postMessage`.

    `auth_domain` is configuration, but it lands in a header, so anything that is
    not a hostname (a space, a `;`, a scheme, a wildcard) is refused."""
    if _HOSTNAME.fullmatch(auth_domain) is None:
        raise ValueError("the auth domain is not a hostname")
    wider = {
        "script-src": f"'self' {GOOGLE_GAPI_HOST}",
        "frame-src": f"'self' https://{auth_domain}",
    }
    return "; ".join(f"{name} {wider.get(name, value)}" for name, value in _SHARED_DIRECTIVES)


router = APIRouter()


class Assets(StaticFiles):
    """`/static`, with `nosniff` so a script is only ever run as a script."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


@router.post("/session")
async def start_session(
    request: Request,
    response: Response,
    member: Annotated[Member, Depends(bearer_member)],
) -> dict[str, object]:
    """Swap a verified Firebase ID token (the Bearer header) for the session
    cookie. The token is checked as it is for any route, so a non-member's gets
    a 403 and no cookie."""
    codec: SessionCodec | None = request.app.state.session
    if codec is None:
        raise HTTPException(503, "sessions are not configured")
    expires_at = codec.expiry_for(member)
    max_age = codec.max_age(expires_at)
    if max_age <= 0:
        raise HTTPException(401, "token expired", headers={"WWW-Authenticate": "Bearer"})
    response.set_cookie(
        COOKIE_NAME,
        codec.seal(member, expires_at=expires_at),
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"email": member.email, "expires_in": max_age}


@router.post("/session/logout", status_code=204)
async def end_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"


@router.get("/signin", response_class=HTMLResponse)
async def sign_in_page(request: Request) -> HTMLResponse:
    settings = services_of(request).settings
    if not settings.firebase_web_api_key or not settings.firebase_project_id:
        raise HTTPException(503, "the sign-in page is not configured")
    config = {
        "api-key": settings.firebase_web_api_key,
        "auth-domain": settings.web_auth_domain,
        "project-id": settings.firebase_project_id,
    }
    try:
        policy = sign_in_content_security_policy(settings.web_auth_domain)
    except ValueError:
        raise HTTPException(503, "the sign-in page is not configured") from None
    attributes = " ".join(f'data-{k}="{html.escape(v, quote=True)}"' for k, v in config.items())
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in</title>
<link rel="stylesheet" href="/static/signin.css">
</head>
<body>
<main id="signin" {attributes}>
<h1>Sign in</h1>
<form id="signin-form" autocomplete="on">
<label>Email <input name="email" type="email" autocomplete="username" required></label>
<label>Password
<input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button>
</form>
<p id="google-row"><button id="google-sign-in" type="button">Sign in with Google</button></p>
<p id="signin-status" role="status"></p>
<p id="signed-in" hidden>Signed in as <strong id="signed-in-email"></strong>.
<a href="/chats">Open your chats</a>
<button id="sign-out" type="button">Sign out</button></p>
</main>
<script type="module" src="/static/signin.js"></script>
</body>
</html>
"""
    return HTMLResponse(
        page,
        headers={
            "Content-Security-Policy": policy,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
