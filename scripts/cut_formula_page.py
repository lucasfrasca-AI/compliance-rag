"""Write the committed cut of the PDF page s 6DA(2)'s formula is on (#338).

`docs/research/fixtures/privacy-act-1988-C2026C00227.pdf-page-62.json` is the
page's text spans and horizontal rules exactly as `extract_page` reads them
from the pinned PDF compilation (sha256 checked): the PDF is a raw source and
is not committed, as the ePub isn't, and this cut is what CI reads the formula
from, as `privacy-act-1988-C2026C00227.constructs.html` is cut from the ePub.
With the PDF on disk, `PRIVACY_ACT_COMPILATION_PDF` turns on the test that a
fresh extraction is this file exactly. Never edited by hand.

    uv run python -m scripts.cut_formula_page [--pdf path/to/C2026C00227.pdf]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from compliance_rag.corpus.pdf_formula import extract_page
from scripts.ingest_compilation import pinned_pdf, signed_formulas

FIXTURES = Path(__file__).resolve().parents[1] / "docs" / "research" / "fixtures"


def cut_path(page: int) -> Path:
    return FIXTURES / f"privacy-act-1988-C2026C00227.pdf-page-{page}.json"


def main(pdf_path: Path | None) -> None:
    pdf = pinned_pdf(pdf_path)
    for page in sorted({f.source.page for f in signed_formulas()}):
        path = cut_path(page)
        cut = extract_page(pdf, page=page)
        path.write_text(json.dumps(cut.to_json(), indent=1, ensure_ascii=False) + "\n", "utf-8")
        print(f"page {page}: {len(cut.spans)} spans, {len(cut.rules)} rules -> {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=None)
    main(parser.parse_args().pdf)
