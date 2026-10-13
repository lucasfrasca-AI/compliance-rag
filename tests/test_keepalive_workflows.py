"""The keep-alive workflows (issue #119; the Aura one is checked in
tests/test_neo4j_graph.py): each holds its own store's credentials and no
other workflow does, runs no repository code, and leaves nothing behind.

The Qdrant keep-alive's own script is also run, as GitHub runs it (`bash -e`),
against a real server where one is available (CI's service container), with
the collection `scripts/bootstrap_qdrant_collection.py` creates (#139): green
when it is there, and a failure that names the fix when it isn't.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient, models

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
    assert "actions/checkout" not in text and "uv " not in text
    assert "permissions: {}" in text
    point = re.search(r"POINT: (\S+)", text)
    assert point is not None
    # One fixed point: upserted without vectors, deleted, then checked gone.
    assert re.search(r"vector[^a-z]*:[^a-z]*\{\}", text)
    assert (
        text.index("-X PUT") < text.index("/delete?wait=true") < text.index('test "$status" = 404')
    )
    # The key never reaches argv: it goes to curl on stdin.
    assert "-K -" in text and "api-key: %s" in text
    # A refused key names itself; the other failures are run below.
    assert "401|403) fail" in text and "the API key was refused" in text


# ---- the script itself, run -----------------------------------------------------


def _keepalive(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """The workflow's one step, run as GitHub runs it (`bash -e {0}`), with
    its fixed env (the collection, the point) plus `env` for the secrets."""
    step = yaml.safe_load(QDRANT.read_text())["jobs"]["keepalive"]["steps"][0]
    fixed = {k: str(v) for k, v in step["env"].items() if "secrets." not in str(v)}
    script = tmp_path / "keepalive.sh"
    script.write_text(step["run"])
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
    connection): the error says to resume the cluster, not a bare curl code."""
    result = _keepalive(tmp_path, URL="http://127.0.0.1:9", API_KEY="unused")
    assert result.returncode != 0
    assert "resume it in the Qdrant Cloud console" in result.stdout


def _settings(collection: str) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
        qdrant_url=_QDRANT_URL,
        qdrant_api_key=SecretStr(""),
        qdrant_collection=collection,
    )


@pytest.fixture
async def collection() -> AsyncIterator[str]:
    """A unique collection name, dropped afterwards if anything created it."""
    name = f"keepalive-test-{uuid.uuid4().hex[:8]}"
    yield name
    client = AsyncQdrantClient(url=_QDRANT_URL)
    try:
        await client.delete_collection(name)
    finally:
        await client.close()


@needs_qdrant
async def test_bootstrap_creates_the_collection_once_and_never_empties_it(
    collection: str,
) -> None:
    first = await bootstrap_collection(_settings(collection))
    assert first.created and first.points == 0
    # Every payload index the adapter's filters need (ADR-0020, ADR-0023).
    assert {"clause_id", "tenant_id", "scope", "instrument_id", "valid_from", "valid_to"} <= set(
        first.payload_indexes
    )
    client = AsyncQdrantClient(url=_QDRANT_URL)
    try:
        await client.upsert(
            collection, points=[models.PointStruct(id=1, vector={}, payload={"kept": True})]
        )
    finally:
        await client.close()
    again = await bootstrap_collection(_settings(collection))
    assert not again.created and again.points == 1
    assert again.payload_indexes == first.payload_indexes


async def test_bootstrap_refuses_an_unset_url() -> None:
    """Never the client's localhost default."""
    unset = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg
        qdrant_url="",
    )
    with pytest.raises(ValueError, match="QDRANT_URL"):
        await bootstrap_collection(unset)


@needs_qdrant
async def test_the_keepalive_writes_and_deletes_in_a_bootstrapped_collection(
    collection: str, tmp_path: Path
) -> None:
    await bootstrap_collection(_settings(collection))
    result = _keepalive(tmp_path, URL=_QDRANT_URL, API_KEY="unused", COLLECTION=collection)
    assert result.returncode == 0, result.stdout + result.stderr
    client = AsyncQdrantClient(url=_QDRANT_URL)
    try:
        assert (await client.count(collection)).count == 0
    finally:
        await client.close()


@needs_qdrant
async def test_a_missing_collection_names_the_bootstrap(collection: str, tmp_path: Path) -> None:
    result = _keepalive(tmp_path, URL=_QDRANT_URL, API_KEY="unused", COLLECTION=collection)
    assert result.returncode != 0
    assert "HTTP 404" in result.stdout and "bootstrap_qdrant_collection" in result.stdout
