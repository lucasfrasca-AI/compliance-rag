"""Asking a question (#149): `answer()` with Track A's scope and the tenant's
policies, streamed as server-sent events (PHASES.md Phase 3).

The pipeline:
1. The tenant's evidence goes to Track A coverage on `as_of`.
2. `LawScope.of(coverage)` scopes the law (D-03, ADR-0030).
3. `answer()` runs with that scope, and with the tenant's in-force internal
   policies as the cached prefix (D-11, #148).

The question is the retriever's query and nothing else (#29); nothing in it
names the tenant, the scope or the date.

The stream carries stages and the verified result, never unverified tokens
(D-02):
- `coverage` once Track A has answered;
- `retrieval` once retrieval has, before generation starts;
- `answer` with the verified citations, each with its canonical clause text;
- or `error`.

A blocked citation is named by clause and outcome only, never by the model's
quote. The draft's prose travels as `unverified_text`, labelled as such (#145
default 3): whether a client shows it beside a blocked citation is a UI
decision.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from compliance_rag.adapters.interfaces import LawScope
from compliance_rag.api.deps import Caller, current_caller, services_of
from compliance_rag.api.tenant import EARLIEST, LATEST, today
from compliance_rag.assessment.coverage import Outcome
from compliance_rag.assessment.coverage_report import cite
from compliance_rag.assessment.draft import MalformedGeneration
from compliance_rag.assessment.evidence import Facts
from compliance_rag.assessment.pipeline import Answer
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.retrieval.pipeline import RetrievalTrace

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)
Member = Annotated[Caller, Depends(current_caller)]
MALFORMED: dict[str, object] = {
    "error": "malformed_generation",
    "message": "the model's reply wasn't a valid answer; nothing in it is shown",
}
FAILED: dict[str, object] = {"error": "internal", "message": "the answer couldn't be completed"}


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
    ]
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
    return {
        "verified": answer.verified,
        "unverified_text": answer.unverified_draft.answer,
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
        "blocked": [
            {"clause_id": c.citation.clause_id, "outcome": c.outcome.value} for c in answer.blocked
        ],
        "trace_id": answer.trace_id,
        "cost_usd": answer.cost_usd,
    }


@router.post("/answers")
async def ask(request: Request, who: Member, body: Question) -> StreamingResponse:
    day = body.as_of or today()
    services = services_of(request)

    async def stages() -> AsyncIterator[str]:
        try:
            evidence = await services.ledger.evidence(who.tenant_id)
            coverage = await services.graph.coverage(
                Facts.of(evidence), tenant_id=who.tenant_id, as_of=day
            )
            yield event(
                "coverage",
                {
                    "as_of": day.isoformat(),
                    "counts": {o.value: len(coverage.with_outcome(o)) for o in Outcome},
                },
            )
            policies = await tenant_policies(
                services.ledger, services.clauses, tenant_id=who.tenant_id, as_of=day
            )
            found: asyncio.Queue[str] = asyncio.Queue()

            async def retrieved(trace: RetrievalTrace) -> None:
                await found.put(event("retrieval", _retrieval_json(trace)))

            # In this request's task, so it runs inside the request span and
            # its ledger rows carry the request's trace_id.
            answering = asyncio.ensure_future(
                services.answers.answer(
                    body.question,
                    who.tenant_id,
                    day,
                    law=LawScope.of(coverage),
                    policies=policies,
                    on_retrieved=retrieved,
                )
            )
            try:
                waiting = asyncio.ensure_future(found.get())
                await asyncio.wait({answering, waiting}, return_when=asyncio.FIRST_COMPLETED)
                if waiting.done():
                    yield waiting.result()
                else:
                    waiting.cancel()
                    if not found.empty():  # put in the same tick the answer finished
                        yield found.get_nowait()
                answer = await answering
            finally:
                answering.cancel()  # a client that left stops the work
            yield event("answer", _answer_json(answer))
        except MalformedGeneration:
            yield event("error", MALFORMED)
        except Exception:
            log.exception("answering failed")
            yield event("error", FAILED)

    return StreamingResponse(stages(), media_type="text/event-stream")
