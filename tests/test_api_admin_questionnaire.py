"""The admin questionnaire (issue #304, #123, ADR-0053 I5 and U6).

`/admin/questionnaire` over Track A's coverage of the real Privacy Act mappings
(the fake graph runs the same evaluator as the real one) and the tenant's
evidence, driven end to end against `create_app`: an admin signs in, reads the
questions, answers them the way htmx does (a form post carrying `HX-Request`)
or the way a browser without script does (a plain post, which redirects), and
corrects an answer by superseding it.

What is checked, in the acceptance criteria's order:

- the questionnaire never asks about a short-circuited leaf, on the page and on
  the write: an answer to a question Track A has stopped asking is refused;
- a correction supersedes the earlier answer, which stays on record;
- disagreeing answers are flagged, and nothing resolves them;
- answering moves obligations out of unassessable (the response says which).

Then the rules every page and every write keeps: admin only, the one CSP, no
inline script, markup in an answer reaching the browser as text, the ledger's
rules the only rules, a refusal in words that records nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx
import pytest

from compliance_rag.adapters.fakes import FakeGraphStore, FakeLedgerStore
from compliance_rag.adapters.interfaces import Profile
from compliance_rag.api import questionnaire
from compliance_rag.api.app import create_app
from compliance_rag.api.sign_in import CONTENT_SECURITY_POLICY, STATIC_DIR
from compliance_rag.assessment.evidence import Evidence, EvidenceProvenance, Facts, Period
from compliance_rag.eval.applicability import load_applicability_set
from compliance_rag.graph.mappings import load_mapping_set
from compliance_rag.settings import Settings
from tests.api_support import MEMBER, SECOND, TENANT, api_settings, bearer, fake_api_services, mint
from tests.test_api_admin_subscriptions import parsed, words
from tests.test_applicability_gate import CORPUS_MAPPINGS, fake_for

SECRET = "s3cr3t-" + "k" * 40
ORIGIN = "https://api"
PAGE = "/admin/questionnaire"
ANSWER = f"{PAGE}/answer"
CORRECT = f"{PAGE}/correct"
HTMX = {"HX-Request": "true"}
TODAY = dt.date(2026, 10, 12)
"""FY2026-27, so the previous financial year is FY2025-26."""
APP11 = "OB-PA-APP11-1"
S6D4 = [
    "provides_health_service",
    "holds_health_information_outside_employee_records",
    "discloses_personal_information_for_benefit",
    "provides_benefit_to_collect_personal_information",
    "commonwealth_contracted_service_provider",
    "credit_reporting_body",
]


# ---- the world ---------------------------------------------------------------------------


@dataclass
class World:
    app: httpx.AsyncClient
    ledger: FakeLedgerStore
    settings: Settings


async def _ledger() -> FakeLedgerStore:
    """Acme registered, and another tenant with an answer of its own that must
    never reach Acme's page."""
    ledger = FakeLedgerStore()
    for tenant in (TENANT, "other-tenant"):
        await ledger.put_profile(Profile(tenant))
    await ledger.record_evidence(
        Evidence(
            tenant_id="other-tenant",
            evidence_id="other-1",
            field="legal_form",
            value="trust",
            period=Period(dt.date(2010, 1, 1)),
            provenance=EvidenceProvenance.ASSERTED,
            source="other tenant's own words",
            asserted_by="someone@other.example",
            trace_id="f" * 32,
        )
    )
    return ledger


async def _world(
    monkeypatch: pytest.MonkeyPatch, email: str, *, admins: str = MEMBER
) -> AsyncIterator[World]:
    monkeypatch.setattr(questionnaire, "today", lambda: TODAY)
    ledger = await _ledger()
    graph: FakeGraphStore = fake_for(
        load_mapping_set(sorted(CORPUS_MAPPINGS.glob("*.yaml"))), load_applicability_set()
    )
    settings = api_settings(session_secret=SECRET, api_admins=admins)
    services = fake_api_services(settings=settings, ledger=ledger, graph=graph)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    ) as client:
        assert (await client.post("/session", headers=bearer(mint(email=email)))).status_code == 200
        yield World(client, ledger, settings)


@pytest.fixture
async def admin(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    async for world in _world(monkeypatch, MEMBER):
        yield world


@pytest.fixture
async def member(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    """Listed in `API_MEMBERS`, not in `API_ADMINS`."""
    async for world in _world(monkeypatch, SECOND):
        yield world


# ---- reading the page --------------------------------------------------------------------


class Form:
    def __init__(self, attrs: dict[str, str]) -> None:
        self.attrs = attrs
        self.hidden: dict[str, str] = {}
        self.inputs: list[dict[str, str]] = []


class Page(HTMLParser):
    """The questionnaire's forms and elements, with the hidden fields of each."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self.elements: list[tuple[str, dict[str, str]]] = []
        self._form: Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapping = {k: v or "" for k, v in attrs}
        self.elements.append((tag, mapping))
        if tag == "form":
            self._form = Form(mapping)
            self.forms.append(self._form)
        elif tag in {"input", "select"} and self._form is not None:
            self._form.inputs.append({"tag": tag, **mapping})
            if mapping.get("type") == "hidden":
                self._form.hidden[mapping["name"]] = mapping.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None

    def where(self, tag: str, **attrs: str) -> list[dict[str, str]]:
        return [
            a for t, a in self.elements if t == tag and all(a.get(k) == v for k, v in attrs.items())
        ]

    def open_questions(self) -> list[tuple[str, str]]:
        """(field, slot) of every answer form."""
        return [
            (f.hidden["field"], f.hidden["slot"])
            for f in self.forms
            if f.attrs.get("class") == "answer"
        ]

    def fields(self) -> list[str]:
        return [a["data-field"] for a in self.where("article", **{"class": "card q-item"})]

    def routes(self) -> dict[str, list[str]]:
        """Route label to the fields asked under it."""
        found: dict[str, list[str]] = {}
        label = ""
        for tag, attrs in self.elements:
            if tag == "section" and "data-route" in attrs:
                label = attrs["data-route"]
                found[label] = []
            elif tag == "article" and attrs.get("class") == "card q-item" and label:
                found[label].append(attrs["data-field"])
            elif tag == "section":
                label = ""
        return found

    def slots(self) -> dict[tuple[str, str], str]:
        """(field, slot) to its status, for every slot drawn."""
        found: dict[tuple[str, str], str] = {}
        field = ""
        for tag, attrs in self.elements:
            if tag == "article" and attrs.get("class") == "card q-item":
                field = attrs["data-field"]
            elif tag == "div" and "data-slot" in attrs:
                found[(field, attrs["data-slot"])] = attrs["data-status"]
        return found


def page_of(text: str) -> Page:
    page = Page()
    page.feed(text)
    return page


async def read(world: World) -> Page:
    response = await world.app.get(PAGE)
    assert response.status_code == 200
    return page_of(response.text)


async def answer(
    world: World,
    field: str,
    slot: str,
    value: str,
    *,
    source: str = "the finance lead",
    holds_from: str | None = None,
    headers: dict[str, str] = HTMX,
) -> httpx.Response:
    data = {"field": field, "slot": slot, "value": value, "source": source}
    if holds_from is not None:
        data["holds_from"] = holds_from
    return await world.app.post(ANSWER, data=data, headers=headers)


async def correct(
    world: World,
    supersedes: str,
    value: str,
    *,
    source: str = "a correction",
    headers: dict[str, str] = HTMX,
) -> httpx.Response:
    data = {"supersedes": supersedes, "value": value, "source": source}
    return await world.app.post(CORRECT, data=data, headers=headers)


async def rows(world: World, field: str | None = None) -> list[Evidence]:
    found = await world.ledger.evidence(TENANT)
    return [e for e in found if field is None or e.field == field]


async def outcome_of(world: World, obligation_id: str) -> str:
    coverage = (await world.app.get("/v1/coverage", params={"as_of": TODAY.isoformat()})).json()
    return next(
        o["outcome"] for o in coverage["obligations"] if o["obligation_id"] == obligation_id
    )


# ---- an admin reads the questions ---------------------------------------------------------


async def test_an_admin_is_asked_the_facts_track_a_names_grouped_by_the_provision_they_decide(
    admin: World,
) -> None:
    response = await admin.app.get(PAGE)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    page = page_of(response.text)
    routes = page.routes()
    assert routes["s 6D(4)"] == S6D4
    assert routes["s 6D(9)"] == ["related_body_corporate_not_small"]
    assert routes["s 6E(1)"] == ["prescribed_under_s6e_1"]
    assert routes["s 6EA(1)"] == ["opted_in_under_s6ea"]
    # a state on the date, a year for each year the law reads
    questions = page.open_questions()
    assert ("legal_form", "state") in questions
    assert ("annual_turnover_aud", "fy2025") in questions
    assert ("annual_turnover_aud", "fy2026") in questions
    text = words(response.text)
    assert "FY2025-26" in text and "On 2026-10-12" in text
    assert "37 unassessable" in text and "0 applicable" in text
    # the question as the mapping words it, and the clause that makes it matter
    assert "Is the entity an agency within the meaning of s6(1)" in text
    assert "s 6D(4)(c)" in text


async def test_each_answer_form_is_a_post_the_server_answers_and_htmx_swaps(admin: World) -> None:
    page = await read(admin)
    forms = [f for f in page.forms if f.attrs.get("class") == "answer"]
    assert forms
    for form in forms:
        assert form.attrs["method"] == "post" and form.attrs["action"] == ANSWER
        assert form.attrs["hx-post"] == ANSWER
        assert form.attrs["hx-target"] == "#questionnaire"
        assert form.attrs["hx-swap"] == "outerHTML"
    by_field = {f.hidden["field"]: f for f in forms}
    # the input is the shape of the fact the leaf reads
    kinds = {
        name: {i.get("name"): i for i in by_field[name].inputs}["value"]
        for name in ("legal_form", "annual_turnover_aud", "business_started_on", "is_agency")
    }
    assert kinds["legal_form"]["tag"] == "select"
    assert kinds["annual_turnover_aud"]["inputmode"] == "decimal"
    assert kinds["business_started_on"]["type"] == "date"
    assert kinds["is_agency"]["tag"] == "select"
    # a state that isn't a date says when it holds from; a date is its own start
    holds = {f.hidden["field"]: any(i.get("name") == "holds_from" for i in f.inputs) for f in forms}
    assert holds["legal_form"] and holds["is_agency"]
    assert not holds["business_started_on"] and not holds["annual_turnover_aud"]


async def test_a_fact_read_by_many_conditions_is_one_question_with_each_of_its_clauses(
    admin: World,
) -> None:
    page = await read(admin)
    assert page.fields().count("legal_form") == 1
    assert len(page.fields()) == len(set(page.fields()))


async def test_the_fragment_is_the_panel_alone_and_the_page_is_the_panel_in_the_shell(
    admin: World,
) -> None:
    fragment = (await answer(admin, "is_agency", "state", "false", holds_from="2020-01-01")).text
    page = (await admin.app.get(PAGE)).text
    assert "<html" not in fragment and "<body" not in fragment
    assert page_of(fragment).where("div", id="questionnaire")
    assert page_of(page).where("div", id="questionnaire") and "<html" in page


async def test_another_tenants_answer_never_reaches_the_page_or_settles_a_question(
    admin: World,
) -> None:
    page = await admin.app.get(PAGE)
    assert "other tenant's own words" not in page.text
    assert ("legal_form", "state") in page_of(page.text).open_questions()  # theirs didn't settle it


# ---- answering ---------------------------------------------------------------------------


async def test_a_yes_or_no_is_recorded_as_asserted_evidence_in_the_admins_name(
    admin: World,
) -> None:
    response = await answer(
        admin, "is_agency", "state", "false", source="Companies register", holds_from="2019-05-01"
    )
    assert response.status_code == 200
    (row,) = await rows(admin, "is_agency")
    assert row.value is False and row.provenance is EvidenceProvenance.ASSERTED
    assert row.asserted_by == MEMBER and row.source == "Companies register"
    assert row.period == Period(dt.date(2019, 5, 1))
    assert row.trace_id == response.headers["x-trace-id"]
    assert row.supersedes is None
    # the same row the API would have made: Track A reads it
    assert Facts.of(await admin.ledger.evidence(TENANT)).on("is_agency", TODAY).value is False
    # and the question is gone, with the answer in the record
    page = page_of(response.text)
    assert ("is_agency", "state") not in page.open_questions()
    assert "Recorded: is agency" in words(response.text)
    assert "Companies register" in words(response.text)


async def test_a_start_date_holds_from_the_date_itself(admin: World) -> None:
    response = await answer(admin, "business_started_on", "state", "2015-03-02")
    assert response.status_code == 200
    (row,) = await rows(admin, "business_started_on")
    assert row.value == dt.date(2015, 3, 2) and row.period == Period(dt.date(2015, 3, 2))
    # knowing it opens the years it decides: each financial year since, the one it
    # started in too, now that s6DA(2)'s formula is in the corpus (#338, ADR-0058)
    page = page_of(response.text)
    years = {s for f, s in page.open_questions() if f == "annual_turnover_aud"}
    assert {"fy2014", "fy2015", "fy2016", "fy2025"} <= years and "fy2013" not in years
    assert ("business_started_on", "state") not in page.open_questions()


async def test_a_figure_is_recorded_for_its_financial_year_as_a_whole_number(
    admin: World,
) -> None:
    for slot, value in (("fy2025", "3,000,000"), ("fy2026", "2500000.00")):
        response = await answer(admin, "annual_turnover_aud", slot, value)
        assert response.status_code == 200
    first, second = await rows(admin, "annual_turnover_aud")
    assert (first.value, first.period) == (3_000_000, Period.financial_year(2025))
    assert (second.value, second.period) == (2_500_000, Period.financial_year(2026))
    assert isinstance(first.value, int) and isinstance(second.value, int)


async def test_a_text_fact_is_one_of_the_values_the_law_names(admin: World) -> None:
    response = await answer(admin, "legal_form", "state", "body_corporate", holds_from="2010-01-01")
    assert response.status_code == 200
    (row,) = await rows(admin, "legal_form")
    assert row.value == "body_corporate"


async def test_without_script_a_write_redirects_back_to_the_page(admin: World) -> None:
    response = await answer(
        admin, "is_agency", "state", "false", holds_from="2020-01-01", headers={}
    )
    assert response.status_code == 303 and response.headers["location"] == PAGE
    assert len(await rows(admin, "is_agency")) == 1
    refused = await answer(admin, "is_agency", "state", "true", holds_from="2020-01-01", headers={})
    assert refused.status_code == 409 and "<html" in refused.text
    assert len(await rows(admin, "is_agency")) == 1


# ---- a write that is not allowed records nothing ------------------------------------------


@pytest.mark.parametrize(
    ("field", "slot", "value", "extra", "why"),
    [
        ("is_agency", "state", "maybe", {}, "answer yes or no"),
        ("is_agency", "state", "", {}, "give an answer"),
        ("is_agency", "state", "false", {"holds_from": "2030-01-01"}, "after 2026-10-12"),
        ("is_agency", "state", "false", {"holds_from": "yesterday"}, "is a date"),
        ("is_agency", "state", "false", {"holds_from": "1700-01-01"}, "must be between"),
        ("is_agency", "state", "false", {}, "say the date it holds from"),
        ("annual_turnover_aud", "fy2025", "lots", {}, "answer with a number"),
        ("annual_turnover_aud", "fy2025", "nan", {}, "answer with a number"),
        ("annual_turnover_aud", "fy2025", "inf", {}, "answer with a number"),
        ("annual_turnover_aud", "fy2025", "1e400", {}, "answer with a number"),
        ("legal_form", "state", "company", {"holds_from": "2010-01-01"}, "answer with one of"),
        ("legal_form", "state", "trust\x00", {"holds_from": "2010-01-01"}, "NUL"),
        ("business_started_on", "state", "2027-01-01", {}, "after 2026-10-12"),
        ("business_started_on", "state", "not a date", {}, "is a date"),
        ("business_started_on", "state", "1800-01-01", {}, "must be between"),
    ],
)
async def test_an_answer_that_is_not_a_fact_is_said_in_words_and_records_nothing(
    admin: World, field: str, slot: str, value: str, extra: dict[str, str], why: str
) -> None:
    data = {"field": field, "slot": slot, "value": value, "source": "someone", **extra}
    fragment = await admin.app.post(ANSWER, data=data, headers=HTMX)
    assert fragment.status_code == 200  # htmx does not swap an error status
    assert page_of(fragment.text).where("div", **{"data-notice": "refused"})
    assert why in words(fragment.text)
    page = await admin.app.post(ANSWER, data=data)
    assert page.status_code == 422 and "<html" in page.text and why in words(page.text)
    assert await rows(admin) == [e for e in await rows(admin) if e.tenant_id == TENANT]
    assert await rows(admin, field) == []


async def test_an_answer_with_no_source_is_refused(admin: World) -> None:
    for source in ("", "   "):
        data = {
            "field": "is_agency",
            "slot": "state",
            "value": "false",
            "source": source,
            "holds_from": "2020-01-01",
        }
        response = await admin.app.post(ANSWER, data=data, headers=HTMX)
        assert "say where the answer comes from" in words(response.text)
    assert await rows(admin, "is_agency") == []


async def test_a_request_can_name_no_provenance_tenant_or_author(admin: World) -> None:
    data = {
        "field": "is_agency",
        "slot": "state",
        "value": "false",
        "source": "me",
        "holds_from": "2020-01-01",
        "provenance": "parsed",
        "tenant_id": "other-tenant",
        "asserted_by": "ceo@acme.example",
    }
    assert (await admin.app.post(ANSWER, data=data, headers=HTMX)).status_code == 200
    (row,) = await rows(admin, "is_agency")
    assert row.provenance is EvidenceProvenance.ASSERTED and row.tenant_id == TENANT
    assert row.asserted_by == MEMBER


async def test_the_form_is_bounded_and_unambiguous(admin: World) -> None:
    base = {"field": "is_agency", "slot": "state", "value": "false", "source": "x"}
    json_post = await admin.app.post(ANSWER, json=base, headers=HTMX)
    assert json_post.status_code == 415
    twice = await admin.app.post(
        ANSWER,
        content="field=is_agency&field=legal_form&slot=state&value=false&source=x",
        headers={**HTMX, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert twice.status_code == 400
    huge = await admin.app.post(
        ANSWER,
        content="source=" + "a" * 20_000,
        headers={**HTMX, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert huge.status_code == 413
    garbage = await admin.app.post(
        ANSWER,
        content=b"field=%ff%fe",
        headers={**HTMX, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert garbage.status_code == 400
    assert await rows(admin) == []


# ---- AC1: never a short-circuited leaf -----------------------------------------------------


async def test_once_one_s6d4_exception_holds_the_others_are_not_asked_and_cannot_be_answered(
    admin: World,
) -> None:
    before = await read(admin)
    assert set(S6D4) <= set(before.fields())
    response = await answer(
        admin, "credit_reporting_body", "state", "true", holds_from="2012-01-01"
    )
    assert response.status_code == 200
    after = page_of(response.text)
    assert set(S6D4).isdisjoint(after.fields())
    assert "s 6D(4)" not in after.routes()
    # the page the server draws now is the same
    assert (await read(admin)).fields() == after.fields()
    # and a request that names a question the tree no longer asks is refused
    for field in S6D4[:5]:
        stale = await answer(admin, field, "state", "true", holds_from="2012-01-01")
        assert stale.status_code == 200 and "isn't open" in words(stale.text)
        assert page_of(stale.text).where("div", **{"data-notice": "refused"})
        plain = await answer(admin, field, "state", "true", holds_from="2012-01-01", headers={})
        assert plain.status_code == 409
        assert await rows(admin, field) == []
    assert [e.field for e in await rows(admin)] == ["credit_reporting_body"]


async def test_a_field_the_law_never_reads_cannot_be_asked_about_by_naming_it(
    admin: World,
) -> None:
    for field, slot in (
        ("favourite_colour", "state"),
        ("legal_form", "fy2025"),  # a state, not a year
        ("annual_turnover_aud", "fy1999"),  # a year Track A isn't asking for
        ("annual_turnover_aud", "state"),
        ("legal_form", ""),
        ("", ""),
    ):
        response = await answer(admin, field, slot, "x", holds_from="2012-01-01")
        assert response.status_code == 200 and "isn't open" in words(response.text), (field, slot)
    assert await rows(admin) == []


async def test_a_year_before_the_business_started_is_not_asked(admin: World) -> None:
    await answer(admin, "business_started_on", "state", "2015-03-02")
    response = await answer(admin, "annual_turnover_aud", "fy2013", "1000")  # before it began
    assert "isn't open" in words(response.text)
    assert await rows(admin, "annual_turnover_aud") == []
    # The year it began in is asked: its figure is worked out by s6DA(2)'s formula,
    # which the corpus holds since #338 (ADR-0058), as the question says.
    response = await answer(admin, "annual_turnover_aud", "fy2014", "1000")
    assert "isn't open" not in words(response.text)
    assert [r.value for r in await rows(admin, "annual_turnover_aud")] == [1000]


# ---- AC2: a correction supersedes ----------------------------------------------------------


async def test_a_correction_is_new_evidence_superseding_the_old_which_stays_on_record(
    admin: World,
) -> None:
    await answer(admin, "legal_form", "state", "trust", holds_from="2010-01-01")
    (old,) = await rows(admin, "legal_form")
    response = await correct(admin, old.evidence_id, "partnership", source="Deed of partnership")
    assert response.status_code == 200
    first, second = await rows(admin, "legal_form")
    assert first == old  # untouched
    assert second.value == "partnership" and second.supersedes == old.evidence_id
    assert second.period == old.period and second.source == "Deed of partnership"
    assert second.provenance is EvidenceProvenance.ASSERTED and second.asserted_by == MEMBER
    assert second.trace_id == response.headers["x-trace-id"]
    # Track A reads the new answer only; the old one is what an earlier assessment read
    current = Facts.of(await admin.ledger.evidence(TENANT))
    assert [e.evidence_id for e in current.current if e.field == "legal_form"] == [
        second.evidence_id
    ]
    earlier = Facts.of(await admin.ledger.evidence(TENANT), recorded_by=first.recorded_at)
    assert earlier.on("legal_form", TODAY).value == "trust"
    # the page shows both, the old one as superseded
    page = page_of(response.text)
    states = {
        a["data-evidence"]: a["data-current"] for a in page.where("li") if "data-evidence" in a
    }
    assert states == {old.evidence_id: "false", second.evidence_id: "true"}
    assert "Corrected: legal form" in words(response.text)
    # and the API reads the same
    profile = (await admin.app.get("/v1/profile")).json()
    flags = {
        e["evidence_id"]: e["current"] for e in profile["evidence"] if e["field"] == "legal_form"
    }
    assert flags == {old.evidence_id: False, second.evidence_id: True}


async def test_a_corrected_start_date_moves_the_period_it_holds_from(admin: World) -> None:
    await answer(admin, "business_started_on", "state", "2015-03-02")
    (old,) = await rows(admin, "business_started_on")
    assert (await correct(admin, old.evidence_id, "2016-08-09")).status_code == 200
    _, new = await rows(admin, "business_started_on")
    assert new.value == dt.date(2016, 8, 9) and new.period == Period(dt.date(2016, 8, 9))
    assert new.supersedes == old.evidence_id


async def test_a_figure_is_corrected_for_the_same_year(admin: World) -> None:
    await answer(admin, "annual_turnover_aud", "fy2025", "1000000")
    (old,) = await rows(admin, "annual_turnover_aud")
    await correct(admin, old.evidence_id, "4,200,000.50")
    _, new = await rows(admin, "annual_turnover_aud")
    assert new.value == 4_200_000.5 and new.period == Period.financial_year(2025)


async def test_an_answer_already_corrected_cannot_be_corrected_again(admin: World) -> None:
    await answer(admin, "legal_form", "state", "trust", holds_from="2010-01-01")
    (old,) = await rows(admin, "legal_form")
    assert (await correct(admin, old.evidence_id, "partnership")).status_code == 200
    again = await correct(admin, old.evidence_id, "individual")
    assert again.status_code == 200 and "isn't current" in words(again.text)
    plain = await correct(admin, old.evidence_id, "individual", headers={})
    assert plain.status_code == 409
    for unknown in ("nope", "", "other-1"):  # another tenant's id is no answer of this one
        gone = await correct(admin, unknown, "individual")
        assert "isn't current" in words(gone.text)
    assert len(await rows(admin, "legal_form")) == 2


async def test_a_correction_is_checked_like_any_answer(admin: World) -> None:
    await answer(admin, "annual_turnover_aud", "fy2025", "1000000")
    (old,) = await rows(admin, "annual_turnover_aud")
    for value in ("lots", "", "nan"):
        response = await correct(admin, old.evidence_id, value)
        assert page_of(response.text).where("div", **{"data-notice": "refused"})
    no_source = await correct(admin, old.evidence_id, "5", source=" ")
    assert "say where the answer comes from" in words(no_source.text)
    assert len(await rows(admin, "annual_turnover_aud")) == 1


async def test_a_correction_offers_the_closed_list_the_law_names(admin: World) -> None:
    await answer(admin, "legal_form", "state", "trust", holds_from="2010-01-01")
    (old,) = await rows(admin, "legal_form")
    # on record, the input to correct it is the same select
    page = await read(admin)
    (form,) = [f for f in page.forms if f.hidden.get("supersedes") == old.evidence_id]
    value = next(i for i in form.inputs if i.get("name") == "value")
    assert value["tag"] == "select"
    refused = await correct(admin, old.evidence_id, "company")
    assert "answer with one of" in words(refused.text)


# ---- AC3: disagreeing answers are flagged, not resolved ------------------------------------


async def _two_admins_disagree(world: World) -> tuple[Evidence, Evidence]:
    """The way it happens: two answers to the same open question, from two tabs
    or two admins, through the API's own write."""
    for value in ("trust", "partnership"):
        response = await world.app.post(
            "/v1/profile/evidence",
            json={
                "field": "legal_form",
                "value": value,
                "period": {"start": "2010-01-01"},
                "source": f"said {value}",
            },
        )
        assert response.status_code == 201
    first, second = await rows(world, "legal_form")
    return first, second


async def test_two_current_answers_that_disagree_are_flagged_and_left_alone(admin: World) -> None:
    first, second = await _two_admins_disagree(admin)
    page = await read(admin)
    for _ in range(2):  # reading changes nothing
        response = await admin.app.get(PAGE)
        page = page_of(response.text)
        assert page.slots()[("legal_form", "state")] == "disagree"
        assert "Answers disagree" in words(response.text)
        records = {
            a["data-field"]: a["data-disagree"]
            for a in page.where("article", **{"class": "card record"})
        }
        assert records["legal_form"] == "true"
        # no second answer is invited, and nothing has chosen between them
        assert ("legal_form", "state") not in page.open_questions()
        current = {e.evidence_id for e in Facts.of(await admin.ledger.evidence(TENANT)).current}
        assert {first.evidence_id, second.evidence_id} <= current
    # each current answer carries its own correction
    supersedes = {f.hidden["supersedes"] for f in page.forms if "supersedes" in f.hidden}
    assert {first.evidence_id, second.evidence_id} <= supersedes
    # Track A treats it as unknown, with the same flag
    coverage = (await admin.app.get("/v1/coverage", params={"as_of": TODAY.isoformat()})).json()
    flagged = [q for q in coverage["questions"] if q["field"] == "legal_form"]
    assert flagged and all(q["conflict"] for q in flagged)


async def test_a_new_answer_cannot_be_added_beside_a_disagreement(admin: World) -> None:
    await _two_admins_disagree(admin)
    response = await answer(admin, "legal_form", "state", "individual", holds_from="2010-01-01")
    assert "already has an answer" in words(response.text)
    assert len(await rows(admin, "legal_form")) == 2


async def test_the_disagreement_ends_only_when_a_person_corrects_one_to_agree(
    admin: World,
) -> None:
    first, second = await _two_admins_disagree(admin)
    response = await correct(
        admin, first.evidence_id, "partnership", source="settled with the board"
    )
    page = page_of(response.text)
    assert ("legal_form", "state") not in page.slots()  # agreed, so settled, so not asked
    flags = {a["data-disagree"] for a in page.where("article", **{"class": "card record"})}
    assert "true" not in flags
    now = Facts.of(await admin.ledger.evidence(TENANT))
    assert now.on("legal_form", TODAY).value == "partnership"
    assert not now.on("legal_form", TODAY).conflict
    # the answer it replaced is still on record
    assert first.evidence_id in {e.evidence_id for e in await rows(admin, "legal_form")}
    assert second.evidence_id in {e.evidence_id for e in now.current}


async def test_a_figure_for_part_of_a_year_is_flagged_not_hidden(admin: World) -> None:
    await admin.app.post(
        "/v1/profile/evidence",
        json={
            "field": "annual_turnover_aud",
            "value": 100,
            "period": {"start": "2025-09-01", "end": "2026-07-01"},
            "source": "part year",
        },
    )
    page = page_of((await admin.app.get(PAGE)).text)
    assert page.slots()[("annual_turnover_aud", "fy2025")] == "partial"
    assert ("annual_turnover_aud", "fy2025") not in page.open_questions()


# ---- AC4: answering moves obligations out of unassessable ----------------------------------

PROFILE: dict[str, str] = {
    "legal_form": "body_corporate",
    "carries_on_a_business": "true",
    "business_started_on": "2015-03-02",
    "annual_turnover_aud": "5000000",
}
"""A body corporate over the threshold; every other yes or no is no."""


async def _answer_everything_asked(
    world: World, profile: dict[str, str] = PROFILE
) -> list[httpx.Response]:
    """What an admin does: answer what the page asks, then read what it asks
    next, until nothing is asked."""
    responses: list[httpx.Response] = []
    for _ in range(6):
        asked = (await read(world)).open_questions()
        if not asked:
            return responses
        for field, slot in asked:
            value = profile.get(field, "false")
            holds = (
                None if field in {"business_started_on", "annual_turnover_aud"} else "2010-01-01"
            )
            response = await answer(world, field, slot, value, holds_from=holds)
            assert response.status_code == 200
            responses.append(response)
    raise AssertionError("the questionnaire never ran dry")


async def test_answering_moves_an_obligation_out_of_unassessable(admin: World) -> None:
    assert await outcome_of(admin, APP11) == "unassessable"
    responses = await _answer_everything_asked(admin)
    assert await outcome_of(admin, APP11) == "applicable"
    # the page said so as it happened
    moves = [
        (a["data-before"], a["data-after"])
        for r in responses
        for a in page_of(r.text).where("li")
        if a.get("data-obligation") == APP11
    ]
    assert moves == [("unassessable", "applicable")]
    # nothing is left to ask, and the totals say what is now decided
    final = await admin.app.get(PAGE)
    assert page_of(final.text).open_questions() == []
    assert page_of(final.text).routes() == {}
    assert "Nothing is asked" in words(final.text)
    counts = (await admin.app.get("/v1/coverage", params={"as_of": TODAY.isoformat()})).json()[
        "counts"
    ]
    assert counts["unassessable"] < 37 and counts["applicable"] > 0
    for outcome, n in counts.items():
        assert f"{n} {outcome.replace('_', ' ')}" in words(final.text)


async def test_the_response_to_the_answer_that_decides_it_lists_what_moved(admin: World) -> None:
    last: httpx.Response | None = None
    for response in await _answer_everything_asked(admin):
        if page_of(response.text).where("ul", **{"class": "moved"}):
            last = response
            assert "Moved by this answer" in words(response.text)
    assert last is not None


async def test_an_answer_that_decides_nothing_says_so_and_may_open_more(admin: World) -> None:
    response = await answer(admin, "business_started_on", "state", "2015-03-02")
    assert "No obligation changed outcome yet" in words(response.text)
    assert not page_of(response.text).where("ul", **{"class": "moved"})


async def test_a_conditional_conclusion_firms_up_only_because_coverage_does(admin: World) -> None:
    """A check's finding against an obligation Track A can't decide is
    conditional (ADR-0053 F4); it is firm once that obligation is decided.
    Check isn't built yet, so what is proved is the premise it reads: the
    obligation leaves `unassessable` for `applicable` or `not_applicable`, the
    same coverage `GET /v1/coverage` serves."""
    before = (await admin.app.get("/v1/coverage", params={"as_of": TODAY.isoformat()})).json()
    assert before["counts"] == {"applicable": 0, "not_applicable": 0, "unassessable": 37}
    await _answer_everything_asked(admin)
    after = (await admin.app.get("/v1/coverage", params={"as_of": TODAY.isoformat()})).json()
    assert after["counts"]["unassessable"] < 37
    assert after["counts"]["applicable"] + after["counts"]["not_applicable"] > 0
    assert not after["questions"]


# ---- admin only ----------------------------------------------------------------------------


async def test_a_member_gets_a_403_page_not_the_questionnaire(member: World) -> None:
    response = await member.app.get(PAGE)
    assert response.status_code == 403
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "legal_form" not in response.text and page_of(response.text).forms == []


@pytest.mark.parametrize("headers", [{}, HTMX], ids=["plain", "htmx"])
async def test_a_member_gets_403_on_both_writes_and_nothing_is_recorded(
    member: World, headers: dict[str, str]
) -> None:
    refused = await answer(
        member, "is_agency", "state", "false", holds_from="2020-01-01", headers=headers
    )
    assert refused.status_code == 403
    assert (await correct(member, "other-1", "x", headers=headers)).status_code == 403
    assert await rows(member) == []


async def test_a_member_sees_no_link_to_the_questionnaire_and_an_admin_does(
    member: World, admin: World
) -> None:
    for path in ("/chats", "/chats/new"):
        assert PAGE not in (await member.app.get(path)).text, path
        assert page_of((await admin.app.get(path)).text).where("a", href=PAGE), path


async def test_the_role_is_read_on_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async for world in _world(monkeypatch, MEMBER):
        assert (await world.app.get(PAGE)).status_code == 200
        # the same app, the same cookie: only configuration changes
        object.__setattr__(world.settings, "api_admins", "")
        assert (await world.app.get(PAGE)).status_code == 403
        denied = await answer(world, "is_agency", "state", "false", holds_from="2020-01-01")
        assert denied.status_code == 403
        assert await rows(world) == []


async def test_nobody_signed_in_is_sent_to_sign_in_and_nothing_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(questionnaire, "today", lambda: TODAY)
    ledger = await _ledger()
    services = fake_api_services(settings=api_settings(session_secret=SECRET), ledger=ledger)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url=ORIGIN
    ) as client:
        for method, path in (("GET", PAGE), ("POST", ANSWER), ("POST", CORRECT)):
            response = await client.request(method, path)
            assert response.status_code == 303 and response.headers["location"] == "/signin"
            leaving = await client.request(method, path, headers=HTMX)
            assert leaving.status_code == 401 and leaving.headers["hx-redirect"] == "/signin"
            assert leaving.text == ""
    assert [e.tenant_id for e in await ledger.evidence(TENANT)] == []


async def test_a_post_from_another_origin_with_the_cookie_is_refused(admin: World) -> None:
    for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}):
        data = {
            "field": "is_agency",
            "slot": "state",
            "value": "false",
            "source": "x",
            "holds_from": "2020-01-01",
        }
        assert (await admin.app.post(ANSWER, data=data, headers=headers)).status_code == 403
        assert (await admin.app.post(CORRECT, data=data, headers=headers)).status_code == 403
    assert await rows(admin) == []


# ---- the rules every page keeps ------------------------------------------------------------


async def test_the_one_strict_csp_is_on_the_page_the_fragments_and_every_refusal(
    admin: World, member: World
) -> None:
    await answer(admin, "legal_form", "state", "trust", holds_from="2010-01-01")
    (old,) = await rows(admin, "legal_form")
    responses = {
        "page": await admin.app.get(PAGE),
        "answer fragment": await answer(
            admin, "is_agency", "state", "false", holds_from="2020-01-01"
        ),
        "correct fragment": await correct(admin, old.evidence_id, "partnership"),
        "refusal": await answer(admin, "nope", "state", "x"),
        "redirect": await answer(
            admin, "is_agency", "state", "true", holds_from="2020-01-01", headers={}
        ),
        "member page": await member.app.get(PAGE),
        "member write": await answer(
            member, "is_agency", "state", "false", holds_from="2020-01-01"
        ),
        "GET on a write (405)": await admin.app.get(ANSWER),
        "not a form (415)": await admin.app.post(ANSWER, json={}),
        "cross-origin": await admin.app.post(ANSWER, headers={"Origin": "https://evil.example"}),
    }
    assert [r.status_code for r in responses.values()] == [
        200,
        200,
        200,
        200,
        409,
        403,
        403,
        405,
        415,
        403,
    ]
    for name, response in responses.items():
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY, name
        assert response.headers["x-content-type-options"] == "nosniff", name
        assert response.headers["cache-control"] == "no-store", name


async def test_the_page_has_no_inline_script_handler_or_style_and_loads_only_our_files(
    admin: World,
) -> None:
    await _two_admins_disagree(admin)
    await answer(admin, "business_started_on", "state", "2015-03-02")
    (old,) = await rows(admin, "business_started_on")
    pages = {
        "page": (await admin.app.get(PAGE)).text,
        "answered": (
            await answer(admin, "is_agency", "state", "false", holds_from="2020-01-01")
        ).text,
        "corrected": (await correct(admin, old.evidence_id, "2016-01-01")).text,
        "refused": (
            await answer(admin, "is_agency", "state", "maybe", holds_from="2020-01-01")
        ).text,
    }
    for name, text in pages.items():
        for tag, attrs in page_of(text).elements:
            assert tag != "style", name
            assert not [k for k in attrs if k.startswith("on")], (name, tag)
            assert "style" not in attrs, (name, tag)
            for key in ("src", "href", "action"):
                value = attrs.get(key, "")
                assert not re.match(r"(?i)\s*(javascript:|data:|https?:|//)", value), (name, value)
        assert "<script>" not in text, name
    assert sorted(a["src"] for a in page_of(pages["page"]).where("script")) == [
        "/static/admin.js",
        "/static/ask.js",
        "/static/vendor/htmx/htmx.min.js",
    ]
    for name in ("answered", "corrected", "refused"):
        assert not page_of(pages[name]).where("script"), name


async def test_markup_in_an_answers_source_reaches_the_browser_as_text(admin: World) -> None:
    hostile = '<img src=x onerror=alert(1)> & "q" <script>alert(2)</script>'
    response = await answer(
        admin, "is_agency", "state", "false", source=hostile, holds_from="2020-01-01"
    )
    page = (await admin.app.get(PAGE)).text
    for text in (response.text, page):
        assert "<img" not in text and "<script>alert" not in text
        assert "&lt;img src=x onerror=alert(1)&gt;" in text
    (row,) = await rows(admin, "is_agency")
    assert row.source == hostile  # stored as it was said, never as markup


async def test_an_answer_that_echoes_into_a_notice_is_text_too(admin: World) -> None:
    hostile = "<b onclick=x()>"
    response = await answer(admin, hostile, "state", "x")
    assert "<b onclick" not in response.text and "isn't open" in words(response.text)


def test_the_pages_script_is_the_admin_script_and_writes_no_html() -> None:
    script = (STATIC_DIR / "admin.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in script, forbidden


async def test_the_page_is_made_for_a_phone(admin: World) -> None:
    (viewport,) = page_of((await admin.app.get(PAGE)).text).where("meta", name="viewport")
    assert viewport["content"] == "width=device-width, initial-scale=1"
    css = (STATIC_DIR / "app.css").read_text()
    assert not re.search(r"(?<!-)width:\s*\d+(\.\d+)?px", css)


async def test_the_parsed_page_matches_the_subscriptions_panels_reader(admin: World) -> None:
    """The panel's own helper reads this page too: one shell, one set of rules."""
    assert parsed((await admin.app.get(PAGE)).text).where("main")


async def outcomes(world: World) -> dict[str, str]:
    coverage = (await world.app.get("/v1/coverage", params={"as_of": TODAY.isoformat()})).json()
    return {o["obligation_id"]: o["outcome"] for o in coverage["obligations"]}


# ---- an answer Track A can't read is corrected as the question asks it ---------------------


async def test_a_number_recorded_as_text_is_corrected_as_a_number_and_the_obligation_moves(
    admin: World,
) -> None:
    posted = await admin.app.post(
        "/v1/profile/evidence",
        json={
            "field": "annual_turnover_aud",
            "value": "plenty",
            "period": {"financial_year": 2025},
            "source": "a slip",
        },
    )
    assert posted.status_code == 201
    # every other year is under the threshold, so the unreadable year decides
    await _answer_everything_asked(admin, {**PROFILE, "annual_turnover_aud": "1000000"})
    page = await read(admin)
    assert page.slots()[("annual_turnover_aud", "fy2025")] == "unreadable"
    before = await outcomes(admin)
    # the correction input is the number the question asks for, not the text it was given
    (old,) = [e for e in await rows(admin, "annual_turnover_aud") if e.value == "plenty"]
    forms = [f for f in page.forms if f.hidden.get("supersedes") == old.evidence_id]
    assert len(forms) == 2  # on the question, and among the answers on record
    for form in forms:
        value = next(i for i in form.inputs if i.get("name") == "value")
        assert value["inputmode"] == "decimal"
    # and it is validated as one
    refused = await correct(admin, old.evidence_id, "plenty")
    assert "answer with a number" in words(refused.text)
    response = await correct(admin, old.evidence_id, "5,000,000")
    assert response.status_code == 200 and "Corrected:" in words(response.text)
    new = [e for e in await rows(admin, "annual_turnover_aud") if e.supersedes == old.evidence_id]
    assert [(e.value, e.period) for e in new] == [(5_000_000, Period.financial_year(2025))]
    assert isinstance(new[0].value, int)
    after = await outcomes(admin)
    assert (before[APP11], after[APP11]) == ("unassessable", "applicable")
    assert after != before


# ---- one answer to a slot, however many times it is sent ------------------------------------


def _interleaving(ledger: FakeLedgerStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real ledger yields to other requests on every read and write; the
    in-memory one never does. Make it, so two requests really are between
    their read and their write at the same time."""
    read_evidence, write_evidence = ledger.evidence, ledger.record_evidence

    async def evidence(tenant_id: str) -> tuple[Evidence, ...]:
        found = await read_evidence(tenant_id)
        await asyncio.sleep(0)
        return found

    async def record(item: Evidence) -> None:
        await asyncio.sleep(0)
        await write_evidence(item)

    monkeypatch.setattr(ledger, "evidence", evidence)
    monkeypatch.setattr(ledger, "record_evidence", record)


async def test_a_double_submit_records_one_answer_and_tells_the_other_the_slot_has_one(
    admin: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _interleaving(admin.ledger, monkeypatch)
    sent = [
        answer(admin, "legal_form", "state", "trust", source=f"tab {n}", holds_from="2010-01-01")
        for n in (1, 2)
    ]
    first, second = await asyncio.gather(*sent)
    assert first.status_code == second.status_code == 200
    assert len(await rows(admin, "legal_form")) == 1
    texts = sorted(
        [
            "already has an answer" in words(r.text) or "isn't open" in words(r.text)
            for r in (first, second)
        ]
    )
    assert texts == [False, True]
    page = await read(admin)
    assert ("legal_form", "state") not in page.slots()  # settled, not flagged


async def test_many_at_once_still_record_one_answer(
    admin: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _interleaving(admin.ledger, monkeypatch)
    sent = [
        answer(admin, "is_agency", "state", "false", source=f"tab {n}", holds_from="2010-01-01")
        for n in range(6)
    ]
    await asyncio.gather(*sent)
    assert len(await rows(admin, "is_agency")) == 1
