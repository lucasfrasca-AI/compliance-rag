"""What every endpoint depends on (#146, #147): the services the API runs
on, and who is asking, for which tenant. The tenant is always
`Settings.tenant_id`; nothing in a request names or changes it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from compliance_rag.adapters.interfaces import (
    GraphStore,
    LedgerStore,
    Member,
    NotAMember,
    TokenVerifier,
    Unauthenticated,
    VerifierUnavailable,
)
from compliance_rag.settings import Settings


@dataclass(frozen=True, slots=True)
class ApiServices:
    """What the API runs on. Grows with each endpoint ticket (#149)."""

    settings: Settings
    verifier: TokenVerifier
    ledger: LedgerStore
    graph: GraphStore
    closers: tuple[Callable[[], Awaitable[None]], ...] = field(
        default_factory=tuple[Callable[[], Awaitable[None]], ...]
    )
    """What to close on shutdown besides the verifier (the graph's driver)."""

    async def aclose(self) -> None:
        await self.verifier.aclose()
        for close in self.closers:
            await close()


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
