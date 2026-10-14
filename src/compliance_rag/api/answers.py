"""Asking a question (#149): `answer()` with Track A's scope and the tenant's
policies, streamed as server-sent events (PHASES.md Phase 3).

The pipeline:
1. The tenant's evidence goes to Track A coverage on `as_of`.
2. `LawScope.of(coverage)` scopes the law (D-03, ADR-0030).
3. `answer()` runs with that scope, and with the tenant's in-force internal
   policies as the cached prefix (D-11, #148).

The question is the retriever's query and nothing else (#29); nothing in it
names the tenant, the scope or the date. Earlier turns of a chat do not reach
the model: the prompt and retrieval are as they were without a chat.

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

**The chat** (#162). Every event carries a `chat_id`: the one the request
named, or the new chat it starts. The question and what the member is shown
for it are recorded as the chat's next two turns, in one ledger transaction,
with the request's trace and the member's uid (ADR-0036), *before* the `answer`
or `error` event is sent: nothing is shown that the history lacks. What is
recorded is built from the same `ChatAnswer` the event is built from, and that
type has no field for the model's draft, so a withheld answer's turn cannot
hold the prose its bad citation may repeat. A `chat_id` that isn't the member's
(another member's, another tenant's, or none at all) is a 404 before anything
is checked, retrieved, generated or written.

- **An answer** is recorded by the task that generated it, so a client that
  has left still finds it in the chat afterwards, as the tokens it spent are
  recorded either way.
- **A refused or failed request** records the question and an error marker:
  `residency`, `malformed_generation` or `internal`. Never the model's text,
  and not the message, which can name clauses: the marker is the whole record.
  The question is the member's own words and stays in the ledger, in Sydney
  (SERVICES.md); a residency refusal exists so that it never leaves Australia,
  not so that it is forgotten. A request abandoned before anything was
  generated (the client left first) records nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_question_allowed,
)
from compliance_rag.adapters.interfaces import (
    ChatAnswer,
    ChatCitation,
    ChatExchange,
    LawScope,
    LedgerStore,
    TurnOutcome,
)
from compliance_rag.api.deps import (
    EARLIEST,
    LATEST,
    UUID4_HEX,
    Caller,
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
from compliance_rag.corpus.models import ClauseRecord
from compliance_rag.retrieval.pipeline import RetrievalTrace
from compliance_rag.telemetry import current_trace_id

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)
MALFORMED: dict[str, object] = {
    "error": "malformed_generation",
    "message": "the model's reply wasn't a valid answer; nothing in it is shown",
}
FAILED: dict[str, object] = {"error": "internal", "message": "the answer couldn't be completed"}
BLOCKED = "a citation didn't match its clause, so the answer is withheld (ADR-0003)"
UNCITED = "the answer cited no clause, so nothing in it could be verified"
WITHHELD = {TurnOutcome.BLOCKED: BLOCKED, TurnOutcome.UNCITED: UNCITED}
"""Why an answer was withheld, in the words a member is shown: on the stream,
and again when the chat is read back."""

Answered = tuple[Answer, ChatAnswer]
"""An answer, and the part of it the member is shown (and the chat keeps)."""

_running: set[asyncio.Task[Answered]] = set()
"""Answers still being generated for a client that left: kept until they
finish, so the tokens they spend are recorded, and so is the answer."""

QuestionText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: QuestionText
    as_of: dt.date | None = Field(default=None, ge=EARLIEST, le=LATEST)
    chat_id: Annotated[str, StringConstraints(pattern=UUID4_HEX)] | None = Field(
        default=None, description="Continue this chat; without one a new chat is started"
    )

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


def _verified_clauses(answer: Answer) -> tuple[ClauseRecord, ...]:
    """The clauses a verified answer cites, as read from the clause store."""
    return tuple(c.clause for c in answer.citations if c.clause is not None)


def _shown(answer: Answer) -> ChatAnswer:
    """What the member is shown of an answer, and all of it that is kept: the
    verified text and its citations, or the ids of the citations that blocked
    it. The model's draft is not an input to anything but the verified text,
    which is shown only when every citation verified (ADR-0003)."""
    if answer.verified:
        return ChatAnswer(
            TurnOutcome.VERIFIED,
            text=answer.unverified_draft.answer,
            citations=tuple(
                ChatCitation(c.clause_id, c.version_id) for c in _verified_clauses(answer)
            ),
        )
    return ChatAnswer(
        TurnOutcome.BLOCKED if answer.blocked else TurnOutcome.UNCITED,
        blocked=tuple(c.citation.clause_id for c in answer.blocked),
    )


def _answer_json(answer: Answer, shown: ChatAnswer) -> dict[str, object]:
    common: dict[str, object] = {
        "verified": shown.outcome is TurnOutcome.VERIFIED,
        "blocked": [
            {"clause_id": c.citation.clause_id, "outcome": c.outcome.value} for c in answer.blocked
        ],
        "trace_id": answer.trace_id,
        "cost_usd": answer.cost_usd,
    }
    if shown.outcome is not TurnOutcome.VERIFIED:
        return {**common, "text": None, "citations": [], "withheld": WITHHELD[shown.outcome]}
    return {
        **common,
        "text": shown.text,
        "citations": [
            {
                "clause_id": c.clause_id,
                "cite": cite(c.clause_id),
                "version_id": c.version_id,
                "canonical_text": c.canonical_text,
            }
            for c in _verified_clauses(answer)
        ],
    }


def _failure(error: Exception) -> TurnOutcome:
    """How a request that ended in `error` is recorded."""
    if isinstance(error, GenerationResidencyError):
        return TurnOutcome.RESIDENCY
    if isinstance(error, MalformedGeneration):
        return TurnOutcome.MALFORMED_GENERATION
    return TurnOutcome.INTERNAL


def _error_json(outcome: TurnOutcome, error: Exception) -> dict[str, object]:
    if outcome is TurnOutcome.RESIDENCY:
        return {"error": "residency", "message": str(error)}
    return MALFORMED if outcome is TurnOutcome.MALFORMED_GENERATION else FAILED


@dataclass(frozen=True, slots=True)
class _ChatRecorder:
    """Writes the two turns this request adds to its chat: what the member
    asked, and what they were shown for it (#162)."""

    ledger: LedgerStore
    who: Caller
    chat_id: str
    question: str
    as_of: dt.date

    async def record(self, shown: ChatAnswer) -> None:
        await self.ledger.record_chat_exchange(
            ChatExchange(
                tenant_id=self.who.tenant_id,
                chat_id=self.chat_id,
                # The stable sign-in uid, not the email: an email can be
                # reassigned to someone who would then read this history
                # (ADR-0036).
                user_id=self.who.member.uid,
                question=self.question,
                answer=shown,
                as_of=self.as_of,
                trace_id=current_trace_id(),
            )
        )
        current = trace.get_current_span()
        current.set_attribute("chat.id", self.chat_id)
        current.set_attribute("chat.outcome", shown.outcome.value)

    async def record_failure(self, outcome: TurnOutcome) -> None:
        """The question and an error marker. A ledger that can't take it
        doesn't hide the error that is already being reported."""
        try:
            await self.record(ChatAnswer(outcome))
        except Exception:
            log.exception("a failed request couldn't be recorded in its chat")


def _finished(task: asyncio.Task[Answered]) -> None:
    _running.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning(
            "an answer finished with an error after its client left", exc_info=task.exception()
        )


@router.post("/answers")
async def ask(request: Request, who: CurrentCaller, body: Question) -> StreamingResponse:
    day = body.as_of or today()
    services = services_of(request)
    # Before anything else, and before the response is started: a chat that
    # isn't this member's, in this tenant, is not found, whoever's it is, so
    # nothing is checked, retrieved, generated or written for it (#162).
    if body.chat_id is None:
        chat_id = uuid.uuid4().hex
    elif await services.ledger.chat_turns(who.tenant_id, who.member.uid, body.chat_id):
        chat_id = body.chat_id
    else:
        raise HTTPException(404, "no such chat")
    chat = _ChatRecorder(services.ledger, who, chat_id, body.question, day)

    def staged(name: str, data: dict[str, object]) -> str:
        return event(name, {"chat_id": chat_id, **data})

    async def stages() -> AsyncIterator[str]:
        answering: asyncio.Task[Answered] | None = None
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
            yield staged("coverage", {"as_of": day.isoformat(), "counts": counts(coverage)})
            policies = await tenant_policies(
                services.ledger, services.clauses, tenant_id=who.tenant_id, as_of=day
            )
            retrieved: asyncio.Future[str] = asyncio.get_running_loop().create_future()

            async def on_retrieved(trace: RetrievalTrace) -> None:
                if not retrieved.done():
                    retrieved.set_result(staged("retrieval", _retrieval_json(trace)))

            async def answered() -> Answered:
                """Generates the answer and records it, whether or not a
                client is still there to be sent it."""
                try:
                    answer = await services.pipeline.answer(
                        body.question,
                        who.tenant_id,
                        day,
                        law=LawScope.of(coverage),
                        policies=policies,
                        on_retrieved=on_retrieved,
                        # Their own private additions are theirs alone: the
                        # stable sign-in uid, not the email (ADR-0036).
                        user_id=who.member.uid,
                    )
                    shown = _shown(answer)
                except Exception as failed:
                    await chat.record_failure(_failure(failed))
                    raise
                # Before it is sent: nothing is shown that the history lacks,
                # so a ledger that can't record it is an error, not an answer.
                # The member is then shown that error, so it is what's kept.
                try:
                    await chat.record(shown)
                except Exception:
                    await chat.record_failure(TurnOutcome.INTERNAL)
                    raise
                return answer, shown

            # A child task carries a copy of this request's context, so it runs
            # inside the request span and its usage and chat rows carry the
            # request's trace_id. It isn't cancelled if the client leaves: the
            # tokens it spends and the answer it makes are recorded either way.
            answering = asyncio.ensure_future(answered())
            _running.add(answering)
            answering.add_done_callback(_finished)
            await asyncio.wait({answering, retrieved}, return_when=asyncio.FIRST_COMPLETED)
            if retrieved.done():
                yield retrieved.result()
            answer, shown = await asyncio.shield(answering)
            yield staged("answer", _answer_json(answer, shown))
        except Exception as failed:
            outcome = _failure(failed)
            if answering is None:  # not yet started, so nothing has recorded this request
                await chat.record_failure(outcome)
            if outcome is TurnOutcome.RESIDENCY:
                log.warning("an answer was refused by the residency gate: %s", failed)
                trace.get_current_span().set_attribute("answer.refused", "residency")
            elif outcome is TurnOutcome.INTERNAL:
                log.exception("answering failed")
            yield staged("error", _error_json(outcome, failed))

    return StreamingResponse(stages(), media_type="text/event-stream")
