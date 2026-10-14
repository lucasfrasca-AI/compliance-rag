# legislation.gov.au format spike (issue #9)

**Go/no-go: GO**, with four concrete follow-ups the parser ticket needs to carry (not re-planned, but not "just parse the tags" either — see [Action items](#action-items-for-the-parser-ticket)).

Investigated: Privacy Act 1988, series `C2004A03712`, compilation `C2026C00227` (compilation No. 104, effective 4 June 2026). All findings below are read from the actual bytes fetched at the URL in [Fetching the bytes](#fetching-the-bytes) — a fixture excerpted verbatim from that response is checked in at `docs/research/fixtures/privacy-act-1988-C2026C00227.excerpt.html` (`corpus/instruments/**/raw/` is gitignored — raw sources are fetched, not committed — so the spike's own fixture lives beside this report instead).

## Fetching the bytes

**What didn't work.** `www.legislation.gov.au` is an Angular SPA; every human-facing URL (`/C2004A03712/C2026C00227/latest/text`, `.../latest/downloads`, `.../latest/epub`) returns the same ~300KB Angular shell over HTTP, regardless of path — the actual page is built client-side by JavaScript. This is exactly the trap the spike exists to catch: a markdown-converting fetch of that URL wouldn't give you empty content or an error, it would confidently markdownify a JavaScript app shell and never contain the Act's text at all.

The site's own API (`api.prod.legislation.gov.au`, an OData v4 service) *can* answer questions about a compilation — title, version dates, available document formats and their sizes — via root-collection queries with `$filter`/`$expand`, e.g.:

```
GET https://api.prod.legislation.gov.au/v1/Titles('C2004A03712')?$expand=versions
GET https://api.prod.legislation.gov.au/v1/Documents?$filter=titleId eq 'C2004A03712' and registerId eq 'C2026C00227'
```

The second call confirms the compilation has `Word`, `Epub` and `Pdf` renditions (234,835 bytes for the Epub) — but every key-based access pattern (`Documents(...)`, `Versions(...)`, bound functions like `Find`/`GetZipItems`) 404s: this OData service only supports composite-keyed entities via `$filter` on the collection, not direct key addressing, and it never populates the `bytes`/`contents` fields on a `Document` row through any query shape tried. **The raw `.epub` zip is not fetchable through the public API.**

**What worked.** The website's own "text" viewer is server-side–rendered (Angular Universal), and its embedded hydration state (visible in the plain HTML response, no JS execution needed) contains real links into the *unpacked* ePub — the server extracts the epub once and serves its `OEBPS` contents as plain static files:

```
https://www.legislation.gov.au/{seriesId}/{start}/{retrospectiveStart}/text/original/epub/OEBPS/document_1/document_1.html
https://www.legislation.gov.au/{seriesId}/{start}/{retrospectiveStart}/text/original/epub/OEBPS/document_1/styles.css
```

`{start}` and `{retrospectiveStart}` are the compilation's effective-date fields (`2026-06-04` for `C2026C00227` here) — **not** the human-facing compilation id. So the parser's fetch is two steps: resolve `compilationId → (start, retrospectiveStart)` via the OData `Titles(...)?$expand=versions` call above (matching on `registerId`), then build the URL. This returned `HTTP 200`, `content-type: text/html`, 2,358,278 bytes — the entire Act (both Schedules, endnotes, amendment history) in one file, no `document_2`.

This is the same content that would come out of unzipping the downloadable `.epub` (same `OEBPS/` path, same `styles.css`, no `META-INF`/`content.opf` served standalone at that path, confirming it's the epub's insides exposed directly rather than a separate export). Fetching it this way — one deterministic GET, no zip handling — is a *better* integration point for the parser than downloading and unzipping a `.epub` client-side would be, so this is what `corpus/instruments/`'s parser should use.

## Tags, classes and attributes that carry clause structure

The markup is Word-export HTML (inline `style=` attributes for indentation, `&#xa0;` spacer spans, no semantic HTML5 elements at all — no `<section>`, `<article>`, `<ol>`). Structure lives entirely in a fixed, small set of CSS class names on `<p>` (there is no nesting — the whole document is a flat sequence of sibling `<p>` elements; hierarchy is implied by which heading class most recently appeared, not by DOM nesting):

| Class | Carries |
|---|---|
| `ActHead1` | Schedule heading. Number in `<span class="CharChapNo">`, title in `<span class="CharChapText">`. |
| `ActHead2` | Part heading. Number in `<span class="CharPartNo">`, title in `<span class="CharPartText">`. |
| `ActHead3` / `ActHead4` | Division / Subdivision headings (same pattern, not directly inspected — same convention held for every other level, so assumed consistent; low risk). |
| `ActHead5` | Section heading — used for ordinary Act sections **and** for each Australian Privacy Principle inside Schedule 1, at the same nesting depth. Number in `<span class="CharSectno">`, title as plain trailing text. |
| `subsection` | A numbered subsection, e.g. `(1)` or `11.1` — the number itself is **plain text inside an unclassed `<span>`**, not its own element. |
| `paragraph` / `paragraphsub` / `paragraphsub-sub` | Lettered paragraph, sub-paragraph, sub-sub-paragraph — `(a)`, `(i)`, `(A)`. Same story: the letter is plain text. |
| `subsection2` | The concluding operative sentence after a list of lettered conditions (the "...the entity must..." that follows a chapeau + `(a)`/`(b)`/`(c)` list). Still part of the same subsection, not a new one. It also appears between two paragraphs (a connector such as s6(1) *record*'s "but does not include:", or s7(1)'s "but does not include a reference to an act done …"), and inside a `Definition`, where it belongs to the definition (issues #178 and #186, ADR-0039). |
| `SubsectionHead` | An informal drafting sub-heading inside a section/APP (e.g. "Overview", "Refusal to correct information") — prose, not a numbered clause. Must be recognised and skipped/merged, not mistaken for a new subsection. |
| `Definition` | A defined term (`organisation means: ...`). Used heavily in s6 (~hundreds, one per term, all nested under a single `(1)` with no further subsection breaks between them). |
| `notetext` / `notepara` / `notemargin` | Notes and examples — not part of the operative text. |
| `Tabletext`, `TableHeading`, `Table*` | Tables — seen in the endnotes/amendment-history section (not in the operative Act text checked). |
| `TOC1`–`TOC5` | Table of contents — not body content, must be skipped entirely. |

**Numbers are only semantically marked up at the section/Part/Schedule level** (`CharSectno`/`CharPartNo`/`CharChapNo`). Below that — subsection numbers, paragraph letters — the number is indistinguishable, markup-wise, from surrounding text; a parser must regex the leading token out of each `subsection`/`paragraph*` element's text content (`^\(\d+\)`, `^\d+\.\d+`, `^\([a-z]+\)`, `^\([ivx]+\)`, `^\([A-Z]\)`).

## Is the hierarchy machine-readable or presentational?

**Machine-readable, via a state machine — not presentational soup, but not free either.** A parser can walk the flat `<p>` sequence and reconstruct the full tree (Schedule → Part → Section/APP → subsection → paragraph) purely from the class-name sequence, without any layout heuristics (no need to read `style=` widths or infer nesting from indentation). But two things make it a real parsing job, not a markup-to-clause_path mechanical mapping:

1. **Section numbers are not globally unique.** `CharSectno` = `"6"` appears three times in this one instrument: main-body s6 ("Interpretation"), APP 6 inside Schedule 1 ("use or disclosure of personal information"), and Schedule 2's own independent s6 ("Interpretation", Schedules restart their own numbering). The parser must track which container (main body / Schedule 1 / Schedule 2) it's currently inside to build the right `clause_path` prefix — the number alone is ambiguous.
2. **The Schedule 1 numbering scheme changes register.** Main-body subsections are `(1)`, `(2)`... but APP subclauses are `11.1`, `11.2`, `11.3` — `{APP number}.{subclause number}`, matching `CONTEXT.md`'s own `sch1/app11/1` example exactly. A single subsection-number regex has to handle both forms.

## How ss 6, 6C, 6D, 6E nest; subsection/paragraph distinguishability

Verified directly (`document_1.html`, byte offsets 97,854 / 243,663 / 256,511 / 270,811 for ss 6 / 6C / 6D / 6E respectively):

- Each of 6, 6A–6F is its own `ActHead5`, a sibling of every other section — there is no wrapper element grouping "the 6-series" together; adjacency in the flat `<p>` sequence is the only relationship, exactly as for any other pair of sections.
- **s6 ("Interpretation")** is one `(1)` subsection containing ~hundreds of flat `Definition` paragraphs, alphabetically ordered, with no further subsection breaks between them (e.g. `ACC means...`, `access seeker has the meaning given by...`). This is a real open design question for the parser ticket: `CONTEXT.md`'s clause_path grain (`s6c/1/a`) implies subsection/paragraph-level addressing, but s6 has no paragraph-level structure at all inside `(1)` — just a long flat list. Whether each definition becomes its own clause (and what its path segment would be, since it has no letter or number) needs a decision before ingesting s6.
- **s6C ("Organisations")** shows the full richness in one place: `ActHead5` → `subsection (1)` ("In this Act:") → `Definition` (`organisation means:`) → five `paragraph` items `(a)`–`(e)` → `subsection2` (the concluding "...that is not a small business operator...") → two `notetext` notes → an `Example`. Subsection and paragraph levels **are** distinguishable — by class name, not by number format — `subsection` vs `paragraph` vs `paragraphsub` are always distinct classes at each depth.
- **s6D/6E** ("Small business and small business operators" / "Small business operator treated as organisation") follow the identical `subsection`/`paragraph`/`Definition` pattern; no new structural forms.

## Stable anchors for click-to-clause

**Not below section/Part/Schedule level.** Every heading paragraph (`ActHead1`–`ActHead5`) carries a stable `id` (`navPoint_NNN`, sequential document order) and its inner `<a>` carries a second, Word-generated TOC bookmark id (`_TocNNNNNNNNN`). Neither is derived from the section/clause number — both are positional, so a parser can capture "APP 11's heading is `navPoint_422`" at parse time but can't compute that id from `"sch1/app11"` afterwards without having parsed the document first.

**Individual subclauses and paragraphs have no `id` at all** — `<p class="subsection">` and `<p class="paragraph">` are bare. `CONTEXT.md`'s Position Map entry ("law resolves to an anchor in the stored HTML") doesn't hold at full `clause_path` granularity out of the box: the parser has to *synthesize* the anchor (e.g. record a char-offset range into the stored HTML per clause, the same mechanism already used for uploaded PDFs) rather than relying on a pre-existing one. This is the most consequential finding in this spike for click-to-clause design and is called out separately below.

## Whitespace and entities

- `&#xa0;` (non-breaking space) is used two ways: as real inter-word spacing (`"11"` + `&#xa0;` + `"Australian Privacy Principle"`) and as the *entire content* of invisible indentation-spacer `<span>`s. Both collapse correctly under `canonicalise()`'s existing nbsp → space folding and whitespace-run collapsing — **no gap here**.
- `&#x2011;` (**non-breaking hyphen**, U+2011) is used throughout for compound words the drafter didn't want to break across a line: `cross‑border`, `out‑of‑date`, `de‑identified`, `Self‑Government`. **`canonicalise()` does not fold this to ASCII `-`** — it only maps en dash and em dash (U+2013/U+2014). NFC normalisation doesn't touch it either (verified: `unicodedata.normalize("NFC", "cross‑border")` is a no-op). A citation quoting "cross-border" (typed with an ordinary hyphen) would fail to match canonical text containing "cross‑border" (U+2011) — a real false-negative risk for D-02's verifier. **Action item, not a blocker** (see below).
- Curly quotes appear in body text too (`"Independent Commissioner Against Corruption..."` in the endnotes uses `"`/`"`) — already handled by the existing canonicalisation table.

## Fixture

`docs/research/fixtures/privacy-act-1988-C2026C00227.excerpt.html` — two byte-verbatim, contiguous excerpts from the real response (concatenated, each internally unedited, each labelled with its source byte offset in an HTML comment): section 6C in full, and Australian Privacy Principle 10 through 11 plus the Part heading that follows. Between them they exercise every paragraph class this spike found in the operative text (`ActHead1/2/5`, `subsection`, `subsection2`, `paragraph`, `paragraphsub`, `Definition`, `notetext`). Parses cleanly with the stdlib `html.parser`. When the parser ticket starts, it should move (or be pointed at) whatever location the parser's own tests standardise on — this just needed a committed home outside the gitignored `corpus/instruments/**/raw/`.

## Action items for the parser ticket

None of these are a no-go — the markup is machine-derivable — but the parser estimate should account for all four:

1. **Resolve compilation id → effective date before fetching.** The download URL is keyed by `(start, retrospectiveStart)` date, not by `registerId`; requires one OData call per compilation before the HTML fetch.
2. **Track container context, not just class name.** Section numbers repeat across main body / Schedule 1 / Schedule 2; the parser needs a stack (current Schedule, current Part) to build correct `clause_path` prefixes, not a flat class → clause_path mapping.
3. **Synthesize clause-level anchors.** No pre-existing `id` exists below section/APP granularity; click-to-clause needs the parser to record its own position map (char offsets, matching the existing PDF mechanism) rather than reading one off the markup.
4. **Fix `canonicalise()` to fold U+2011** (non-breaking hyphen) to ASCII `-`, alongside the existing en/em dash handling, before any real ingestion — otherwise expect silent citation-verification false negatives on any compound word the drafter hyphenated.

A fifth, open design question rather than an action item: how (or whether) to give each of s6's ~hundreds of flat, unnumbered `Definition` paragraphs its own `clause_path`, since they have no subsection/paragraph-level structure to anchor one.

## Assumption the parser (#17) carries forward, unverified beyond this spike

Everything above — the fixed class-name vocabulary (`ActHead1`–`ActHead5`, `subsection`, `Definition`, `paragraph`/`paragraphsub`/`paragraphsub-sub`, `subsection2`), the flat non-nesting `<p>` structure, the two numbering registers (bracketed vs. dot-separated) — was verified against exactly one instrument, one compilation: Privacy Act 1988, `C2026C00227`. `parse_compilation()` (issue #17, `src/compliance_rag/corpus/parser.py`) is written as if this markup shape is legislation.gov.au's general Word-export convention, not a property of this one Act — but that's an assumption carried forward from a single sample, not something this spike checked against a second instrument. Phase 4's second corpus (`PHASES.md`) is the first real test of it: re-verify the class vocabulary and flat structure against that instrument's actual fetched HTML before assuming the parser needs no changes, the same way this spike verified it here rather than trusting the API's format documentation.
