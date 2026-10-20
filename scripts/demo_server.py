"""THROWAWAY DEMO (#247): a local-only server for `demo/index.html`, one page
over the production `POST /v1/answers`. Deliberately disposable: it prejudges
nothing about Phase 3's UI (upload flows, findings, I2's rendering). It is
never deployed, and nothing in `src/` imports it.

    docker run -d --name crag-demo-qdrant -p 6333:6333 \
        -e QDRANT__TELEMETRY_DISABLED=true qdrant/qdrant:v1.19.1
    uv run python -m scripts.demo_server --fake-generator \
        --bucket compliance-rag-508904-corpus              # no model is called
    VERTEX_CLAUDE_MODEL=claude-sonnet-5 VERTEX_CLAUDE_REGION=us \
    OFFSHORE_GENERATION_TESTERS=local-demo@acme.example \
        uv run python -m scripts.demo_server --bucket compliance-rag-508904-corpus

The real run sends the question offshore only with the demo member listed
as a tester (ADR-0032); leave the list out and the page shows the gate's
refusal instead. Then open http://127.0.0.1:8765/. Needs a local Qdrant (`--qdrant-url`, the
corpus is loaded into it at start), the reranker's ONNX model
(`models/reranker-onnx/`, or `--reranker-onnx`), and Application Default
Credentials with `GCP_PROJECT_ID` set (the question is embedded in Sydney, and
law source HTML is read from the corpus bucket, `--bucket` or `CORPUS_BUCKET`).

**The app is production's.** `api.app.create_app` with its own routers, so the
answer comes from the same `answer()`, verifier, `answers.citation_json` and
display form (ADR-0049) a deployment uses. One thing differs: whoever asks is
a fixed local member of Acme (`DEMO_MEMBER`), by a dependency override on this
app instance alone. `api/main.py` and `deps.current_member` are untouched, so
the production app still requires sign-in (`tests/test_demo_server.py`). With
no sign-in, the server binds 127.0.0.1 and nothing else, and won't start where
a production marker says it is deployed (`refuse_unless_local`).

**What it runs on:**
- **Retrieval:** a throwaway collection on a *local* Qdrant (a remote URL is
  refused), loaded at start with the gate corpus and dropped on exit. That is
  the whole compilation's selected clauses and Acme's six synthetic policies,
  marked synthetic from the registry. Chunk vectors come from the checked-in
  `gemini-embedding-001` snapshot. The question, which no snapshot holds, is
  embedded by `VertexEmbedder` in australia-southeast1. The reranker is the
  int8 cross-encoder, in process.
- **Clause text:** the gate corpus's canonical text, in memory: the text its
  chunks were made from, so the verifier checks against what was retrieved.
- **Ledger:** in memory. Every corpus version is recorded with its title, and
  Acme has a profile, a subscription to the Privacy Act and the facts of
  `DEMO_PROFILE`, the signed applicability set's base Acme profile. Chats last
  as long as the process.
- **Graph:** Track A in process over the signed mappings (`FakeGraphStore`:
  the snapshot and evaluator `GraphTrackA` uses).
- **Law source HTML:** the corpus bucket, read only, through
  `GCSClauseStore.get_source_html`.
- **Generation:** Claude on Vertex, built by `VertexClaudeGenerator.create`, so
  the residency gate runs first (ADR-0023, ADR-0024). Offshore, ADR-0032 holds
  as it does in production: the demo member's question is sent only if
  `OFFSHORE_GENERATION_TESTERS` names `DEMO_MEMBER`'s email, which this server
  never adds for you. Until the US quota is raised, the call fails with a 429.
  The API reports that as `internal`, as it must, so the page asks
  `/demo/failures/<trace id>` what failed and shows it plainly.
- **`--fake-generator`:** an in-process stand-in that quotes the top-ranked
  retrieved clause and Acme's first policy clause verbatim, so the page can be
  exercised. It is never the default, and the page carries a "FAKE GENERATOR —
  not a real answer" banner whenever it is on. It runs in this process, so
  generation isn't offshore, and the gates are given Sydney as its region. A
  question containing "withhold" makes it misquote, to show a withheld answer.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import re
from collections import OrderedDict
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from compliance_rag import telemetry
from compliance_rag.adapters.fakes import FakeClauseStore, FakeGraphStore, FakeLedgerStore
from compliance_rag.adapters.generation_residency import TenantTextGate, is_offshore
from compliance_rag.adapters.interfaces import (
    ClauseStore,
    Embedder,
    Generation,
    Generator,
    GraphStore,
    Member,
    Profile,
    TokenVerifier,
    Unauthenticated,
)
from compliance_rag.api.app import create_app
from compliance_rag.api.deps import ApiServices, current_member
from compliance_rag.assessment.pipeline import AssessmentPipeline
from compliance_rag.assessment.prompt import REPAIR_SYSTEM_PROMPT
from compliance_rag.corpus.incomplete_reading import committed_incomplete_readings
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    VERSION_ID_RE,
    ClauseRecord,
    clause_anchor_id,
    instrument_tenant_prefix,
    parse_stable_id,
    validate_library_instrument_id,
)
from compliance_rag.eval.applicability import ApplicabilityCase, load_applicability_set
from compliance_rag.eval.embedding_cache import EmbeddingCache, text_key
from compliance_rag.eval.golden_corpus import (
    LAW_INSTRUMENT_ID,
    POLICY_TENANT_ID,
    gate_chunks,
    gate_clauses,
    gate_law_clauses,
)
from compliance_rag.eval.harness import golden_ledger
from compliance_rag.graph.mappings import load_mapping_set
from compliance_rag.retrieval.pipeline import RetrievalPipeline
from compliance_rag.settings import REGION, Settings
from compliance_rag.telemetry import current_trace_id
from compliance_rag.verifier.citations import CanonicalCitationVerifier, Verifier

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "demo" / "index.html"
MAPPINGS = REPO / "corpus" / "mappings"

LOCAL_HOST = "127.0.0.1"
"""The only address the demo binds: it has no sign-in."""
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_PORT = 8765
DEFAULT_QDRANT = "http://127.0.0.1:6333"

DEMO_TENANT = POLICY_TENANT_ID
DEMO_MEMBER = Member(uid="local-demo-member", email="local-demo@acme.example")
"""Whoever uses the page. Not a tester unless the operator lists this email in
`OFFSHORE_GENERATION_TESTERS` (ADR-0032)."""
DEMO_PROFILE = "GA-01"
"""The applicability set's base Acme profile: every APP applies but the two
that bind only agencies."""
_SETUP_TRACE = "de30" + "0" * 28
"""The trace id the demo's own setup records Acme's facts under."""

FAKE_BANNER = "FAKE GENERATOR — not a real answer"
BANNER_SLOT = "<!-- demo:fake-banner -->"
POLICY_HAS_NO_ANCHOR = (
    "A policy has no anchor in a stored source: how policies are rendered is I2's "
    "open question. The demo shows the clause's canonical text instead."
)
_SOURCE_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
"""The stored source is served as a document, never as a script: no script in
it runs, and it loads nothing."""
_TARGET_STYLE = (
    "<style>:target{background:#fff1b8;outline:3px solid #d4a017;scroll-margin-top:4rem}</style>"
)


class RefusedToStart(RuntimeError):
    """The demo runs on a laptop, bound to loopback, and nowhere else."""


def refuse_unless_local(host: str, settings: Settings) -> None:
    """Before anything is built: 127.0.0.1 only, and never where the process
    is deployed. The demo has no sign-in, so either would serve answers to
    anyone who can reach it."""
    if host != LOCAL_HOST:
        raise RefusedToStart(
            f"the demo has no sign-in, so it binds {LOCAL_HOST} only, never {host!r}"
        )
    if settings.production_markers:
        raise RefusedToStart(
            "the demo is local only, and "
            f"{', '.join(settings.production_markers)} says this process is deployed"
        )


def refuse_unless_local_qdrant(url: str) -> None:
    """The demo loads a throwaway collection at start: never into the deployed
    cluster."""
    if urlparse(url).hostname not in _LOOPBACK:
        raise RefusedToStart(f"the demo loads its corpus into a local Qdrant only, not {url!r}")


# --- What a request's failure was, for the page -----------------------------

_TOKEN_RE = re.compile(r"ya29\.[\w.-]+|Bearer\s+[\w.~+/=-]+", re.IGNORECASE)
_FAILURE_CHARS = 800


def describe(error: BaseException) -> str:
    """An exception as one line the page can show: its type and message,
    with anything shaped like an access token removed, and cut short."""
    text = _TOKEN_RE.sub("[redacted]", f"{type(error).__name__}: {error}")
    return text if len(text) <= _FAILURE_CHARS else text[:_FAILURE_CHARS] + "…"


@dataclass
class FailureLog:
    """What failed in a request, by its trace id. The API's `error` event
    says only "internal" for a failure it doesn't recognise, rightly, so the
    page asks here what it was: today, the zero-quota 429. In memory, local,
    the most recent requests only."""

    limit: int = 50
    _by_trace: OrderedDict[str, list[dict[str, str]]] = field(
        default_factory=OrderedDict[str, list[dict[str, str]]]
    )

    def record(self, where: str, error: BaseException) -> None:
        trace_id = current_trace_id()
        self._by_trace.setdefault(trace_id, []).append({"where": where, "error": describe(error)})
        self._by_trace.move_to_end(trace_id)
        while len(self._by_trace) > self.limit:
            self._by_trace.popitem(last=False)

    def of(self, trace_id: str) -> list[dict[str, str]]:
        return list(self._by_trace.get(trace_id, ()))


class RecordingGenerator(Generator):
    """The real generator, its failure noted for the page and re-raised."""

    def __init__(self, inner: Generator, failures: FailureLog) -> None:
        self._inner = inner
        self._failures = failures

    async def generate(
        self, *, system: str, cached_prefix: str | None, user: str, max_tokens: int
    ) -> Generation:
        try:
            return await self._inner.generate(
                system=system, cached_prefix=cached_prefix, user=user, max_tokens=max_tokens
            )
        except Exception as error:
            self._failures.record("generation (Claude on Vertex)", error)
            raise


class SnapshotThenSydney(Embedder):
    """A chunk's vector from the checked-in snapshot, which holds what
    `VertexEmbedder` returned for it; any other text (the question) from
    `fresh`, `VertexEmbedder` in australia-southeast1. Loading the corpus
    therefore makes no call, and a question makes one."""

    def __init__(self, cache: EmbeddingCache, fresh: Embedder, failures: FailureLog) -> None:
        self._cache = cache
        self._fresh = fresh
        self._failures = failures

    @property
    def dimensions(self) -> int:
        return self._cache.dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        missing = tuple(dict.fromkeys(self._cache.missing(texts)))
        fresh: dict[str, tuple[float, ...]] = {}
        if missing:
            try:
                fresh = dict(zip(missing, await self._fresh.embed(missing), strict=True))
            except Exception as error:
                self._failures.record(
                    "embedding the question (Vertex, australia-southeast1)", error
                )
                raise
        return tuple(fresh[t] if t in fresh else self._cache.vectors[text_key(t)] for t in texts)


# --- The fake generator, for exercising the page -----------------------------

# The fence and question format `assessment/prompt.py` writes (`fence`,
# `user_prompt`). Should it change, the fake cites nothing and
# tests/test_demo_server.py's end-to-end test fails.
_BLOCK_RE = re.compile(
    r"\[(?P<boundary>[0-9a-f]{24}) clause (?P<clause_id>\S+)[^\n]*\]\n"
    r"(?P<text>.*?)\n\[(?P=boundary) end\]",
    re.DOTALL,
)
_QUOTE_CHARS = 120
_MISQUOTE = "words the fake generator made up, which no clause contains"


def _fenced(prompt: str | None) -> list[tuple[str, str]]:
    """Each fenced clause in a prompt, in order: its id and its text."""
    return [(m["clause_id"], m["text"]) for m in _BLOCK_RE.finditer(prompt or "")]


def _opening(text: str) -> str:
    """A verbatim quote: the clause's opening, cut at a word."""
    if len(text) <= _QUOTE_CHARS:
        return text
    return text[:_QUOTE_CHARS].rsplit(" ", 1)[0]


class FakeDemoGenerator(Generator):
    """`--fake-generator` only. No model is called: it reads the clauses the
    prompt fences and quotes the top-ranked retrieved clause and the first
    clause of the tenant's policies verbatim, so the verifier, the citations
    and click-to-clause can be exercised. A question containing "withhold"
    makes it misquote, and it never repairs, so the answer is withheld
    (ADR-0003)."""

    async def generate(
        self, *, system: str, cached_prefix: str | None, user: str, max_tokens: int
    ) -> Generation:
        if system == REPAIR_SYSTEM_PROMPT:
            clause_id = next(iter(_fenced(user)), ("", ""))[0]
            reply: dict[str, object] = {"clause_id": clause_id, "quote": _MISQUOTE}
        else:
            question = user.rsplit("\nQuestion: ", 1)[-1]
            withhold = "withhold" in question.lower()
            quoted = [*_fenced(user)[:1], *_fenced(cached_prefix)[:1]]
            reply = {
                "answer": f"{FAKE_BANNER}. No model was called. This stand-in quotes "
                + (
                    "words no clause contains, because the question says 'withhold', "
                    "so the verifier blocks the answer."
                    if withhold
                    else "the top-ranked retrieved clause and the first clause of Acme's "
                    "policies verbatim, so the citations, the verifier and "
                    "click-to-clause can be exercised."
                ),
                "citations": [
                    {"clause_id": clause_id, "quote": _MISQUOTE if withhold else _opening(text)}
                    for clause_id, text in quoted
                ],
            }
        return Generation(
            text=json.dumps(reply),
            input_tokens=0,
            output_tokens=0,
            stop_reason="end_turn",
            model="fake-demo-generator",
        )


# --- The app ------------------------------------------------------------------


class NoSignIn(TokenVerifier):
    """The demo app's verifier, never consulted: `current_member` is
    overridden on that app alone. Should anything reach it, it refuses."""

    async def verify(self, token: str) -> Member:
        raise Unauthenticated("the demo has no sign-in")

    async def aclose(self) -> None:
        pass


async def demo_member() -> Member:
    return DEMO_MEMBER


def render_page(*, fake_generator: bool) -> str:
    """The page, with the fake banner written in by the server whenever the
    fake generator is on: it doesn't depend on the page's script running."""
    page = PAGE.read_text(encoding="utf-8")
    if page.count(BANNER_SLOT) != 1:
        raise RefusedToStart(f"{PAGE} must hold exactly one {BANNER_SLOT}")
    if not fake_generator:
        return page
    banner = (
        f'<div class="fake-banner" role="alert" id="fake-banner">{FAKE_BANNER}. '
        "Every answer on this page was written by an in-process stand-in, not a model.</div>"
    )
    return page.replace(BANNER_SLOT, banner)


def _with_target_highlight(html: str) -> str:
    """The stored source as stored, plus a style marking the clause the link
    opened at. The stored object is unchanged."""
    head = html.find("</head>")
    return html[:head] + _TARGET_STYLE + html[head:] if head >= 0 else _TARGET_STYLE + html


def create_demo_app(
    services: ApiServices,
    *,
    law_sources: ClauseStore,
    failures: FailureLog,
    fake_generator: bool,
) -> FastAPI:
    """Production's app over `services`, answering as `DEMO_MEMBER`, plus the
    page and the three things it needs: where a citation opens, the stored
    law source, and what a failed request's failure was."""
    app = create_app(services)
    app.dependency_overrides[current_member] = demo_member
    page = render_page(fake_generator=fake_generator)
    sources: dict[tuple[str, str], str] = {}

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction] — a route
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/demo/config")
    async def config() -> dict[str, object]:  # pyright: ignore[reportUnusedFunction] — a route
        region = services.settings.vertex_claude_region
        return {
            "fake_generator": fake_generator,
            "tenant_id": services.settings.tenant_id,
            "member": DEMO_MEMBER.email,
            "generation": "fake, in process"
            if fake_generator
            else f"{services.settings.vertex_claude_model or '(no model configured)'} at {region}"
            + (" (offshore: ADR-0032's tester list applies)" if is_offshore(region) else ""),
        }

    @app.get("/demo/open/{clause_id:path}")
    async def open_clause(clause_id: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction] — a route
        """Where a citation opens: for law, the stored source at the clause's
        anchor (ADR-0019), computed by the one `clause_anchor_id`."""
        try:
            instrument_id, version_id, path = parse_stable_id(clause_id)
        except ValueError:
            raise HTTPException(404, "not a clause id") from None
        if instrument_tenant_prefix(instrument_id) is not None:
            return {"clause_id": clause_id, "href": None, "why": POLICY_HAS_NO_ANCHOR}
        anchor = clause_anchor_id(path)
        href = f"/demo/source/{instrument_id}/{version_id}#{anchor}"
        return {"clause_id": clause_id, "href": href, "why": None}

    @app.get("/demo/source/{instrument_id}/{version_id}", include_in_schema=False)
    async def source(instrument_id: str, version_id: str) -> HTMLResponse:  # pyright: ignore[reportUnusedFunction] — a route
        """A library instrument version's stored, annotated source, read
        through `ClauseStore` once and kept."""
        try:
            validate_library_instrument_id(instrument_id)
        except ValueError:
            raise HTTPException(404, "not a library instrument") from None
        if not VERSION_ID_RE.match(version_id):
            raise HTTPException(404, "not a version id")
        key = (instrument_id, version_id)
        if key not in sources:
            with telemetry.span(
                "demo.source_html", instrument_id=instrument_id, version_id=version_id
            ):
                html = await law_sources.get_source_html(
                    instrument_id, version_id, tenant_id=PUBLIC_TENANT_ID
                )
            if html is None:
                raise HTTPException(404, "no stored source for that version")
            sources[key] = html
        return HTMLResponse(
            _with_target_highlight(sources[key]),
            headers={"Content-Security-Policy": _SOURCE_CSP},
        )

    @app.get("/demo/failures/{trace_id}")
    async def failures_of(trace_id: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction] — a route
        return {"trace_id": trace_id, "failures": failures.of(trace_id)}

    return app


# --- What it runs on -----------------------------------------------------------


def demo_profile() -> ApplicabilityCase:
    return next(c for c in load_applicability_set().cases if c.id == DEMO_PROFILE)


async def demo_ledger() -> FakeLedgerStore:
    """Every corpus version recorded with its title (ADR-0049), and Acme: its
    profile, its subscription to the Act, and `DEMO_PROFILE`'s facts."""
    ledger = await golden_ledger()
    await ledger.put_profile(Profile(DEMO_TENANT))
    await ledger.subscribe(
        tenant_id=DEMO_TENANT, instrument_id=LAW_INSTRUMENT_ID, subscribed_by="the demo server"
    )
    for evidence in demo_profile().evidence():
        await ledger.record_evidence(dataclasses.replace(evidence, trace_id=_SETUP_TRACE))
    return ledger


def demo_graph() -> FakeGraphStore:
    """Track A in process over every signed mapping file, knowing which
    clauses the stored corpus marks incomplete (ADR-0047), as the graph does.
    A mapping file still awaiting review is refused, as
    `scripts/load_mappings.py` refuses it, and an unsigned `incomplete.yaml`
    as `api/main.py` refuses it (D-01)."""
    committed_incomplete_readings()
    mappings = load_mapping_set(sorted(MAPPINGS.glob("*.yaml")))
    pending = [f.reviewed_by for f in mappings.files if f.reviewed_by.startswith("PENDING")]
    if pending:
        raise RefusedToStart(f"{len(pending)} mapping file(s) await review (D-01)")
    validity = load_applicability_set().clause_validity(set(mappings.clause_ids))
    incomplete = {c.clause_id: c.incomplete for c in gate_law_clauses() if c.incomplete}
    return FakeGraphStore(mappings, clause_validity=validity, incomplete=incomplete)


async def clause_store(clauses: Sequence[ClauseRecord]) -> FakeClauseStore:
    store = FakeClauseStore()
    await store.put_clauses(clauses)
    return store


def assemble(
    settings: Settings,
    *,
    ledger: FakeLedgerStore,
    clauses: ClauseStore,
    graph: GraphStore,
    retrieval: RetrievalPipeline,
    generator: Generator,
) -> ApiServices:
    """The demo's services, wired as `api/main.py` wires production's: the
    real verifier and the synthetic-text gate (ADR-0032) around `answer()`."""
    return ApiServices(
        settings=settings,
        verifier=NoSignIn(),
        ledger=ledger,
        graph=graph,
        clauses=clauses,
        pipeline=AssessmentPipeline(
            retrieval,
            generator,
            Verifier(clauses, ledger, CanonicalCitationVerifier()),
            ledger,
            tenant_text=TenantTextGate.from_settings(settings),
        ),
    )


@asynccontextmanager
async def local_services(
    settings: Settings, *, fake_generator: bool, onnx_path: Path | None, failures: FailureLog
) -> AsyncGenerator[tuple[ApiServices, ClauseStore]]:
    """The demo's services on the stores the module docstring names, and the
    clause store law source HTML is read from. Real adapters are imported
    here, not at module level, so the tests compose fakes without them."""
    from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
    from compliance_rag.adapters.qdrant_retrieval_store import QdrantRetrievalStore
    from compliance_rag.adapters.vertex_embedder import VertexEmbedder
    from compliance_rag.eval.harness import in_process_reranker
    from compliance_rag.reranker_service.model import RerankerModel

    refuse_unless_local_qdrant(settings.qdrant_url)
    if not settings.corpus_bucket:
        raise RefusedToStart(
            "no corpus bucket (--bucket or CORPUS_BUCKET): law source HTML is read from it"
        )
    # Before the corpus is loaded, so missing credentials fail in a second.
    law_sources = GCSClauseStore.for_bucket(
        settings.corpus_bucket, project=settings.gcp_project_id or None
    )
    ledger = await demo_ledger()
    embedder = SnapshotThenSydney(
        EmbeddingCache.load(), VertexEmbedder.from_settings(settings), failures
    )
    model = RerankerModel(onnx_path) if onnx_path is not None else RerankerModel()
    async with (
        QdrantRetrievalStore.ephemeral(
            settings.qdrant_url, embedder=embedder, ledger=ledger, prefix="demo"
        ) as store,
        in_process_reranker(model) as reranker,
    ):
        await store.upsert_chunks(gate_chunks())
        if fake_generator:
            generator: Generator = FakeDemoGenerator()
        else:
            from compliance_rag.adapters.vertex_claude_generator import VertexClaudeGenerator

            real = await VertexClaudeGenerator.create(settings, corpus=store)
            generator = RecordingGenerator(real, failures)
        services = assemble(
            settings,
            ledger=ledger,
            clauses=await clause_store(gate_clauses()),
            graph=demo_graph(),
            retrieval=RetrievalPipeline(store, reranker),
            generator=generator,
        )
        yield services, law_sources


def demo_settings(*, qdrant_url: str, fake_generator: bool, bucket: str | None = None) -> Settings:
    """`Settings` as configured, on Acme and the local Qdrant, and on
    `bucket` if one is named (as the other operator scripts take `--bucket`).
    The fake generator runs in this process, so the gates are told generation
    is in Sydney: what they ask is where the generator runs."""
    settings = Settings(tenant_id=DEMO_TENANT, qdrant_url=qdrant_url)
    if bucket:
        settings = settings.model_copy(update={"corpus_bucket": bucket})
    if fake_generator:
        return settings.model_copy(update={"vertex_claude_region": REGION})
    return settings


async def serve(
    settings: Settings, *, host: str, port: int, fake_generator: bool, onnx_path: Path | None
) -> None:
    # Again here, as well as in `main`: whatever calls `serve` is refused too.
    refuse_unless_local(host, settings)
    failures = FailureLog()
    async with local_services(
        settings, fake_generator=fake_generator, onnx_path=onnx_path, failures=failures
    ) as (services, law_sources):
        app = create_demo_app(
            services, law_sources=law_sources, failures=failures, fake_generator=fake_generator
        )
        await uvicorn.Server(uvicorn.Config(app, host=host, port=port)).serve()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="THROWAWAY local demo over /v1/answers (#247)")
    parser.add_argument("--host", default=LOCAL_HOST, help=f"refused unless {LOCAL_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT, help="a local Qdrant only")
    parser.add_argument("--reranker-onnx", type=Path, default=None, help="the int8 model.onnx")
    parser.add_argument(
        "--bucket", default=None, help="the corpus bucket, read only (default: CORPUS_BUCKET)"
    )
    parser.add_argument(
        "--fake-generator",
        action="store_true",
        help="answer with an in-process stand-in, never a model; the page says so",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    settings = demo_settings(
        qdrant_url=args.qdrant_url, fake_generator=args.fake_generator, bucket=args.bucket
    )
    refuse_unless_local(args.host, settings)
    refuse_unless_local_qdrant(settings.qdrant_url)
    telemetry.configure(settings)
    if args.fake_generator:
        print(f"*** {FAKE_BANNER}: answers come from an in-process stand-in ***", flush=True)
    elif (
        is_offshore(settings.vertex_claude_region)
        and DEMO_MEMBER.email not in settings.offshore_testers
    ):
        print(
            f"Generation runs at {settings.vertex_claude_region}, outside Australia, and "
            f"{DEMO_MEMBER.email} isn't in OFFSHORE_GENERATION_TESTERS: every question will "
            "be refused by the residency gate (ADR-0032) until the operator lists it.",
            flush=True,
        )
    print(
        f"Loading the gate corpus into {settings.qdrant_url}, then http://{LOCAL_HOST}:{args.port}/",
        flush=True,
    )
    asyncio.run(
        serve(
            settings,
            host=args.host,
            port=args.port,
            fake_generator=args.fake_generator,
            onnx_path=args.reranker_onnx,
        )
    )


if __name__ == "__main__":
    main()
