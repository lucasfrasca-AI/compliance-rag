"""The product API (PHASES.md Phase 3; scaffolding #145). Built by
`create_app` from its services, so tests pass fakes and `api/main.py` passes the
real adapters.

Every route but `/healthz` needs a signed-in member of the tenant (`auth.py`,
#146), and acts on `Settings.tenant_id`. Nothing in a request names or changes
the tenant. Every request runs in a span (D-16), so ledger rows written while
handling it carry its `trace_id`. The response returns that id as `X-Trace-Id`,
so a reported problem can be found.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from compliance_rag.api.auth import (
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


_bearer = HTTPBearer(auto_error=False)
_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def services_of(request: Request) -> ApiServices:
    services: ApiServices = request.app.state.services
    return services


async def current_member(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Member:
    if credentials is None or credentials.scheme.lower() != "bearer":
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
    settings = services.settings
    if settings.env == "cloud" and not settings.firebase_project_id:
        raise ValueError("FIREBASE_PROJECT_ID is required in env=cloud (#146)")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await services.aclose()

    local = settings.env == "local"
    app = FastAPI(
        title="compliance-rag",
        lifespan=lifespan,
        # The schema is for local development, not published by the service.
        docs_url="/docs" if local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if local else None,
    )
    app.state.services = services

    @app.middleware("http")
    async def request_span(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # The path only: never a header, so never the token.
        with span(
            "api.request", **{"http.method": request.method, "http.path": request.url.path}
        ) as current:
            response = await call_next(request)
            current.set_attribute("http.status_code", response.status_code)
            response.headers["X-Trace-Id"] = current_trace_id()
            return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/me")
    async def me(who: Annotated[Caller, Depends(current_caller)]) -> dict[str, str]:
        return {"tenant_id": who.tenant_id, "uid": who.member.uid, "email": who.member.email}

    return app
