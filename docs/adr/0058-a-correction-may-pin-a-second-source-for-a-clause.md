# ADR-0058 — A correction may pin a second source for one clause; the signed form supplies structure only

- Status: accepted for the mechanism; the cloud correction and the GA-04 labels
  wait on the human (below).
- Date: 2026-11-11
- Issue: #338 (research: #276, `docs/research/s6da-2-pdf-extraction.md`)
- Extends: ADR-0042 (the `correct` path), ADR-0047 (completing an incomplete
  clause, Decision 8), ADR-0039 (the text-node conservation test)
- Touches: ADR-0052 (who authors a reviewed file), D-01

## Context

s 6DA(2)'s turnover formula is an image in the pinned ePub (sha256
`6d0a43e5…5c3b`), so the parse stores the clause incomplete (ADR-0047). The
Register's PDF of the same compilation, C2026C00227 (sha256
`443e895f1e1536763af78e4553d469b0857a84553ca0c011ed796a623f954e38`), sets it as
text on page 62: seven spans of words, one `SymbolMT` glyph whose `ToUnicode`
says U+00B4 but which is drawn as `×`, and a vector line for the fraction bar
(#276). The words are parse-grade; how a fraction is written in a line of text
is not something the PDF says.

On 2026-11-11 Lucas Frasca signed the canonical text (#338, "sign 1"):

> However, if a business has been carried on for only part of a financial
> year, its annual turnover for the financial year is the amount worked out
> using the formula: Amount that would be the annual turnover of the business
> under subsection (1) if the part were a whole financial year × (Number of
> days in the whole financial year ÷ Number of days in the part)

with the `×` (U+00D7), the `÷` and the brackets as the signed convention.

ADR-0042's `correct` refuses unless the source parsed is the version's one
pinned source. The ePub alone can't supply these words.

## Decision

**1. A clause may have a second source: the same compilation's other
rendering, pinned by its own sha256.** The version id stays C2026C00227: the
Register published one compilation in two renderings, and the law didn't
change. `scripts/ingest_compilation.py` pins it beside the ePub
(`PDF_SHA256`, `SECOND_SOURCES = {"s6da/2": PDF_SHA256}`).

**2. The PDF supplies the words; the signed form supplies only the
structure.** `corpus/instruments/privacy-act-1988/C2026C00227/formula.yaml`
is a reviewed artefact (D-01): author `model:claude-opus-5-5` (drafted
2026-11-08, ADR-0052), reviewed by Lucas Frasca, 2026-11-11, naming `s6da/2`,
the PDF's URL, page and sha256, the reading convention (numerator above the
bar; the `SymbolMT` glyph the text layer gives as `´` reads as `×`), the form
`{left} {operator} ({numerator} ÷ {denominator})`, the words of each block and
the signed canonical text. It is refused while pending.

`corpus/pdf_formula.py` reads the page by geometry alone (the model's call on
the rule, from #276): the operator is the page's one span in the signed font;
the formula is the spans in the operator's type size; the bar is the one rule
inside them; a span over the bar's width is numerator above it and denominator
below it; every other span is left of the operator. Each block's words must be
the signed words in the same order. It fails loudly on other bytes, a span
straddling the bar, no bar or two, another operator font or glyph, or other
words. Nothing is completed from a reading that doesn't match.

**3. A completion is applied to the parse, never to a store.**
`parse_compilation(..., completions=)` appends the formula to the clause the
ePub gave and removes exactly the reason it completes; it is refused unless
the clause is in the parse, marked for that reason, and comes out as the
signed canonical text. Every store gets it the way it gets any parse: ingest
(`ingest_compilation(..., completions=)`), the graph load, the export of
`clauses.json`, and the revision.

**4. `correct` pins the second source.** The plan lists each clause the parse
completed and its source (`RevisionPlan.second_sources`). `check_correction`
refuses unless those are exactly the version's pinned second sources, by clause
and sha256, before the acknowledgement is looked at. The record carries
`second_sources` beside `source_sha256` (absent when there are none, so every
earlier record reads back unchanged), and the clause's graph trail entry names
its source. `finish` refuses a parse whose second sources differ from the
record's. Write order, acknowledgement and findings count are ADR-0042's.

**5. Anchors and the position map stay the ePub's** (the model's call).
`source.html` is unchanged, so the "anchors exactly as stored" check holds and
click-to-clause opens s 6DA(2)'s block, where the image is. The formula's
words are not in `source.html`; the verifier checks quotes against canonical
text, which holds them. Pointing the position map at PDF page 62 is left for
when a reader asks for it.

**6. ADR-0039's conservation test learns the second source.** The completed
parse stores the ePub's operative words plus exactly the formula's: the PDF's
words once each, and the signed form's marks (`×`, `÷`, the brackets), all on
`s6da/2` (`tests/test_s6da_2_formula.py`). The ePub-only count is unchanged.

**7. CI reads a committed cut of the page; the PDF is cached as the ePub is.**
The PDF is a raw source and isn't committed. `scripts/cut_formula_page.py`
writes `docs/research/fixtures/privacy-act-1988-C2026C00227.pdf-page-62.json`
(the page's spans and rules, as `extract_page` reads them from the pinned
bytes), as the constructs fixture is cut from the ePub. CI reads the formula
from it, and tampered copies of it fail. With the PDF on disk,
`PRIVACY_ACT_COMPILATION_PDF` turns on the test that the real bytes read to
exactly that cut and the signed form; CI doesn't download it, as it doesn't
download the ePub (`PRIVACY_ACT_COMPILATION_HTML`). `pymupdf` was already a
dependency (the policy parser); nothing is added.

## Consequences

- **The companion files are retired from the corpus**: `incomplete.yaml` and
  `part_year.yaml` described a clause that is now complete, and a reading of a
  complete clause is dead. They move unchanged, signatures and all, to
  `tests/fixtures/incomplete_readings/`, where the incomplete-clause mechanism
  (kept for the next image, table or penalty line) is tested on the corpus as
  it stood before #338 (`tests/before_338.py`).
- **The three part-year conditions' logic doesn't change.**
  `COND-PA-TURNOVER-PREVIOUS-YEAR-UNDER`, `COND-PA-TURNOVER-CURRENT-YEAR-UNDER`
  and `COND-PA-S6D4A-EVER-OVER-THRESHOLD` already ask for a part year's figure
  "by the s6DA(2) formula". With the clause complete, a part year is decided
  from its figure like any other, as before 2026-10-20: the start year is asked
  for again, and GA-04 goes back to "the APPs don't apply". The labels are
  re-drafted by the model and PENDING Lucas Frasca's signature; the gate
  refuses them until he signs, so this PR's CI is red there by design.
- **The embedding cache and the recall baseline need a live refresh**:
  `s6da/2`'s chunk text changed, so the recall gate misses one vector until
  `scripts/refresh_golden_embeddings.py` runs (Vertex, under a cent) and the
  baseline is re-recorded on the new corpus.
- **The deployed stores need the correction**, run by the lead after merge
  with `--acknowledge`, as ADR-0042's was: bucket manifest, then graph, then
  Qdrant (one chunk re-embedded).
- **The corpus version moves** (ADR-0034): the parser, `formula.yaml`,
  `clauses.json` and the readers are under its paths.
