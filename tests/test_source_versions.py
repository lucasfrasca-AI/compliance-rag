"""Corpus and mapping versions as a load records them (#153, ADR-0034): the
value types, and their resolution from git in a temporary repository built
here — the checkout a loader runs from, in miniature.

A version is a tag over one path and the commit the tag pointed at. What
decides it is the path's content, so a commit that changes only code leaves it
where it was.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from compliance_rag.adapters.git_source_versions import (
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

MAPPINGS_FILE = "corpus/mappings/privacy-act-1988.yaml"
CORPUS_FILE = "corpus/instruments/privacy-act-1988/C2026C00227/manifest.yaml"
CODE_FILE = "src/code.py"


# ---- the value types ----------------------------------------------------------------


def test_each_kind_is_a_tag_over_one_path_with_its_own_prefix() -> None:
    assert (SourceKind.CORPUS.path, SourceKind.CORPUS.tag_prefix) == (
        "corpus/instruments",
        "corpus-",
    )
    assert (SourceKind.MAPPING.path, SourceKind.MAPPING.tag_prefix) == (
        "corpus/mappings",
        "mappings-",
    )


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


class Repo:
    """A throwaway repository. Every commit and tag gets its own minute, so
    'newest first' is a fact of the history and not of the clock."""

    def __init__(self, root: Path) -> None:
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

    def _stamp(self) -> dict[str, str]:
        self._minute += 1
        when = (
            dt.datetime(2026, 10, 1, tzinfo=dt.UTC) + dt.timedelta(minutes=self._minute)
        ).isoformat()
        return {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}

    def git(self, *args: str, stamped: bool = False) -> str:
        environment = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            **(self._stamp() if stamped else {}),
        }
        done = subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return done.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, **files: str) -> str:
        """Write `files` (keyword names stand for the three paths below) and
        commit everything; returns the new commit's sha."""
        paths = {"mappings": MAPPINGS_FILE, "corpus": CORPUS_FILE, "code": CODE_FILE}
        for name, text in files.items():
            self.write(paths[name], text)
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


def mapping(repo: Repo) -> SourceVersion:
    return resolve_source_version(SourceKind.MAPPING, repo=repo.root)


def corpus(repo: Repo) -> SourceVersion:
    return resolve_source_version(SourceKind.CORPUS, repo=repo.root)


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


def test_a_commit_that_changes_only_code_leaves_the_version_where_it_was(repo: Repo) -> None:
    tagged = repo.commit(mappings="v1", corpus="c1", code="x")
    repo.tag("mappings-2026.10")
    repo.tag("corpus-2026.10")
    head = repo.commit(code="y")
    assert head != tagged
    # The sha names the commit the tag points at: the one that holds this content.
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, tagged, "mappings-2026.10")
    assert corpus(repo) == SourceVersion(SourceKind.CORPUS, tagged, "corpus-2026.10")


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
    second = repo.commit(code="y")  # same content for the path
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


def test_a_tag_on_a_commit_before_the_path_existed_matches_nothing(repo: Repo) -> None:
    before = repo.commit(code="x")
    head = repo.commit(mappings="v1")
    repo.tag("mappings-2026.10", on=before)
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)


def test_a_tag_that_is_not_on_a_commit_is_passed_over(repo: Repo) -> None:
    head = repo.commit(mappings="v1")
    tree = repo.git("rev-parse", "HEAD^{tree}")
    repo.git("tag", "mappings-on-a-tree", tree)
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head)


# ---- refusing what no commit holds -----------------------------------------------------


def _edit(repo: Repo) -> None:
    repo.write(MAPPINGS_FILE, "edited")


def _add_a_file(repo: Repo) -> None:
    repo.write("corpus/mappings/new.yaml", "new")


def _delete(repo: Repo) -> None:
    (repo.root / MAPPINGS_FILE).unlink()


def _stage(repo: Repo) -> None:
    repo.write(MAPPINGS_FILE, "staged")
    repo.git("add", MAPPINGS_FILE)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_edit, id="edited"),
        pytest.param(_add_a_file, id="untracked-file"),
        pytest.param(_delete, id="deleted"),
        pytest.param(_stage, id="staged-not-committed"),
    ],
)
def test_uncommitted_content_under_the_path_is_refused(
    repo: Repo, change: Callable[[Repo], None]
) -> None:
    repo.commit(mappings="v1", corpus="c1")
    repo.tag("mappings-2026.10")
    change(repo)
    with pytest.raises(UncommittedSource, match="no SHA could name it"):
        mapping(repo)
    assert corpus(repo).untagged  # the corpus is clean: only the mappings are refused


def test_uncommitted_changes_elsewhere_do_not_matter(repo: Repo) -> None:
    head = repo.commit(mappings="v1", corpus="c1", code="x")
    repo.tag("mappings-2026.10")
    repo.write(CODE_FILE, "edited")
    repo.write("notes.txt", "untracked")
    repo.write(CORPUS_FILE, "edited too")  # the other kind's path
    assert mapping(repo) == SourceVersion(SourceKind.MAPPING, head, "mappings-2026.10")
    with pytest.raises(UncommittedSource):
        corpus(repo)


def test_a_path_in_no_commit_is_refused(repo: Repo) -> None:
    repo.commit(code="x")
    with pytest.raises(UncommittedSource, match="no commit at HEAD"):
        mapping(repo)


def test_a_directory_that_is_not_a_repository_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SourceVersionError, match="git status"):
        resolve_source_version(SourceKind.MAPPING, repo=tmp_path / "nowhere" / "at-all")


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
