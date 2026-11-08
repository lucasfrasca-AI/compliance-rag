# s 6DA(2) turnover formula: extraction from the PDF compilation

Spike, 2026-11-08. No store was written and nothing under `corpus/` changed.
Issue context: ADR-0047 Decision 8 ("extraction from the PDF compilation");
the human's decision of 2026-11-08 was "try extracting from the PDF
compilation first. If that fails, come back and I'll transcribe and sign it."

**Result: the extraction did not fail.** The formula is real, selectable text
in the PDF, with the fraction bar as a vector line. Its words are extractable
exactly. Its two-dimensional structure and its multiplication sign are not
extractable as plain text without a rule that someone has to decide (see
"Parse or transcription"). The image alt text was not used anywhere.

## Source

- Fetched from the Federal Register of Legislation, same compilation as the
  pinned ePub (C2026C00227, series C2004A03712, start 2026-06-04):

  `https://www.legislation.gov.au/C2004A03712/2026-06-04/2026-06-04/text/original/pdf`

  This is the same `{series}/{start}/{retrospectiveStart}/text/original/…`
  path family `scripts/ingest_compilation.py` uses for the unpacked ePub
  (`SOURCE_URL`), with `/pdf` in place of `/epub/OEBPS/document_1/document_1.html`.
  Plain HTTP GET, no JavaScript, HTTP 200, `content-type: application/pdf`,
  `content-disposition: attachment; filename=C2026C00227.pdf`, 1,920,725
  bytes, served by the Register's CDN on 2026-11-08.
- SHA-256: `443e895f1e1536763af78e4553d469b0857a84553ca0c011ed796a623f954e38`
- PDF 2.0, 472 pages, `modDate` 2026-06-17. Each page footer reads
  "Authorised Version C2026C00227 registered 17/06/2026" and the header
  "Compilation No. 104 / Compilation date: 04/06/2026", so it is the
  authorised rendering of the same compilation. (The ePub pin is
  `6d0a43e5…5c3b`; a PDF pin would be a second, separate sha256.)
- Saved only in the session scratch area, not committed.

## Where s 6DA(2) is

s 6DA is on PDF page 61 (printed page 43). Its lead-in ends at the foot of
that page: "(2) However, if a business has been carried on for only part of a
financial year, its annual turnover for the financial year is the amount worked
out using the formula:". That sentence is word-for-word the stored `s6da/2`
`canonical_text` (line breaks aside). **The formula itself sits at the top of
PDF page 62** (printed page 44), above the "6E" heading, on the page after its
lead-in. s 6DA has no subsection after (2).

## How the formula is encoded

Probed with pymupdf 1.28.2 (`get_text("rawdict")`, `get_images`,
`get_image_info`, `get_drawings`) on pages 61 and 62.

| Probe, page 62 | Finding |
| --- | --- |
| `get_images()` / `get_image_info()` | empty: **no raster image** |
| `get_drawings()` | 3 stroked lines: two page rules (header y=104, footer y=612.8) and one **fraction bar**, x 319.2 to 427.9, y 148.7, width 0.496 |
| text spans in the formula region | 7 text spans, 9.9 pt `TimesNewRomanPSMT` (embedded TrueType, WinAnsi), all real characters |
| operator | 1 span, `SymbolMT` (Type0, Identity-H), `ToUnicode` maps its one glyph to **U+00B4 (acute accent `´`)**, but the glyph drawn is the multiplication sign `×` (confirmed by rendering) |

So: real text for every word, a vector line for the bar, and a symbol-font
glyph for the operator whose Unicode mapping is wrong. Rendering the region at
200 dpi shows (left to right) a four-line left block, `×`, and a fraction with
a two-line numerator over a one-line denominator.

The only `SymbolMT` span in the entire 472-page PDF is this `×`.

### Raw extracted text (verbatim)

Spans with bounding boxes (x0, y0, x1, y1; the PDF's y grows downwards), in the
order pymupdf returns them:

```
(329.5, 126.2, 418.2, 137.1) 'Number of days in the'
(190.3, 127.7, 294.1, 138.6) 'Amount that would be the'
(333.3, 136.1, 413.9, 147.0) 'whole financial year'
(180.0, 137.6, 304.2, 148.5) 'annual turnover of the business'
(180.8, 147.4, 303.2, 158.3) 'under subsection (1) if the part'
(320.3, 150.1, 426.9, 161.0) 'Number of days in the part'
(187.6, 157.3, 296.7, 168.2) 'were a whole financial year'
(308.7, 141.2, 314.2, 153.3) '´'          # SymbolMT, U+00B4
fraction bar: y = 148.7, x 319.2 to 427.9
```

Plain `page.get_text()` returns the same seven spans in content-stream order and
places them at the *end* of the page text, after the 6E body and its Notes,
just before the footer:

```
Number of days in the
Amount that would be the
whole financial year
annual turnover of the business
under subsection (1) if the part
Number of days in the part
were a whole financial year
´
Authorised Version C2026C00227 registered 17/06/2026
```

`page.get_text(sort=True)` puts them at the top of the page body but still
interleaves the left block with the numerator by y coordinate.

### Reading-order problems

1. **Interleaving.** The left block (4 lines, vertically centred on the bar)
   and the fraction (2 lines over the bar, 1 under) overlap in y. Neither the
   stream order nor a y-sort gives a readable sequence: "Number of days in the"
   comes before "Amount that would be the", and "Number of days in the part"
   falls between two lines of the left block.
2. **Numerator wraps.** "Number of days in the / whole financial year" is one
   phrase on two lines (126 and 136); a line-by-line extraction cuts it.
3. **Which side of the bar.** The only evidence that "Number of days in the
   part" is the denominator and "Number of days in the whole financial year"
   the numerator is geometry: the first two spans end above y=148.7 and the
   last starts below it, and all three are horizontally inside the bar's x
   range (319.2 to 427.9).
4. **The operator.** Text extraction yields `´` (U+00B4). The glyph is `×`
   (U+00D7). Nothing in the PDF's text layer says so; it follows from the
   font's identity (Adobe Symbol code 0xB4 is the multiplication sign) and
   from the render.
5. **Page break.** The lead-in is on page 61 and the formula on page 62; a
   page-by-page parser would not attach the formula to s 6DA(2) without a
   cross-page rule.

### What a deterministic extraction yields

Reading the geometry (left block, then operator, then numerator over
denominator), the formula's content is:

- left factor: "Amount that would be the annual turnover of the business under
  subsection (1) if the part were a whole financial year"
- operator: `×`
- right factor, a fraction: numerator "Number of days in the whole financial
  year", denominator "Number of days in the part"

The words are exactly the Register's. There is no one canonical linear string,
because CONTEXT.md's canonicalisation (NFC, quote and dash folds, NBSP,
whitespace collapse, strip) is defined for running text and says nothing about
a fraction. Every linearisation adds a symbol the page doesn't contain as a
character, for example:

```
However, if a business has been carried on for only part of a financial year, its annual turnover for the financial year is the amount worked out using the formula: Amount that would be the annual turnover of the business under subsection (1) if the part were a whole financial year × Number of days in the whole financial year / Number of days in the part
```

The `/` (or "over", or a bracketed form) is the author's convention, not the
Act's character. This line is illustrative of the problem, not proposed as the
canonical text.

## Parse or transcription

**The content is parse-grade. The structure is not, on its own.**

For a parse (D-01 "parsed, never written"):

- Every word comes from the Register's authorised rendering as characters, not
  an image, so no OCR and no model is needed to read them.
- The extraction is reproducible and pinnable by its own sha256, and the
  lead-in sentence in the PDF matches the stored `s6da/2` text exactly, which
  is the cross-check ADR-0047 asks for before trusting a PDF parse.

Against:

- The text layer alone does not say what the formula is. Left block, operator,
  numerator and denominator are recovered from geometry (a vector bar, y
  ranges, x overlap) by a rule written for this one formula. The rule is
  deterministic, but nothing in the source says its output is the Act's
  meaning: a one-line mistake in the bar test would silently swap numerator and
  denominator, which changes the turnover figure a small-business test is run
  on, and the verifier cannot catch it (stored text matches itself, as in #178).
- The operator needs a glyph-to-character decision against the PDF's own
  (wrong) `ToUnicode`.
- The linear form needs a convention that is a human choice and that the
  verifier's quoted-span matching will then hold the whole corpus to.

I therefore judge it **not safely a pure parse**, and **not a pure
transcription either**: the model should not write the formula, and a human
should not have to read it off an image again when the Register supplied the
characters. The route I'd recommend, for the human to decide:

1. The human signs the canonical linear form and the reading convention
   (which factor is the numerator, how a fraction is written, `×` as U+00D7)
   as a reviewed artefact under D-01: `author`, `reviewed_by`, `date`, naming
   `s6da/2`, the PDF page and the PDF's sha256. The model may draft it, marked
   pending as CONTEXT.md and ADR-0047 require.
2. A parser step then **extracts the PDF's formula words and checks them
   against that signed form** (same words, same order within each block,
   numerator above the bar, `×` from `SymbolMT`), and fails loudly if the
   source bytes or the geometry differ. The signed form supplies only the
   structure; the PDF supplies, and constrains, the words.

That keeps "parsed, never written" for the words and puts the one genuine
judgement (the representation of a fraction in a text corpus) where D-01
already puts judgement: with a signed human. If the human prefers not to build
a PDF extractor for one formula, a transcription signed against page 62 is
equivalent in risk, and the PDF extraction in this note is then the check that
the transcription's words are the Register's.

## How a completing parse goes through the ADR-0042 `correct` path

ADR-0042 Decision 2 keeps the `version_id` (C2026C00227) because the law did
not change, only how it was parsed. A formula supplied from the PDF fits that:
the Register published the same compilation twice, in two renderings. Mechanics
and the places it does not fit as written:

1. **Plan.** A fresh parse of the version's sources compared clause by clause
   with the bucket. Only `s6da/2` would change (text and canonical text
   gain the formula; `incomplete` becomes empty). The clause set is unchanged,
   so the "no addition, no removal" check holds.
2. **Pin check needs extending.** `correct` today refuses unless "the source
   parsed is the version's pinned bytes (its sha256 is the pin)". The ePub
   HTML alone cannot supply the words. The revision must pin a second source,
   the PDF, by its own sha256 (`443e895f…4e38`), recorded in the manifest and
   in the revision record next to `6d0a43e5…5c3b`. This is an ADR-0042
   extension (an ADR when it is built), not a new version.
3. **Anchors.** `source.html` is byte-identical, so the "anchors exactly as
   stored" check holds. But the formula's words are not in `source.html`: the
   click-to-clause position map for `s6da/2` would point at the HTML block
   that holds the image, and the verifier's quoted-span check runs against
   canonical text, which would now contain words absent from the stored HTML.
   Whether the position map should also reference the PDF page is a design
   question for the ticket. ADR-0039's text-node conservation test would need
   to learn that this clause's text has a second source.
4. **Acknowledgement and record.** Names exactly `s6da/2`, who authorised it,
   why, and the findings count citing it (three signed conditions cite `s6da/2`
   via mappings; the count is of tenant findings citing the clause by exact id).
   The record keeps the previous hash and the previous incomplete state, so the
   trail shows the clause was incomplete and who completed it (ADR-0047,
   "What un-marks a clause").
5. **Write order** as ADR-0042 Decision 4: bucket manifest (the commit), then
   graph (`canonical_sha256` moves, a recorded load), then Qdrant (re-embed the
   one chunk). One clause, so the part-way states are small.
6. **Companion files.** `incomplete.yaml` (clause reading) and `part_year.yaml`
   (the Track A part-year scoping) were written because the clause is incomplete.
   Once it is complete they would be stale and must be retired or reviewed in
   the same change; they are not touched here. The three conditions that return
   unassessable for part years would need their own review (they were made
   unassessable *because* the formula was absent).

## Reproduce

```
curl -sSL -o C2026C00227.pdf \
  https://www.legislation.gov.au/C2004A03712/2026-06-04/2026-06-04/text/original/pdf
shasum -a 256 C2026C00227.pdf   # 443e895f1e1536763af78e4553d469b0857a84553ca0c011ed796a623f954e38
```

then, with pymupdf, page index 61: `get_text("rawdict")`, `get_images()`,
`get_drawings()`.
