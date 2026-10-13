"""The product API (PHASES.md Phase 3; scaffolding #145). Built by
`create_app` from its services, so tests pass fakes and `api/main.py` passes the
real adapters.

Every route but `/health` needs a signed-in member of the tenant
(`TokenVerifier`, #146), and acts on `Settings.tenant_id`. Nothing in a request
names or changes the tenant. Every request runs in one span, from the first
byte to the last, a streamed response included (D-16), so ledger rows written
while handling it carry its `trace_id`. The response returns that id as
`X-Trace-Id`, so a reported problem can be found.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from compliance_rag.adapters.interfaces import (
    Member,
    NotAMember,
    TokenVerifier,
    Unauthenticated,
    VerifierUnavailable,
)
from compliance_rag.settings import Settings
from compliance_rag.telemetry import current_trace_id, span


@dataclass(frozen=True, slots=True)
class ApiServices:
    """What the API runs on. Grows with each endpoint ticket (#147-#149)."""

    settings: Settings
    verifier: TokenVerifier

    async def aclose(self) -> None:
        await self.verifier.aclose()


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is asking, and for which tenant: always the configured one."""

    member: Member
    tenant_id: str


class RequestSpan:
    """One `api.request` span per HTTP request, open until the response's last
    byte, so a streamed response's work is inside it. Pure ASGI rather than
    `BaseHTTPMiddleware`, which ends at the response headers. It records the
    method, the path and the status; never a header, so never the token."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        attributes = {"http.method": str(scope["method"]), "http.path": str(scope["path"])}
        with span("api.request", **attributes) as current:
            trace_id = current_trace_id()

            async def send_traced(message: Message) -> None:
                if message["type"] == "http.response.start":
                    current.set_attribute("http.status_code", int(message["status"]))
                    MutableHeaders(scope=message).append("X-Trace-Id", trace_id)
                await send(message)

            await self.app(scope, receive, send_traced)


_bearer = HTTPBearer(auto_error=False)
_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def services_of(request: Request) -> ApiServices:
    services: ApiServices = request.app.state.services
    return services


async def current_member(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Member:
    if credentials is None:
        raise HTTPException(401, "sign in required", headers=_CHALLENGE)
    try:
        return await services_of(request).verifier.verify(credentials.credentials)
    except Unauthenticated as refused:
        raise HTTPException(401, str(refused), headers=_CHALLENGE) from None
    except NotAMember as refused:
        raise HTTPException(403, str(refused)) from None
    except VerifierUnavailable:
        raise HTTPException(503, "sign-in can't be checked right now") from None


async def current_caller(
    request: Request, signed_in: Annotated[Member, Depends(current_member)]
) -> Caller:
    """The dependency every endpoint takes: the member, and the tenant from
    configuration."""
    return Caller(member=signed_in, tenant_id=services_of(request).settings.tenant_id)


def create_app(services: ApiServices) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await services.aclose()

    docs = services.settings.api_docs
    app = FastAPI(
        title="compliance-rag",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.services = services
    app.add_middleware(RequestSpan)

    # Not /healthz: Cloud Run reserves paths ending in "z".
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/me")
    async def me(who: Annotated[Caller, Depends(current_caller)]) -> dict[str, str]:
        return {"tenant_id": who.tenant_id, "uid": who.member.uid, "email": who.member.email}

    return app
