"""Corpus and mapping versions as a load records them (#153, ADR-0034): the
value types, and their resolution from git in a temporary repository built
here — the checkout a loader runs from, in miniature.

A version is a tag over the data a graph is built from and the code that turns
it into what the graph holds, and the commit the tag pointed at. What decides
it is the content of those paths, so a commit that changes nothing under them
leaves it where it was, and one that changes a parser does not.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from compliance_rag.adapters import git_source_versions
from compliance_rag.adapters.git_source_versions import (
    PINNED_RAW,
    SOURCE_PATHS,
    TAG_PREFIXES,
    SourceVersionError,
    UncommittedSource,
    resolve_source_version,
)
from compliance_rag.adapters.interfaces import (
    LoadedSource,
    LoadedSources,
    SourceKind,
    SourceVersion,
)

SHA = "1a2b3c4d" * 5

FILES = {
    "mappings": "corpus/mappings/privacy-act-1988.yaml",
    "graph_mappings": "src/compliance_rag/graph/mappings.py",
    "mapping_script": "scripts/load_mappings.py",
    "graph_loader": "src/compliance_rag/graph/loader.py",  # in both kinds
    "corpus": "corpus/instruments/privacy-act-1988/C2026C00227/manifest.yaml",
    "parser": "src/compliance_rag/corpus/parser.py",
    "ingest": "scripts/ingest_compilation.py",
    "corpus_script": "scripts/load_graph_structure.py",
    "code": "src/compliance_rag/api/app.py",  # in neither kind
}
"""A file under each path a version is a tag over, by a short name."""

PATHS_OF = {
    SourceKind.MAPPING: ("mappings", "graph_mappings", "mapping_script", "graph_loader"),
    SourceKind.CORPUS: ("corpus", "parser", "ingest", "corpus_script", "graph_loader"),
}
GITIGNORE = "__pycache__/\n*.pyc\ncorpus/instruments/**/raw/\n*.draft.yaml\n"
"""The real `.gitignore`'s rules that bear on these paths, and one more."""


# ---- the value types ----------------------------------------------------------------


def test_the_paths_and_prefixes_are_the_git_adapters_not_the_interfaces() -> None:
    assert not hasattr(SourceKind.CORPUS, "path") and not hasattr(SourceKind.CORPUS, "tag_prefix")
    assert set(SOURCE_PATHS) == set(TAG_PREFIXES) == set(SourceKind)
    assert TAG_PREFIXES == {SourceKind.CORPUS: "corpus-", SourceKind.MAPPING: "mappings-"}


def test_the_paths_each_kind_is_a_tag_over_include_the_code_that_builds_it() -> None:
    """The graph holds what the code made of the data: a parser fix changes it
    with no manifest touched."""
    assert SOURCE_PATHS[SourceKind.CORPUS] == (
        "corpus/instruments",
        "src/compliance_rag/corpus",
        "scripts/ingest_compilation.py",
        "scripts/load_graph_structure.py",
        "src/compliance_rag/graph/loader.py",
    )
    assert SOURCE_PATHS[SourceKind.MAPPING] == (
        "corpus/mappings",
        "src/compliance_rag/graph",
        "scripts/load_mappings.py",
    )


def test_the_writer_of_the_graph_is_in_both_kinds_and_storage_mechanics_in_neither() -> None:
    """`graph/loader.py` writes the corpus's clause nodes as well as the
    mappings'; how a node is stored (`neo4j_graph.py`) is neither's content."""

    def covered_by(kind: SourceKind, file: str) -> bool:
        return any(file == p or file.startswith(f"{p}/") for p in SOURCE_PATHS[kind])

    for kind in SourceKind:
        assert covered_by(kind, "src/compliance_rag/graph/loader.py")
        assert not covered_by(kind, "src/compliance_rag/adapters/neo4j_graph.py")
        assert not covered_by(kind, "src/compliance_rag/adapters/graph_schema.py")
    assert covered_by(SourceKind.MAPPING, "src/compliance_rag/graph/mappings.py")
    assert not covered_by(SourceKind.CORPUS, "src/compliance_rag/graph/mappings.py")


def test_every_path_a_version_is_a_tag_over_exists_in_this_repository() -> None:
    root = Path(__file__).resolve().parents[1]
    for paths in SOURCE_PATHS.values():
        for path in paths:
            assert (root / path).exists(), f"{path} moved: update SOURCE_PATHS (ADR-0034)"


def test_a_tagged_version_is_described_by_its_tag_and_a_short_sha() -> None:
    version = SourceVersion(SourceKind.MAPPING, SHA, "mappings-2026.10")
    assert not version.untagged
    assert version.described == "mappings-2026.10 @ 1a2b3c4"


def test_an_untagged_version_is_the_sha_alone() -> None:
    version = SourceVersion(SourceKind.CORPUS, SHA)
    assert version.untagged and version.tag is None
    assert version.described == "untagged @ 1a2b3c4"


@pytest.mark.parametrize(
    "sha",
    ["", "1a2b3c4", SHA[:39], SHA + "0", SHA.upper(), "g" * 40, f" {SHA[:39]}", "HEAD"],
    ids=["empty", "abbreviated", "39", "41", "uppercase", "not-hex", "padded", "a-name"],
)
def test_a_sha_is_forty_lowercase_hex_characters(sha: str) -> None:
    with pytest.raises(ValueError, match="40 lowercase hex"):
        SourceVersion(SourceKind.CORPUS, sha)


@pytest.mark.parametrize("tag", ["", "  "])
def test_a_tag_is_a_name_or_absent_never_blank(tag: str) -> None:
    with pytest.raises(ValueError, match="untagged"):
        SourceVersion(SourceKind.CORPUS, SHA, tag)


def test_nothing_is_loaded_by_default() -> None:
    nothing = LoadedSources()
    for kind in SourceKind:
        assert nothing.of(kind) == LoadedSource(version=None, loading=False)


def test_loaded_sources_are_read_by_kind() -> None:
    corpus = SourceVersion(SourceKind.CORPUS, SHA)
    loaded = LoadedSources(corpus=LoadedSource(corpus), mapping=LoadedSource(loading=True))
    assert loaded.of(SourceKind.CORPUS).version == corpus
    assert loaded.of(SourceKind.MAPPING) == LoadedSource(None, True)


# ---- a repository to resolve from ----------------------------------------------------


@pytest.fixture(autouse=True)
def _git_config_is_the_repositorys_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the developer or CI has configured (an excludes file, a status
    default) must not decide these tests; a repository's own config does."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


class Repo:
    """A throwaway repository, with a baseline commit holding a file under
    every path a version is a tag over. Every commit and tag gets its own
    minute, so 'newest first' is a fact of the history and not of the clock."""

    def __init__(self, root: Path, *, baseline: bool = True) -> None:
        self.root = root
        self._minute = 0
        self.git("init", "-q", "-b", "main")
        for key, value in (
            ("user.name", "Test"),
            ("user.email", "test@example.com"),
            ("commit.gpgsign", "false"),
            ("tag.gpgsign", "false"),
        ):
            self.git("config", key, value)
        if baseline:
            self.write(".gitignore", GITIGNORE)
            self.commit(**{name: "0" for name in FILES})

    def _stamp(self) -> dict[str, str]:
        self._minute += 1
        when = (
            dt.datetime(2026, 10, 1, tzinfo=dt.UTC) + dt.timedelta(minutes=self._minute)
        ).isoformat()
        return {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}

    def git(self, *args: str, stamped: bool = False) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, **(self._stamp() if stamped else {})},
        )
        return done.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, **files: str) -> str:
        """Write `files` (by the short names in `FILES`) and commit everything;
        returns the new commit's sha."""
        for name, text in files.items():
            self.write(FILES[name], text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change", stamped=True)
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str, *, annotated: bool = False, on: str = "HEAD") -> None:
        if annotated:
            self.git("tag", "-a", "-m", name, name, on, stamped=True)
        else:
            self.git("tag", name, on)

    def untag(self, name: str) -> None:
        self.git("tag", "-d", name)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


@pytest.fixture
def bare(tmp_path: Path) -> Repo:
    """A repository whose first commit holds none of the paths."""
    return Repo(tmp_path, baseline=False)


def mapping(repo: Repo) -> SourceVersion:
    return resolve_source_version(SourceKind.MAPPING, repo=repo.root)


def corpus(repo: Repo) -> SourceVersion:
    return resolve_source_version(SourceKind.CORPUS, repo=repo.root)


def resolve(kind: SourceKind, repo: Repo) -> SourceVersion:
    return resolve_source_version(kind, repo=repo.root)


# ---- resolving a version from git -----------------------------------------------------


def test_a_tag_at_head_is_the_version_with_the_commit_it_points_at(repo: Repo) -> None:
    head = repo.commit(mappings="v1", corpus="c1", code="x")
    repo.tag("mappings-2026.10")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head, "mappings-2026.10")


def test_an_annotated_tag_is_recorded_by_the_commit_not_the_tag_object(repo: Repo) -> None:
    head = repo.commit(mappings="v1", corpus="c1")
    repo.tag("mappings-2026.10", annotated=True)
    assert repo.git("rev-parse", "mappings-2026.10") != head  # the tag object's own id
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head, "mappings-2026.10")


def test_a_commit_that_changes_nothing_a_version_is_built_from_leaves_it_where_it_was(
    repo: Repo,
) -> None:
    tagged = repo.commit(mappings="v1", corpus="c1", code="x")
    repo.tag("mappings-2026.10")
    repo.tag("corpus-2026.10")
    head = repo.commit(code="y")
    assert head != tagged
    # The sha names the commit the tag points at: the one that holds this content.
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, tagged, "mappings-2026.10")
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, tagged, "corpus-2026.10")


def test_a_change_to_the_api_is_not_a_new_version_of_either_kind(repo: Repo) -> None:
    tagged = repo.commit(mappings="v1", corpus="c1")
    repo.tag("mappings-2026.10")
    repo.tag("corpus-2026.10")
    repo.commit(code="an api change")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, tagged, "mappings-2026.10")
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, tagged, "corpus-2026.10")


def test_a_parser_fix_is_a_new_corpus_version_though_no_manifest_changed(repo: Repo) -> None:
    """The clauses are what the parser makes of the pinned source: checking out
    the old tag's commit would parse them differently."""
    tagged = repo.commit(corpus="c1", parser="p1")
    repo.tag("corpus-2026.10")
    repo.tag("mappings-2026.10")
    head = repo.commit(parser="p2")
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, head)
    assert corpus(repo).untagged
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, tagged, "mappings-2026.10")


def test_a_change_to_the_graph_loader_is_a_new_version_of_both_kinds(repo: Repo) -> None:
    """It writes the corpus's clause nodes and the mappings' nodes alike."""
    repo.commit(**{name: "1" for name in FILES})
    repo.tag("mappings-2026.10")
    repo.tag("corpus-2026.10")
    head = repo.commit(graph_loader="2")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, head)


def test_a_change_to_the_mapping_compiler_is_a_new_mapping_version_only(repo: Repo) -> None:
    tagged = repo.commit(**{name: "1" for name in FILES})
    repo.tag("mappings-2026.10")
    repo.tag("corpus-2026.10")
    head = repo.commit(graph_mappings="2")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, tagged, "corpus-2026.10")


def test_a_change_to_the_mapping_script_is_a_new_mapping_version_only(repo: Repo) -> None:
    """Its glob and its D-01 review filter decide which files are loaded."""
    tagged = repo.commit(**{name: "1" for name in FILES})
    repo.tag("mappings-2026.10")
    repo.tag("corpus-2026.10")
    head = repo.commit(mapping_script="2")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, tagged, "corpus-2026.10")


def test_a_new_file_of_exceptions_beside_a_signed_mapping_is_a_new_mapping_version(
    repo: Repo,
) -> None:
    """#164: an obligation's exceptions are mapping content in a file of their
    own, the signed file untouched. Adding that file moves the mapping
    version, and only it; uncommitted, it refuses the load."""
    tagged = repo.commit(**{name: "1" for name in FILES})
    repo.tag("mappings-2026.10")
    repo.tag("corpus-2026.10")
    repo.write("corpus/mappings/privacy-act-1988-exceptions.yaml", "exceptions: []\n")
    with pytest.raises(UncommittedSource, match=r"privacy-act-1988-exceptions\.yaml"):
        mapping(repo)
    head = repo.commit()
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, tagged, "corpus-2026.10")


@pytest.mark.parametrize(
    ("kind", "changed"),
    [(kind, name) for kind, names in PATHS_OF.items() for name in names],
)
def test_a_change_under_any_path_a_version_is_a_tag_over_leaves_it_untagged(
    repo: Repo, kind: SourceKind, changed: str
) -> None:
    repo.commit(**{name: "1" for name in FILES})
    repo.tag(f"{TAG_PREFIXES[kind]}2026.10")
    head = repo.commit(**{changed: "2"})
    assert resolve(kind, repo) == SourceVersion(kind, head)


@pytest.mark.parametrize(
    ("kind", "changed"),
    [(kind, name) for kind, own in PATHS_OF.items() for name in FILES if name not in own],
)
def test_a_change_under_none_of_them_keeps_the_version(
    repo: Repo, kind: SourceKind, changed: str
) -> None:
    tagged = repo.commit(**{name: "1" for name in FILES})
    tag = f"{TAG_PREFIXES[kind]}2026.10"
    repo.tag(tag)
    repo.commit(**{changed: "2"})
    assert resolve(kind, repo) == SourceVersion(kind, tagged, tag)


def test_a_change_to_the_path_after_the_tag_is_untagged_at_head(repo: Repo) -> None:
    repo.commit(mappings="v1", corpus="c1")
    repo.tag("mappings-2026.10")
    head = repo.commit(mappings="v2")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)
    assert mapping(repo).untagged


def test_the_two_kinds_are_tagged_independently(repo: Repo) -> None:
    """A mapping correction changes no clause text, and a new compilation
    changes no mapping (CONTEXT.md)."""
    first = repo.commit(mappings="v1", corpus="c1")
    repo.tag("mappings-2026.10")
    repo.tag("corpus-2026.10")
    second = repo.commit(mappings="v2")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, second)
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, first, "corpus-2026.10")


def test_with_no_tag_at_all_the_version_is_head_untagged(repo: Repo) -> None:
    head = repo.commit(mappings="v1", corpus="c1")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head, None)
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, head, None)


def test_of_two_matching_tags_the_newest_wins(repo: Repo) -> None:
    """By when the tag was made: not by name (the middle one by name wins) and
    not by which commit it points at (it is on the oldest)."""
    oldest = repo.commit(mappings="v1")
    middle = repo.commit(code="y")
    repo.commit(code="z")
    repo.tag("mappings-1", annotated=True, on=middle)
    repo.tag("mappings-3", annotated=True, on=middle)
    repo.tag("mappings-2", annotated=True, on=oldest)
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, oldest, "mappings-2")


def test_a_newer_tag_over_other_content_is_passed_over_for_one_that_matches(repo: Repo) -> None:
    old = repo.commit(mappings="v1")
    current = repo.commit(mappings="v2")
    repo.tag("mappings-current", annotated=True, on=current)
    repo.tag("mappings-stale", annotated=True, on=old)  # made later, but over v1
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, current, "mappings-current")
    repo.untag("mappings-current")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, current)


def test_a_tag_matches_only_if_every_path_matches(repo: Repo) -> None:
    """The mappings are the same at the tag as at HEAD; the code that loads
    them is not."""
    old = repo.commit(mappings="v1", graph_mappings="g1")
    head = repo.commit(graph_mappings="g2")
    repo.tag("mappings-stale", on=old)
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)


def test_the_other_kinds_tag_is_ignored(repo: Repo) -> None:
    head = repo.commit(mappings="v1", corpus="c1")
    repo.tag("corpus-2026.10")
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)
    repo.untag("corpus-2026.10")
    repo.tag("mappings-2026.10")
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, head)


def test_a_tag_that_is_not_one_of_ours_is_ignored(repo: Repo) -> None:
    head = repo.commit(mappings="v1")
    for name in ("v1.0", "mappings", "release-mappings-1", "corpus-1"):
        repo.tag(name)
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)


def test_a_tag_moved_to_another_commit_resolves_to_where_it_is_now(repo: Repo) -> None:
    """A tag can move; the commit a load recorded can't."""
    first = repo.commit(mappings="v1")
    repo.tag("mappings-2026.10")
    recorded = mapping(repo)
    second = repo.commit(code="y")  # same content for the paths
    repo.untag("mappings-2026.10")
    repo.tag("mappings-2026.10")
    moved = mapping(repo)
    assert recorded == SourceVersion(SourceKind.MAPPING, first, "mappings-2026.10")
    assert moved == SourceVersion(SourceKind.MAPPING, second, "mappings-2026.10")
    assert moved.sha != recorded.sha


def test_a_tag_moved_onto_other_content_no_longer_names_this_content(repo: Repo) -> None:
    old = repo.commit(mappings="v1")
    repo.commit(mappings="v2")
    head = repo.commit(code="y")
    repo.tag("mappings-2026.10", on=old)
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)


def test_a_tag_on_a_commit_before_the_paths_existed_matches_nothing(bare: Repo) -> None:
    before = bare.commit(code="x")
    head = bare.commit(**{name: "1" for name in PATHS_OF[SourceKind.MAPPING]})
    bare.tag("mappings-2026.10", on=before)
    assert mapping(bare) == SourceVersion(SourceKind.MAPPING, head)


def test_a_tag_that_is_not_on_a_commit_is_passed_over(repo: Repo) -> None:
    head = repo.commit(mappings="v1")
    tree = repo.git("rev-parse", "HEAD^{tree}")
    repo.git("tag", "mappings-on-a-tree", tree)
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)


# ---- refusing what no commit holds -----------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "name"),
    [(kind, name) for kind, names in PATHS_OF.items() for name in names],
)
def test_uncommitted_content_under_any_path_a_version_is_built_from_is_refused(
    repo: Repo, kind: SourceKind, name: str
) -> None:
    repo.commit(**{n: "1" for n in FILES})
    repo.tag(f"{TAG_PREFIXES[kind]}2026.10")
    repo.write(FILES[name], "edited")
    with pytest.raises(UncommittedSource, match="no SHA could name it") as refused:
        resolve(kind, repo)
    assert FILES[name] in str(refused.value)
    other = next(k for k in SourceKind if k is not kind)
    if name not in PATHS_OF[other]:
        assert resolve(other, repo).untagged  # the other kind is clean: only this one is refused
    else:
        with pytest.raises(UncommittedSource):  # a file in both kinds is uncommitted in both
            resolve(other, repo)


def _edit(repo: Repo) -> None:
    repo.write(FILES["mappings"], "edited")


def _add_a_file(repo: Repo) -> None:
    repo.write("corpus/mappings/new.yaml", "new")


def _delete(repo: Repo) -> None:
    (repo.root / FILES["mappings"]).unlink()


def _stage(repo: Repo) -> None:
    repo.write(FILES["mappings"], "staged")
    repo.git("add", FILES["mappings"])


def _add_code(repo: Repo) -> None:
    repo.write("src/compliance_rag/graph/new.py", "x = 1")


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_edit, id="edited"),
        pytest.param(_add_a_file, id="untracked-file"),
        pytest.param(_delete, id="deleted"),
        pytest.param(_stage, id="staged-not-committed"),
        pytest.param(_add_code, id="untracked-code"),
    ],
)
def test_uncommitted_content_under_the_paths_is_refused(
    repo: Repo, change: Callable[[Repo], None]
) -> None:
    repo.commit(mappings="v1", corpus="c1")
    repo.tag("mappings-2026.10")
    change(repo)
    with pytest.raises(UncommittedSource, match="no SHA could name it"):
        mapping(repo)
    assert corpus(repo).untagged  # the corpus is clean: only the mappings are refused


def test_an_untracked_file_is_found_whatever_status_is_configured_to_show(repo: Repo) -> None:
    repo.commit(mappings="v1")
    repo.tag("mappings-2026.10")
    repo.git("config", "status.showUntrackedFiles", "no")
    repo.write("corpus/mappings/a.yaml", "a loaded file, in no commit")
    assert repo.git("status", "--porcelain") == ""  # git itself says nothing
    with pytest.raises(UncommittedSource, match=r"corpus/mappings/a\.yaml"):
        mapping(repo)


def test_an_ignored_file_is_found_because_it_is_loaded_all_the_same(repo: Repo) -> None:
    repo.commit(mappings="v1")
    repo.tag("mappings-2026.10")
    repo.write("corpus/mappings/draft.draft.yaml", "ignored by .gitignore, loaded by *.yaml")
    assert repo.git("status", "--porcelain") == ""
    with pytest.raises(UncommittedSource, match=r"draft\.draft\.yaml"):
        mapping(repo)


def test_a_file_ignored_by_the_users_own_excludes_is_found_too(repo: Repo, tmp_path: Path) -> None:
    excludes = tmp_path.parent / f"{tmp_path.name}-excludes"
    excludes.write_text("*.local.yaml\n", encoding="utf-8")
    repo.git("config", "core.excludesFile", str(excludes))
    repo.commit(mappings="v1")
    repo.write("corpus/mappings/mine.local.yaml", "hidden by a global ignore")
    assert repo.git("status", "--porcelain") == ""
    with pytest.raises(UncommittedSource, match=r"mine\.local\.yaml"):
        mapping(repo)


def test_ignored_code_under_the_paths_is_found_too(repo: Repo) -> None:
    repo.write("src/compliance_rag/corpus/notes.draft.yaml", "ignored")
    with pytest.raises(UncommittedSource, match=r"notes\.draft\.yaml"):
        corpus(repo)


def test_what_is_not_source_is_let_through(repo: Repo) -> None:
    """Bytecode, Finder metadata and the raw compilations that are fetched, not
    committed, and pinned by a sha256: none of it can change what is loaded."""
    head = repo.commit(mappings="v1", corpus="c1")
    repo.tag("mappings-2026.10")
    repo.tag("corpus-2026.10")
    repo.write("src/compliance_rag/graph/__pycache__/loader.cpython-312.pyc", "bytecode")
    repo.write("src/compliance_rag/corpus/__pycache__/parser.cpython-312.pyc", "bytecode")
    repo.write("corpus/mappings/.DS_Store", "finder")
    repo.write("corpus/instruments/.DS_Store", "finder")
    repo.write(
        "corpus/instruments/privacy-act-1988/C2026C00227/raw/document_1.html", "<html>fetched"
    )
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head, "mappings-2026.10")
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, head, "corpus-2026.10")


def test_the_raw_sources_let_through_are_exactly_the_pinned_ones() -> None:
    """`scripts/ingest_compilation.py` pins one source by sha256; only its
    directory is exempt."""
    from scripts import ingest_compilation

    pinned = (
        f"corpus/instruments/{ingest_compilation.INSTRUMENT_ID}/"
        f"{ingest_compilation.VERSION_ID}/raw/"
    )
    assert (pinned,) == PINNED_RAW
    assert re.fullmatch(r"[0-9a-f]{64}", ingest_compilation.SOURCE_SHA256)


def test_every_instrument_version_with_a_manifest_has_its_source_pinned() -> None:
    """Fails when an instrument version is added whose source isn't pinned: its
    raw directory would otherwise refuse every load, or be let through unpinned.
    Pin it in `scripts/ingest_compilation.py` and name it in `PINNED_RAW`."""
    root = Path(__file__).resolve().parents[1] / "corpus" / "instruments"
    manifests = sorted(root.glob("*/*/manifest.yaml"))
    assert manifests
    for manifest in manifests:
        raw = f"corpus/instruments/{manifest.parent.parent.name}/{manifest.parent.name}/raw/"
        assert raw in PINNED_RAW, f"{manifest.parent} has no pinned source (ADR-0034)"


def test_the_raw_directory_of_an_unpinned_source_is_refused(repo: Repo) -> None:
    repo.write("corpus/instruments/some-other-act/V1/raw/document_1.html", "<html>unpinned")
    with pytest.raises(UncommittedSource, match=r"some-other-act/V1/raw/document_1\.html"):
        corpus(repo)


def test_a_raw_directory_is_let_through_only_under_the_instruments(repo: Repo) -> None:
    repo.write("corpus/mappings/raw/anything.yaml", "loaded by nothing, but in no commit")
    with pytest.raises(UncommittedSource, match=r"raw/anything\.yaml"):
        mapping(repo)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_a_file_git_was_told_not_to_look_at_is_refused(repo: Repo, flag: str) -> None:
    """A change to it shows nowhere in `git status`."""
    repo.commit(mappings="v1")
    repo.git("update-index", flag, FILES["mappings"])
    repo.write(FILES["mappings"], "changed, and git doesn't say so")
    assert FILES["mappings"] not in repo.git("status", "--porcelain")
    with pytest.raises(UncommittedSource, match="assume-unchanged or skip-worktree"):
        mapping(repo)


def test_the_refusal_names_what_it_found_and_how_much_more(repo: Repo) -> None:
    for number in range(7):
        repo.write(f"corpus/mappings/new-{number}.yaml", "x")
    with pytest.raises(UncommittedSource, match=r"7 file\(s\).*new-0.*and 2 more") as refused:
        mapping(repo)
    assert "new-6" not in str(refused.value)


def test_uncommitted_changes_elsewhere_do_not_matter(repo: Repo) -> None:
    head = repo.commit(mappings="v1", corpus="c1", code="x")
    repo.tag("mappings-2026.10")
    repo.write(FILES["code"], "edited")
    repo.write("notes.txt", "untracked")
    repo.write(FILES["corpus"], "edited too")  # the other kind's path
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head, "mappings-2026.10")
    with pytest.raises(UncommittedSource):
        corpus(repo)


def test_a_path_in_no_commit_is_refused(bare: Repo) -> None:
    bare.commit(code="x")
    with pytest.raises(UncommittedSource, match="no commit at HEAD"):
        mapping(bare)


def test_a_directory_that_is_not_a_repository_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SourceVersionError, match="git status"):
        resolve_source_version(SourceKind.MAPPING, repo=tmp_path / "nowhere" / "at-all")


# ---- git is a process: with a timeout ---------------------------------------------------


def test_every_git_call_has_a_timeout(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    repo.commit(mappings="v1")
    repo.tag("mappings-2026.10")
    real: Callable[..., Any] = subprocess.run
    timeouts: list[object] = []

    def recording(*args: Any, **kwargs: Any) -> Any:  # Any: forwarded to subprocess.run
        timeouts.append(kwargs.get("timeout"))
        return real(*args, **kwargs)

    monkeypatch.setattr(git_source_versions.subprocess, "run", recording)
    mapping(repo)
    assert git_source_versions.TIMEOUT_SECONDS == 30
    assert len(timeouts) >= 6 and set(timeouts) == {30}


def test_a_git_that_hangs_is_an_error_not_a_loader_that_never_starts(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hangs(*args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args[0], 30)

    monkeypatch.setattr(git_source_versions.subprocess, "run", hangs)
    with pytest.raises(SourceVersionError, match="took longer than 30s"):
        mapping(repo)


# ---- the loaders resolve before they touch the graph -----------------------------------


async def test_an_uncommitted_mapping_refuses_the_load_before_the_graph_is_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import load_mappings

    def uncommitted(kind: SourceKind, *, repo: Path) -> SourceVersion:
        assert kind is SourceKind.MAPPING
        raise UncommittedSource("corpus/mappings is edited: no SHA could name it")

    def opened(*_: object, **__: object) -> None:
        raise AssertionError("the graph was opened")

    monkeypatch.setattr(load_mappings, "resolve_source_version", uncommitted)
    monkeypatch.setattr(load_mappings.Neo4jGraph, "from_settings", opened)
    with pytest.raises(SystemExit, match="no SHA could name it"):
        await load_mappings.main(dt.date(2026, 10, 14), None)


async def test_an_uncommitted_corpus_refuses_the_load_before_anything_is_read_or_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import load_graph_structure

    def uncommitted(kind: SourceKind, *, repo: Path) -> SourceVersion:
        assert kind is SourceKind.CORPUS
        raise UncommittedSource("corpus/instruments is edited: no SHA could name it")

    def touched(*_: object, **__: object) -> None:
        raise AssertionError("the source was fetched or the graph opened")

    monkeypatch.setattr(load_graph_structure, "resolve_source_version", uncommitted)
    monkeypatch.setattr(load_graph_structure, "pinned_source", touched)
    monkeypatch.setattr(load_graph_structure.Neo4jGraph, "from_settings", touched)
    with pytest.raises(SystemExit, match="no SHA could name it"):
        await load_graph_structure.main(None)


# ---- the ledger row ---------------------------------------------------------------------


def test_a_ledger_row_is_an_assessment_record() -> None:
    from compliance_rag.adapters.postgres_ledger_store import assessment_from_row

    record = assessment_from_row(
        "acme",
        "a1",
        (
            "lead@acme.example",
            "corpus-2026.10",
            "c" * 40,
            None,
            "d" * 40,
            dt.date(2026, 10, 14),
            "t",
        ),
    )
    assert record.corpus == SourceVersion(SourceKind.CORPUS, "c" * 40, "corpus-2026.10")
    assert record.mapping == SourceVersion(SourceKind.MAPPING, "d" * 40)  # NULL: untagged
    assert (record.user_id, record.as_of, record.trace_id) == (
        "lead@acme.example",
        dt.date(2026, 10, 14),
        "t",
    )


@pytest.mark.parametrize(
    "row",
    [
        pytest.param((None, "c", None, "m", None, dt.date(2026, 10, 1), "t"), id="before-0013"),
        pytest.param(
            ("u", "c", "c" * 40, "m", None, dt.date(2026, 10, 1), "t"), id="no-mapping-sha"
        ),
        pytest.param(
            (None, None, "c" * 40, None, "d" * 40, dt.date(2026, 10, 1), "t"), id="no-user"
        ),
    ],
)
def test_a_ledger_row_recorded_before_source_versions_is_refused_not_guessed(
    row: tuple[object, ...],
) -> None:
    from compliance_rag.adapters.postgres_ledger_store import assessment_from_row

    with pytest.raises(ValueError, match="before source versions"):
        assessment_from_row("acme", "old", row)
