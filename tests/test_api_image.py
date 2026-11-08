"""The API's container image (issue #292), checked without building it.

CI doesn't build images (Actions minutes), so what a build and a run prove is
done by hand and stated in the PR. What can be pinned here is pinned: that the
Dockerfile copies every file the API reads through a repo-relative path (a
missing one would only fail at the first request, in the cloud), that it runs
as a non-root user under `uvicorn --factory`, that it bakes the BM25 model the
store looks for, and that the build context leaves out `.env`, `.git`, `models/`,
`.venv` and the tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from compliance_rag.adapters import qdrant_retrieval_store
from compliance_rag.assessment.environment import INSTRUMENTS_ROOT, REGISTER_PATH
from compliance_rag.corpus.reranker_tokenizer import (
    _TOKENIZER_MODEL,  # pyright: ignore[reportPrivateUsage]
)
from compliance_rag.corpus.synthetic_registry import SYNTHETIC_REGISTRY_PATH

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "api.Dockerfile"
API_IGNORE = ROOT / "docker" / "api.Dockerfile.dockerignore"
ROOT_IGNORE = ROOT / ".dockerignore"


def _instructions(name: str) -> list[str]:
    """The arguments of each `name` instruction, continuation lines joined."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    return [
        line.split(None, 1)[1].strip()
        for line in text.splitlines()
        if line.split(None, 1)[:1] == [name]
    ]


def _copied() -> list[tuple[str, str]]:
    """Each `COPY <source> <destination>`, relative to the repo root and /app."""
    pairs: list[tuple[str, str]] = []
    for args in _instructions("COPY"):
        *sources, destination = args.split()
        pairs.extend((source, destination) for source in sources)
    return pairs


def _ignored(path: str, patterns: list[str]) -> bool:
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in patterns)


def _patterns(ignore_file: Path) -> list[str]:
    return [
        line.strip()
        for line in ignore_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_it_follows_the_reranker_image() -> None:
    assert _instructions("FROM") == ["python:3.12-slim"]
    sync = [i for i in _instructions("RUN") if i.startswith("uv sync")]
    assert sync and all("--frozen" in s and "--no-dev" in s for s in sync)


def test_it_runs_as_a_non_root_user() -> None:
    users = _instructions("USER")
    assert users == ["app"]
    assert "useradd" in " ".join(_instructions("RUN"))


def test_it_serves_the_app_factory_on_the_port_cloud_run_injects() -> None:
    (cmd,) = _instructions("CMD")
    assert "uvicorn --factory compliance_rag.api.main:build_app" in cmd
    assert "--port ${PORT" in cmd  # shell form, so Cloud Run's PORT expands
    assert cmd.startswith("exec ")  # uvicorn is PID 1 and gets SIGTERM


def _same_place(source: str, destination: str) -> bool:
    return destination.removeprefix("./").rstrip("/") == source.rstrip("/")


@pytest.mark.parametrize(
    "read_at_runtime",
    [INSTRUMENTS_ROOT, REGISTER_PATH, SYNTHETIC_REGISTRY_PATH, _TOKENIZER_MODEL],
    ids=["instruments", "register", "synthetic-registry", "reranker-tokenizer"],
)
def test_every_repo_relative_file_the_api_reads_is_copied_to_the_same_place(
    read_at_runtime: Path,
) -> None:
    """The code finds these by `Path(__file__).parents[3]`, the repo root, which
    in the image is /app: so each is copied to the same path under /app."""
    wanted = read_at_runtime.relative_to(ROOT).as_posix()
    covering = [
        source
        for source, destination in _copied()
        if _same_place(source, destination)
        and (wanted == source.rstrip("/") or wanted.startswith(source.rstrip("/") + "/"))
    ]
    assert covering, f"{wanted} is read at runtime and nothing copies it to the same place"
    assert read_at_runtime.exists()


def test_the_source_of_every_copy_exists_and_is_not_in_the_build_context_ignore() -> None:
    ignored = _patterns(API_IGNORE)
    for source, destination in _copied():
        if source in ("pyproject.toml", "uv.lock"):
            continue
        assert (ROOT / source).exists(), source
        assert not _ignored(source.rstrip("/"), ignored), f"{source} is ignored, so COPY fails"
        assert _same_place(source, destination), (source, destination)


def test_it_bakes_the_bm25_model_where_the_store_looks_and_then_stays_offline() -> None:
    run = " ".join(_instructions("RUN"))
    assert qdrant_retrieval_store._SPARSE_MODEL_NAME in run  # pyright: ignore[reportPrivateUsage]
    cache = qdrant_retrieval_store._SPARSE_CACHE_DIR  # pyright: ignore[reportPrivateUsage]
    assert f"cache_dir='{cache}'" in run  # relative: /app/.fastembed_cache, the WORKDIR's
    assert _instructions("WORKDIR") == ["/app"]
    assert f"/app/{cache}" in run  # the user that serves can write to it
    assert "HF_HUB_OFFLINE=1" in _instructions("ENV")


def test_the_api_build_context_leaves_out_secrets_history_models_the_venv_and_tests() -> None:
    ignored = _patterns(API_IGNORE)
    for path in (".env", ".git", "models", ".venv", "tests", ".claude"):
        assert _ignored(path, ignored), path
    # `.env.local` and the like too, and no exception puts one back
    assert {".env.*", "**/.env", "**/.env.*", "**/__pycache__"} <= set(ignored)
    assert not any(p.startswith("!") for p in ignored)


def test_the_root_ignore_has_the_same_exclusions_but_for_the_reranker_s_models() -> None:
    root, api = _patterns(ROOT_IGNORE), _patterns(API_IGNORE)
    assert not _ignored("models/reranker-onnx", root)  # docker/reranker.Dockerfile copies it
    assert [p for p in api if p != "models"] == root
    assert re.search(
        r"COPY models/reranker-onnx", (ROOT / "docker/reranker.Dockerfile").read_text()
    )
    for path in (".env", ".git", ".venv", "tests"):
        assert _ignored(path, root), path


# ---- the BM25 model, loaded by path -----------------------------------------------------------


def _snapshot(cache: Path, *, files: tuple[str, ...]) -> Path:
    snapshot = cache / "models--Qdrant--bm25" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    for name in files:
        (snapshot / name).write_text("x", encoding="utf-8")
    return snapshot


def test_a_cached_bm25_snapshot_is_found_only_when_whole(tmp_path: Path) -> None:
    find = qdrant_retrieval_store._cached_sparse_model  # pyright: ignore[reportPrivateUsage]
    assert find(str(tmp_path / "nowhere")) is None
    assert find(str(tmp_path)) is None  # an empty cache: downloaded as before
    # Without the English stopword list fastembed quietly uses none, changing
    # every score: not whole.
    _snapshot(tmp_path, files=("config.json", "german.txt"))
    assert find(str(tmp_path)) is None
    whole = _snapshot(tmp_path / "other", files=("config.json", "english.txt"))
    assert find(str(tmp_path / "other")) == str(whole)


def test_loading_by_path_gives_the_same_vectors_with_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the image relies on: with `HF_HUB_OFFLINE=1`, fastembed's own cache
    lookup can't load `Qdrant/bm25` (its `model_file` is a placeholder that
    exists in no snapshot); loading the baked snapshot by path can, and scores
    exactly as loading it by name does."""
    from fastembed import SparseTextEmbedding

    cache = qdrant_retrieval_store._SPARSE_CACHE_DIR  # pyright: ignore[reportPrivateUsage]
    snapshot = qdrant_retrieval_store._cached_sparse_model(cache)  # pyright: ignore[reportPrivateUsage]
    if snapshot is None:
        pytest.skip("no local .fastembed_cache: the sparse tests download it, this one doesn't")
    text = "Acme must take reasonable steps to protect personal information it holds"
    by_name = next(iter(SparseTextEmbedding("Qdrant/bm25", cache_dir=cache).embed([text])))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(ValueError, match="from any source"):
        SparseTextEmbedding("Qdrant/bm25", cache_dir=cache)  # the failure the image would have had
    loaded = SparseTextEmbedding("Qdrant/bm25", cache_dir=cache, specific_model_path=snapshot)
    by_path = next(iter(loaded.embed([text])))
    assert by_path.indices.tolist() == by_name.indices.tolist()
    assert by_path.values.tolist() == by_name.values.tolist()
