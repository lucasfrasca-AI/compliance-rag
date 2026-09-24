"""The selection manifest: which clauses of a pinned compilation are in
scope for ingest (issue #19, `corpus/instruments/{instrument_id}/{version_id}/manifest.yaml`).

`corpus/mappings/README.md`'s four-field front-matter convention applies here
minus `source_clause_ids` (`author`, `reviewed_by`, `date` only): a selection
manifest declares which clauses are *in scope*, prospectively — it is not
derived from clauses a human already has in hand, so there is nothing to list
as its source.

Scope is declared as clause-path prefixes (`"sch1/"`, `"s6/"`), not a
hand-reviewed literal enumeration — the bucket's own `manifest.json` is what
records the expanded set for audit (`corpus/ingest.py`), not this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import yaml

from compliance_rag.corpus.models import ClauseRecord

_REQUIRED_FIELDS = ("author", "reviewed_by", "date", "prefixes")


@dataclass(frozen=True, slots=True)
class SelectionManifest:
    """A reviewed `manifest.yaml`: who authored and reviewed it, and which
    clause-path prefixes are in scope."""

    author: str
    reviewed_by: str
    date: str
    prefixes: tuple[str, ...]

    def select(self, clauses: tuple[ClauseRecord, ...]) -> tuple[ClauseRecord, ...]:
        """Every clause whose path equals or starts with one of `prefixes`,
        sorted by clause path.

        A prefix that matches nothing is a hard failure, not a silent
        no-op (`CLAUDE.md`: "a fixture not exercised is a fixture that
        isn't actually testing anything" — the same principle applied to a
        prefix that isn't actually scoping anything): a stale or mistyped
        prefix in a hand-reviewed `manifest.yaml` must be caught
        immediately, not discovered later as a clause that's silently
        missing from the corpus.
        """
        matched: dict[str, ClauseRecord] = {}
        hits = dict.fromkeys(self.prefixes, 0)
        for clause in clauses:
            for prefix in self.prefixes:
                if clause.clause_path == prefix or clause.clause_path.startswith(prefix):
                    matched[clause.clause_path] = clause
                    hits[prefix] += 1
                    break
        unmatched = [prefix for prefix, count in hits.items() if count == 0]
        if unmatched:
            raise ValueError(f"selection manifest prefixes matched no clauses: {unmatched!r}")
        return tuple(sorted(matched.values(), key=lambda c: c.clause_path))


def parse_selection_manifest(source: str) -> SelectionManifest:
    """Parse a `manifest.yaml` document.

    Requires `author`, `reviewed_by`, `date` and `prefixes`
    (`corpus/mappings/README.md`'s convention, minus `source_clause_ids`,
    which does not apply to a selection manifest). Unlike mapping YAML,
    `author` and `reviewed_by` are *not* required to differ: a distinct
    second reviewer doesn't exist yet for a solo builder, and a placeholder
    that can never actually resolve is worse than an honest single name
    (`corpus/mappings/README.md`: "Selection manifests: single-author
    review"). A distinct reviewer becomes required before any corpus is
    used for a real customer — that's a review-process change, not
    something this parser can enforce.
    """
    loaded = yaml.safe_load(source)
    if not isinstance(loaded, dict):
        raise ValueError("manifest.yaml must be a YAML mapping")
    data = cast("dict[str, Any]", loaded)  # yaml.safe_load has no return type richer than Any
    missing = [field for field in _REQUIRED_FIELDS if field not in data]
    if missing:
        raise ValueError(f"manifest.yaml missing required field(s): {missing!r}")
    author = str(data["author"])
    reviewed_by = str(data["reviewed_by"])
    prefixes = tuple(str(p) for p in data["prefixes"])
    if not prefixes:
        raise ValueError("manifest.yaml must list at least one prefix")
    return SelectionManifest(
        author=author,
        reviewed_by=reviewed_by,
        date=str(data["date"]),
        prefixes=prefixes,
    )
