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


CurrentCaller = Annotated[Caller, Depends(current_caller)]
"""An endpoint's caller: a signed-in member, on the configured tenant."""

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
