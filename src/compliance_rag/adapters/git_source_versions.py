"""Corpus and mapping versions, resolved from git when a loader runs (#153,
ADR-0034).

A version is a tag over one path (`SourceKind.path`) named with one prefix
(`SourceKind.tag_prefix`), and what a load records is the tag *and* the commit
it pointed at: a tag can move, a commit can't. The loader resolves it before it
writes anything, from the checkout it is about to read.

What decides the tag is the path's content, not the commit. The version is a
tag over `corpus/mappings/`, so a commit that changes only code leaves it
where it was: the newest tag, by creation date, whose commit holds the same
tree for the path as `HEAD` does. With none, the load is untagged and records
`HEAD`'s commit alone; it is never refused for want of a tag.

The one thing it refuses is a path with uncommitted changes. The content a
load would write is then in no commit, and no SHA could name it.

git is an external process, so this lives in `adapters/`. Every call is an
argument list handed to `git -C <repo>`, never a shell string.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from compliance_rag.adapters.interfaces import SourceKind, SourceVersion


class SourceVersionError(RuntimeError):
    """git couldn't say what a version is: not a repository, or not git at all."""


class UncommittedSource(SourceVersionError):
    """The path has content no commit holds, so no SHA could name what a load
    would write."""


def _git(repo: Path, *arguments: str) -> str:
    """One git command's stdout, stripped."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as failed:
        raise SourceVersionError(
            f"git {' '.join(arguments)} failed in {repo}: {failed.stderr.strip()}"
        ) from failed
    except OSError as missing:
        raise SourceVersionError(f"git couldn't be run: {missing}") from missing
    return done.stdout.strip()


def resolve_source_version(kind: SourceKind, *, repo: Path) -> SourceVersion:
    """The version of `kind` that `repo`'s checkout holds: the newest tag
    over its path whose content `HEAD`'s matches, with the commit that tag
    points at; or, with no such tag, `HEAD`'s commit and no tag.

    Refused with `UncommittedSource` if the path has changes that aren't
    committed (staged, unstaged or untracked); changes elsewhere don't matter.
    """
    path = kind.path
    if _git(repo, "status", "--porcelain", "--", path):
        raise UncommittedSource(
            f"{path} has changes that aren't committed, so the content a load would write is in "
            "no commit and no SHA could name it (#153): commit them, then load"
        )
    try:
        head_tree = _git(repo, "rev-parse", f"HEAD:{path}")
    except SourceVersionError as absent:
        raise UncommittedSource(
            f"{path} is in no commit at HEAD, so no SHA could name what a load would write (#153)"
        ) from absent
    newest_first = _git(
        repo, "tag", "--list", f"{kind.tag_prefix}*", "--sort=-creatordate"
    ).splitlines()
    for tag in newest_first:
        try:
            commit = _git(repo, "rev-parse", "--verify", f"{tag}^{{commit}}")
            if _git(repo, "rev-parse", "--verify", f"{commit}:{path}") == head_tree:
                return SourceVersion(kind, sha=commit, tag=tag)
        except SourceVersionError:
            continue  # a tag on something that isn't a commit, or on one without this path
    return SourceVersion(kind, sha=_git(repo, "rev-parse", "HEAD"))
