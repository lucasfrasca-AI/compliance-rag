"""The keep-alive workflows (issue #119; the Aura one is checked in
tests/test_neo4j_graph.py): each holds its own store's credentials and no
other workflow does, runs no repository code, and leaves nothing behind.

The Qdrant keep-alive's own script is also run, as GitHub runs it (`bash -e`):
against a real server where one is available (CI's service container), with
the collection `scripts/bootstrap_qdrant_collection.py` creates (#139), and
against a stub that answers with the statuses a local server can't give (a
refused or read-only key, a server error). Every failure names its fix.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient, models
from scripts import bootstrap_qdrant_collection

from compliance_rag.adapters.qdrant_retrieval_store import bootstrap_collection
from compliance_rag.settings import Settings
from tests.conftest import qdrant_reachable

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
QDRANT = WORKFLOWS / "qdrant-keepalive.yml"

_QDRANT_URL = os.environ.get("QDRANT_URL", "")
needs_qdrant = pytest.mark.skipif(
    not qdrant_reachable(_QDRANT_URL),
    reason="QDRANT_URL not set or not reachable — needs a real Qdrant server",
)


def _step() -> dict[str, Any]:  # Any: parsed YAML
    return yaml.safe_load(QDRANT.read_text())["jobs"]["keepalive"]["steps"][0]


def test_only_the_qdrant_keepalive_holds_qdrant_cloud_credentials() -> None:
    """CI's Qdrant is a service container: no other workflow names a Qdrant
    secret or the Qdrant Cloud host (the same guard as Aura's)."""
    cloud = re.compile(r"secrets\.\w*QDRANT|cloud\.qdrant\.io", re.IGNORECASE)
    for workflow in WORKFLOWS.glob("*.yml"):
        if workflow != QDRANT:
            assert not cloud.search(workflow.read_text()), workflow.name
    assert "secrets.QDRANT_KEEPALIVE_API_KEY" in QDRANT.read_text()


def test_the_qdrant_keepalive_runs_no_code_and_deletes_what_it_writes() -> None:
    text = QDRANT.read_text()
    run = str(_step()["run"])
    assert "actions/checkout" not in text and "setup-uv" not in text
    # The only `uv` is inside the 404 message: no line runs it.
    assert not re.search(r"^\s*uv\b", run, re.MULTILINE)
    assert "permissions: {}" in text
    assert re.search(r"POINT: (\S+)", text)
    # One fixed point: upserted without vectors, deleted, then checked gone.
    assert re.search(r"vector[^a-z]*:[^a-z]*\{\}", text)
    assert (
        text.index("-X PUT") < text.index("/delete?wait=true") < text.index('test "$status" = 404')
    )
    # The key never reaches argv: it goes to curl on stdin.
    assert "-K -" in text and "api-key: %s" in text
    # curl's own error text, which can name the host, is never printed.
    assert "curl -s " in run and "-sS" not in run


def test_the_keepalive_pings_the_collection_the_app_uses() -> None:
    assert _step()["env"]["COLLECTION"] == Settings.model_fields["qdrant_collection"].default


# ---- the script itself, run -----------------------------------------------------


def _keepalive(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """The workflow's one step, run as GitHub runs it (`bash -e {0}`), with
    its fixed env (the collection, the point) plus `env` for the secrets."""
    step = _step()
    fixed = {k: str(v) for k, v in step["env"].items() if "secrets." not in str(v)}
    script = tmp_path / "keepalive.sh"
    script.write_text(str(step["run"]))
    return subprocess.run(
        ["bash", "-e", str(script)],
        env={"PATH": os.environ["PATH"], **fixed, **env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_keepalive_fails_loudly_without_its_secrets(tmp_path: Path) -> None:
    result = _keepalive(tmp_path, URL="", API_KEY="")
    assert result.returncode != 0
    assert "secrets are not set" in result.stdout


def test_a_cluster_that_does_not_answer_is_reported_as_suspended(tmp_path: Path) -> None:
    """No HTTP answer at all (run 38350672418's TLS reset; here, a refused
    connection): the error says to resume the cluster, and curl's own error
    text, which can name the host, isn't printed."""
    result = _keepalive(tmp_path, URL="http://127.0.0.1:9", API_KEY="unused")
    assert result.returncode != 0
    assert "resume it in the Qdrant Cloud console" in result.stdout
    assert "127.0.0.1" not in result.stdout + result.stderr


@pytest.fixture
def stub() -> Iterator[tuple[str, dict[str, int]]]:
    """A local HTTP server answering each method with the status set in the
    yielded dict (200 by default): the statuses a key-less local Qdrant can't
    give."""
    statuses: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(statuses.get(self.command, 200))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","result":{}}')

        do_GET = do_PUT = do_POST = _answer  # http.server dispatches on these names

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", statuses
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("statuses", "message"),
    [
        pytest.param({"PUT": 403, "GET": 200}, "it is read-only", id="403-but-it-reads"),
        pytest.param({"PUT": 401, "GET": 401}, "wrong or rotated", id="401-everywhere"),
        pytest.param({"PUT": 403, "GET": 403}, "wrong or rotated", id="403-everywhere"),
        pytest.param({"PUT": 500}, "upsert: HTTP 500", id="server-error"),
        pytest.param({"GET": 200}, "still there after its delete", id="point-not-deleted"),
    ],
)
def test_each_failure_names_its_cause(
    tmp_path: Path, stub: tuple[str, dict[str, int]], statuses: dict[str, int], message: str
) -> None:
    url, answer = stub
    answer.update(statuses)
    result = _keepalive(tmp_path, URL=url, API_KEY="a-test-key")
    assert result.returncode != 0
    assert message in result.stdout, result.stdout
    assert "a-test-key" not in result.stdout + result.stderr


@asynccontextmanager
async def _qdrant() -> AsyncGenerator[AsyncQdrantClient]:
    client = AsyncQdrantClient(url=_QDRANT_URL)
    try:
        yield client
    finally:
        await client.close()


def _settings(collection: str = "clauses", *, url: str = _QDRANT_URL) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
        qdrant_url=url,
        qdrant_api_key=SecretStr(""),
        qdrant_collection=collection,
    )


@pytest.fixture
async def collection() -> AsyncIterator[str]:
    """A unique collection name, dropped afterwards if anything created it."""
    name = f"keepalive-test-{uuid.uuid4().hex[:8]}"
    yield name
    async with _qdrant() as client:
        await client.delete_collection(name)


@needs_qdrant
async def test_bootstrap_creates_the_production_shape_once_and_never_empties_it(
    collection: str,
) -> None:
    first = await bootstrap_collection(_settings(collection))
    assert first.created and first.points == 0
    assert set(first.payload_indexes) == {
        "clause_id",
        "ancestors",  # ADR-0035
        "tenant_id",
        "scope",
        "user_id",  # ADR-0036
        "instrument_id",
        "version_id",
        "valid_from",
        "valid_to",
        "withdrawn",
        "synthetic",
    }
    async with _qdrant() as client:
        params = (await client.get_collection(collection)).config.params
        assert isinstance(params.vectors, dict)
        dense = params.vectors["dense"]
        assert (dense.size, dense.distance) == (1536, models.Distance.COSINE)
        assert params.sparse_vectors is not None and "sparse" in params.sparse_vectors
        await client.upsert(
            collection, points=[models.PointStruct(id=1, vector={}, payload={"kept": True})]
        )
    again = await bootstrap_collection(_settings(collection))
    assert not again.created and again.points == 1
    assert again.payload_indexes == first.payload_indexes


@needs_qdrant
async def test_bootstrap_refuses_a_collection_of_another_shape(collection: str) -> None:
    async with _qdrant() as client:
        await client.create_collection(
            collection,
            vectors_config={"dense": models.VectorParams(size=8, distance=models.Distance.DOT)},
        )
    with pytest.raises(ValueError, match="another shape"):
        await bootstrap_collection(_settings(collection))


async def test_bootstrap_refuses_an_unset_url() -> None:
    """Never the client's localhost default."""
    with pytest.raises(ValueError, match="QDRANT_URL"):
        await bootstrap_collection(_settings(url=""))


async def test_bootstrap_says_when_the_key_is_refused(stub: tuple[str, dict[str, int]]) -> None:
    url, answer = stub
    answer.update(GET=403, PUT=403, POST=403)
    with pytest.raises(PermissionError, match="refused the API key"):
        await bootstrap_collection(_settings(url=url))


@needs_qdrant
async def test_the_script_prints_what_it_did_and_never_the_url(
    collection: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("QDRANT_URL", _QDRANT_URL)
    monkeypatch.setenv("QDRANT_API_KEY", "")
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    await bootstrap_qdrant_collection.main()
    await bootstrap_qdrant_collection.main()
    first, second = capsys.readouterr().out.splitlines()
    assert first.startswith(f"{collection}: created; 0 points; payload indexes: ")
    assert second.startswith(f"{collection}: already there; 0 points")
    assert _QDRANT_URL not in first + second


@needs_qdrant
async def test_the_keepalive_writes_and_deletes_in_a_bootstrapped_collection(
    collection: str, tmp_path: Path
) -> None:
    await bootstrap_collection(_settings(collection))
    result = _keepalive(tmp_path, URL=_QDRANT_URL, API_KEY="unused", COLLECTION=collection)
    assert result.returncode == 0, result.stdout + result.stderr
    async with _qdrant() as client:
        assert (await client.count(collection)).count == 0


@needs_qdrant
async def test_a_missing_collection_names_the_bootstrap(collection: str, tmp_path: Path) -> None:
    result = _keepalive(tmp_path, URL=_QDRANT_URL, API_KEY="unused", COLLECTION=collection)
    assert result.returncode != 0
    assert "HTTP 404" in result.stdout
    assert "uv run python -m scripts.bootstrap_qdrant_collection" in result.stdout
