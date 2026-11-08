"""Click-to-clause through the API (#277, ADR-0053 I2): every citation carries
an `href` to `GET /v1/sources/{instrument_id}/{version_id}`, which opens the
stored source at the clause's anchor (ADR-0019).

Law is the stored HTML, a policy is rendered from its stored Markdown, and the
route is tenant-scoped: anything outside the caller's environment is the same
404. Run on `FakeClauseStore` and on the real `GCSClauseStore` over the
versioned in-memory bucket its own tests use, never a real one. All six Acme
policies are served, not a representative one.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

import httpx
import pytest

from compliance_rag.adapters.fakes import (
    FakeClauseStore,
    FakeGenerator,
    FakeGraphStore,
    FakeLedgerStore,
    FakeReranker,
    FakeRetrievalStore,
)
from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import ClauseStore
from compliance_rag.api.app import create_app
from compliance_rag.api.sources import SOURCE_CSP, source_href
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.corpus.models import (
    CANONICALISATION_VERSION,
    PUBLIC_TENANT_ID,
    ClauseRecord,
    DocumentClass,
    Scope,
    clause_anchor_id,
)
from compliance_rag.corpus.policy_parser import parse_policy_markdown
from compliance_rag.eval.golden_corpus import (
    POLICY_INSTRUMENT_IDS,
    POLICY_TENANT_ID,
    POLICY_VERSION_ID,
    golden_clauses,
    policy_source,
    policy_titles,
)
from compliance_rag.eval.harness import GOLDEN_VALID_FROM, golden_ledger, law_source_html
from compliance_rag.graph.mappings import MappingSet
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier
from tests.api_support import TENANT, api_settings, bearer, fake_api_services, mint
from tests.test_api_answers import APP11, QUESTION, _reply  # pyright: ignore[reportPrivateUsage]
from tests.test_gcs_clause_store import _FakeBucket  # pyright: ignore[reportPrivateUsage]

LAW = "privacy-act-1988"
LAW_VERSION = "C2026C00227"
OWNER = mint()  # uid-123, the default sign-in
COLLEAGUE = mint(sub="uid-colleague", email="second@acme.example")
NOT_FOUND = {"detail": "no such source"}
EVERY_POLICY = tuple(POLICY_INSTRUMENT_IDS)
"""All six fixtures (`CLAUDE.md`: every fixture, not a representative one)."""
HOSTILE_LAW = """<!DOCTYPE html>
<html><head><meta http-equiv="refresh" content="0;url=https://evil.example">
<script>window.stolen = document.cookie</script><style>p{color:red}</style></head>
<body onload="steal()"><p id="clause-s1" onclick="steal()">Law text</p>
<a id="clause-s2" href="java&#9;script:steal()">link</a>
<img src="x" onerror="steal()"><SCRIPT type="text/javascript">steal()</SCRIPT>
<svg><script>steal()</script></svg><iframe src="https://evil.example"></iframe>
<!--[if IE]><script>steal()</script><![endif]-->
<noscript><a title="</noscript><img src=x onerror=steal()>"></noscript>
<math><mtext><table><mglyph><style>
<img src=x onerror=steal()></style></mglyph></table></mtext></math>
<svg><a><animate attributeName=href values="javascript:steal()"/>
<set attributeName=href to="javascript:steal()"/></a></svg>
</body></html>"""


@dataclass
class World:
    client: httpx.AsyncClient
    ledger: FakeLedgerStore
    clauses: ClauseStore


async def _publish(
    world_clauses: ClauseStore,
    ledger: FakeLedgerStore,
    clauses: tuple[ClauseRecord, ...],
    *,
    source: str,
    title: str,
    document_class: DocumentClass = DocumentClass.INTERNAL_POLICY,
) -> None:
    first = clauses[0]
    await world_clauses.put_clauses(clauses)
    await world_clauses.put_manifest(
        first.instrument_id,
        first.version_id,
        tenant_id=first.tenant_id,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html=source,
    )
    await ledger.record_version(
        tenant_id=first.tenant_id,
        instrument_id=first.instrument_id,
        version_id=first.version_id,
        valid_from=GOLDEN_VALID_FROM,
        supersedes=None,
        document_class=document_class,
        title=title,
    )


def _policy(filename: str, *, tenant_id: str = POLICY_TENANT_ID) -> tuple[ClauseRecord, ...]:
    instrument_id = POLICY_INSTRUMENT_IDS[filename]
    if tenant_id != POLICY_TENANT_ID:
        instrument_id = instrument_id.replace(f"{POLICY_TENANT_ID}/", f"{tenant_id}/")
    return parse_policy_markdown(
        policy_source(filename),
        instrument_id=instrument_id,
        version_id=POLICY_VERSION_ID,
        tenant_id=tenant_id,
    )


@pytest.fixture(params=("fake", "gcs"))
def clause_store(request: pytest.FixtureRequest) -> ClauseStore:
    kind: str = request.param
    return GCSClauseStore(_FakeBucket()) if kind == "gcs" else FakeClauseStore()  # pyright: ignore[reportArgumentType] — the versioned bucket double


async def _world(clauses: ClauseStore, *, generator: FakeGenerator | None = None) -> World:
    """The golden corpus as an API tenant sees it: the law's anchored source,
    the six Acme policies' Markdown, the ledger's records of both, and Acme
    subscribed to the Privacy Act only."""
    ledger = await golden_ledger()
    await ledger.subscribe(tenant_id=TENANT, instrument_id=LAW, subscribed_by="setup")
    law = tuple(c for c in golden_clauses() if c.tenant_id == PUBLIC_TENANT_ID)
    await clauses.put_clauses(law)
    await clauses.put_manifest(
        LAW,
        LAW_VERSION,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html=law_source_html(),
    )
    for filename in EVERY_POLICY:
        policy = _policy(filename)
        await clauses.put_clauses(policy)
        await clauses.put_manifest(
            policy[0].instrument_id,
            POLICY_VERSION_ID,
            tenant_id=TENANT,
            canonicalisation_version=CANONICALISATION_VERSION,
            source_html=policy_source(filename),
        )
    settings = api_settings()
    services = fake_api_services(
        settings=settings,
        ledger=ledger,
        clauses=clauses,
        graph=FakeGraphStore(MappingSet(files=()), clause_validity={}),
        pipeline=AssessmentPipeline(
            RetrievalPipeline(FakeRetrievalStore(ledger=ledger), FakeReranker()),
            generator or FakeGenerator("{}"),
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
            tenant_text=TenantTextGate.from_settings(settings),
        ),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)),
        base_url="http://api",
        headers=bearer(OWNER),
    )
    return World(client, ledger, clauses)


def _path_and_anchor(href: str) -> tuple[str, str]:
    path, anchor = href.split("#")
    return path, anchor


# ---- the href -------------------------------------------------------------------------


def test_a_citations_href_is_the_versions_route_at_the_clauses_anchor() -> None:
    assert source_href(APP11) == "/v1/sources/privacy-act-1988/C2026C00227#clause-sch1-app11-1"
    assert (
        source_href("acme/privacy-policy:v1:3/1") == "/v1/sources/acme/privacy-policy/v1#clause-3-1"
    )


# ---- law -------------------------------------------------------------------------------


async def test_a_law_citations_href_opens_the_stored_html_and_the_anchor_exists(
    clause_store: ClauseStore,
) -> None:
    world = await _world(clause_store)
    async with world.client as client:
        law = [c for c in golden_clauses() if c.tenant_id == PUBLIC_TENANT_ID]
        assert len(law) > 50
        hrefs = {c.clause_id: source_href(c.clause_id) for c in law}
        pages: dict[str, str] = {}
        for clause_id, href in hrefs.items():
            path, anchor = _path_and_anchor(href)
            if path not in pages:
                response = await client.get(path)
                assert response.status_code == 200, clause_id
                assert response.headers["content-type"].startswith("text/html")
                pages[path] = response.text
            assert f'id="{anchor}"' in pages[path], clause_id
    assert any('class="' in page for page in pages.values())  # structure stays


# ---- a policy --------------------------------------------------------------------------


@pytest.mark.parametrize("filename", EVERY_POLICY)
async def test_a_policy_citations_href_opens_rendered_html_whose_anchor_exists(
    filename: str, clause_store: ClauseStore
) -> None:
    world = await _world(clause_store)
    clauses = _policy(filename)
    assert clauses
    title = policy_titles()[POLICY_INSTRUMENT_IDS[filename]]
    page = ""
    async with world.client as client:
        for clause in clauses:
            path, anchor = _path_and_anchor(source_href(clause.clause_id))
            response = await client.get(path)
            assert response.status_code == 200
            page = response.text
            assert page.count(f'id="{anchor}"') == 1, clause.clause_id
            assert anchor == clause_anchor_id(clause.clause_path)
        # The page names the document by its recorded title (ADR-0049), and a
        # clause shows its number the way the document writes it.
        assert f"<title>{title.replace('&', '&amp;')}</title>" in page
        assert f"<h1>{title.replace('&', '&amp;')}</h1>" in page
        number = clauses[-1].clause_path.replace("/", ".")
        assert f'<span class="clause-number">{number}</span>' in page


async def test_a_policys_page_holds_every_clauses_text(clause_store: ClauseStore) -> None:
    """Nothing of a clause is dropped from the page: its words, in order, as
    the parser stored them (a bullet's dash is markup on the page)."""
    world = await _world(clause_store)

    def words(text: str) -> str:
        return " ".join(w for w in text.split() if w != "-")

    async with world.client as client:
        for filename in EVERY_POLICY:
            clauses = _policy(filename)
            page = (await client.get(_path_and_anchor(source_href(clauses[0].clause_id))[0])).text
            shown = words(html.unescape(re.sub(r"<[^>]+>", " ", page)))
            for clause in clauses:
                assert words(clause.text) in shown, (filename, clause.clause_path)


# ---- tenant scope: a miss is the same 404 ----------------------------------------------


async def test_another_tenants_policy_is_not_found(clause_store: ClauseStore) -> None:
    world = await _world(clause_store)
    other = _policy(EVERY_POLICY[0], tenant_id="globex")
    await _publish(
        world.clauses, world.ledger, other, source=policy_source(EVERY_POLICY[0]), title="Globex"
    )
    async with world.client as client:
        response = await client.get(_path_and_anchor(source_href(other[0].clause_id))[0])
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
        assert response.headers["content-security-policy"] == SOURCE_CSP
        # A prefix that is the caller's tenant but a version that isn't theirs:
        # the same answer as a version that never existed.
        absent = await client.get("/v1/sources/acme/privacy-policy/v99")
        assert (absent.status_code, absent.json()) == (404, NOT_FOUND)
        theirs = await client.get(
            _path_and_anchor(source_href(_policy(EVERY_POLICY[0])[0].clause_id))[0]
        )
        assert theirs.status_code == 200


async def test_a_library_instrument_the_tenant_does_not_subscribe_to_is_not_found(
    clause_store: ClauseStore,
) -> None:
    world = await _world(clause_store)
    other = ClauseRecord.build(
        instrument_id="spam-act-2003",
        version_id="C2020C00001",
        clause_path="s16/1",
        text="A person must not send, or cause to be sent, an unsolicited commercial message.",
        scope=Scope.PUBLIC,
        tenant_id=PUBLIC_TENANT_ID,
    )
    await _publish(
        world.clauses,
        world.ledger,
        (other,),
        source='<html><body><p id="clause-s16-1">x</p></body></html>',
        title="Spam Act 2003",
        document_class=DocumentClass.LIBRARY_LAW,
    )
    path = _path_and_anchor(source_href(other.clause_id))[0]
    async with world.client as client:
        response = await client.get(path)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
        await world.ledger.subscribe(
            tenant_id=TENANT, instrument_id="spam-act-2003", subscribed_by="x"
        )
        assert (await client.get(path)).status_code == 200
        # Unsubscribing is the subscription row alone (D-17): the source goes
        # out of the tenant's environment again.
        await world.ledger.unsubscribe(tenant_id=TENANT, instrument_id="spam-act-2003")
        assert (await client.get(path)).status_code == 404


async def test_a_user_document_opens_for_its_owner_only(clause_store: ClauseStore) -> None:
    world = await _world(clause_store)
    mine = ClauseRecord.build(
        instrument_id="acme/my-notes",
        version_id="v1",
        clause_path="1/1",
        text="My private note about retention.",
        scope=Scope.USER,
        tenant_id=TENANT,
        user_id="uid-123",
    )
    await _publish(
        world.clauses,
        world.ledger,
        (mine,),
        source="## 1. Notes\n\n1.1 My private note about retention.\n",
        title="My notes",
        document_class=DocumentClass.EXTERNAL_UPLOAD,
    )
    path, anchor = _path_and_anchor(source_href(mine.clause_id))
    async with world.client as client:
        owner = await client.get(path)
        assert owner.status_code == 200 and f'id="{anchor}"' in owner.text
        other = await client.get(path, headers=bearer(COLLEAGUE))
        assert (other.status_code, other.json()) == (404, NOT_FOUND)


async def test_a_version_the_ledger_never_recorded_is_not_found(
    clause_store: ClauseStore,
) -> None:
    """A publish that wrote its clauses and source but stopped before the
    ledger's record isn't citable (ADR-0048), so it isn't openable either."""
    world = await _world(clause_store)
    clauses = _policy(EVERY_POLICY[0])
    unrecorded = tuple(
        c.model_copy(update={"version_id": "v2"})
        for c in clauses  # a half-published v2
    )
    await world.clauses.put_clauses(unrecorded)
    await world.clauses.put_manifest(
        unrecorded[0].instrument_id,
        "v2",
        tenant_id=TENANT,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html=policy_source(EVERY_POLICY[0]),
    )
    async with world.client as client:
        response = await client.get(f"/v1/sources/{unrecorded[0].instrument_id}/v2")
        assert response.status_code == 404


async def test_a_withdrawn_version_still_opens_so_an_old_citation_does(
    clause_store: ClauseStore,
) -> None:
    world = await _world(clause_store)
    clause = _policy(EVERY_POLICY[0])[0]
    await world.ledger.mark_withdrawn(
        tenant_id=TENANT,
        instrument_id=clause.instrument_id,
        version_id=clause.version_id,
        withdrawn_by="admin",
    )
    async with world.client as client:
        response = await client.get(_path_and_anchor(source_href(clause.clause_id))[0])
        assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/v1/sources/privacy-act-1988/..%2f..%2fetc",
        "/v1/sources/privacy-act-1988/bad version",
        "/v1/sources/_public/privacy-act-1988/C2026C00227",
        "/v1/sources/acme/a/b/v1",
        "/v1/sources/C2026C00227",
        "/v1/sources//v1",
        "/v1/sources/privacy-act-1988/C2026C00227/",
    ],
)
async def test_a_malformed_source_is_not_found_not_an_error(
    path: str, clause_store: ClauseStore
) -> None:
    world = await _world(clause_store)
    async with world.client as client:
        response = await client.get(path)
        assert response.status_code == 404
        assert response.headers["content-security-policy"] == SOURCE_CSP  # a 404 too


async def test_a_source_needs_a_signed_in_member() -> None:
    world = await _world(FakeClauseStore())
    async with world.client as client:
        response = await client.get(
            "/v1/sources/privacy-act-1988/C2026C00227", headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 401
        assert (
            await client.get("/v1/sources/x/y", headers={"Authorization": ""})
        ).status_code == 401


# ---- served HTML can't run anything ----------------------------------------------------


async def test_the_csp_is_present_and_no_script_survives_in_stored_law(
    clause_store: ClauseStore,
) -> None:
    world = await _world(clause_store)
    hostile = ClauseRecord.build(
        instrument_id="hostile-act",
        version_id="v1",
        clause_path="s1",
        text="Law text",
        scope=Scope.PUBLIC,
        tenant_id=PUBLIC_TENANT_ID,
    )
    await _publish(
        world.clauses,
        world.ledger,
        (hostile,),
        source=HOSTILE_LAW,
        title="Hostile Act",
        document_class=DocumentClass.LIBRARY_LAW,
    )
    await world.ledger.subscribe(tenant_id=TENANT, instrument_id="hostile-act", subscribed_by="x")
    async with world.client as client:
        response = await client.get("/v1/sources/hostile-act/v1")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == SOURCE_CSP
    assert "script-src" not in SOURCE_CSP and "sandbox" in SOURCE_CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    page = response.text
    body = page[page.index("<body>") :]
    assert not re.search(r"<\s*/?\s*(script|style|noscript|math|svg|img|iframe|meta)", body, re.I)
    assert not re.search(r"\son[a-z]+\s*=", page, re.IGNORECASE)
    assert "steal" not in page and "stolen" not in page and "evil.example" not in page
    assert "javascript" not in re.sub(r"\s", "", page).lower()
    assert 'id="clause-s1"' in page and 'id="clause-s2"' in page  # the anchors stay
    assert "Law text" in page  # the text stays


async def test_a_policy_page_escapes_whatever_its_document_says(
    clause_store: ClauseStore,
) -> None:
    world = await _world(clause_store)
    markdown = (
        "---\ntitle: x\n---\n\n# <script>steal()</script>\n\n"
        "<img src=x onerror=steal()>\n\n## 1. <b>Heading</b>\n\n"
        "1.1 <script>steal()</script> then *text* & more\n\n- <svg onload=steal()>\n"
    )
    clauses = parse_policy_markdown(
        markdown, instrument_id="acme/evil-policy", version_id="v1", tenant_id=TENANT
    )
    await _publish(
        world.clauses, world.ledger, clauses, source=markdown, title="Evil <script>x</script>"
    )
    async with world.client as client:
        response = await client.get("/v1/sources/acme/evil-policy/v1")
    page = response.text
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == SOURCE_CSP
    assert not re.search(r"<\s*/?\s*script", page, re.IGNORECASE)
    assert "<img" not in page and "<svg" not in page and "<b>" not in page
    assert "&lt;script&gt;steal()&lt;/script&gt; then *text* &amp; more" in page
    assert 'id="clause-1-1"' in page


# ---- the live event and the chat read-back carry the same href ---------------------------


async def test_the_chat_read_back_carries_the_same_href_as_the_live_event(
    clause_store: ClauseStore,
) -> None:
    # Retrieval is a fake over the golden chunks here, so the answer is asked
    # through the same helpers the answers tests use.
    from compliance_rag.eval.harness import golden_clause_store
    from tests.test_api_answers import (
        _client as answers_client,  # pyright: ignore[reportPrivateUsage]
    )
    from tests.test_api_answers import _events as events_of  # pyright: ignore[reportPrivateUsage]
    from tests.test_api_answers import _ledger as acme_ledger  # pyright: ignore[reportPrivateUsage]

    clause = next(c for c in golden_clauses() if c.clause_id == APP11)
    ledger = await acme_ledger()
    async with await answers_client(
        FakeGenerator(_reply((APP11, clause.canonical_text[:60]))),
        ledger,
        await golden_clause_store(),
    ) as client:
        response = await client.post(
            "/v1/answers", json={"question": QUESTION, "as_of": "2026-10-12"}
        )
        answer = dict(events_of(response.text))["answer"]
        chat = (await client.get(f"/v1/chats/{answer['chat_id']}")).json()
    (live,) = answer["citations"]
    (read_back,) = chat["turns"][1]["citations"]
    assert live["href"] == read_back["href"] == source_href(APP11)
