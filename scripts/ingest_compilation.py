"""Ingest one pinned compilation into the corpus bucket (issue #99).

Fetches the compilation from the Federal Register of Legislation's unpacked
ePub (the route `docs/research/legislation-gov-au-format.md` established),
refuses unless its sha256 matches the pin, parses it, selects clauses through
the instrument version's reviewed `manifest.yaml`, and writes them with
`ingest_compilation` into `GCSClauseStore` — idempotent, and a re-run whose
selection or content drifted stops before writing (`IngestDriftError`).

Since #338 (ADR-0058) the version has a second pinned source: the Register's
PDF of the same compilation, from which s 6DA(2)'s formula is completed
against the signed form beside the manifest (`formula.yaml`). It is fetched,
or read from `--pdf`, and refused unless its sha256 is the pin, as the ePub is.

Run locally with Application Default Credentials; it writes objects into the
already-approved corpus bucket and creates no resource.

    uv run python scripts/ingest_compilation.py \\
        --bucket compliance-rag-508904-corpus [--html path/to/document_1.html] \\
        [--pdf path/to/C2026C00227.pdf]

A completed version whose reviewed manifest has since grown stops with the
exact paths being added; re-run with `--acknowledge <those paths>` to add them
(and only them). Removals and edits can't be acknowledged here: a changed
clause text is a corpus revision (`scripts/revise_compilation.py`, ADR-0042).
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
from compliance_rag.corpus.pdf_formula import (
    SignedFormula,
    formula_completion,
    load_signed_formulas,
)
from compliance_rag.corpus.second_source import ClauseCompletion
from compliance_rag.corpus.selection_manifest import parse_selection_manifest

INSTRUMENT_ID = "privacy-act-1988"
VERSION_ID = "C2026C00227"
TITLE = "Privacy Act 1988"
"""The Act's title as the compilation's own `<title>` gives it: what the
ledger records with the version (ADR-0049). Library law's display form doesn't
name it ("APP 11.1"), but no version is recorded without its title."""
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
VERSION_DIR = (
    Path(__file__).resolve().parents[1] / "corpus" / "instruments" / INSTRUMENT_ID / VERSION_ID
)
MANIFEST = VERSION_DIR / "manifest.yaml"
PDF_URL = f"https://www.legislation.gov.au/{SERIES_ID}/{START}/{START}/text/original/pdf"
PDF_SHA256 = "443e895f1e1536763af78e4553d469b0857a84553ca0c011ed796a623f954e38"
"""The Register's PDF of the same compilation, fetched 2026-11-08 (#276) and
2026-11-11 (#338): the second source s 6DA(2)'s formula is read from
(ADR-0058). Its own pin, beside the ePub's."""
SECOND_SOURCES = {"s6da/2": PDF_SHA256}
"""Every clause of this version whose text has a second source, with that
source's pin: what a correction checks the parse against (ADR-0058)."""


def pinned_source(html_path: Path | None) -> str:
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


def pinned_pdf(pdf_path: Path | None) -> bytes:
    raw = (
        pdf_path.read_bytes()
        if pdf_path is not None
        else httpx.get(PDF_URL, timeout=120, follow_redirects=True).raise_for_status().content
    )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != PDF_SHA256:
        raise SystemExit(
            f"PDF sha256 {digest} is not the pinned {PDF_SHA256}: the Register served "
            "different bytes — re-run the formula tests on them before using them"
        )
    return raw


def signed_formulas() -> tuple[SignedFormula, ...]:
    """The version's signed formulas (`formula.yaml`), each pinned to the
    source `SECOND_SOURCES` pins for its clause."""
    signed = load_signed_formulas(VERSION_DIR)
    pins = {f.clause_path: f.source.sha256 for f in signed}
    if pins != SECOND_SOURCES:
        raise SystemExit(
            f"the signed formulas pin {pins}; the version's second sources are {SECOND_SOURCES}"
        )
    return signed


def pinned_completions(pdf_path: Path | None) -> tuple[ClauseCompletion, ...]:
    """Every clause completion this version's parse takes: the signed
    formulas, read from the pinned PDF (ADR-0058)."""
    signed = signed_formulas()
    if not signed:
        return ()
    pdf = pinned_pdf(pdf_path)
    return tuple(formula_completion(pdf, formula) for formula in signed)


async def main(
    bucket: str, html_path: Path | None, pdf_path: Path | None, acknowledged: frozenset[str]
) -> None:
    selection = parse_selection_manifest(MANIFEST.read_text(encoding="utf-8"))
    store = GCSClauseStore.for_bucket(bucket)
    result = await ingest_compilation(
        pinned_source(html_path),
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        tenant_id="_public",
        scope=Scope.PUBLIC,
        selection=selection,
        store=store,
        acknowledged_additions=acknowledged,
        completions=pinned_completions(pdf_path),
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
    parser.add_argument("--pdf", type=Path, default=None)
    parser.add_argument(
        "--acknowledge",
        nargs="*",
        default=[],
        metavar="CLAUSE_PATH",
        help="every clause path a reviewed manifest adds to an already-ingested version, "
        "exactly as the refusal lists them (issue #19's drift check)",
    )
    arguments = parser.parse_args()
    asyncio.run(
        main(arguments.bucket, arguments.html, arguments.pdf, frozenset(arguments.acknowledge))
    )
