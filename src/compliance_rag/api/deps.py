"""What every endpoint depends on (#146, #147): the services the API runs
on, and who is asking, for which tenant. The tenant is always
`Settings.tenant_id`; nothing in a request names or changes it.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from compliance_rag.adapters.firebase_auth import is_member
from compliance_rag.adapters.interfaces import (
    ClauseStore,
    GraphStore,
    LedgerStore,
    Member,
    NotAMember,
    TokenVerifier,
    Unauthenticated,
    VerifierUnavailable,
)
from compliance_rag.api.session import COOKIE_NAME, SessionCodec
from compliance_rag.assessment.coverage import Coverage, Outcome
from compliance_rag.assessment.evidence import Facts
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.settings import Settings


@dataclass(frozen=True, slots=True)
class ApiServices:
    """What the API runs on (#146-#149)."""

    settings: Settings
    verifier: TokenVerifier
    ledger: LedgerStore
    graph: GraphStore
    clauses: ClauseStore
    """Canonical text: the tenant's policies for the cached prefix (#148)."""
    pipeline: AssessmentPipeline
    """`answer()`: retrieval, generation, verification (#149)."""
    checks: tuple[Callable[[], Awaitable[None]], ...] = field(
        default_factory=tuple[Callable[[], Awaitable[None]], ...]
    )
    """Run at startup; one that raises stops the app starting (the graph's
    connectivity: bad credentials fail the deploy, not the first request)."""
    closers: tuple[Callable[[], Awaitable[None]], ...] = field(
        default_factory=tuple[Callable[[], Awaitable[None]], ...]
    )
    """What to close on shutdown besides the verifier (the graph's driver)."""

    async def start(self) -> None:
        for check in self.checks:
            await check()

    async def aclose(self) -> None:
        """Every closer runs, whichever raises."""
        async with AsyncExitStack() as stack:
            for close in (self.verifier.aclose, *self.closers):
                stack.push_async_callback(close)


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


async def bearer_member(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Member:
    """The member a Bearer ID token signs in; 401/403/503 otherwise. The only
    way to start a session (`POST /session`): a cookie can't mint a cookie."""
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


def cookie_member(request: Request, value: str) -> Member:
    """The member a session cookie signs in, with the outcomes a token has: 401
    if it is forged, tampered or expired, 403 if the member has since left the
    tenant. It needs no signing keys, so it is never a 503."""
    codec: SessionCodec | None = request.app.state.session
    if codec is None:
        raise HTTPException(401, "sign in required", headers=_CHALLENGE)
    try:
        member = codec.open(value)
    except Unauthenticated as refused:
        raise HTTPException(401, str(refused), headers=_CHALLENGE) from None
    if not is_member(member.email, services_of(request).settings.members):
        raise HTTPException(403, "not a member of this tenant")
    return member


async def current_member(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Member:
    """The Bearer token if one is sent, else the session cookie (#280). A Bearer
    header is never overridden by a cookie."""
    cookie = request.cookies.get(COOKIE_NAME)
    if credentials is None and cookie is not None:
        return cookie_member(request, cookie)
    return await bearer_member(request, credentials)


async def current_caller(
    request: Request, signed_in: Annotated[Member, Depends(current_member)]
) -> Caller:
    """The dependency every endpoint takes: the member, and the tenant from
    configuration."""
    return Caller(member=signed_in, tenant_id=services_of(request).settings.tenant_id)


CurrentCaller = Annotated[Caller, Depends(current_caller)]
"""An endpoint's caller: a signed-in member, on the configured tenant."""


async def current_admin(request: Request, caller: CurrentCaller) -> Caller:
    """The caller, if they are an admin (ADR-0053 U6, #294); 403 otherwise.

    Admins are configuration. Neither the Bearer token nor the cookie holds a
    role, so `API_ADMINS` is read afresh on every request and removing an
    address ends its admin access on the next one. An admin is a member first:
    an address in `API_ADMINS` alone is refused. Both lists go through
    `is_member`, the one comparison the verifier and the cookie reader use."""
    settings = services_of(request).settings
    email = caller.member.email
    if not (is_member(email, settings.members) and is_member(email, settings.admins)):
        raise HTTPException(403, "admin only")
    return caller


CurrentAdmin = Annotated[Caller, Depends(current_admin)]
"""An admin-only endpoint's caller: a member who is also in `API_ADMINS`."""

UUID4_HEX = r"^[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$"
"""What `uuid.uuid4().hex` makes: the form of an id the API makes (an
assessment's, a chat's), so no other form names one."""

SYDNEY = ZoneInfo("Australia/Sydney")
EARLIEST, LATEST = dt.date(1901, 1, 1), dt.date(2199, 12, 31)
"""The dates a request may name: the calendar the library's law uses."""


def today() -> dt.date:
    """The default `as_of`: today in Sydney, where the Commonwealth law the
    library holds is in force (the server's own clock runs in UTC)."""
    return dt.datetime.now(SYDNEY).date()


async def coverage_on(services: ApiServices, tenant_id: str, day: dt.date) -> Coverage:
    """Track A over the tenant's evidence on `day` (D-06)."""
    evidence = await services.ledger.evidence(tenant_id)
    return await services.graph.coverage(Facts.of(evidence), tenant_id=tenant_id, as_of=day)


def outcome_counts(outcomes: Iterable[Outcome]) -> dict[str, int]:
    """How many obligations came out each way, every outcome named."""
    tally = Counter(outcomes)
    return {o.value: tally[o] for o in Outcome}


def counts(coverage: Coverage) -> dict[str, int]:
    return outcome_counts(o.outcome for o in coverage.obligations)
