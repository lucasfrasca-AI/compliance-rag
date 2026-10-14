"""Corpus and mapping versions, resolved from git when a loader runs (#153,
ADR-0034).

A version is a tag over the paths a graph is built from, named with one prefix
per kind, and what a load records is the tag *and* the commit it pointed at: a
tag can move, a commit can't. The loader resolves it before it writes anything,
from the checkout it is about to read.

What a kind is built from is more than the data it reads. The corpus is the
selection manifest in `corpus/instruments/` *and* the code that turns the
pinned source into clauses; the mappings are the YAML *and* the code that
compiles and loads it. A parser fix changes what the graph holds without
touching a manifest, so it has to change the version, and checking out a
recorded SHA has to reproduce what was loaded. The paths are in `SOURCE_PATHS`.

What decides the tag is the content of those paths, not the commit. A commit
that changes nothing under them leaves the version where it was: it is the
newest tag, by creation date, whose commit holds the same tree (or blob) for
every one of the paths as `HEAD` does. With none, the load is untagged and
records `HEAD`'s commit alone; it is never refused for want of a tag.

The one thing it refuses is a path with anything uncommitted. The content a
load would write is then in no commit, and no SHA could name it. That counts
tracked changes, untracked files whatever `status.showUntrackedFiles` says,
and files git ignores, because an ignored `corpus/mappings/draft.yaml` is
loaded all the same. Only what is not source is let through (`NOT_SOURCE`).

git is an external process, so this lives in `adapters/`. Every call is an
argument list handed to `git -C <repo>`, never a shell string, with a timeout.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from compliance_rag.adapters.interfaces import SourceKind, SourceVersion

SOURCE_PATHS: dict[SourceKind, tuple[str, ...]] = {
    SourceKind.CORPUS: (
        "corpus/instruments",  # the reviewed selection manifest per instrument version
        "src/compliance_rag/corpus",  # the parser and chunker that turn the source into clauses
        "scripts/ingest_compilation.py",  # the pinned source (SOURCE_SHA256) the clauses come from
        "scripts/load_graph_structure.py",  # what of it is loaded into the graph
    ),
    SourceKind.MAPPING: (
        "corpus/mappings",  # the YAML
        "src/compliance_rag/graph",  # compiled (mappings.py) and loaded (loader.py) into the graph
    ),
}
"""The paths each kind of version is a tag over. A tag matches only if every
one of a kind's paths holds the same content at the tag as at `HEAD`."""

TAG_PREFIXES: dict[SourceKind, str] = {
    SourceKind.CORPUS: "corpus-",
    SourceKind.MAPPING: "mappings-",
}
"""What a tag is called: `corpus-2026.10.14`, `mappings-2026.10.14`. The prefix
is how the two kinds are told apart."""

NOT_SOURCE = re.compile(
    r"(^|/)__pycache__/|\.py[co]$|(^|/)\.DS_Store$|^corpus/instruments/.+/raw/",
)
"""Untracked or ignored files that are not uncommitted source: bytecode caches,
Finder metadata, and the raw compilations `corpus/instruments/**/raw/` holds,
which are fetched and not committed (`.gitignore`) but pinned by a sha256 the
ingest checks, so they cannot change what is loaded. Anything else untracked or
ignored under a path refuses the load."""

TIMEOUT_SECONDS = 30
"""For every git call: a hung git is an error, not a loader that never starts."""

HOW_MANY_TO_NAME = 5


class SourceVersionError(RuntimeError):
    """git couldn't say what a version is: not a repository, or not git at all."""


class UncommittedSource(SourceVersionError):
    """A path holds content no commit holds, so no SHA could name what a load
    would write."""


def _git(repo: Path, *arguments: str) -> str:
    """One git command's stdout, without its final newline."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as failed:
        raise SourceVersionError(
            f"git {' '.join(arguments)} failed in {repo}: {failed.stderr.strip()}"
        ) from failed
    except subprocess.TimeoutExpired as slow:
        raise SourceVersionError(
            f"git {' '.join(arguments)} took longer than {TIMEOUT_SECONDS}s in {repo}"
        ) from slow
    except OSError as missing:
        raise SourceVersionError(f"git couldn't be run: {missing}") from missing
    return done.stdout.rstrip("\n")


def _uncommitted(repo: Path, paths: tuple[str, ...]) -> list[str]:
    """Everything under `paths` that no commit holds, however git is configured
    to show it."""
    found: list[str] = []
    # Tracked changes, and untracked files: all of them, whatever
    # `status.showUntrackedFiles` says.
    status = _git(repo, "status", "--porcelain", "--untracked-files=all", "--", *paths)
    for line in status.splitlines():
        if line[:2] != "??":
            found.append(line.strip())
        elif not NOT_SOURCE.search(line[3:]):
            found.append(line[3:])
    # Ignored files (`.gitignore`, `core.excludesFile`): loaded all the same.
    ignored = _git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--", *paths)
    found += [path for path in ignored.splitlines() if not NOT_SOURCE.search(path)]
    # Files git has been told not to look at: a change to one shows nowhere.
    for line in _git(repo, "ls-files", "-v", "--", *paths).splitlines():
        if line[0] != "H":
            found.append(f"{line[2:]} (marked assume-unchanged or skip-worktree)")
    return found


def _objects(repo: Path, revision: str, paths: tuple[str, ...]) -> list[str]:
    """The tree or blob each path is, at `revision`."""
    return _git(repo, "rev-parse", *(f"{revision}:{path}" for path in paths)).splitlines()


def resolve_source_version(kind: SourceKind, *, repo: Path) -> SourceVersion:
    """The version of `kind` that `repo`'s checkout holds: the newest tag with
    the kind's prefix whose commit holds the same content as `HEAD` for every
    one of the kind's paths, with the commit that tag points at; or, with no
    such tag, `HEAD`'s commit and no tag.

    Refused with `UncommittedSource` if any of the kind's paths has anything
    uncommitted (staged, unstaged, untracked or ignored; `NOT_SOURCE` apart),
    or isn't in `HEAD` at all. Changes elsewhere don't matter.
    """
    paths = SOURCE_PATHS[kind]
    unclean = _uncommitted(repo, paths)
    if unclean:
        shown = ", ".join(sorted(unclean)[:HOW_MANY_TO_NAME])
        left = len(unclean) - HOW_MANY_TO_NAME
        more = f" and {left} more" if left > 0 else ""
        raise UncommittedSource(
            f"{len(unclean)} file(s) under {', '.join(paths)} aren't committed ({shown}{more}), "
            "so the content a load would write is in no commit and no SHA could name it (#153): "
            "commit or remove them, then load"
        )
    try:
        at_head = _objects(repo, "HEAD", paths)
    except SourceVersionError as absent:
        raise UncommittedSource(
            f"one of {', '.join(paths)} is in no commit at HEAD, so no SHA could name what a "
            f"load would write (#153): {absent}"
        ) from absent
    newest_first = _git(
        repo, "tag", "--list", f"{TAG_PREFIXES[kind]}*", "--sort=-creatordate"
    ).splitlines()
    for tag in newest_first:
        try:
            commit = _git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
            if _objects(repo, commit, paths) == at_head:
                return SourceVersion(kind, sha=commit, tag=tag)
        except SourceVersionError:
            continue  # a tag on something that isn't a commit, or on one without these paths
    return SourceVersion(kind, sha=_git(repo, "rev-parse", "HEAD"))
