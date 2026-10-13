"""The product API (PHASES.md Phase 3; scaffolding #145). Built by
`create_app` from its services (`deps.ApiServices`), so tests pass fakes and
`api/main.py` passes the real adapters.

Every route but `/health` needs a signed-in member of the tenant
(`TokenVerifier`, #146), and acts on `Settings.tenant_id` (`deps.py`). Every
request runs in one span, from the first byte to the last, a streamed response
included (D-16), so ledger rows written while handling it carry its
`trace_id`. The response returns that id as `X-Trace-Id`, so a reported
problem can be found.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from compliance_rag.api import tenant
from compliance_rag.api.deps import ApiServices, Caller, current_caller
from compliance_rag.telemetry import current_trace_id, span


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
    app.include_router(tenant.router)

    # Not /healthz: Cloud Run reserves paths ending in "z".
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/me")
    async def me(who: Annotated[Caller, Depends(current_caller)]) -> dict[str, str]:
        return {"tenant_id": who.tenant_id, "uid": who.member.uid, "email": who.member.email}

    return app
