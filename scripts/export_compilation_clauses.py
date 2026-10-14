"""Write the recall gate's law corpus: the pinned compilation's selected
clauses, as the parser emits them (issue #180), to
`corpus/instruments/privacy-act-1988/C2026C00227/clauses.json`.

The only writer of that file. Refuses any bytes that aren't the pinned sha256
(`pinned_source`, shared with `scripts/ingest_compilation.py`), parses them with
the current parser, selects through the reviewed `manifest.yaml`, and renders
them. The raw source is not committed; this file is. Re-run it whenever the
parser or the manifest changes, then refresh the embedding cache
(`scripts/refresh_golden_embeddings.py`) and re-baseline the gate deliberately.

Reads a local copy, or fetches the Register's ePub HTML (public, no cloud):

    uv run python -m scripts.export_compilation_clauses [--html path/to/document_1.html]
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from compliance_rag.eval.compilation_clauses import (
    COMPILATION_CLAUSES_PATH,
    render_compilation_clauses,
    select_compilation_clauses,
    selection_manifest,
)
from scripts.ingest_compilation import INSTRUMENT_ID, SOURCE_SHA256, VERSION_ID, pinned_source


def main(html_path: Path | None) -> None:
    selection = selection_manifest()
    clauses = select_compilation_clauses(
        pinned_source(html_path),
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        selection=selection,
    )
    COMPILATION_CLAUSES_PATH.write_text(
        render_compilation_clauses(
            clauses, source_sha256=SOURCE_SHA256, selection=selection.prefixes
        ),
        encoding="utf-8",
    )
    by_section = Counter(c.clause_path.split("/")[0] for c in clauses)
    print(f"{len(clauses)} clauses -> {COMPILATION_CLAUSES_PATH}")
    print({section: by_section[section] for section in sorted(by_section)})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--html", type=Path, default=None)
    main(parser.parse_args().html)
