"""Asking a question (#149): `answer()` with Track A's scope and the tenant's
policies, streamed as server-sent events (PHASES.md Phase 3).

The pipeline:
1. The tenant's evidence goes to Track A coverage on `as_of`.
2. `LawScope.of(coverage)` scopes the law (D-03, ADR-0030).
3. `answer()` runs with that scope, and with the tenant's in-force internal
   policies as the cached prefix (D-11, #148).

The question is the retriever's query and nothing else (#29); nothing in it
names the tenant, the scope or the date.

While generation runs outside Australia (the build phase, ADR-0023), the
request is checked before anything is retrieved or generated (ADR-0032): the
member must be a registered build-phase tester (here, first), and every
clause of the CAG prefix registered synthetic (`answer()`). A refusal is an
`error` event naming the rule.

The stream carries stages and a verified result, never unverified text
(D-02):
- `coverage` once Track A has answered;
- `retrieval` once retrieval has, before generation starts;
- `answer`;
- or `error`.

A bad citation blocks the answer rather than shipping it (ADR-0003). So an
answer's text and citations are sent only when every citation verified. A
blocked answer sends its blocked citations (clause id and outcome, never the
model's quote) and why it was withheld. Nothing the model wrote goes with it,
because its prose can repeat a quote the verifier refused.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_question_allowed,
)
from compliance_rag.adapters.interfaces import LawScope
from compliance_rag.api.deps import (
    EARLIEST,
    LATEST,
    CurrentCaller,
    counts,
    coverage_on,
    services_of,
    today,
)
from compliance_rag.assessment.coverage_report import cite
from compliance_rag.assessment.draft import MalformedGeneration
from compliance_rag.assessment.pipeline import Answer
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.retrieval.pipeline import RetrievalTrace

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)
MALFORMED: dict[str, object] = {
    "error": "malformed_generation",
    "message": "the model's reply wasn't a valid answer; nothing in it is shown",
}
FAILED: dict[str, object] = {"error": "internal", "message": "the answer couldn't be completed"}
BLOCKED = "a citation didn't match its clause, so the answer is withheld (ADR-0003)"
UNCITED = "the answer cited no clause, so nothing in it could be verified"

_running: set[asyncio.Task[Answer]] = set()
"""Answers still being generated for a client that left: kept until they
finish, so the tokens they spend are recorded."""

QuestionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: QuestionText
    as_of: dt.date | None = Field(default=None, ge=EARLIEST, le=LATEST)

    @field_validator("question")
    @classmethod
    def _no_nul(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("text may not contain NUL")
        return v


def event(name: str, data: dict[str, object]) -> str:
    """One server-sent event."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _retrieval_json(trace: RetrievalTrace) -> dict[str, object]:
    return {
        "candidates": len(trace.ranked_candidates()),
        "by_channel": {channel.value: len(found) for channel, found in trace.per_channel.items()},
    }


def _answer_json(answer: Answer) -> dict[str, object]:
    common: dict[str, object] = {
        "verified": answer.verified,
        "blocked": [
            {"clause_id": c.citation.clause_id, "outcome": c.outcome.value} for c in answer.blocked
        ],
        "trace_id": answer.trace_id,
        "cost_usd": answer.cost_usd,
    }
    if not answer.verified:
        return {
            **common,
            "text": None,
            "citations": [],
            "withheld": BLOCKED if answer.blocked else UNCITED,
        }
    return {
        **common,
        "text": answer.unverified_draft.answer,
        "citations": [
            {
                "clause_id": c.clause.clause_id,
                "cite": cite(c.clause.clause_id),
                "version_id": c.clause.version_id,
                "canonical_text": c.clause.canonical_text,
            }
            for c in answer.citations
            if c.clause is not None
        ],
    }


def _finished(task: asyncio.Task[Answer]) -> None:
    _running.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning(
            "an answer finished with an error after its client left", exc_info=task.exception()
        )


@router.post("/answers")
async def ask(request: Request, who: CurrentCaller, body: Question) -> StreamingResponse:
    day = body.as_of or today()
    services = services_of(request)

    async def stages() -> AsyncIterator[str]:
        try:
            # Before anything else: offshore, only a registered tester's
            # question leaves Australia (ADR-0032). The prefix is checked by
            # `answer()` itself.
            assert_question_allowed(
                generation_region=services.settings.vertex_claude_region,
                asker=who.member.email,
                testers=services.settings.offshore_testers,
            )
            coverage = await coverage_on(services, who.tenant_id, day)
            yield event("coverage", {"as_of": day.isoformat(), "counts": counts(coverage)})
            policies = await tenant_policies(
                services.ledger, services.clauses, tenant_id=who.tenant_id, as_of=day
            )
            retrieved: asyncio.Future[str] = asyncio.get_running_loop().create_future()

            async def on_retrieved(trace: RetrievalTrace) -> None:
                if not retrieved.done():
                    retrieved.set_result(event("retrieval", _retrieval_json(trace)))

            # A child task carries a copy of this request's context, so it runs
            # inside the request span and its usage rows carry the request's
            # trace_id. It isn't cancelled if the client leaves: the tokens it
            # spends are recorded either way.
            answering = asyncio.ensure_future(
                services.pipeline.answer(
                    body.question,
                    who.tenant_id,
                    day,
                    law=LawScope.of(coverage),
                    policies=policies,
                    on_retrieved=on_retrieved,
                    # Their own private additions are theirs alone: the stable
                    # sign-in uid, not the email (ADR-0036).
                    user_id=who.member.uid,
                )
            )
            _running.add(answering)
            answering.add_done_callback(_finished)
            await asyncio.wait({answering, retrieved}, return_when=asyncio.FIRST_COMPLETED)
            if retrieved.done():
                yield retrieved.result()
            answer = await asyncio.shield(answering)
            yield event("answer", _answer_json(answer))
        except GenerationResidencyError as refused:
            log.warning("an answer was refused by the residency gate: %s", refused)
            trace.get_current_span().set_attribute("answer.refused", "residency")
            yield event("error", {"error": "residency", "message": str(refused)})
        except MalformedGeneration:
            yield event("error", MALFORMED)
        except Exception:
            log.exception("answering failed")
            yield event("error", FAILED)

    return StreamingResponse(stages(), media_type="text/event-stream")
