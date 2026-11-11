"""The context `answer()` verified against is on the answer, and the live eval
records that set and no other (#335).

The live eval's recorded `context` is what `tests/test_recorded_answers.py`
re-verifies each citation against. It was recomputed in the script as the
ranked candidates plus the policies, but the pipeline verifies against more:
the delivered whole provisions (ADR-0045), the exceptions and qualifiers
fetched beside them (ADR-0037, ADR-0044) and the policies. The first live
recording therefore re-checked 38 citations against too small a set and
reported them `not_in_context`. The pipeline now hands back the set it gave
the verifier, so a recording can't drift from it.

Fakes only; no model is called.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

import pytest
from scripts import run_answer_eval

from compliance_rag.adapters.interfaces import LawScope
from compliance_rag.assessment.draft import DraftCitation
from compliance_rag.assessment.pipeline import LIBRARY_QUESTION, Answer, AssessmentPipeline
from compliance_rag.assessment.prompt import SourceClause
from compliance_rag.verifier.citations import CheckedCitation, Verifier
from tests.test_applicability import AS_OF
from tests.test_obligation_exceptions import (
    KEEP,
    KEEP_EXCEPTION,
    QUESTION,
    TEXTS,
    answering,
    citing,
    coverage_of,
)
from tests.test_repair_markers import (
    LAW,
    LAW_QUESTION,
    POLICY,
    THEN,
    policy_and_incomplete_law,
    wrong_quotes,
)

ACME_POLICY = SourceClause("acme/privacy-policy:v1:3/1", "Acme protects personal information.")


class SpyVerifier(Verifier):
    """The real verifier, recording the `context` every `check_all` received."""

    def __init__(self, inner: Verifier) -> None:
        self._inner = inner
        self.contexts: list[frozenset[str]] = []

    async def check_all(
        self,
        citations: Iterable[DraftCitation],
        *,
        context: frozenset[str],
        as_of: dt.date,
        user_id: str | None = None,
    ) -> tuple[CheckedCitation, ...]:
        self.contexts.append(context)
        return await self._inner.check_all(citations, context=context, as_of=as_of, user_id=user_id)

    async def check(
        self,
        citation: DraftCitation,
        *,
        context: frozenset[str],
        as_of: dt.date,
        user_id: str | None = None,
    ) -> CheckedCitation:
        return await self._inner.check(citation, context=context, as_of=as_of, user_id=user_id)


def spied(pipeline: AssessmentPipeline) -> SpyVerifier:
    spy = SpyVerifier(pipeline.verifier)
    pipeline.verifier = spy
    return spy


async def owed_exception_pipeline(
    reply: dict[str, object],
) -> tuple[AssessmentPipeline, SpyVerifier]:
    pipeline, _, _ = await answering(TEXTS, reply)
    return pipeline, spied(pipeline)


async def test_the_answers_context_is_the_set_the_verifier_received_with_owed_exceptions() -> None:
    """The exception is fetched by id: the ranking never returned it, so a
    context of ranked candidates would lack it."""
    law = LawScope.of(await coverage_of())
    pipeline, spy = await owed_exception_pipeline(citing(KEEP_EXCEPTION))
    answer = await pipeline.answer(QUESTION, "acme", AS_OF, law=law)

    ranked = {c.clause_id for c in answer.retrieval.ranked_candidates()}
    assert KEEP_EXCEPTION not in ranked  # the ranking left it out
    assert spy.contexts == [answer.context]
    assert answer.context == frozenset({KEEP, KEEP_EXCEPTION})
    assert answer.verified


async def test_the_answers_context_is_the_set_the_verifier_received_with_policies() -> None:
    pipeline, _, policies = await policy_and_incomplete_law(wrong_quotes(POLICY, LAW))
    spy = spied(pipeline)
    answer = await pipeline.answer(
        LAW_QUESTION, "acme", THEN, law=LIBRARY_QUESTION, policies=policies
    )
    assert spy.contexts == [answer.context]
    assert answer.context == frozenset({POLICY, LAW})


async def test_the_answers_context_holds_policies_and_owed_exceptions_together() -> None:
    law = LawScope.of(await coverage_of())
    pipeline, spy = await owed_exception_pipeline(citing(KEEP))
    answer = await pipeline.answer(QUESTION, "acme", AS_OF, law=law, policies=(ACME_POLICY,))
    assert spy.contexts == [answer.context]
    assert answer.context == frozenset({ACME_POLICY.clause_id, KEEP, KEEP_EXCEPTION})


async def recorded_row(
    monkeypatch: pytest.MonkeyPatch,
    pipeline: AssessmentPipeline,
    *,
    law: LawScope,
    policies: tuple[SourceClause, ...],
    question: str,
    as_of: dt.date,
) -> dict[str, Any]:  # Any: a JSON row
    """The eval script's own `_answer`, with the policies it would send and the
    law scope under test (the script asks with `LIBRARY_QUESTION`, which names
    no exceptions)."""
    monkeypatch.setattr(run_answer_eval, "_policies", lambda: policies)
    original = pipeline.answer

    async def with_law(*args: Any, **kwargs: Any) -> Answer:  # Any: forwards answer()
        return await original(*args, **{**kwargs, "law": law})

    monkeypatch.setattr(pipeline, "answer", with_law)
    return await run_answer_eval._answer(  # pyright: ignore[reportPrivateUsage]
        pipeline, question, "acme", as_of
    )


async def test_a_recorded_row_holds_the_verifiers_context_with_owed_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old formula (ranked candidates plus policies) lacked the fetched
    exception, so a recorded citation of it re-verified as `not_in_context`."""
    pipeline, spy = await owed_exception_pipeline(citing(KEEP_EXCEPTION))
    row = await recorded_row(
        monkeypatch,
        pipeline,
        law=LawScope.of(await coverage_of()),
        policies=(),
        question=QUESTION,
        as_of=AS_OF,
    )
    assert row["context"] == sorted(spy.contexts[0]) == sorted({KEEP, KEEP_EXCEPTION})
    assert row["final_citations"][0]["outcome"] == "verified"


async def test_a_recorded_row_holds_the_verifiers_context_with_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, _, policies = await policy_and_incomplete_law(wrong_quotes(POLICY, LAW))
    spy = spied(pipeline)
    row = await recorded_row(
        monkeypatch,
        pipeline,
        law=LIBRARY_QUESTION,
        policies=policies,
        question=LAW_QUESTION,
        as_of=THEN,
    )
    assert row["context"] == sorted(spy.contexts[0]) == sorted({POLICY, LAW})
