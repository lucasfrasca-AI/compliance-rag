"""The admin questionnaire (#304, #123; ADR-0053 I5 and U6): the facts Track A
says are missing, asked, answered as dated evidence, corrected by superseding.

`GET /admin/questionnaire` computes Track A's coverage of the workspace on
today's date in Sydney and draws `assessment.questionnaire.build` over it: the
unknown leaves of the unassessable obligations, grouped by the provision they
decide, each fact asked for the period the law reads (a state, a financial year,
each year a history leaf still lacks). A leaf a decided sibling short-circuited
is not among them, so it is never asked. The page below the questions lists the
answers on record, current and superseded, with disagreements flagged.

Two writes, both admin only (`PageAdmin`: sign-in, then the 403 page for a
member) and both through the one operation `POST /v1/profile/evidence` calls
(`tenant.record_asserted_evidence`), so the ledger's rules are the only rules:

- `POST /admin/questionnaire/answer` answers a slot nothing is recorded for.
  The server derives the open slots from Track A afresh and refuses an answer to
  any other (answered meanwhile, or Track A no longer needs it): a request names
  a field and a slot, never a question, and cannot ask one the tree doesn't.
- `POST /admin/questionnaire/correct` records a new answer that supersedes a
  current one, for the same field and period. The old row stays: it is what an
  earlier assessment read. Two current answers that disagree are never resolved
  here; they are flagged until a person corrects one.

The answer is `asserted` evidence in the admin's name, from a source they give,
under the request's `trace_id`. The request body can name no provenance.

After a write the page says which obligations left *unassessable* and how they
came out, from Track A before and after. htmx swaps the server's own fragment
(`_questionnaire.html`); the same form posted without script answers 303 back to
the page. A refusal is said in words and records nothing (a 200 fragment, which
htmx swaps; a 422 or 409 full page).
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from compliance_rag.api.admin import PageAdmin
from compliance_rag.api.deps import EARLIEST, LATEST, Caller, coverage_on, services_of, today
from compliance_rag.api.pages import PAGE_HEADERS, PageError, fact_in_words, is_htmx, render
from compliance_rag.api.tenant import record_asserted_evidence
from compliance_rag.assessment.coverage import AnswerKind, Coverage, Outcome
from compliance_rag.assessment.evidence import Evidence, Facts, FactValue, Period
from compliance_rag.assessment.questionnaire import (
    Item,
    Questionnaire,
    answer_kind_of,
    answer_period,
    build,
    on_record,
    year_of_key,
)
from compliance_rag.corpus.models import NO_END_DATE

router = APIRouter(prefix="/admin")

PAGE_PATH = "/admin/questionnaire"
MAX_FORM_BYTES = 16_384
MAX_TEXT = 2_000
MAX_ITEM = 200
MAX_ITEMS = 100
MAX_SOURCE = 500
_FORM_TYPE = "application/x-www-form-urlencoded"
_NUMBER = re.compile(r"-?(\d+|\d{1,3}(,\d{3})+)(\.\d+)?")


@dataclass(frozen=True, slots=True)
class Notice:
    text: str
    ok: bool


@dataclass(frozen=True, slots=True)
class Moved:
    """An obligation an answer changed the outcome of."""

    obligation_id: str
    summary: str
    before: Outcome
    after: Outcome


class Refused(Exception):
    """An answer the questionnaire won't record, in words for the admin."""

    def __init__(self, text: str, *, stale: bool = False) -> None:
        super().__init__(text)
        self.text = text
        self.stale = stale
        """The question moved on (answered meanwhile, no longer asked): the page
        should be read again, rather than the answer fixed."""


# ---- reading the form ---------------------------------------------------------------


async def _form(request: Request) -> dict[str, str]:
    """The urlencoded body of a form post, bounded and with no repeated name.
    (No multipart parser is installed, and a one-line form needs none.)"""
    kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind != _FORM_TYPE:
        raise PageError(415, "Not a form", "This takes a form post.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise PageError(413, "Too large", "That form is larger than any answer needs.")
    try:
        fields = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=20, errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise PageError(400, "Not a form", "That form could not be read.") from None
    if any(len(values) != 1 for values in fields.values()):
        raise PageError(400, "Not a form", "A form field was sent twice.")
    return {name: values[0] for name, values in fields.items()}


def _date(text: str, what: str) -> dt.date:
    try:
        day = dt.date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"{what} is a date, such as 2024-07-01") from None
    if not EARLIEST <= day <= LATEST:
        raise ValueError(f"{what} must be between {EARLIEST} and {LATEST}")
    return day


def parse_value(kind: AnswerKind, options: tuple[str, ...], raw: str) -> FactValue:
    """The fact `raw` says, as the leaf reads it, or `ValueError` in words. A
    yes or no is `true` or `false`; a number is finite (commas as thousands
    separators are read, nothing else is guessed) and a whole one is an int, so
    `3000000` and `3000000.00` are the same answer; a date is ISO."""
    text = raw.strip()
    if not text:
        raise ValueError("give an answer")
    if "\x00" in text:
        raise ValueError("an answer may not contain NUL")
    match kind:
        case AnswerKind.BOOLEAN:
            if text not in {"true", "false"}:
                raise ValueError("answer yes or no")
            return text == "true"
        case AnswerKind.NUMBER:
            if _NUMBER.fullmatch(text) is None:
                raise ValueError("answer with a number, such as 2500000")
            number = float(text.replace(",", ""))
            if not math.isfinite(number):
                raise ValueError("answer with a finite number")
            return int(number) if number.is_integer() else number
        case AnswerKind.DATE:
            return _date(text, "the answer")
        case AnswerKind.LIST:
            parts = text.replace("\n", ",").split(",")
            items = tuple(part.strip() for part in parts if part.strip())
            if not items or len(items) > MAX_ITEMS or any(len(i) > MAX_ITEM for i in items):
                raise ValueError(
                    f"give 1 to {MAX_ITEMS} items separated by commas, "
                    f"each up to {MAX_ITEM} characters"
                )
            return items
        case AnswerKind.TEXT:
            if len(text) > MAX_TEXT:
                raise ValueError(f"an answer is at most {MAX_TEXT} characters")
            if options and text not in options:
                raise ValueError(f"answer with one of: {', '.join(options)}")
            return text


def _source(form: dict[str, str]) -> str:
    source = form.get("source", "").strip()
    if not source:
        raise ValueError("say where the answer comes from (a document, or who told you)")
    if len(source) > MAX_SOURCE or "\x00" in source:
        raise ValueError(f"the source is at most {MAX_SOURCE} characters")
    return source


def _holds_from(form: dict[str, str], as_of: dt.date) -> dt.date | None:
    text = form.get("holds_from", "").strip()
    if not text:
        return None
    day = _date(text, "the date it holds from")
    if day > as_of:
        raise ValueError(f"it can't hold from {day}, after {as_of}: nothing would read it yet")
    return day


def _readable_on(period: Period, as_of: dt.date) -> None:
    """A state is read on `as_of`, so one beginning after it would be recorded
    and never read: refuse it in words."""
    if period.end == NO_END_DATE and period.start > as_of:
        raise ValueError(
            f"{period.start} is after {as_of}, so nothing would read it yet: "
            "give the date it holds from, on or before today"
        )


# ---- the page -----------------------------------------------------------------------


async def _world(request: Request, who: Caller) -> tuple[Coverage, tuple[Evidence, ...]]:
    services = services_of(request)
    return (
        await coverage_on(services, who.tenant_id, today()),
        await services.ledger.evidence(who.tenant_id),
    )


def _moved(before: Coverage, after: Coverage) -> tuple[Moved, ...]:
    """The obligations whose outcome an answer changed, worst first by id."""
    earlier = {o.obligation_id: o.outcome for o in before.obligations}
    return tuple(
        Moved(o.obligation_id, o.summary, earlier[o.obligation_id], o.outcome)
        for o in after.obligations
        if o.obligation_id in earlier and earlier[o.obligation_id] is not o.outcome
    )


async def _page(
    request: Request,
    who: Caller,
    *,
    fragment: bool,
    notice: Notice | None = None,
    moved: tuple[Moved, ...] = (),
    status: int = 200,
) -> HTMLResponse:
    coverage, evidence = await _world(request, who)
    asked = build(coverage, evidence)
    return render(
        request,
        "_questionnaire.html" if fragment else "questionnaire.html",
        status=status,
        q=asked,
        records=on_record(evidence),
        options=dict(asked.options),
        notice=notice,
        moved=moved,
    )


@router.get("/questionnaire", response_class=HTMLResponse)
async def questionnaire_page(request: Request, who: PageAdmin) -> HTMLResponse:
    return await _page(request, who, fragment=False)


async def _answered(
    request: Request,
    who: Caller,
    before: Coverage,
    done: Notice,
) -> Response:
    """After a write: the page as the ledger now has it, with what moved. A
    browser without script is sent back to the page instead."""
    if not is_htmx(request):
        return RedirectResponse(PAGE_PATH, status_code=303, headers=PAGE_HEADERS)
    after = await coverage_on(services_of(request), who.tenant_id, today())
    return await _page(request, who, fragment=True, notice=done, moved=_moved(before, after))


async def _refused(request: Request, who: Caller, refusal: Refused) -> Response:
    """A refusal in words, nothing recorded: the fragment is a 200 (htmx swaps no
    error status), the full page a 409 if the question moved on and a 422 if the
    answer is what's wrong."""
    notice = Notice(refusal.text, ok=False)
    if is_htmx(request):
        return await _page(request, who, fragment=True, notice=notice)
    return await _page(
        request, who, fragment=False, notice=notice, status=409 if refusal.stale else 422
    )


def _asked_item(q: Questionnaire, field: str, slot_key: str) -> tuple[Item, str]:
    item = q.item(field)
    slot = item.slot(slot_key) if item is not None else None
    if item is None or slot is None:
        raise Refused(
            "That question isn't open. Track A doesn't need it now (it was answered, or "
            "the answers already given settle the rest), so nothing was recorded.",
            stale=True,
        )
    if not slot.status.takes_answer:
        raise Refused(
            f"{slot.label} already has an answer. Correct it instead, so the earlier "
            "one is superseded and stays on record.",
            stale=True,
        )
    return item, slot.label


@router.post("/questionnaire/answer")
async def answer(request: Request, who: PageAdmin) -> Response:
    form = await _form(request)
    before, evidence = await _world(request, who)
    try:
        q = build(before, evidence)
        item, label = _asked_item(q, form.get("field", ""), form.get("slot", ""))
        try:
            value = parse_value(item.answer_kind, item.options, form.get("value", ""))
            slot_year = year_of_key(form.get("slot", ""))
            period = answer_period(
                item.answer_kind, value, year=slot_year, holds_from=_holds_from(form, q.as_of)
            )
            _readable_on(period, q.as_of)
            recorded = await record_asserted_evidence(
                request, who, field=item.field, value=value, period=period, source=_source(form)
            )
        except ValueError as invalid:
            raise Refused(f"Not recorded: {invalid}") from None
    except Refused as refusal:
        return await _refused(request, who, refusal)
    text = f"Recorded: {item.field.replace('_', ' ')}, {label}: {_said(recorded)}."
    return await _answered(request, who, before, Notice(text, ok=True))


@router.post("/questionnaire/correct")
async def correct(request: Request, who: PageAdmin) -> Response:
    """A new answer superseding a current one, for the same field. A date-valued
    state is recorded with the period it holds from, the date itself, so a
    corrected start date moves its period; any other keeps the old row's."""
    form = await _form(request)
    before, evidence = await _world(request, who)
    try:
        old = next(
            (e for e in Facts.of(evidence).current if e.evidence_id == form.get("supersedes")),
            None,
        )
        if old is None:
            raise Refused(
                "That answer isn't current: it was corrected already, or isn't on record. "
                "Nothing was recorded.",
                stale=True,
            )
        kind = answer_kind_of(old.value)
        options = build(before, evidence).options_of(old.field)
        try:
            value = parse_value(kind, options, form.get("value", ""))
            moves = kind is AnswerKind.DATE and old.period.end == NO_END_DATE
            period = Period(value) if moves and isinstance(value, dt.date) else old.period
            _readable_on(period, before.as_of)
            recorded = await record_asserted_evidence(
                request,
                who,
                field=old.field,
                value=value,
                period=period,
                source=_source(form),
                supersedes=old.evidence_id,
            )
        except ValueError as invalid:
            raise Refused(f"Not recorded: {invalid}") from None
    except Refused as refusal:
        return await _refused(request, who, refusal)
    text = (
        f"Corrected: {old.field.replace('_', ' ')}, {old.period.label}: "
        f"{_said(old)} is superseded by {_said(recorded)}."
    )
    return await _answered(request, who, before, Notice(text, ok=True))


def _said(evidence: Evidence) -> str:
    return fact_in_words(evidence.value)
