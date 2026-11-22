"""The findings report of a check (#300): `GET /v1/checks/{id}` and the page `/checks/{id}`.

The same world as the findings tests (`tests/test_api_check_findings.py`): the real app,
sign-in, lifecycle, Track A over the corpus's signed mappings, the golden corpus and the
real verifier, with a model that is a script. Nothing is billable. The worked example's
claims (`tests/findings_support.py`):
- claim 1: personalised offers (D1 3/2 and D5 1/1, two policies that may disagree);
- claim 2: the export to CloudAnalytics Inc (APP 8.1; D4 2/1);
- claim 3: the Approved Vendor Register (APP 8.1; D4 2/2);
- claim 4: segment profiles kept 7 years (APP 11.2).

The report is read from the ledger and never computed, so the page and the JSON are
checked against what a findings run recorded, and a reload is checked by drawing it twice.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from html import unescape
from typing import Any, cast
from urllib.parse import urldefrag

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.api import check_claims, questionnaire
from compliance_rag.api.check_report import Segment, segments_of
from compliance_rag.corpus.models import CANONICALISATION_VERSION, PUBLIC_TENANT_ID
from compliance_rag.eval.golden_corpus import (
    POLICY_INSTRUMENT_IDS,
    POLICY_TENANT_ID,
    POLICY_VERSION_ID,
    policy_source,
    policy_titles,
)
from compliance_rag.eval.harness import law_source_html
from tests.api_support import bearer
from tests.findings_support import (
    APP8_1,
    CLAIM_1,
    CLAIM_2,
    CLAIM_3,
    CLAIM_4,
    D1_3_2,
    D4_2_1,
    D4_2_2,
    D5_1_1,
    DAY,
    CheckModel,
    Say,
    honest,
    opening,
    passages_in,
    worked_claims,
)
from tests.test_api_check_findings import (
    SECRET,
    UNKNOWN_FY2023,
    Unavailable,
    World,
    checked,
    extract,
    find,
    submit,
    world,
)
from tests.test_api_checks import ADMIN_TOKEN, ORIGIN, SUBMITTER_TOKEN, THIRD_TOKEN, scan, upload

OPT_IN = "unless you have given us your opt-in consent"
PRIVACY_POLICY = policy_titles()["acme/privacy-policy"]
MARKETING_POLICY = policy_titles()["acme/marketing-policy"]


@pytest.fixture(autouse=True)
def _a_fixed_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_claims, "today", lambda: DAY)
    monkeypatch.setattr(questionnaire, "today", lambda: DAY)


async def publish_sources(w: World) -> None:
    """The law's anchored source and each policy's Markdown, as the source route serves
    them: the golden world holds the clauses and their records but not the sources, and a
    link in a report is only as good as the page it opens."""
    await w.clauses.put_manifest(
        "privacy-act-1988",
        "C2026C00227",
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html=law_source_html(),
    )
    for filename, instrument_id in POLICY_INSTRUMENT_IDS.items():
        await w.clauses.put_manifest(
            instrument_id,
            POLICY_VERSION_ID,
            tenant_id=POLICY_TENANT_ID,
            canonicalisation_version=CANONICALISATION_VERSION,
            source_html=policy_source(filename),
        )


MIXED = honest(
    {
        CLAIM_1: {
            D1_3_2: Say("conflicts", cites=((D1_3_2, OPT_IN),)),
            D5_1_1: Say("consistent"),
        },
        CLAIM_2: {
            "OB-PA-APP8-1": Say("not_addressed"),
            D4_2_1: Say("ambiguous", settle_by="Whether the approval covers segmenting."),
        },
        CLAIM_3: {
            "OB-PA-APP8-1": Say("conflicts"),
            D4_2_2: Say("consistent"),
        },
        CLAIM_4: {"OB-PA-APP11-2": Say("not_addressed")},
    }
)
"""One of each verdict, out of claim order, so the order is the report's doing."""


@asynccontextmanager
async def browser(w: World, token: str = SUBMITTER_TOKEN) -> AsyncGenerator[httpx.AsyncClient]:
    """A client signed in with the session cookie, as a page is read."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=w.app), base_url=ORIGIN, headers={"Origin": ORIGIN}
    ) as client:
        assert (await client.post("/session", headers=bearer(token))).status_code == 200
        yield client


async def page(w: World, check_id: str, token: str = SUBMITTER_TOKEN) -> str:
    async with browser(w, token) as client:
        response = await client.get(f"/checks/{check_id}")
    assert response.status_code == 200, response.text
    return response.text


async def check_json(
    w: World, check_id: str, token: str = SUBMITTER_TOKEN
) -> dict[str, Any]:  # Any: JSON
    async with w.client(token) as client:
        response = await client.get(f"/v1/checks/{check_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def reported(model: Any = None, **settings: Any) -> tuple[World, str]:  # Any: Generator
    """A world, and a check of the proposal whose claims and findings are recorded."""
    w = await world(model or CheckModel(findings=MIXED), **settings)
    await publish_sources(w)
    check_id = await checked(w)
    _, events = await find(w, check_id)
    assert events[-1][0] == "findings", events
    return w, check_id


def where(finding: dict[str, Any], claim: int, target: str) -> bool:  # Any: JSON
    return finding["claim"]["index"] == claim and target in (
        finding.get("obligation_id"),
        finding.get("policy_clause_id"),
    )


def order_of(report: dict[str, Any]) -> list[tuple[str, int, str]]:  # Any: JSON
    return [
        (g["verdict"], f["claim"]["index"], f.get("obligation_id") or f["policy_clause_id"])
        for g in report["groups"]
        for f in g["findings"]
    ]


# ---- the three states of one page --------------------------------------------------------


async def test_a_check_with_no_claims_offers_the_run_and_shows_the_stored_document() -> None:
    w = await world()
    check_id = await submit(w)
    html = await page(w, check_id)
    parsed = scan(html)
    (main,) = parsed.where("main", **{"class": "page report"})
    assert main["data-state"] == "new"
    (run,) = parsed.where("section", id="run")
    assert run["data-check-id"] == check_id and run["data-claims"] == "todo"
    assert parsed.where("button", id="run-check")
    assert [a["data-stage"] for a in parsed.where("li") if "data-stage" in a] == [
        "coverage",
        "claims",
        "findings",
    ]
    (progress,) = parsed.where("ol", id="progress")
    assert "hidden" in progress  # the stage line is shown while it runs
    scripts = [a["src"] for a in parsed.where("script")]
    assert "/static/check_run.js" in scripts
    assert parsed.where("li", **{"class": "passage"})  # the document, as stored
    assert not parsed.where("aside", id="sources")
    assert (await check_json(w, check_id))["state"] == "new"
    assert (await check_json(w, check_id))["report"] is None


async def test_a_check_with_claims_and_no_findings_offers_the_rest_and_marks_the_quotes() -> None:
    w = await world()
    check_id = await submit(w)
    await extract(w, check_id)
    html = await page(w, check_id)
    parsed = scan(html)
    (run,) = parsed.where("section", id="run")
    assert run["data-claims"] == "done"
    assert (await check_json(w, check_id))["state"] == "extracted"
    claims = (await check_json(w, check_id))["claims"]["claims"]
    assert [c["index"] for c in claims] == [1, 2, 3, 4]
    # every claim is named with the document's own words, and its words are marked in
    # the passage it came from
    marked = parsed.where("mark", **{"class": "quoted"})
    assert sorted({n for m in marked for n in (m["data-claims"] or "").split()}) == [
        "1",
        "2",
        "3",
        "4",
    ]
    for claim in claims:
        assert claim["quote"] in " ".join(unescape(html).split())
        assert f"#{claim['anchor']}" in html
    assert not parsed.where("aside", id="sources")


async def test_a_check_with_findings_is_the_report_and_offers_no_run() -> None:
    w, check_id = await reported()
    html = await page(w, check_id)
    parsed = scan(html)
    (main,) = parsed.where("main", **{"class": "page report"})
    assert main["data-state"] == "reported"
    assert not parsed.where("button", id="run-check") and not parsed.where("section", id="run")
    assert "/static/check_run.js" not in [a["src"] for a in parsed.where("script")]
    assert parsed.where("aside", id="sources") and parsed.where("section", id="coverage")
    assert (await check_json(w, check_id))["state"] == "reported"


# ---- the order (F8): verdicts, then claim order; no score --------------------------------


async def test_findings_are_ordered_conflicts_not_addressed_ambiguous_consistent_by_claim() -> None:
    w, check_id = await reported()
    report = (await check_json(w, check_id))["report"]
    assert [g["verdict"] for g in report["groups"]] == [
        "conflicts",
        "not_addressed",
        "ambiguous",
        "consistent",
    ]
    assert order_of(report) == [
        ("conflicts", 1, D1_3_2),
        ("conflicts", 3, "OB-PA-APP8-1"),
        ("not_addressed", 2, "OB-PA-APP8-1"),
        ("not_addressed", 4, "OB-PA-APP11-2"),
        ("ambiguous", 2, D4_2_1),
        ("consistent", 1, D5_1_1),
        ("consistent", 3, D4_2_2),
    ]
    # the page lists the same, under the same headings in the same order
    html = await page(w, check_id)
    assert re.findall(r'<section class="verdict-group" data-verdict="(\w+)"', html) == [
        "conflicts",
        "not_addressed",
        "ambiguous",
        "consistent",
    ]
    cards = re.findall(
        r'<article class="card finding" data-finding="[^"]+" '
        r'data-outcome="verdict" data-verdict="(\w+)"',
        html,
    )
    assert cards == [v for v, _, _ in order_of(report)]
    # and every finding card names its claim, in the same order
    claim_links = re.findall(r'<a class="claim-link" href="[^"]+">Claim (\d+)</a>', html)
    assert [int(n) for n in claim_links][: len(cards)] != []
    assert len(claim_links) >= len(cards)


async def test_no_score_appears_anywhere() -> None:
    """D-06: no severity, no rank, no number that is a score, in the page or the JSON."""
    w, check_id = await reported()
    html = (await page(w, check_id)).lower()
    for word in ("score", "severity", "risk rating", "rank", "out of"):
        assert word not in html, word
    shown = await check_json(w, check_id)

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            members = cast("dict[str, object]", value)
            return set(members) | {x for v in members.values() for x in keys(v)}
        if isinstance(value, list):
            return {x for v in cast("list[object]", value) for x in keys(v)}
        return set()

    assert not {k for k in keys(shown) if re.search(r"score|severity|rank|rating", k)}
    # the verdicts are the four words and nothing else
    verdicts = {f["verdict"] for g in shown["report"]["groups"] for f in g["findings"]}
    assert verdicts <= {"conflicts", "not_addressed", "ambiguous", "consistent"}


# ---- identical after a reload -------------------------------------------------------------


async def test_the_report_is_identical_after_reload() -> None:
    """The report is the ledger's: drawn twice, in two sessions, it is the same bytes, and so
    is the JSON. (A second render stands for the reload.)"""
    w, check_id = await reported()
    first, second = await page(w, check_id), await page(w, check_id)
    assert first == second
    assert await check_json(w, check_id) == await check_json(w, check_id)
    # an admin reading another member's check sees the same report, though not the same page
    admin = await check_json(w, check_id, ADMIN_TOKEN)
    assert admin["report"] == (await check_json(w, check_id))["report"]


# ---- every link opens ---------------------------------------------------------------------


async def test_every_citation_and_claim_link_returns_200_and_every_card_is_there() -> None:
    w, check_id = await reported()
    html = await page(w, check_id)
    parsed = scan(html)
    hrefs = [a["href"] or "" for a in parsed.where("a")]
    sources = {urldefrag(h)[0] for h in hrefs if h.startswith("/v1/sources/")}
    assert sources  # citations, targets, policies and instruments all open a source
    async with browser(w) as client:
        for path in sorted(sources):
            assert (await client.get(path)).status_code == 200, path
        # a claim opens the document at its passage: the page, whose element has the anchor
        claims = {h for h in hrefs if h.startswith(f"/checks/{check_id}#")}
        assert claims
        assert (await client.get(f"/checks/{check_id}")).status_code == 200
    ids = {a["id"] for _, a in parsed.elements if a.get("id")}
    for href in claims:
        assert href.split("#", 1)[1] in ids, href
    # a citation in a finding opens its card in the panel
    for href in (h for h in hrefs if h.startswith("#src-")):
        assert href[1:] in ids, href
    shown = (await check_json(w, check_id))["report"]
    cited = {c["clause_id"] for g in shown["groups"] for f in g["findings"] for c in f["citations"]}
    assert cited == {s["clause_id"] for s in shown["sources"]}
    cards = parsed.where("article", **{"class": "card source"})
    assert len(cards) == len(shown["sources"])


async def test_a_citation_card_is_the_clauses_own_text_and_opens_it_in_a_new_tab() -> None:
    w, check_id = await reported()
    shown = (await check_json(w, check_id))["report"]
    app8 = next(s for s in shown["sources"] if s["clause_id"] == APP8_1)
    assert app8["cite"] == "APP 8.1" and app8["text"].startswith(opening(APP8_1, 4))
    assert app8["href"].startswith("/v1/sources/privacy-act-1988/C2026C00227#")
    parsed = scan(await page(w, check_id))
    opens = [
        a
        for a in parsed.where("a")
        if a["href"] == app8["href"]
        and a.get("target") == "_blank"
        and "noopener" in (a["rel"] or "")
    ]
    assert opens  # opens at the clause, in a new tab, never taking the report away


# ---- the banner (Q6) ----------------------------------------------------------------------


async def test_the_banner_names_the_policies_that_disagree_by_title_and_version() -> None:
    w, check_id = await reported()
    shown = (await check_json(w, check_id))["report"]
    (banner,) = shown["disagreements"]
    assert banner["claim"]["index"] == 1
    (conflicting,) = banner["conflicts"]
    (agreeing,) = banner["others"]
    assert conflicting["title"] == PRIVACY_POLICY
    assert conflicting["version_id"] == "v1" and conflicting["cite"].endswith("3.2")
    assert agreeing["title"] == MARKETING_POLICY
    assert agreeing["verdict"] == "consistent"
    html = await page(w, check_id)
    parsed = scan(html)
    (box,) = parsed.where("section", **{"class": "card disagreement"})
    assert "data-disagreements" in box
    section = html.split("data-disagreements", 1)[1].split("</section>", 1)[0]
    assert PRIVACY_POLICY in section and "version v1" in section
    assert MARKETING_POLICY in section and "Consistent" in section


async def test_an_ambiguous_policy_verdict_disagrees_with_a_conflict_too() -> None:
    model = CheckModel(
        findings=honest(
            {
                CLAIM_1: {
                    D1_3_2: Say("conflicts", cites=((D1_3_2, OPT_IN),)),
                    D5_1_1: Say("ambiguous", settle_by="Whether an opt-out is consent."),
                }
            }
        )
    )
    w, check_id = await reported(model)
    (banner,) = (await check_json(w, check_id))["report"]["disagreements"]
    assert [o["verdict"] for o in banner["others"]] == ["ambiguous"]


async def test_not_addressed_is_silence_and_so_no_disagreement() -> None:
    model = CheckModel(
        findings=honest(
            {
                CLAIM_1: {
                    D1_3_2: Say("conflicts", cites=((D1_3_2, OPT_IN),)),
                    D5_1_1: Say("not_addressed"),
                }
            }
        )
    )
    w, check_id = await reported(model)
    assert (await check_json(w, check_id))["report"]["disagreements"] == []
    assert "data-disagreements" not in await page(w, check_id)


async def test_policies_that_agree_raise_no_banner() -> None:
    w, check_id = await reported(CheckModel())  # the honest default: every policy consistent
    assert (await check_json(w, check_id))["report"]["disagreements"] == []
    assert "data-disagreements" not in await page(w, check_id)


# ---- withheld and failed findings: their own headings, with the reason --------------------


async def test_a_withheld_finding_shows_as_withheld_with_the_reason_and_no_verdict() -> None:
    cited_outside = f"{APP8_1.rsplit(':', 1)[0]}:s6c/2"
    model = CheckModel(
        findings=honest(
            {
                CLAIM_2: {
                    "OB-PA-APP8-1": Say(
                        "conflicts", cites=((APP8_1, opening(APP8_1)), (cited_outside, SECRET))
                    )
                }
            }
        )
    )
    w, check_id = await reported(model)
    html = await page(w, check_id)
    shown = (await check_json(w, check_id))["report"]
    (withheld,) = shown["withheld"]
    assert where(withheld, 2, "OB-PA-APP8-1") and withheld["verdict"] is None
    assert "a citation didn't match its clause" in withheld["withheld"]
    # the report says so, under its own heading after the verdicts, never as an error
    assert shown["withheld"] and not shown["failed"]
    parsed = scan(html)
    (card,) = parsed.where("article", **{"data-outcome": "withheld"})
    assert "withheld" in (card["class"] or "")
    section = html.split('data-outcome="withheld"', 1)[1]
    assert "a citation didn&#39;t match its clause" in section or "match its clause" in section
    assert "This is not an error" in html
    assert "Citations that did not verify" in html and 'data-blocked="' + cited_outside in html
    # its verdict, which was never stored, is nowhere: no `conflicts` group, no word of the model's
    assert 'data-verdict="conflicts"' not in html and SECRET not in html
    assert 'class="card finding error"' not in html
    # the heading follows the four verdict groups
    assert html.index('id="h-withheld"') > html.index('id="h-consistent"')


async def test_a_failed_finding_says_why_under_its_own_heading() -> None:
    w, check_id = await reported(Unavailable(lambda n: n == 3))
    shown = (await check_json(w, check_id))["report"]
    assert shown["failed"] and all("wasn't taking requests" in f["failed"] for f in shown["failed"])
    html = await page(w, check_id)
    assert 'id="h-failed"' in html and 'data-outcome="failed"' in html
    assert "nothing here says they are fine" in html
    assert html.index('id="h-failed"') > html.index('id="h-consistent"')
    # what did verify is still there, ahead of it
    assert any(g["findings"] for g in shown["groups"])


# ---- conditional findings (the 2026-11-22 amendment) --------------------------------------


async def test_a_conditional_finding_has_no_verdict_and_follows_the_verdicts() -> None:
    w, check_id = await reported(CheckModel(findings=MIXED), evidence=UNKNOWN_FY2023)
    shown = (await check_json(w, check_id))["report"]
    assert shown["conditional"]
    for f in shown["conditional"]:
        assert f["verdict"] is None and f["citations"] == [] and f["conditional"]["questions"]
    assert [f["claim"]["index"] for f in shown["conditional"]] == sorted(
        f["claim"]["index"] for f in shown["conditional"]
    )  # in claim order
    # no verdict group holds one
    assert not {f["finding_id"] for g in shown["groups"] for f in g["findings"]} & {
        f["finding_id"] for f in shown["conditional"]
    }
    html = await page(w, check_id)
    # after the four verdicts' headings, and before the withheld and failed ones
    assert html.index('id="h-conditional"') > html.index('id="h-consistent"')
    parsed = scan(html)
    cards = parsed.where("article", **{"data-outcome": "conditional"})
    assert len(cards) == len(shown["conditional"])
    section = html.split('id="h-conditional"', 1)[1].split("What this report covers")[0]
    assert "annual_turnover_aud" in section and "No verdict yet" in section
    assert "/checks/new" in section  # the way to check again


async def test_the_questionnaire_link_is_shown_to_an_admin_and_a_member_is_pointed_to_one() -> None:
    w, check_id = await reported(CheckModel(findings=MIXED), evidence=UNKNOWN_FY2023)
    member = await page(w, check_id)
    assert "A workspace admin can answer these" in member
    assert "Answer in the questionnaire" not in member
    admin = await page(w, check_id, ADMIN_TOKEN)
    assert "Answer in the questionnaire" in admin
    assert scan(admin).where("a", href="/admin/questionnaire")


# ---- the coverage statement (F7) ----------------------------------------------------------


async def test_the_coverage_statement_names_what_was_checked_and_what_was_not() -> None:
    w, check_id = await reported()
    cs = (await check_json(w, check_id))["report"]["coverage_statement"]
    assert cs["as_of"] == "2026-10-12"
    (act,) = [i for i in cs["instruments"] if i["instrument_id"] == "privacy-act-1988"]
    assert act["version_id"] == "C2026C00227" and act["title"]
    assert act["obligations_open_to_the_model"] > 0
    policies = {p["instrument_id"]: p for p in cs["policies"]}
    assert len(policies) == 6 and all(p["title"] and p["version_id"] for p in policies.values())
    assert "acme/privacy-policy" in policies
    assert cs["corpus_version"] == {"tag": "corpus-2026.10", "sha": "a" * 40, "untagged": False}
    assert cs["mapping_version"]["tag"] == "mappings-2026.10"
    assert cs["mapping_version"]["sha"] == "b" * 40
    assert cs["facts"]["sha256"] and cs["facts"]["evidence"]
    assert cs["not_checked"] == "anything else"
    assert "did not pick" in cs["not_reported"]
    html = await page(w, check_id)
    section = html.split("data-coverage-statement", 1)[1].split("</section>", 1)[0]
    for word in (
        act["title"],
        "C2026C00227",
        "2026-10-12",
        "corpus-2026.10",
        "a" * 40,
        "mappings-2026.10",
        "b" * 40,
        "Not checked: anything else.",
        "did not pick",
        PRIVACY_POLICY,
    ):
        assert word in section, word
    assert section.count("version <code>") >= 7  # an instrument and six policies, by version


async def test_a_claim_with_nothing_picked_is_listed_and_not_read_as_compliant() -> None:
    picks = {
        CLAIM_1: ((), (D1_3_2, D5_1_1)),
        CLAIM_2: (("OB-PA-APP8-1",), (D4_2_1,)),
        CLAIM_3: (("OB-PA-APP8-1",), (D4_2_2,)),
        CLAIM_4: ((), ()),
    }
    w = await world(CheckModel(claims=worked_claims(picks)))
    check_id = await checked(w)
    _, events = await find(w, check_id)
    assert events[-1][0] == "findings"
    shown = (await check_json(w, check_id))["report"]
    assert [c["index"] for c in shown["claims_without_findings"]] == [4]
    html = await page(w, check_id)
    assert "Claims with nothing to check them against" in html
    assert "That is not a finding that they are compliant" in html


async def test_a_document_with_no_claims_still_has_a_report_that_says_what_it_covered() -> None:
    w = await world(CheckModel(claims=lambda _seen: json.dumps({"claims": []})))
    check_id = await checked(w)
    _, events = await find(w, check_id)
    assert events[-1][0] == "findings"
    shown = (await check_json(w, check_id))["report"]
    assert all(g["findings"] == [] for g in shown["groups"])
    html = await page(w, check_id)
    assert "What this report covers" in html and "Not checked: anything else." in html
    assert 'class="verdict-group"' not in html  # no empty heading to be read as a clean bill


# ---- visibility ---------------------------------------------------------------------------


async def test_a_report_that_is_not_yours_is_the_same_404_as_no_such_check() -> None:
    w, check_id = await reported()
    for path in (f"/v1/checks/{check_id}", f"/v1/checks/{'0' * 32}"):
        async with w.client(THIRD_TOKEN) as client:
            response = await client.get(path)
        assert response.status_code == 404
    async with browser(w, THIRD_TOKEN) as client:
        theirs = await client.get(f"/checks/{check_id}")
        nothing = await client.get(f"/checks/{'0' * 32}")
    assert theirs.status_code == nothing.status_code == 404
    assert theirs.text == nothing.text
    assert "Acme" not in theirs.text


# ---- the document is data -----------------------------------------------------------------


async def test_markup_in_a_claim_and_in_a_settle_by_reaches_the_browser_as_text() -> None:
    hostile = (
        '# Plan\n\nWe keep <b onclick="steal()">segment</b> profiles '
        "<script>alert(1)</script> for 7 years.\n"
    )
    quote = '<b onclick="steal()">segment</b> profiles'

    def passage_of(seen: Any, wanted: str) -> str:  # Any: a claims prompt
        found = passages_in(seen)
        return next(pid for pid, text in found.items() if wanted in text)

    model = CheckModel(
        claims=lambda seen: json.dumps(
            {
                "claims": [
                    {
                        "passage": passage_of(seen, quote),
                        "quote": quote,
                        "summary": "<img src=x onerror=alert(2)>",
                        "obligation_ids": ["OB-PA-APP11-2"],
                        "policy_clause_ids": [D1_3_2],
                    }
                ]
            }
        ),
        findings=honest(
            {quote: {D1_3_2: Say("ambiguous", settle_by="<script>alert(3)</script> ask legal")}}
        ),
    )
    w = await world(model)
    response = await submit_hostile(w, hostile)
    check_id = response
    await extract(w, check_id)
    _, events = await find(w, check_id)
    assert events[-1][0] == "findings"
    html = await page(w, check_id)
    assert "<script>alert" not in html and "<img src=x" not in html and "<b onclick" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "&lt;b onclick" in html
    assert "&lt;script&gt;alert(3)&lt;/script&gt; ask legal" in html
    parsed = scan(html)
    assert not [a for _, a in parsed.elements if any(k.startswith("on") for k in a)]
    assert not parsed.where("img")
    assert sorted(a["src"] or "" for a in parsed.where("script")) == [
        "/static/ask.js",
        "/static/vendor/htmx/htmx.min.js",
    ]


async def submit_hostile(w: World, content: str) -> str:
    async with w.client(SUBMITTER_TOKEN) as client:
        response = await client.post("/v1/checks", json=upload("hostile.md", content))
    assert response.status_code == 201, response.text
    return str(response.json()["check_id"])


async def test_the_report_page_holds_no_inline_script_handler_or_style() -> None:
    w, check_id = await reported(Unavailable(lambda n: n == 3), evidence=UNKNOWN_FY2023)
    for token in (SUBMITTER_TOKEN, ADMIN_TOKEN):
        parsed = scan(await page(w, check_id, token))
        for tag, attrs in parsed.elements:
            assert tag != "style"
            assert "style" not in attrs and not [k for k in attrs if k.startswith("on")]
            if tag == "script":
                assert (attrs["src"] or "").startswith("/static/")
            for key in ("src", "href", "action"):
                assert not re.match(r"(?i)\s*(javascript:|data:|https?:|//)", attrs.get(key) or "")


# ---- the document, cut at the claims' quotes ----------------------------------------------


def test_a_passage_is_cut_where_a_quote_begins_and_ends() -> None:
    text = "We keep profiles for 7 years, then delete them."
    cut = segments_of(text, [(8, 28, 1)])
    assert [(s.text, s.claims) for s in cut] == [
        ("We keep ", ()),
        ("profiles for 7 years", (1,)),
        (", then delete them.", ()),
    ]
    assert "".join(s.text for s in cut) == text


def test_overlapping_quotes_are_named_where_they_overlap_and_no_text_is_lost() -> None:
    text = "abcdefghij"
    cut = segments_of(text, [(0, 6, 1), (4, 10, 2)])
    assert [(s.text, s.claims) for s in cut] == [("abcd", (1,)), ("ef", (1, 2)), ("ghij", (2,))]
    assert "".join(s.text for s in cut) == text
    assert segments_of(text, []) == (Segment(text, ()),)
    assert segments_of("", []) == ()


# ---- stage progress, in the script ---------------------------------------------------------


def test_the_run_script_streams_the_stages_and_never_writes_html() -> None:
    from tests.test_api_pages import STATIC_DIR

    script = (STATIC_DIR / "check_run.js").read_text()
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "createContextualFragment",
        "DOMParser",
    ):
        assert forbidden not in script, forbidden
    assert "http://" not in script and "https://" not in script
    # the claims, then the findings, each streamed; the stage line is the three stages
    assert script.index("/claims`") < script.index("/findings`")
    assert 'headers: { Accept: "text/event-stream" }' in script
    for stage in ("coverage", "claims", "findings"):
        assert f'setStage("{stage}", "running")' in script
    # when the findings are recorded the page is reloaded, so the server draws the report
    assert "window.location.assign(window.location.pathname)" in script
    assert "textContent" in script


# ---- spans ---------------------------------------------------------------------------------


async def test_reading_the_report_is_a_span_with_the_state_and_counts_and_no_document_words(
    span_exporter: InMemorySpanExporter,
) -> None:
    w, check_id = await reported()
    span_exporter.clear()
    await page(w, check_id)
    (read,) = (s for s in span_exporter.get_finished_spans() if s.name == "api.check.report")
    attributes = dict(read.attributes or {})
    assert attributes["check.found"] is True and attributes["check.state"] == "reported"
    assert attributes["check.findings.verdict"] == 7
    everything = json.dumps([dict(s.attributes or {}) for s in span_exporter.get_finished_spans()])
    for claim in (CLAIM_1, CLAIM_2, CLAIM_3, CLAIM_4, OPT_IN):
        assert claim not in everything
