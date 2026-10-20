"""The coverage report D-06 requires (issue #115): every obligation as
applicable, not applicable or unassessable, each unassessable one with its
clause and the fact that's missing. No score anywhere — the provenance of a
decision is its citations and the evidence it read.

Rendered as Markdown for a human; the structured form is `Coverage` itself.
"""

from __future__ import annotations

from compliance_rag.assessment.coverage import Coverage, Outcome, Question
from compliance_rag.corpus.display_form import NO_TITLES, cite

SHOWN_PERIODS = 6
"""Missing years listed by name before the rest are counted."""


def _cites(clause_ids: tuple[str, ...]) -> str:
    # An obligation and its questions rest on library law (ADR-0028).
    return ", ".join(cite(c, NO_TITLES) for c in clause_ids) or "no clause (reference data)"


def _question(question: Question) -> str:
    detail = f"{question.question} ({question.period}; {_cites(question.clause_ids)})"
    if question.conflict:
        detail += " — the evidence recorded disagrees or covers only part of the period"
    if question.also_needs:
        detail += f" — also needs: {', '.join(question.also_needs)}"
    if question.missing_periods:
        shown = ", ".join(question.missing_periods[:SHOWN_PERIODS])
        more = len(question.missing_periods) - SHOWN_PERIODS
        detail += f" — missing: {shown}" + (f" and {more} more" if more > 0 else "")
    return detail


def render(coverage: Coverage) -> str:
    lines = [
        f"# Coverage for {coverage.tenant_id} as of {coverage.as_of}",
        "",
        " · ".join(f"{len(coverage.with_outcome(o))} {o.label}" for o in Outcome),
    ]
    for outcome in Outcome:
        found = coverage.with_outcome(outcome)
        if not found:
            continue
        lines += ["", f"## {outcome.label.capitalize()}", ""]
        for obligation in found:
            lines.append(
                f"- **{obligation.obligation_id}** ({_cites(obligation.clause_ids)}): "
                f"{obligation.summary}"
            )
            if outcome is Outcome.UNASSESSABLE:
                questions = obligation.questions
                if not questions:
                    lines.append(f"  - {obligation.reason}")
                for question in questions:
                    lines.append(
                        f"  - Missing: {question.unassessable_reason} "
                        f"({_cites(question.clause_ids)})"
                    )
            else:
                lines.append(f"  - Because: {_cites(obligation.citations)}")
    if coverage.questions:
        lines += ["", "## Questions that would settle it", ""]
        lines += [f"{n}. {_question(q)}" for n, q in enumerate(coverage.questions, start=1)]
    return "\n".join(lines) + "\n"
