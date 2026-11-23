"""The skeleton a check's model runs share (#298 claims, #299 findings), and a policy
version's proposing run (#305): a streamed response of server-sent events, a reservation
held for the run and released by every path that ends without recording, and our words
for what went wrong.

**One run per check, whoever's request it is.** The endpoint reserves its run
(`LedgerStore.reserve_extraction`, `reserve_findings`) before the stream starts, so
of any number of concurrent requests exactly one holds it and the rest are a 409
that calls nothing. `run_stream` then owns the rest of the reservation's life:

- the work runs in a task the client leaving does not cancel (`CheckRun.spawn`), so
  the tokens it spends and what it records are kept either way, and the task
  releases the reservation itself if it fails;
- a request that ends before it handed the work off (a refusal, an error, a client
  that left first) releases the reservation here;
- a ledger that cannot take the release does not hide the error being reported: the
  reservation then lapses after its own time-out.

**Offshore, first.** While generation runs outside Australia only a registered
build-phase tester may send a document to be checked (`assert_submission_allowed`,
ADR-0032), before anything is read. That is `submission_gate`, the default; a run that
sends something else says its own (`gate`: a proposing run's is
`assert_mapping_run_allowed`).

**Our words, never the model's or the provider's.** `residency`,
`generation_unavailable`, `policy_corpus_too_large` and `internal` are common to
every run; an endpoint adds its own through `error_of`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_submission_allowed,
)
from compliance_rag.adapters.interfaces import GenerationUnavailable
from compliance_rag.api.deps import ApiServices, Caller
from compliance_rag.assessment.prefix_cap import PolicyCorpusTooLarge
from compliance_rag.telemetry import current_trace_id

log = logging.getLogger(__name__)

ErrorBody = tuple[dict[str, object], bool]
"""What an `error` event carries, and whether the operator should see it in the log
(an unexpected failure)."""

_running: set[asyncio.Task[Any]] = set()  # Any: the work's result, whatever it is
"""Runs still going for a client that left: kept until they finish, so the tokens
they spend are recorded, and so is what they make."""


def event(name: str, data: dict[str, object]) -> str:
    """One server-sent event."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def common_error(failed: Exception) -> ErrorBody | None:
    """Our word for a failure every run can have, or None for one that is its own."""
    if isinstance(failed, GenerationResidencyError):
        return {"error": "residency", "message": str(failed)}, False
    if isinstance(failed, GenerationUnavailable):
        return {
            "error": "generation_unavailable",
            "message": "the model isn't taking requests, so nothing was generated",
            "reason": failed.reason.value,
            "trace_id": current_trace_id(),
        }, False
    if isinstance(failed, PolicyCorpusTooLarge):
        return {
            "error": "policy_corpus_too_large",
            "message": str(failed),
            "estimated_tokens": failed.estimated_tokens,
            "cap_tokens": failed.cap_tokens,
            "trace_id": current_trace_id(),
        }, False
    return None


def _finished(task: asyncio.Task[Any]) -> None:  # Any: the work's result
    _running.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning(
            "a check's run finished with an error after its client left", exc_info=task.exception()
        )


async def release_quietly(release: Callable[[], Awaitable[None]]) -> None:
    """Release a reservation as failed. A ledger that cannot take it does not hide the
    error being reported."""
    try:
        await release()
    except Exception:
        log.exception("a check's reservation could not be released")


class CheckRun:
    """What a run's body is given: the way to hand its one costly piece of work off."""

    def __init__(self, release: Callable[[], Awaitable[None]]) -> None:
        self._release = release
        self.handed_off = False

    async def spawn[T](self, work: Callable[[], Awaitable[T]]) -> T:
        """Run `work` in a task the client leaving does not cancel, and wait for it. A
        child task carries a copy of this request's context, so its spans and rows
        carry the request's trace id. If `work` raises, the reservation is released
        here, by the task, whatever happens to the stream."""

        async def run() -> T:
            try:
                return await work()
            except BaseException:
                await release_quietly(self._release)
                raise

        task = asyncio.ensure_future(run())
        self.handed_off = True
        _running.add(task)
        task.add_done_callback(_finished)
        return await asyncio.shield(task)


def error_event(
    subject: dict[str, object],
    failed: Exception,
    error_of: Callable[[Exception], ErrorBody | None],
    failure: str,
) -> str:
    """The one `error` event for `failed`, in our words (`error_of`'s, then `common_error`'s,
    else `internal`), naming the run's `subject`, and logged as an unexpected failure only if
    it is one. What `run_stream` yields, and what a run refused before it starts says."""
    named = error_of(failed) or common_error(failed)
    message, unexpected = named or (
        {"error": "internal", "message": f"{failure} couldn't be completed"},
        True,
    )
    if unexpected:
        log.exception("%s failed", failure)
    else:
        log.warning("%s was not completed: %s", failure, message["error"])
    return event("error", {**subject, **message})


def submission_gate(services: ApiServices, who: Caller) -> None:
    """The offshore gate of a run over a submitted document: only a registered tester's
    document leaves Australia (ADR-0032), as for a question."""
    assert_submission_allowed(
        generation_region=services.settings.generation_region,
        submitter=who.member.email,
        testers=services.settings.offshore_testers,
    )


async def run_stream(
    *,
    services: ApiServices,
    who: Caller,
    subject: dict[str, object],
    release: Callable[[], Awaitable[None]],
    error_of: Callable[[Exception], ErrorBody | None],
    failure: str,
    body: Callable[[CheckRun], AsyncIterator[str]],
    gate: Callable[[ApiServices, Caller], None] = submission_gate,
) -> AsyncIterator[str]:
    """The events of one run: the offshore gate, then `body`'s, then, for anything
    that goes wrong, one `error` event in our words, naming the run's `subject` (what it
    was for: `{"check_id": ...}`). `error_of` is the endpoint's own words for its own
    failures (None for any it has none for); `failure` is what a failure it cannot name
    is logged and told as. `gate` is the run's offshore gate, run first, before anything
    is read or sent: a document check's by default (`submission_gate`)."""
    run = CheckRun(release)
    try:
        gate(services, who)
        async for chunk in body(run):
            yield chunk
    except Exception as failed:
        if not run.handed_off:  # nothing was handed the reservation: release it here
            await release_quietly(release)
        yield error_event(subject, failed, error_of, failure)
    except BaseException:  # the client left before the work was started
        if not run.handed_off:
            await release_quietly(release)
        raise
