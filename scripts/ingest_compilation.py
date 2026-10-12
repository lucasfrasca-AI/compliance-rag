"""Ingest one pinned compilation into the corpus bucket (issue #99).

Fetches the compilation from the Federal Register of Legislation's unpacked
ePub (the route `docs/research/legislation-gov-au-format.md` established),
refuses unless its sha256 matches the pin, parses it, selects clauses through
the instrument version's reviewed `manifest.yaml`, and writes them with
`ingest_compilation` into `GCSClauseStore` — idempotent, and a re-run whose
selection or content drifted stops before writing (`IngestDriftError`).

Run locally with Application Default Credentials; it writes objects into the
already-approved corpus bucket and creates no resource.

    uv run python scripts/ingest_compilation.py \\
        --bucket compliance-rag-508904-corpus [--html path/to/document_1.html]

A completed version whose reviewed manifest has since grown stops with the
exact paths being added; re-run with `--acknowledge <those paths>` to add them
(and only them). Removals and edits can't be acknowledged.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections import Counter
from pathlib import Path

import httpx

from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.corpus.ingest import ingest_compilation
from compliance_rag.corpus.models import Scope
from compliance_rag.corpus.selection_manifest import parse_selection_manifest

INSTRUMENT_ID = "privacy-act-1988"
VERSION_ID = "C2026C00227"
SERIES_ID = "C2004A03712"
START = "2026-06-04"
"""The compilation's effective date: the Register serves its unpacked ePub
under `{series}/{start}/{retrospectiveStart}` (both this date here)."""
SOURCE_SHA256 = "6d0a43e5da103d1eeb5317ae9ac213c5b51acab2357dc01b981a91e1052c5c3b"
"""The exact bytes parsed and tested in issue #97 (fetched 2026-10-11)."""
SOURCE_URL = (
    f"https://www.legislation.gov.au/{SERIES_ID}/{START}/{START}"
    "/text/original/epub/OEBPS/document_1/document_1.html"
)
MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "corpus"
    / "instruments"
    / INSTRUMENT_ID
    / VERSION_ID
    / "manifest.yaml"
)


def _source(html_path: Path | None) -> str:
    raw = (
        html_path.read_bytes()
        if html_path is not None
        else httpx.get(SOURCE_URL, timeout=120, follow_redirects=True).raise_for_status().content
    )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SOURCE_SHA256:
        raise SystemExit(
            f"source sha256 {digest} is not the pinned {SOURCE_SHA256}: the Register "
            "served different bytes — re-run the parser tests on them before ingesting"
        )
    return raw.decode("utf-8")


async def main(bucket: str, html_path: Path | None, acknowledged: frozenset[str]) -> None:
    selection = parse_selection_manifest(MANIFEST.read_text(encoding="utf-8"))
    store = GCSClauseStore.for_bucket(bucket)
    result = await ingest_compilation(
        _source(html_path),
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        tenant_id="_public",
        scope=Scope.PUBLIC,
        selection=selection,
        store=store,
        acknowledged_additions=acknowledged,
    )
    print(
        f"{result.instrument_id}:{result.version_id}: {result.clauses_written} clauses, "
        f"first ingest={result.first_ingest}, annotated source sha256 "
        f"{result.source_html_sha256}"
    )
    manifest = await store.get_manifest(INSTRUMENT_ID, VERSION_ID, tenant_id="_public")
    # Clauses per top-level section; all of Schedule 1 counts as "sch1".
    by_section = Counter(path.split("/")[0] for path in manifest)
    print({section: by_section[section] for section in sorted(by_section)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--html", type=Path, default=None)
    parser.add_argument(
        "--acknowledge",
        nargs="*",
        default=[],
        metavar="CLAUSE_PATH",
        help="every clause path a reviewed manifest adds to an already-ingested version, "
        "exactly as the refusal lists them (issue #19's drift check)",
    )
    arguments = parser.parse_args()
    asyncio.run(main(arguments.bucket, arguments.html, frozenset(arguments.acknowledge)))
