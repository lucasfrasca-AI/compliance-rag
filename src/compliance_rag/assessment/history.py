"""A chat's earlier turns, as the model and the retriever may use them (#278,
ADR-0053 decision 17, I4).

**To the model**, as fenced data (`prompt.history_note`): the member's earlier
questions and the verified answers they were shown, for the last
`MAX_EXCHANGES` exchanges. A withheld answer and a failed request contribute
their question only: never a draft, never withheld prose, never an error. The
turns come from the ledger, which holds only what the member was shown
(`ChatAnswer` has no field for a draft, ADR-0003), and this module reads nothing
else.

**To the retriever**, only the previous *member question*: the words the member
typed, joined to the current question (`retrieval_query`). An answer, a clause,
a title or anything the model wrote is never part of a query (#29, CLAUDE.md).
Even so the previous question is only ever words in a search: it reaches the
dense and keyword channels and the reranker as text, never query construction.

The not-in-environment check reads the current question alone (ADR-0051): a
question that named an instrument a turn ago does not flag this one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from compliance_rag.adapters.interfaces import ChatTurn, TurnOutcome, TurnRole
from compliance_rag.retrieval.router import is_clause_id_query

MAX_EXCHANGES = 3
"""How many earlier exchanges reach the model; the oldest drops first."""

PREVIOUS_QUESTION_QUERY_CHARS = 250
"""How much of the previous question joins the retrieval query. The reranker
rejects a query and chunk that together overflow its window, and a chunk may
fill all but 92 tokens of it (`chunker.RESERVED_FOR_QUERY_AND_SPECIALS`), so
the extra words are bounded. A question runs to 2,000 characters."""


@dataclass(frozen=True, slots=True)
class PriorExchange:
    """One earlier exchange of the chat: the member's question, and what they
    were shown if that was a verified answer's text."""

    question: str
    answer: str | None = None
    """The verified answer's text as shown. None for a withheld answer and for
    a request that ended in an error: those contribute the question only."""


def prior_exchanges(turns: Sequence[ChatTurn]) -> tuple[PriorExchange, ...]:
    """The chat's last `MAX_EXCHANGES` exchanges, oldest first, from its
    ledger turns. A question is followed by what the member was shown for it;
    only a verified answer's text is carried."""
    ordered = sorted(turns, key=lambda t: t.turn_index)
    exchanges: list[PriorExchange] = []
    for turn in ordered:
        if turn.role is TurnRole.USER:
            if turn.content is not None:
                exchanges.append(PriorExchange(turn.content))
        elif (
            exchanges
            and exchanges[-1].answer is None
            and turn.outcome is TurnOutcome.VERIFIED
            and turn.content is not None
        ):
            exchanges[-1] = PriorExchange(exchanges[-1].question, turn.content)
    return tuple(exchanges[-MAX_EXCHANGES:])


def retrieval_query(question: str, history: Sequence[PriorExchange]) -> str:
    """What the retriever is sent: the current question, led by the previous
    member question when the chat has one, so a follow-up ("and for agencies?")
    is searched with the words it refers to. A question that is itself a clause
    id stays an exact lookup."""
    if not history or is_clause_id_query(question):
        return question
    previous = history[-1].question.strip()
    if len(previous) > PREVIOUS_QUESTION_QUERY_CHARS:
        previous = previous[:PREVIOUS_QUESTION_QUERY_CHARS].rsplit(None, 1)[0]
    return f"{previous} {question}"
