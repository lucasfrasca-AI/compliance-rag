"""The admin's policy versions (issue #303, ADR-0053 U4, U5, U6 and I5, ADR-0021).

`/admin/policies` and `/v1/admin/policies` over fakes, end to end through `create_app` and the
real `CorpusLifecycle`, the real policy parser and the checked-in synthetic registry. An admin
uploads a policy or a new version of one, withdraws a version, and purges it behind a typed
count.

What is checked, in the acceptance criteria's order:

- a title never matches a version: a document whose title equals an existing policy's is a new
  policy, a version lands in the policy the route names whatever its title says, and a title in
  the path finds nothing;
- a past effective date without a reason is refused, and with one it is recorded;
- #78's no-synthetic-field test, extended to this route: a `synthetic` field is a 422, the model
  and the lifecycle's publish have no such parameter, and offshore only a registered fixture's
  bytes are published, by an admin who is a registered tester;
- a purge is refused unless the typed count matches the findings-citing count, and nothing is
  deleted;
- every outcome writes an audit row, with the request's trace.

Then the rules every admin page keeps: admins only on every route, the sidebar link, no inline
script, handler or style, markup in a title reaching the browser as text.
"""

from __future__ import annotations

import datetime as dt
import html
import inspect
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

import httpx
import pytest

from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import (
    DocumentClass,
    Profile,
    RemovalAction,
    RemovalAuditEntry,
    RemovalOutcome,
)
from compliance_rag.api.app import create_app
from compliance_rag.api.deps import today
from compliance_rag.api.policy_upload import PolicyUpload, mint_policy_id
from compliance_rag.api.sign_in import CONTENT_SECURITY_POLICY, STATIC_DIR
from compliance_rag.api.uploads import MAX_DOCUMENT_BYTES, MAX_REQUEST_BYTES
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.models import Scope, parse_stable_id, validate_instrument_id
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.golden_corpus import (
    FIXTURES,
    POLICY_INSTRUMENT_IDS,
    front_matter_title,
)
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION, Settings
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import (
    MEMBER,
    SECOND,
    TENANT,
    api_settings,
    assert_script_writes_no_html,
    bearer,
    fake_api_services,
    mint,
    verifier_for,
)
from tests.conftest import ALL_LAW

ORIGIN = "https://api"
SECRET = "s3cr3t-" + "k" * 40
POLICIES = FIXTURES / "synthetic-policies"
PRIVACY = POLICIES / "acme-privacy-policy.md"
VENDOR = POLICIES / "acme-vendor-cross-border-transfer-policy.md"
EVERY_POLICY = tuple(POLICIES / name for name in sorted(POLICY_INSTRUMENT_IDS))
OFFSHORE = {"gemini_region": BUILD_PHASE_OFFSHORE_REGION}
ADMIN_TOKEN = mint()
MEMBER_TOKEN = mint(sub="uid-second", email=SECOND)
THE_TEXT = """# A tiny policy

## 1. Retention

1.1 We keep payroll records for seven years and then delete them.

1.2 We keep job applications for twelve months and then delete them.

## 2. Access

2.1 Only the people team can read payroll records.
"""
"""A policy no registry vouches for: real customer data, as the gate counts it."""
TODAY = today()
YESTERDAY = TODAY - dt.timedelta(days=1)
TOMORROW = TODAY + dt.timedelta(days=1)
NEXT_MONTH = TODAY + dt.timedelta(days=30)
LATER = TODAY + dt.timedelta(days=60)
PAGE = "/admin/policies"
API = "/v1/admin/policies"


def past(days: int) -> dict[str, str]:
    """An effective date `days` ago, with the reason a past date needs: so a later version, which
    must take effect after it, can take effect on the days since."""
    return {
        "effective_from": (TODAY - dt.timedelta(days=days)).isoformat(),
        "backdate_reason": "the earlier version of this test's policy",
    }


def _settings(**overrides: Any) -> Settings:  # Any: settings field values
    return api_settings(
        api_members=f"{MEMBER}, {SECOND}",
        session_secret=SECRET,
        firebase_web_api_key="AIza-web-key-not-a-secret",
        **overrides,
    )


@dataclass
class World:
    ledger: FakeLedgerStore
    clauses: FakeClauseStore
    retrieval: FakeRetrievalStore
    app: Any  # Any: the FastAPI app, only handed to a transport

    def client(self, token: str | None = None) -> httpx.AsyncClient:
        """A client of the app; with a token, every request carries it as a Bearer header."""
        headers = {"Origin": ORIGIN, **(bearer(token) if token else {})}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN, headers=headers
        )

    async def browser(self, token: str = ADMIN_TOKEN) -> httpx.AsyncClient:
        """A client signed in with the session cookie, as a page is read."""
        client = self.client()
        response = await client.post("/session", headers=bearer(token))
        assert response.status_code == 200
        return client

    def audit(self) -> list[RemovalAuditEntry]:
        return list(self.ledger._audit)  # pyright: ignore[reportPrivateUsage]

    def stores(self) -> tuple[int, int, int]:
        """What is written to the three stores: versions, clause objects, chunks."""
        return (
            len(self.ledger._versions),  # pyright: ignore[reportPrivateUsage]
            len(self.clauses._clauses),  # pyright: ignore[reportPrivateUsage]
            len(self.retrieval._chunks),  # pyright: ignore[reportPrivateUsage]
        )

    def assert_nothing_written(self) -> None:
        assert self.stores() == (0, 0, 0)


async def world(**settings: Any) -> World:  # Any: settings field values
    configured = _settings(**settings)
    ledger = FakeLedgerStore()
    await ledger.put_profile(Profile(TENANT))
    clauses = FakeClauseStore()
    retrieval = FakeRetrievalStore(ledger=ledger)
    sign_in: dict[str, Any] = {"api_members": f"{MEMBER}, {SECOND}", **settings}  # Any: values
    app = create_app(
        fake_api_services(
            settings=configured,
            # The real sign-in, over the same members the settings name.
            verifier=verifier_for(**sign_in),
            ledger=ledger,
            clauses=clauses,
            pipeline=AssessmentPipeline(
                RetrievalPipeline(retrieval, FakeReranker()),
                FakeGenerator(),
                Verifier(clauses, ledger, CanonicalCitationVerifier()),
                ledger,
                tenant_text=TenantTextGate.from_settings(configured),
            ),
            lifecycle=CorpusLifecycle(
                ledger=ledger,
                retrieval=retrieval,
                clauses=clauses,
                generation_region=configured.generation_region,
                synthetic_registry=SyntheticRegistry.load(),
            ),
        )
    )
    return World(ledger, clauses, retrieval, app)


@pytest.fixture
async def sydney() -> AsyncIterator[World]:
    yield await world()


@pytest.fixture
async def offshore() -> AsyncIterator[World]:
    """Generation outside Australia, and the admin the one registered build-phase tester."""
    yield await world(**OFFSHORE, offshore_generation_testers=MEMBER)


def body(path_or_text: Any, **more: Any) -> dict[str, Any]:  # Any: a Path, a str; JSON
    """An upload of a fixture's file, or of text, titled as its front matter says."""
    if isinstance(path_or_text, str):
        return {"filename": "policy.md", "content": path_or_text, "title": "A tiny policy", **more}
    source = path_or_text.read_text(encoding="utf-8")
    return {
        "filename": path_or_text.name,
        "content": source,
        "title": front_matter_title(source),
        **more,
    }


async def post(
    w: World,
    path: str,
    payload: Any,
    token: str = ADMIN_TOKEN,  # Any: JSON
) -> httpx.Response:
    async with w.client(token) as client:
        return await client.post(path, json=payload)


async def new_policy(w: World, payload: Any, token: str = ADMIN_TOKEN) -> httpx.Response:  # Any
    return await post(w, API, payload, token)


async def new_version(
    w: World,
    policy: str,
    payload: Any,
    token: str = ADMIN_TOKEN,  # Any: JSON
) -> httpx.Response:
    return await post(w, f"{API}/{policy}/versions", payload, token)


async def published(w: World, payload: Any = None, **more: Any) -> dict[str, Any]:  # Any: JSON
    response = await new_policy(w, payload or body(PRIVACY, **more))
    assert response.status_code == 201, response.text
    made: dict[str, Any] = response.json()
    return made


async def listing(w: World) -> dict[str, Any]:  # Any: JSON
    async with w.client(ADMIN_TOKEN) as client:
        response = await client.get(API)
    assert response.status_code == 200, response.text
    return {p["policy"]: p for p in response.json()["policies"]}


def refusal_of(response: httpx.Response) -> dict[str, Any]:  # Any: JSON
    detail: dict[str, Any] = response.json()["detail"]
    return detail


def outcomes(w: World, action: RemovalAction) -> list[RemovalOutcome]:
    return [e.outcome for e in w.audit() if e.action is action]


# ---- an admin uploads a policy: new, and new versions of it ------


async def test_a_new_policy_is_published_through_the_parser_and_the_lifecycle(
    sydney: World,
) -> None:
    made = await published(sydney)
    assert made["version_id"] == "v1" and made["supersedes"] is None
    assert made["effective_from"] == TODAY.isoformat() and made["backdate_reason"] is None
    assert made["instrument_id"] == f"{TENANT}/{made['policy']}"
    assert made["clauses"] == len(sydney.clauses._clauses)  # pyright: ignore[reportPrivateUsage]
    instrument = made["instrument_id"]
    # the lifecycle wrote all three stores: clause store, retrieval, ledger
    assert await sydney.clauses.get_source_html(instrument, "v1", tenant_id=TENANT) is not None
    chunks = await sydney.retrieval.version_chunk_ids(
        instrument_id=instrument, tenant_id=TENANT, version_id="v1"
    )
    assert len(chunks) == made["clauses"]
    assert (
        await sydney.ledger.document_class(tenant_id=TENANT, instrument_id=instrument)
        is DocumentClass.INTERNAL_POLICY
    )
    in_force = await sydney.ledger.policy_versions(TENANT, as_of=TODAY)
    assert [(v.instrument_id, v.version_id) for v in in_force] == [(instrument, "v1")]
    # listed, with the title as recorded
    policy = (await listing(sydney))[made["policy"]]
    assert policy["title"] == front_matter_title(PRIVACY.read_text("utf-8"))
    assert [(v["version_id"], v["status"]) for v in policy["versions"]] == [("v1", "in_force")]


@pytest.mark.parametrize("path", EVERY_POLICY, ids=lambda p: p.name)
async def test_every_policy_fixture_is_published_and_its_clauses_read_back(
    sydney: World, path: Any
) -> None:
    """Every available fixture, not a representative one (CLAUDE.md)."""
    made = await published(sydney, body(path))
    instrument = made["instrument_id"]
    manifest = await sydney.clauses.get_manifest(instrument, "v1", tenant_id=TENANT)
    assert len(manifest) == made["clauses"] > 0
    # the stored source is the uploaded file, as the lifecycle stores a publish's source
    stored = await sydney.clauses.get_source_html(instrument, "v1", tenant_id=TENANT)
    assert stored == path.read_text(encoding="utf-8")


async def test_a_new_version_supersedes_the_policys_version_from_today(sydney: World) -> None:
    first = await published(sydney, body(PRIVACY, **past(10)))
    response = await new_version(sydney, first["policy"], body(PRIVACY))
    assert response.status_code == 201, response.text
    second = response.json()
    assert (second["instrument_id"], second["version_id"]) == (first["instrument_id"], "v2")
    assert second["supersedes"] == "v1"
    versions = (await listing(sydney))[first["policy"]]["versions"]
    assert [(v["version_id"], v["status"]) for v in versions] == [
        ("v1", "superseded"),
        ("v2", "in_force"),
    ]
    assert versions[0]["effective_to"] == TODAY.isoformat()
    in_force = await sydney.ledger.policy_versions(TENANT, as_of=TODAY)
    assert [(v.version_id) for v in in_force] == ["v2"]
    # the old version stays where it is: its text and chunks, so an earlier citation verifies
    old = await sydney.retrieval.version_chunk_ids(
        instrument_id=first["instrument_id"], tenant_id=TENANT, version_id="v1"
    )
    assert old


async def test_a_future_date_leaves_the_old_version_in_force_until_then(sydney: World) -> None:
    first = await published(sydney)
    response = await new_version(
        sydney, first["policy"], body(VENDOR, effective_from=NEXT_MONTH.isoformat())
    )
    assert response.status_code == 201, response.text
    versions = (await listing(sydney))[first["policy"]]["versions"]
    assert [(v["version_id"], v["status"]) for v in versions] == [
        ("v1", "in_force"),
        ("v2", "future"),
    ]
    assert versions[0]["effective_to"] == NEXT_MONTH.isoformat()
    assert [v.version_id for v in await sydney.ledger.policy_versions(TENANT, as_of=TODAY)] == [
        "v1"
    ]
    assert [
        v.version_id for v in await sydney.ledger.policy_versions(TENANT, as_of=NEXT_MONTH)
    ] == ["v2"]


async def test_a_version_cannot_take_effect_before_the_one_it_replaces(sydney: World) -> None:
    first = await published(sydney)
    future = await new_version(
        sydney, first["policy"], body(PRIVACY, effective_from=NEXT_MONTH.isoformat())
    )
    assert future.status_code == 201
    before = sydney.stores()
    for day in (TOMORROW, NEXT_MONTH):  # before, and the same day as, v2's
        response = await new_version(
            sydney, first["policy"], body(PRIVACY, effective_from=day.isoformat())
        )
        assert response.status_code == 422, day
        assert refusal_of(response)["error"] == "effective_not_after_current"
    assert sydney.stores() == before
    # after it is fine, and replaces v2, which was the open-ended one
    later = await new_version(
        sydney, first["policy"], body(PRIVACY, effective_from=LATER.isoformat())
    )
    assert later.status_code == 201 and later.json()["supersedes"] == "v2"


async def test_the_next_version_id_follows_the_policys_own_versions(sydney: World) -> None:
    a = await published(sydney, body(PRIVACY, **past(20)))
    b = await published(sydney, body(VENDOR, **past(5)))
    for days in (10, 0):  # two versions after the first, the last of them today
        extra = past(days) if days else {}
        response = await new_version(sydney, a["policy"], body(PRIVACY, **extra))
        assert response.status_code == 201, response.text
    assert [v["version_id"] for v in (await listing(sydney))[a["policy"]]["versions"]] == [
        "v1",
        "v2",
        "v3",
    ]
    assert (await new_version(sydney, b["policy"], body(VENDOR))).json()["version_id"] == "v2"


# ---- a title never matches a version ------


async def test_a_title_never_matches_a_version(sydney: World) -> None:
    """Acceptance: an upload whose title equals an existing policy's title, as a new policy,
    is a second policy. It is not a version of the first, and it changes nothing of it."""
    first = await published(sydney)
    title = front_matter_title(PRIVACY.read_text("utf-8"))
    second_response = await new_policy(sydney, body(PRIVACY, title=title))
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    assert second["instrument_id"] != first["instrument_id"]
    assert second["version_id"] == "v1" and second["supersedes"] is None
    policies = await listing(sydney)
    assert set(policies) == {first["policy"], second["policy"]}
    assert policies[first["policy"]]["title"] == policies[second["policy"]]["title"] == title
    for made in (first, second):
        versions = policies[made["policy"]]["versions"]
        assert [(v["version_id"], v["status"]) for v in versions] == [("v1", "in_force")]
    # both are in force at once: the same words, two policies
    in_force = await sydney.ledger.policy_versions(TENANT, as_of=TODAY)
    assert {v.instrument_id for v in in_force} == {first["instrument_id"], second["instrument_id"]}
    assert not any(e.outcome is RemovalOutcome.REFUSED for e in sydney.audit())


async def test_a_new_policys_id_is_never_made_from_its_title(sydney: World) -> None:
    """Were it made from the title, two uploads of one title would meet at one instrument and
    the second would be a version of the first."""
    ids = {
        (await published(sydney, body(THE_TEXT, title="Same Title")))["instrument_id"]
        for _ in range(3)
    }
    assert len(ids) == 3
    assert all(re.fullmatch(rf"{TENANT}/policy-[0-9a-f]{{12}}", i) for i in ids)


async def test_a_version_goes_to_the_policy_the_route_names_whatever_its_title_says(
    sydney: World,
) -> None:
    a = await published(sydney, body(PRIVACY, **past(5)))
    b = await published(sydney, body(VENDOR))
    b_title = (await listing(sydney))[b["policy"]]["title"]
    # a version of A, titled as B is: it is A's, and B is untouched
    response = await new_version(sydney, a["policy"], body(PRIVACY, title=b_title))
    assert response.status_code == 201
    assert response.json()["instrument_id"] == a["instrument_id"]
    policies = await listing(sydney)
    assert [v["version_id"] for v in policies[a["policy"]]["versions"]] == ["v1", "v2"]
    assert [v["version_id"] for v in policies[b["policy"]]["versions"]] == ["v1"]
    # the version keeps the title it was published with (ADR-0049); A's earlier one is not changed
    assert [v["title"] for v in policies[a["policy"]]["versions"]][1] == b_title
    assert policies[a["policy"]]["versions"][0]["title"] != b_title


@pytest.mark.parametrize("by", ["title", "slug case", "another tenant", "an id with a slash"])
async def test_a_policy_is_named_by_its_instrument_never_by_a_title(sydney: World, by: str) -> None:
    made = await published(sydney)
    title = front_matter_title(PRIVACY.read_text("utf-8"))
    name = {
        "title": title,
        "slug case": made["policy"].upper(),
        "another tenant": "beta/privacy-policy",
        "an id with a slash": made["instrument_id"],
    }[by]
    before = (sydney.stores(), len(sydney.audit()))
    async with sydney.client(ADMIN_TOKEN) as client:
        response = await client.post(f"{API}/{name}/versions", json=body(PRIVACY))
    assert response.status_code == 404
    assert sydney.stores() == before[0]
    if "/" not in name:  # a path with a slash is no route at all, so no handler audits it
        assert len(sydney.audit()) == before[1] + 1
        assert outcomes(sydney, RemovalAction.PUBLISH)[-1] is RemovalOutcome.REFUSED


async def test_the_upload_form_names_no_policy_in_its_body(sydney: World) -> None:
    """The body cannot name an instrument, so it cannot name a policy by anything."""
    assert set(PolicyUpload.model_fields) == {
        "filename",
        "content",
        "title",
        "effective_from",
        "backdate_reason",
    }
    made = await published(sydney)
    for key in ("instrument_id", "policy", "supersedes", "version_id"):
        response = await new_policy(sydney, body(PRIVACY) | {key: made["instrument_id"]})
        assert response.status_code == 422 and response.json()["detail"][0]["type"] == (
            "extra_forbidden"
        ), key


# ---- a past effective date needs a reason ------


async def test_a_past_effective_date_without_a_reason_is_refused(sydney: World) -> None:
    for reason in ({}, {"backdate_reason": None}):
        response = await new_policy(
            sydney, body(PRIVACY, effective_from=YESTERDAY.isoformat(), **reason)
        )
        assert response.status_code == 422, reason
        assert refusal_of(response)["error"] == "backdate_reason_required"
    sydney.assert_nothing_written()
    assert [(e.action, e.outcome) for e in sydney.audit()] == [
        (RemovalAction.PUBLISH, RemovalOutcome.REFUSED)
    ] * 2


async def test_a_past_effective_date_is_published_with_its_reason_recorded(sydney: World) -> None:
    reason = "the board approved it on 1 March; the file reached us later"
    made = await published(
        sydney, body(PRIVACY, effective_from=YESTERDAY.isoformat(), backdate_reason=reason)
    )
    assert made["backdate_reason"] == reason
    assert sydney.ledger.backdate_reasons[(TENANT, made["instrument_id"], "v1")] == reason
    version = (await listing(sydney))[made["policy"]]["versions"][0]
    assert (version["effective_from"], version["backdate_reason"]) == (
        YESTERDAY.isoformat(),
        reason,
    )
    assert [e.reason for e in sydney.audit()] == [reason] * 2  # authorized, completed


@pytest.mark.parametrize("day", [None, TODAY, TOMORROW, NEXT_MONTH], ids=str)
async def test_today_and_the_future_need_no_reason_and_take_none(
    sydney: World, day: dt.date | None
) -> None:
    extra = {} if day is None else {"effective_from": day.isoformat()}
    made = await published(sydney, body(PRIVACY, **extra))
    assert made["effective_from"] == (day or TODAY).isoformat()
    refused = await new_policy(sydney, body(PRIVACY, backdate_reason="not needed", **extra))
    assert refused.status_code == 422
    assert refusal_of(refused)["error"] == "backdate_reason_unneeded"
    assert not sydney.ledger.backdate_reasons  # nothing was recorded for either


@pytest.mark.parametrize(
    "day",
    ["2026-13-01", "2026-02-30", "yesterday", "2026-1-1", 20261201, "2026-12-01T00:00:00", ""],
    ids=str,
)
async def test_a_date_is_an_iso_date_or_a_422(sydney: World, day: object) -> None:
    response = await new_policy(sydney, body(PRIVACY, effective_from=day))
    assert response.status_code == 422
    sydney.assert_nothing_written()


async def test_a_date_outside_the_calendar_the_law_uses_is_refused(sydney: World) -> None:
    for day, reason in (("1900-12-31", {"backdate_reason": "old"}), ("2200-01-01", {})):
        response = await new_policy(sydney, body(PRIVACY, effective_from=day, **reason))
        assert response.status_code == 422 and refusal_of(response)["error"] == "date_out_of_range"
    sydney.assert_nothing_written()


@pytest.mark.parametrize(
    "reason", ["", "   ", "two\nlines", "x" * 501, 7, "a\x00b", "a" + chr(0x2028) + "b"]
)
async def test_a_blank_or_unfit_reason_is_a_422(sydney: World, reason: object) -> None:
    response = await new_policy(
        sydney, body(PRIVACY, effective_from=YESTERDAY.isoformat(), backdate_reason=reason)
    )
    assert response.status_code == 422
    sydney.assert_nothing_written()


# ---- #78, extended to this route: an upload cannot mark itself synthetic ------


async def test_a_body_carrying_synthetic_is_a_422_and_writes_nothing(
    sydney: World, offshore: World
) -> None:
    for w in (sydney, offshore):
        # a policy to add a version to, so the version route reaches its body (a fixture, which
        # offshore is registered)
        made = await published(w, body(PRIVACY, **past(5)))
        before = (w.stores(), len(w.audit()))
        for flag in (True, False, "true", 1):
            for response in (
                await new_policy(w, body(PRIVACY) | {"synthetic": flag}),
                await new_version(w, made["policy"], body(PRIVACY) | {"synthetic": flag}),
            ):
                assert response.status_code == 422, (w, flag)
                assert response.json()["detail"][0]["type"] == "extra_forbidden"
        assert w.stores() == before[0]
        # each refusal is audited, and none of them wrote more
        new = w.audit()[before[1] :]
        assert [(e.action, e.outcome) for e in new] == [
            (RemovalAction.PUBLISH, RemovalOutcome.REFUSED)
        ] * 8


async def test_an_admin_upload_cannot_mark_itself_synthetic_78(offshore: World) -> None:
    """The residency gate treats any tenant text the operator's registry doesn't vouch for as
    real customer data. The request has no way to say otherwise: a `synthetic` field is refused
    (422) on both routes, the model has no such field, the lifecycle's publish has no such
    parameter, and real text offshore is refused before a byte is written, whatever it says."""
    assert "synthetic" not in PolicyUpload.model_fields
    assert not {
        n for n in inspect.signature(CorpusLifecycle.publish_version).parameters if "synth" in n
    }
    made = await published(offshore, body(PRIVACY, **past(5)))  # a registered fixture
    before = offshore.stores()
    for claim in (
        {"synthetic": True},
        {"is_synthetic": True},
        {"metadata": {"synthetic": True}},
    ):
        for response in (
            await new_policy(offshore, body(THE_TEXT, **claim)),
            await new_version(offshore, made["policy"], body(THE_TEXT, **claim)),
        ):
            assert response.status_code == 422, claim  # refused for the field, before the text
    for response in (
        await new_policy(offshore, body(THE_TEXT)),
        await new_version(offshore, made["policy"], body(THE_TEXT)),
    ):
        assert response.status_code == 403 and refusal_of(response)["error"] == "residency"
    assert offshore.stores() == before
    assert not await offshore.retrieval.holds_real_customer_data()


@pytest.mark.parametrize("path", EVERY_POLICY, ids=lambda p: p.name)
async def test_a_policy_byte_identical_to_a_registered_fixture_is_synthetic(
    offshore: World, path: Any
) -> None:
    made = await published(offshore, body(path, **past(5)))
    assert made["version_id"] == "v1"
    assert not await offshore.retrieval.holds_real_customer_data()
    # under another name, and as a new version of itself: the registry vouches for the words
    renamed = body(path) | {"filename": "anything-at-all.md"}
    assert (await new_version(offshore, made["policy"], renamed)).status_code == 201


async def test_an_excerpt_a_reordering_or_a_one_byte_change_of_a_fixture_is_refused_offshore(
    offshore: World,
) -> None:
    """Offshore an upload is synthetic only if its raw bytes are a registered fixture's (ADR-0026)
    as well as every chunk and clause being registered. The excerpt and the reordering below
    consist of registered clauses only, and are refused with nothing written."""
    for path in EVERY_POLICY:
        source = path.read_text("utf-8")
        blocks = source.split("\n\n")
        variants = {
            "an excerpt": "\n\n".join(blocks[:-1]) + "\n",
            "a reordering": "\n\n".join(reversed(blocks)),
            "one byte more": source + "\n",
            "one byte fewer": source[:-1],
            "front matter changed": source.replace("synthetic-fixture", "somebody", 1),
            "CRLF line ends": source.replace("\n", "\r\n"),
        }
        for what, changed in variants.items():
            assert changed != source, (path.name, what)
            response = await new_policy(offshore, body(path) | {"content": changed})
            error = refusal_of(response)
            if what == "a reordering":
                # reversed, its clause numbers are out of order: the parser refuses it first
                assert response.status_code in (403, 422), (path.name, what)
                continue
            assert response.status_code == 403, (path.name, what)
            assert error["error"] == "residency" and "ADR-0026" in error["message"]
        offshore.assert_nothing_written()
        assert (await new_policy(offshore, body(path))).status_code == 201  # and itself
        # reset for the next fixture's "nothing written"
        offshore.ledger._versions.clear()  # pyright: ignore[reportPrivateUsage]
        offshore.ledger._classes.clear()  # pyright: ignore[reportPrivateUsage]
        offshore.clauses._clauses.clear()  # pyright: ignore[reportPrivateUsage]
        offshore.retrieval._chunks.clear()  # pyright: ignore[reportPrivateUsage]


async def test_in_sydney_an_excerpt_and_any_text_are_accepted(sydney: World) -> None:
    """Onshore nothing is decided by the registry."""
    excerpt = "\n\n".join(PRIVACY.read_text("utf-8").split("\n\n")[:-1]) + "\n"
    assert (await new_policy(sydney, body(PRIVACY) | {"content": excerpt})).status_code == 201
    assert (await new_policy(sydney, body(THE_TEXT))).status_code == 201


async def test_a_non_tester_is_refused_offshore_before_the_body_is_read(offshore: World) -> None:
    """Even for a document the registry vouches for: the tester rule is what lets a person send
    anything at all (ADR-0032). The second member is no admin, so the admin is made one tester
    short: a world where the admin is not the tester."""
    other = await world(**OFFSHORE, offshore_generation_testers=SECOND)
    for path in EVERY_POLICY:
        response = await new_policy(other, body(path))
        assert response.status_code == 403
        error = refusal_of(response)
        assert error["error"] == "residency" and "ADR-0032" in error["message"]
    # the body was never read: not even a body that isn't JSON changes the answer
    async with other.client(ADMIN_TOKEN) as client:
        response = await client.post(API, content=b"\xff not json at all")
    assert response.status_code == 403 and refusal_of(response)["error"] == "residency"
    other.assert_nothing_written()
    assert (await new_policy(offshore, body(PRIVACY))).status_code == 201


# ---- size, format and shape ------


async def test_a_request_over_the_limit_is_refused_before_it_is_parsed(sydney: World) -> None:
    async with sydney.client(ADMIN_TOKEN) as client:
        declared = await client.post(
            API,
            content=b"x" * (MAX_REQUEST_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(MAX_REQUEST_BYTES // 65_536 + 2):
                yield b"x" * 65_536

        streamed = await client.post(
            API, content=chunks(), headers={"Content-Type": "application/json"}
        )
    for response in (declared, streamed):
        assert response.status_code == 413 and refusal_of(response)["error"] == "too_large"
    sydney.assert_nothing_written()


async def test_a_document_over_the_limit_is_refused_on_its_bytes(sydney: World) -> None:
    big = THE_TEXT + "\n" + "1.9 " + "é" * (MAX_DOCUMENT_BYTES // 2) + "\n"
    assert len(big) < MAX_DOCUMENT_BYTES < len(big.encode())  # characters are few, bytes are many
    response = await new_policy(sydney, body(big))
    assert response.status_code == 413 and refusal_of(response)["error"] == "too_large"
    sydney.assert_nothing_written()


@pytest.mark.parametrize(
    "filename", ["policy.pdf", "policy.txt", "policy.docx", "policy", "p.md.exe"]
)
async def test_only_markdown_is_a_policy(sydney: World, filename: str) -> None:
    response = await new_policy(sydney, body(THE_TEXT) | {"filename": filename})
    assert response.status_code == 415 and refusal_of(response)["error"] == "unsupported_format"
    sydney.assert_nothing_written()


@pytest.mark.parametrize("filename", ["policy.md", "Policy.MD", "policy.markdown"])
async def test_markdown_names_are_accepted(sydney: World, filename: str) -> None:
    response = await new_policy(sydney, body(THE_TEXT) | {"filename": filename})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize(
    "bad",
    [
        {"filename": "a/b.md"},
        {"filename": "a\\b.md"},
        {"filename": ""},
        {"title": ""},
        {"title": "two\nlines"},
        {"title": "x" * 201},
        {"content": "a\x00b"},
        {"content": 7},
        {"title": None},
    ],
    ids=str,
)
async def test_a_malformed_field_is_a_422_that_never_echoes_the_input(
    sydney: World, bad: dict[str, Any]
) -> None:
    response = await new_policy(sydney, body(THE_TEXT) | bad)
    assert response.status_code == 422
    for echoed in ("two", "a/b", "\\x00"):
        assert echoed not in response.text
    sydney.assert_nothing_written()


async def test_a_lone_surrogate_is_not_text(sydney: World) -> None:
    """Valid JSON, but with no UTF-8 form: a 422, never a 500 when it is encoded."""
    raw = b'{"filename": "a.md", "content": "a\\ud800b", "title": "t"}'
    async with sydney.client(ADMIN_TOKEN) as client:
        response = await client.post(API, content=raw)
    assert response.status_code == 422 and "ud800" not in response.text.lower()
    sydney.assert_nothing_written()


async def test_a_body_that_is_not_json_is_a_422(sydney: World) -> None:
    async with sydney.client(ADMIN_TOKEN) as client:
        for content in (b"not json", b"\xff\xfe", b"[]", b"null", b"[" * 5000):
            response = await client.post(API, content=content)
            assert response.status_code == 422, content[:10]
    sydney.assert_nothing_written()


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("Just a paragraph of words.\n", "no_clauses"),
        ("# Title only\n", "no_clauses"),
        ("## 1. A section with no clauses\n\nSome prose.\n", "no_clauses"),
        ("## 1. Section\n\n2.1 A clause that claims another section.\n", "unreadable_policy"),
    ],
)
async def test_a_document_that_is_not_a_policy_is_refused_with_the_reason(
    sydney: World, text: str, error: str
) -> None:
    response = await new_policy(sydney, body(text))
    assert response.status_code == 422 and refusal_of(response)["error"] == error
    sydney.assert_nothing_written()
    row = sydney.audit()[-1]
    assert row.outcome is RemovalOutcome.REFUSED and error in row.detail
    assert "claims another section" not in row.detail  # a document's text never reaches the audit


# ---- admins only, on every route ------


async def test_a_member_who_is_not_an_admin_is_refused_everywhere_and_nothing_changes(
    sydney: World,
) -> None:
    made = await published(sydney)
    policy = made["policy"]
    before = (sydney.stores(), len(sydney.audit()))
    routes = [
        ("GET", API, None),
        ("POST", API, body(PRIVACY)),
        ("POST", f"{API}/{policy}/versions", body(PRIVACY)),
        ("GET", f"{API}/{policy}/versions/v1/preview", None),
        ("POST", f"{API}/{policy}/versions/v1/withdraw", {"reason": "no"}),
        (
            "POST",
            f"{API}/{policy}/versions/v1/purge",
            {"reason": "no", "acknowledged_findings_citing": 0},
        ),
    ]
    async with sydney.client(MEMBER_TOKEN) as client:
        for method, path, payload in routes:
            response = await client.request(method, path, json=payload)
            assert response.status_code == 403, (method, path)
    browser = await sydney.browser(MEMBER_TOKEN)
    for method, path in (
        ("GET", PAGE),
        ("GET", f"{PAGE}/{policy}/versions/v1"),
        ("POST", f"{PAGE}/{policy}/versions/v1/withdraw"),
        ("POST", f"{PAGE}/{policy}/versions/v1/purge"),
    ):
        response = await browser.request(
            method, path, data={"reason": "x", "typed_count": "0"} if method == "POST" else None
        )
        assert response.status_code == 403, (method, path)
    assert (sydney.stores(), len(sydney.audit())) == before


async def test_a_request_with_no_sign_in_is_refused(sydney: World) -> None:
    async with sydney.client() as client:
        assert (await client.get(API)).status_code == 401
        assert (await client.post(API, json=body(PRIVACY))).status_code == 401
        page = await client.get(PAGE)
    assert page.status_code == 303 and page.headers["location"] == "/signin"
    sydney.assert_nothing_written()


# ---- withdraw ------


async def withdraw(
    w: World,
    policy: str,
    version: str,
    reason: str = "superseded by a rewrite",
    token: str = ADMIN_TOKEN,
) -> httpx.Response:
    return await post(w, f"{API}/{policy}/versions/{version}/withdraw", {"reason": reason}, token)


async def test_withdraw_keeps_the_text_and_takes_the_version_out_of_retrieval(
    sydney: World,
) -> None:
    made = await published(sydney)
    instrument, policy = made["instrument_id"], made["policy"]
    chunks_before = await sydney.retrieval.version_chunk_ids(
        instrument_id=instrument, tenant_id=TENANT, version_id="v1"
    )
    response = await withdraw(sydney, policy, "v1")
    assert response.status_code == 200 and response.json() == {"status": "withdrawn"}
    assert await sydney.ledger.is_withdrawn(
        tenant_id=TENANT, instrument_id=instrument, version_id="v1"
    )
    # text and chunks are kept, so earlier citations still verify
    assert (
        await sydney.retrieval.version_chunk_ids(
            instrument_id=instrument, tenant_id=TENANT, version_id="v1"
        )
        == chunks_before
    )
    assert await sydney.clauses.get_manifest(instrument, "v1", tenant_id=TENANT)
    assert not await sydney.ledger.policy_versions(TENANT, as_of=TODAY)
    versions = (await listing(sydney))[policy]["versions"]
    assert [(v["version_id"], v["status"]) for v in versions] == [("v1", "withdrawn")]
    rows = [e for e in sydney.audit() if e.action is RemovalAction.WITHDRAW]
    assert [e.outcome for e in rows] == [RemovalOutcome.AUTHORIZED, RemovalOutcome.COMPLETED]
    assert {e.reason for e in rows} == {"superseded by a rewrite"}
    assert {e.actor for e in rows} == {MEMBER}


async def test_withdrawing_twice_or_an_unknown_version_is_refused_and_audited(
    sydney: World,
) -> None:
    made = await published(sydney)
    policy = made["policy"]
    assert (await withdraw(sydney, policy, "v1")).status_code == 200
    again = await withdraw(sydney, policy, "v1")
    assert again.status_code == 409 and refusal_of(again)["error"] == "already_withdrawn"
    unknown = await withdraw(sydney, policy, "v9")
    assert unknown.status_code == 404 and refusal_of(unknown)["error"] == "no_such_version"
    nothing = await withdraw(sydney, "no-such-policy", "v1")
    assert nothing.status_code == 404 and refusal_of(nothing)["error"] == "no_such_policy"
    assert outcomes(sydney, RemovalAction.WITHDRAW) == [
        RemovalOutcome.AUTHORIZED,
        RemovalOutcome.COMPLETED,
        RemovalOutcome.REFUSED,
        RemovalOutcome.REFUSED,
        RemovalOutcome.REFUSED,
    ]


@pytest.mark.parametrize("reason", [None, "", "  ", "two\nlines", "x" * 501])
async def test_a_withdrawal_names_its_reason(sydney: World, reason: Any) -> None:  # Any: JSON
    made = await published(sydney)
    payload = {} if reason is None else {"reason": reason}
    response = await post(sydney, f"{API}/{made['policy']}/versions/v1/withdraw", payload)
    assert response.status_code == 422
    assert not await sydney.ledger.is_withdrawn(
        tenant_id=TENANT, instrument_id=made["instrument_id"], version_id="v1"
    )
    assert outcomes(sydney, RemovalAction.WITHDRAW) == [RemovalOutcome.REFUSED]


# ---- purge, behind the typed count ------


async def purge(
    w: World,
    policy: str,
    version: str,
    count: Any,
    reason: str = "retention ended",  # Any: JSON
) -> httpx.Response:
    return await post(
        w,
        f"{API}/{policy}/versions/{version}/purge",
        {"reason": reason, "acknowledged_findings_citing": count},
    )


async def preview(w: World, policy: str, version: str = "v1") -> int:
    async with w.client(ADMIN_TOKEN) as client:
        response = await client.get(f"{API}/{policy}/versions/{version}/preview")
    assert response.status_code == 200, response.text
    count: int = response.json()["findings_citing"]
    return count


async def test_a_purge_is_refused_unless_the_typed_count_matches_the_findings(
    sydney: World,
) -> None:
    made = await published(sydney)
    instrument, policy = made["instrument_id"], made["policy"]
    sydney.ledger.seed_finding(TENANT, f"{instrument}:v1:1/1")
    sydney.ledger.seed_finding(TENANT, f"{instrument}:v1:2/1")
    sydney.ledger.seed_finding(
        "other-tenant", f"{instrument}:v1:1/1"
    )  # another tenant's: not counted
    assert await preview(sydney, policy) == 2
    before = sydney.stores()
    for typed in (0, 1, 3, 99):
        response = await purge(sydney, policy, "v1", typed)
        assert response.status_code == 409, typed
        assert refusal_of(response)["error"] == "refused"
        assert "2 finding(s)" in refusal_of(response)["message"]
    assert sydney.stores() == before  # nothing was deleted
    assert (
        await sydney.ledger.purge_marker(
            tenant_id=TENANT, instrument_id=instrument, version_id="v1"
        )
        is None
    )
    assert await sydney.retrieval.version_chunk_ids(
        instrument_id=instrument, tenant_id=TENANT, version_id="v1"
    )
    # every refusal is one audit row, written once, by the lifecycle
    rows = [e for e in sydney.audit() if e.action is RemovalAction.PURGE]
    assert [e.outcome for e in rows] == [RemovalOutcome.REFUSED] * 4
    assert {e.findings_citing for e in rows} == {2}
    # the right count purges
    done = await purge(sydney, policy, "v1", 2)
    assert done.status_code == 200 and done.json() == {"status": "purged"}
    assert await sydney.ledger.purge_marker(
        tenant_id=TENANT, instrument_id=instrument, version_id="v1"
    ) == (MEMBER, "retention ended")
    assert not await sydney.retrieval.version_chunk_ids(
        instrument_id=instrument, tenant_id=TENANT, version_id="v1"
    )
    assert not await sydney.clauses.get_manifest(instrument, "v1", tenant_id=TENANT)
    # the ledger keeps the version row, marked purged
    versions = (await listing(sydney))[policy]["versions"]
    assert [(v["version_id"], v["status"]) for v in versions] == [("v1", "purged")]
    rows = [e for e in sydney.audit() if e.action is RemovalAction.PURGE]
    assert [e.outcome for e in rows][-2:] == [RemovalOutcome.AUTHORIZED, RemovalOutcome.COMPLETED]


async def test_a_purge_with_no_findings_takes_a_typed_zero(sydney: World) -> None:
    made = await published(sydney)
    assert await preview(sydney, made["policy"]) == 0
    assert (await purge(sydney, made["policy"], "v1", 1)).status_code == 409
    assert (await purge(sydney, made["policy"], "v1", 0)).status_code == 200


async def test_a_finding_written_after_the_preview_forces_a_fresh_look(sydney: World) -> None:
    made = await published(sydney)
    shown = await preview(sydney, made["policy"])
    sydney.ledger.seed_finding(TENANT, f"{made['instrument_id']}:v1:1/1")
    response = await purge(sydney, made["policy"], "v1", shown)
    assert response.status_code == 409 and "1 finding(s)" in refusal_of(response)["message"]
    assert (await purge(sydney, made["policy"], "v1", 1)).status_code == 200


@pytest.mark.parametrize("count", ["0", "1.0", True, None, -1, 1.5, [0]])
async def test_a_typed_count_is_an_integer_or_a_422(sydney: World, count: Any) -> None:  # Any: JSON
    made = await published(sydney)
    response = await purge(sydney, made["policy"], "v1", count)
    assert response.status_code == 422
    assert not await sydney.ledger.purge_marker(
        tenant_id=TENANT, instrument_id=made["instrument_id"], version_id="v1"
    )
    assert outcomes(sydney, RemovalAction.PURGE) == [RemovalOutcome.REFUSED]


async def test_a_purge_of_a_purged_or_unknown_version_is_refused_and_audited(
    sydney: World,
) -> None:
    made = await published(sydney)
    assert (await purge(sydney, made["policy"], "v1", 0)).status_code == 200
    again = await purge(sydney, made["policy"], "v1", 0)
    assert again.status_code == 409 and refusal_of(again)["error"] == "already_purged"
    withdrawn = await withdraw(sydney, made["policy"], "v1")
    assert withdrawn.status_code == 409 and refusal_of(withdrawn)["error"] == "purged"
    unknown = await purge(sydney, made["policy"], "v7", 0)
    assert unknown.status_code == 404
    assert outcomes(sydney, RemovalAction.PURGE)[-2:] == [RemovalOutcome.REFUSED] * 2


async def test_a_purged_versions_id_is_not_used_again(sydney: World) -> None:
    made = await published(sydney)
    assert (await purge(sydney, made["policy"], "v1", 0)).status_code == 200
    again = await new_version(sydney, made["policy"], body(PRIVACY))
    assert again.status_code == 201 and again.json()["version_id"] == "v2"
    assert again.json()["supersedes"] is None  # the only earlier version is purged


async def test_library_law_is_never_purged_or_withdrawn_through_a_policy_route(
    sydney: World,
) -> None:
    """The routes name a policy of this workspace; a library instrument is none (ADR-0021)."""
    for path, payload in (
        (
            f"{API}/privacy-act-1988/versions/C2026C00227/purge",
            {"reason": "x", "acknowledged_findings_citing": 0},
        ),
        (f"{API}/privacy-act-1988/versions/C2026C00227/withdraw", {"reason": "x"}),
    ):
        assert (await post(sydney, path, payload)).status_code == 404


# ---- every outcome writes an audit row ------


async def test_every_outcome_writes_an_audit_row_with_who_what_why_and_the_trace(
    sydney: World,
) -> None:
    made = await published(sydney, body(PRIVACY, **past(10)))
    policy = made["policy"]
    trace_ids: list[str] = []

    async def step(
        response: httpx.Response, status: int, *rows: tuple[RemovalAction, RemovalOutcome]
    ) -> None:
        assert response.status_code == status, response.text
        trace_ids.append(response.headers["x-trace-id"])
        new = sydney.audit()[-len(rows) :]
        assert [(e.action, e.outcome) for e in new] == list(rows)
        assert {e.trace_id for e in new} == {trace_ids[-1]}  # the request's own trace
        assert {e.actor for e in new} == {MEMBER} and {e.tenant_id for e in new} == {TENANT}
        assert all(e.audit_id for e in new)

    P, W, G = RemovalAction.PUBLISH, RemovalAction.WITHDRAW, RemovalAction.PURGE
    R, A, C = RemovalOutcome.REFUSED, RemovalOutcome.AUTHORIZED, RemovalOutcome.COMPLETED
    assert [(e.action, e.outcome) for e in sydney.audit()] == [(P, A), (P, C)]
    await step(await new_version(sydney, policy, body(PRIVACY)), 201, (P, A), (P, C))
    await step(
        await new_version(sydney, policy, body(PRIVACY, effective_from=YESTERDAY.isoformat())),
        422,
        (P, R),
    )
    await step(await new_version(sydney, "nothing", body(PRIVACY)), 404, (P, R))
    await step(await new_policy(sydney, body(PRIVACY) | {"synthetic": True}), 422, (P, R))
    await step(await withdraw(sydney, policy, "v1"), 200, (W, A), (W, C))
    await step(await withdraw(sydney, policy, "v1"), 409, (W, R))
    await step(await purge(sydney, policy, "v2", 3), 409, (G, R))
    await step(await purge(sydney, policy, "v2", 0), 200, (G, A), (G, C))
    await step(await purge(sydney, policy, "v2", 0), 409, (G, R))
    assert len(set(trace_ids)) == len(trace_ids)  # one trace per request
    # a purge row names the count it was shown
    purged = [e for e in sydney.audit() if e.action is G and e.outcome is C]
    assert [e.findings_citing for e in purged] == [0]
    # the publish rows carry the effective date and what it replaced
    details = [e.detail for e in sydney.audit() if e.action is P and e.outcome is C]
    assert details == [
        f"effective {TODAY - dt.timedelta(days=10)}",
        f"effective {TODAY}; replaces v1",
    ]


async def test_a_publish_that_fails_part_way_is_audited_failed(sydney: World) -> None:
    class Broken(FakeRetrievalStore):
        async def upsert_chunks(self, chunks: Any) -> None:  # Any: the chunks tuple
            raise RuntimeError("the vector store is unreachable")

    sydney.retrieval._chunks.clear()  # pyright: ignore[reportPrivateUsage]
    broken = Broken(ledger=sydney.ledger)
    lifecycle = CorpusLifecycle(
        ledger=sydney.ledger,
        retrieval=broken,
        clauses=sydney.clauses,
        generation_region=_settings().generation_region,
        synthetic_registry=SyntheticRegistry.load(),
    )
    sydney.app.state.services = fake_services_with(sydney, lifecycle)
    async with sydney.client(ADMIN_TOKEN) as client:
        client._transport = httpx.ASGITransport(app=sydney.app, raise_app_exceptions=False)  # pyright: ignore[reportPrivateUsage]
        response = await client.post(API, json=body(PRIVACY))
    assert response.status_code == 500
    assert outcomes(sydney, RemovalAction.PUBLISH) == [
        RemovalOutcome.AUTHORIZED,
        RemovalOutcome.FAILED,
    ]
    failed = sydney.audit()[-1]
    assert "RuntimeError" in failed.detail and "unreachable" not in failed.detail
    assert not sydney.ledger._versions  # pyright: ignore[reportPrivateUsage] — no ledger record: not citable


def fake_services_with(w: World, lifecycle: CorpusLifecycle) -> Any:  # Any: ApiServices
    from dataclasses import replace

    return replace(w.app.state.services, lifecycle=lifecycle)


# ---- the page ------


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.versions: list[tuple[str, str, str]] = []
        """`(policy, version, status)` of each version row, under its policy's section."""
        self._policy = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = {k: v or "" for k, v in attrs}
        self.elements.append((tag, found))
        if tag == "section" and "data-policy" in found:
            self._policy = found["data-policy"]
        if tag == "li" and "data-version" in found:
            self.versions.append((self._policy, found["data-version"], found["data-status"]))

    def where(self, tag: str, **attrs: str) -> list[dict[str, str]]:
        return [
            a for t, a in self.elements if t == tag and all(a.get(k) == v for k, v in attrs.items())
        ]


def parsed(text: str) -> Page:
    page = Page()
    page.feed(text)
    return page


def words(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


async def test_the_page_lists_each_policy_with_its_versions_and_their_status(
    sydney: World,
) -> None:
    a = await published(sydney, body(PRIVACY))
    await new_version(sydney, a["policy"], body(PRIVACY, effective_from=NEXT_MONTH.isoformat()))
    b = await published(sydney, body(VENDOR))
    await withdraw(sydney, b["policy"], "v1")
    c = await published(sydney, body(THE_TEXT, title="Retention"))
    await purge(sydney, c["policy"], "v1", 0)
    d = await published(sydney, body(PRIVACY, **past(10)))
    await new_version(sydney, d["policy"], body(PRIVACY))
    browser = await sydney.browser()
    response = await browser.get(PAGE)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    page = parsed(response.text)
    assert sorted(page.versions) == sorted(
        [
            (a["policy"], "v1", "in_force"),
            (a["policy"], "v2", "future"),
            (b["policy"], "v1", "withdrawn"),
            (c["policy"], "v1", "purged"),
            (d["policy"], "v1", "superseded"),
            (d["policy"], "v2", "in_force"),
        ]
    )
    text = words(response.text)
    for status in ("In force", "Future", "Superseded", "Withdrawn", "Purged"):
        assert status in text
    assert front_matter_title(PRIVACY.read_text("utf-8")) in text
    assert f"effective {TODAY}" in text and f"effective {NEXT_MONTH}" in text


async def test_each_policy_has_its_own_new_version_form_that_posts_to_its_route(
    sydney: World,
) -> None:
    a = await published(sydney, body(PRIVACY))
    b = await published(sydney, body(VENDOR))
    page = parsed((await (await sydney.browser()).get(PAGE)).text)
    actions = sorted(f["data-action"] for f in page.where("form", **{"class": "ask policy-upload"}))
    assert actions == sorted(
        [API, f"{API}/{a['policy']}/versions", f"{API}/{b['policy']}/versions"]
    )
    # the form for a version is prefilled with that policy's title, and today's date
    page_text = (await (await sydney.browser()).get(PAGE)).text
    assert f'value="{TODAY}"' in page_text
    text = words(page_text)
    assert "New version of" in text and "New policy" in text
    # the page tells the admin the limit the server holds, on the raw bytes
    forms = page.where("form", **{"class": "ask policy-upload"})
    assert {f["data-max-bytes"] for f in forms} == {str(MAX_DOCUMENT_BYTES)}


async def test_the_sidebar_links_policies_for_an_admin_and_not_for_a_member(
    sydney: World,
) -> None:
    admin = await (await sydney.browser()).get(PAGE)
    assert parsed(admin.text).where("a", href=PAGE, **{"aria-current": "page"})
    member = await (await sydney.browser(MEMBER_TOKEN)).get("/chats")
    assert not parsed(member.text).where("a", href=PAGE)


async def test_a_new_version_form_posts_the_whole_flow_the_page_describes(sydney: World) -> None:
    """The page's script posts JSON to the route in `data-action`; here the same request, from a
    signed-in browser session (the cookie, no Bearer header), as the script sends it."""
    made = await published(sydney, body(PRIVACY, **past(5)))
    browser = await sydney.browser()
    page = parsed((await browser.get(PAGE)).text)
    action = next(
        f["data-action"]
        for f in page.where("form", **{"class": "ask policy-upload"})
        if f["data-action"].endswith("/versions")
    )
    response = await browser.post(action, json=body(PRIVACY))
    assert response.status_code == 201 and response.json()["instrument_id"] == made["instrument_id"]
    assert response.json()["href"] == f"{PAGE}/{made['policy']}/versions/v2"
    assert (await browser.get(response.json()["href"])).status_code == 200


async def test_the_version_page_shows_the_preview_and_the_two_forms(sydney: World) -> None:
    made = await published(sydney)
    sydney.ledger.seed_finding(TENANT, f"{made['instrument_id']}:v1:1/1")
    browser = await sydney.browser()
    response = await browser.get(f"{PAGE}/{made['policy']}/versions/v1")
    assert response.status_code == 200
    page = parsed(response.text)
    assert page.where("p", **{"data-findings-citing": "1"})
    base = f"{PAGE}/{made['policy']}/versions/v1"
    assert {f["action"] for f in page.where("form", method="post")} == {
        f"{base}/withdraw",
        f"{base}/purge",
    }
    text = words(response.text)
    assert "1 finding cites this version" in text
    assert "type the number of findings that cite this version: 1" in text
    # a version that does not exist, or a policy that is not this workspace's, is a 404 page
    for path in (f"{PAGE}/{made['policy']}/versions/v9", f"{PAGE}/nothing/versions/v1"):
        assert (await browser.get(path)).status_code == 404


async def test_a_withdrawn_version_offers_no_second_withdrawal_and_a_purged_one_no_actions(
    sydney: World,
) -> None:
    made = await published(sydney)
    browser = await sydney.browser()
    path = f"{PAGE}/{made['policy']}/versions/v1"
    await withdraw(sydney, made["policy"], "v1")
    withdrawn = parsed((await browser.get(path)).text)
    assert {f["action"] for f in withdrawn.where("form", method="post")} == {f"{path}/purge"}
    await purge(sydney, made["policy"], "v1", 0)
    purged = (await browser.get(path)).text
    assert not parsed(purged).where("form", method="post")
    assert "This version is purged" in words(purged)


async def test_the_page_withdraws_and_redirects_back(sydney: World) -> None:
    made = await published(sydney)
    browser = await sydney.browser()
    path = f"{PAGE}/{made['policy']}/versions/v1"
    response = await browser.post(f"{path}/withdraw", data={"reason": "retired"})
    assert response.status_code == 303 and response.headers["location"] == f"{path}?done=withdrawn"
    assert "Withdrawn." in words((await browser.get(response.headers["location"])).text)
    assert await sydney.ledger.is_withdrawn(
        tenant_id=TENANT, instrument_id=made["instrument_id"], version_id="v1"
    )
    # a second try says so in words, on the page, with the status the JSON gives
    again = await browser.post(f"{path}/withdraw", data={"reason": "retired"})
    assert again.status_code == 409 and "already withdrawn" in words(again.text)
    blank = await browser.post(f"{path}/purge", data={"reason": "   ", "typed_count": "0"})
    assert blank.status_code == 422 and "give a reason" in words(blank.text)
    assert outcomes(sydney, RemovalAction.WITHDRAW)[-1] is RemovalOutcome.REFUSED
    assert outcomes(sydney, RemovalAction.PURGE) == [RemovalOutcome.REFUSED]


async def test_the_page_purges_only_on_the_typed_count(sydney: World) -> None:
    made = await published(sydney)
    sydney.ledger.seed_finding(TENANT, f"{made['instrument_id']}:v1:2/1")
    sydney.ledger.seed_finding(TENANT, f"{made['instrument_id']}:v1:2/2")
    browser = await sydney.browser()
    path = f"{PAGE}/{made['policy']}/versions/v1/purge"
    for typed in ("", "1", "3", "two", "2.0", "-2", " 22 "):
        response = await browser.post(path, data={"reason": "done", "typed_count": typed})
        assert response.status_code in (409, 422), typed
        assert (
            "purged" not in response.headers["location"] if "location" in response.headers else True
        )
    assert await sydney.retrieval.version_chunk_ids(
        instrument_id=made["instrument_id"], tenant_id=TENANT, version_id="v1"
    )
    ok = await browser.post(path, data={"reason": "done", "typed_count": " 2 "})
    assert ok.status_code == 303 and ok.headers["location"].endswith("?done=purged")
    assert not await sydney.retrieval.version_chunk_ids(
        instrument_id=made["instrument_id"], tenant_id=TENANT, version_id="v1"
    )
    refused = [
        e
        for e in sydney.audit()
        if e.action is RemovalAction.PURGE and e.outcome is RemovalOutcome.REFUSED
    ]
    assert len(refused) == 7  # one row for each wrong try, none lost


async def test_a_form_must_be_a_form_and_the_notice_is_a_fixed_text(sydney: World) -> None:
    made = await published(sydney)
    browser = await sydney.browser()
    path = f"{PAGE}/{made['policy']}/versions/v1"
    json_post = await browser.post(f"{path}/withdraw", json={"reason": "x"})
    assert json_post.status_code == 415
    # a notice is chosen by a fixed key; text in the query is never drawn
    shown = await browser.get(f"{PAGE}?done=<b>hello</b>")
    assert "hello" not in shown.text and "card ok" not in shown.text
    twice = await browser.post(
        f"{path}/withdraw",
        content=b"reason=a&reason=b",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert twice.status_code == 400


async def test_markup_in_a_title_reaches_the_browser_as_text(sydney: World) -> None:
    evil = '<img src=x onerror="alert(1)"> & "quotes" <script>alert(2)</script>'
    made = await published(sydney, body(THE_TEXT, title=evil))
    browser = await sydney.browser()
    for path in (PAGE, f"{PAGE}/{made['policy']}/versions/v1"):
        text = (await browser.get(path)).text
        assert "<script>alert" not in text and "<img src=x" not in text
        assert "&lt;img src=x" in text
        assert not parsed(text).where("img")


async def test_the_page_keeps_the_one_csp_and_the_pages_headers(sydney: World) -> None:
    made = await published(sydney)
    browser = await sydney.browser()
    for path in (PAGE, f"{PAGE}/{made['policy']}/versions/v1"):
        response = await browser.get(path)
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["cache-control"] == "no-store"
        text = response.text
        # no inline script, handler or style: the CSP would refuse them
        page = parsed(text)
        assert not [a for t, a in page.elements if t == "script" and "src" not in a]
        assert not [a for _, a in page.elements if any(k.startswith("on") for k in a)]
        assert not [a for _, a in page.elements if "style" in a]
    cross = await sydney.client().post(  # a cookie, from another origin
        f"{PAGE}/{made['policy']}/versions/v1/withdraw",
        headers={"Origin": "https://evil.example"},
        cookies=dict(browser.cookies),
        data={"reason": "x"},
    )
    assert cross.status_code == 403
    assert not await sydney.ledger.is_withdrawn(
        tenant_id=TENANT, instrument_id=made["instrument_id"], version_id="v1"
    )


def test_the_script_writes_no_html_and_names_no_policy_by_title() -> None:
    script = (STATIC_DIR / "admin_policies.js").read_text()
    assert_script_writes_no_html(script)
    # the route is the form's own, from the server; the script builds no path from a field
    assert "dataset.action" in script and "/v1/admin/policies" not in script
    assert "title" in script and "form.elements.title.value" in script
    assert "`/" not in script.replace("`/signin", "")  # no URL assembled from input


# ---- review round: retrieval, honest audit rows, trimmed reasons, ASCII numerals ---------------

NBSP = chr(0xA0)
FULLWIDTH = {n: chr(0xFF10 + n) for n in range(10)}
ARABIC_INDIC = {n: chr(0x660 + n) for n in range(10)}
SUPERSCRIPT_TWO = chr(0xB2)


async def visible_versions(w: World, as_of: dt.date) -> set[str]:
    """The versions of every policy retrieval returns for a question on `as_of`: read off the
    retrieval store's own search, not the ledger."""
    result = await w.retrieval.search(
        "payroll records people team retention",
        law=ALL_LAW,
        tenant_id=TENANT,
        scopes=(Scope.TENANT,),
        as_of=as_of,
        limit=50,
    )
    return {parse_stable_id(c.clause_id)[1] for c in result.fused}


async def test_a_future_dated_versions_chunks_are_in_retrieval_only_from_its_date(
    sydney: World,
) -> None:
    """The ledger says a version is `future`; retrieval, which answers questions, agrees: the new
    version's chunks are absent before its effective date and present on and after it, while the
    version it replaces answers until then."""
    first = await published(sydney, body(THE_TEXT, **past(10)))
    newer = THE_TEXT.replace("seven years", "ten years")
    second = await new_version(
        sydney, first["policy"], body(newer, effective_from=NEXT_MONTH.isoformat())
    )
    assert second.status_code == 201 and second.json()["version_id"] == "v2"
    before = NEXT_MONTH - dt.timedelta(days=1)
    assert await visible_versions(sydney, TODAY) == {"v1"}
    assert await visible_versions(sydney, before) == {"v1"}  # not v2, the day before
    assert await visible_versions(sydney, NEXT_MONTH) == {"v2"}  # on its date, and v1 has ended
    assert await visible_versions(sydney, LATER) == {"v2"}
    assert await visible_versions(sydney, TODAY - dt.timedelta(days=20)) == set()  # before v1


async def test_a_withdrawn_versions_chunks_are_not_in_retrieval_at_any_date(
    sydney: World,
) -> None:
    made = await published(sydney, body(THE_TEXT, **past(10)))
    days = (TODAY - dt.timedelta(days=5), TODAY, LATER)
    assert [await visible_versions(sydney, d) for d in days] == [{"v1"}] * 3
    assert (await withdraw(sydney, made["policy"], "v1")).status_code == 200
    assert [await visible_versions(sydney, d) for d in days] == [set()] * 3
    # the text is kept: only retrieval changed
    assert await sydney.retrieval.version_chunk_ids(
        instrument_id=made["instrument_id"], tenant_id=TENANT, version_id="v1"
    )


async def test_a_refused_new_policy_is_audited_with_no_instrument_and_its_title(
    sydney: World,
) -> None:
    """No id is recorded for a policy that was never made, and none is invented."""
    for upload in (
        body("Just words.\n", title="Refused Policy"),
        body(THE_TEXT, title="Refused Policy", effective_from=YESTERDAY.isoformat()),
    ):
        assert (await new_policy(sydney, upload)).status_code == 422
    rows = sydney.audit()
    assert [(e.action, e.outcome) for e in rows] == [
        (RemovalAction.PUBLISH, RemovalOutcome.REFUSED)
    ] * 2
    for row in rows:
        assert row.instrument_id is None and row.version_id is None
        assert "'Refused Policy'" in row.detail
    assert "policy-" not in " ".join(e.detail for e in rows)
    sydney.assert_nothing_written()
    # a body too broken to have a title says so, and still names no instrument
    async with sydney.client(ADMIN_TOKEN) as client:
        await client.post(API, content=b"not json")
    last = sydney.audit()[-1]
    assert last.instrument_id is None and "title" not in last.detail


async def test_a_refused_new_version_is_audited_under_its_policy_with_no_version_id(
    sydney: World,
) -> None:
    made = await published(sydney, body(THE_TEXT, **past(10)))
    before = len(sydney.audit())
    refused = await new_version(
        sydney, made["policy"], body(THE_TEXT, effective_from=YESTERDAY.isoformat())
    )
    assert refused.status_code == 422
    (row,) = sydney.audit()[before:]
    assert (row.outcome, row.instrument_id, row.version_id) == (
        RemovalOutcome.REFUSED,
        made["instrument_id"],
        None,  # v2 was never created
    )
    # a policy that does not exist: audited as named, with no version either
    missing = await new_version(sydney, "no-such-policy", body(THE_TEXT))
    assert missing.status_code == 404
    last = sydney.audit()[-1]
    assert (last.instrument_id, last.version_id) == (f"{TENANT}/no-such-policy", None)
    # the rows of a version that is written name it
    assert [e.version_id for e in sydney.audit() if e.outcome is RemovalOutcome.COMPLETED] == ["v1"]


@pytest.mark.parametrize("path", ["json", "page"])
async def test_a_reason_is_trimmed_and_a_blank_one_refused_the_same_in_both_layers(
    sydney: World, path: str
) -> None:
    made = await published(sydney)
    policy = made["policy"]
    browser = await sydney.browser()

    async def withdraw_with(reason: str, version: str = "v1") -> int:
        if path == "json":
            return (await withdraw(sydney, policy, version, reason)).status_code
        response = await browser.post(
            f"{PAGE}/{policy}/versions/{version}/withdraw", data={"reason": reason}
        )
        return 200 if response.status_code == 303 else response.status_code

    for blank in ("", "   ", f"{NBSP} \t", "\n"):  # blank after trimming, whatever the space
        assert await withdraw_with(blank) == 422, repr(blank)
    assert outcomes(sydney, RemovalAction.WITHDRAW) == [RemovalOutcome.REFUSED] * 4
    assert await withdraw_with("  retired \n") == 200
    stored = {
        e.reason
        for e in sydney.audit()
        if e.action is RemovalAction.WITHDRAW and e.outcome is RemovalOutcome.COMPLETED
    }
    assert stored == {"retired"}  # the same text, trimmed, whichever layer sent it


async def test_a_backdate_reason_is_trimmed_and_a_blank_one_refused(sydney: World) -> None:
    for blank in ("", "   ", NBSP):
        response = await new_policy(
            sydney,
            body(THE_TEXT, effective_from=YESTERDAY.isoformat(), backdate_reason=blank),
        )
        assert response.status_code == 422, repr(blank)
    made = await published(
        sydney,
        body(THE_TEXT, effective_from=YESTERDAY.isoformat(), backdate_reason="  filed late \n"),
    )
    assert made["backdate_reason"] == "filed late"
    assert sydney.ledger.backdate_reasons[(TENANT, made["instrument_id"], "v1")] == "filed late"


@pytest.mark.parametrize(
    "day",
    [
        f"{FULLWIDTH[2]}026-12-01",
        f"2026-{FULLWIDTH[1]}2-01",
        "".join(ARABIC_INDIC[int(c)] if c.isdigit() else c for c in "2026-12-01"),
        f"2026-12-0{ARABIC_INDIC[1]}",
    ],
    ids=["fullwidth", "one fullwidth", "arabic-indic", "one arabic-indic"],
)
async def test_a_date_of_non_ascii_numerals_is_refused(sydney: World, day: str) -> None:
    """A regex `\\d` matches the numerals of other scripts, and `date.fromisoformat` would read
    some of them: only ASCII numerals are a date."""
    response = await new_policy(sydney, body(THE_TEXT, effective_from=day))
    assert response.status_code == 422
    assert "YYYY-MM-DD" in response.text
    sydney.assert_nothing_written()


async def test_a_typed_count_of_non_ascii_numerals_is_not_a_number(sydney: World) -> None:
    made = await published(sydney)
    browser = await sydney.browser()
    path = f"{PAGE}/{made['policy']}/versions/v1/purge"
    for typed in (
        FULLWIDTH[0],
        ARABIC_INDIC[0],
        SUPERSCRIPT_TWO,
    ):  # a zero in two scripts, a superscript two
        response = await browser.post(path, data={"reason": "done", "typed_count": typed})
        assert response.status_code == 422, typed
        assert "as numerals" in words(response.text)
    assert not await sydney.ledger.purge_marker(
        tenant_id=TENANT, instrument_id=made["instrument_id"], version_id="v1"
    )
    assert outcomes(sydney, RemovalAction.PURGE) == [RemovalOutcome.REFUSED] * 3


def test_a_new_policys_id_is_validated_where_it_is_minted() -> None:
    minted = mint_policy_id(TENANT)
    assert validate_instrument_id(minted) == minted
    assert re.fullmatch(rf"{TENANT}/policy-[0-9a-f]{{12}}", minted)
    assert mint_policy_id(TENANT) != minted
    with pytest.raises(ValueError, match="reserved"):
        mint_policy_id("_public")  # an id that isn't one is refused, not trusted
