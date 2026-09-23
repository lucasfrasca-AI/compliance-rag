# ADR-0019 — Clause-level anchors are synthesized, not read from source markup

- Status: accepted
- Date: 2026-09-23

## Context

`CONTEXT.md`'s **Position map** entry said law "resolves to an anchor in the
stored HTML" — implying the source rendering already carries something to
point at, per clause, for click-to-clause. The ePub format spike (issue #9,
`docs/research/legislation-gov-au-format.md`) fetched the real markup and
checked: stable ids (`id="navPoint_NNN"` on heading `<p>` elements, plus a
Word-generated `_Toc` bookmark inside each) exist only at section, Part and
Schedule level. Individual subclauses and paragraphs — the actual
`clause_path` granularity the verifier and citations work at, e.g.
`sch1/app11/1` — carry no `id` at all in the source.

Two ways to give a clause a position anyway:

1. **Character offsets into the stored HTML**, unifying law with the
   mechanism already used for uploaded PDFs (`char_start`/`char_end` on
   `ClauseRecord`).
2. **A synthesized id derived from `clause_path`**, written into the stored
   HTML by the parser at ingest time.

Character offsets into an HTML string are the wrong fit for what they're
newly used for: a PDF's offsets index into an extracted text layer, used with
PDF-native page/rect lookup. Turning a raw HTML string offset into a scroll
position needs the client to walk the DOM to find where an arbitrary
character count lands — no native browser support for "jump to character N",
unlike a `#fragment`, which every browser handles for free.

## Decision

Law's click-to-clause anchor is `clause_anchor_id(clause_path)`
(`compliance_rag.corpus.models`): a pure, deterministic slug of the clause's
own stable-ID segment (`sch1/app11/1` → `clause-sch1-app11-1`). The parser
writes this id at the corresponding position when it stores a clause's HTML
rendering; citation rendering recomputes the identical id from the
`clause_path` it already has. No new stored field, no render-time lookup
against the source: the id is derivable from what the parser stores by
construction, because the parser is what makes the derivation true.

PDFs keep the existing, separate offset-based mechanism. The two are still
never joined (`CONTEXT.md`'s existing point stands): an HTML anchor and a PDF
offset answer the same question for different source types, not a shared one.

## Consequences

- `CONTEXT.md`'s Position map entry is corrected to describe this mechanism
  instead of the disproven "anchor already exists in the markup" assumption.
- The parser ticket (#17) must inject `id="{clause_anchor_id(clause_path)}"`
  into its stored HTML at each clause's position — a concrete, testable
  requirement, not "capture the anchor" (there is nothing to capture).
- `clause_anchor_id()` is single-sourced in `corpus/models.py`, so the parser
  (writing the id) and citation rendering (constructing the link) can never
  drift into two different slugging schemes.
- If a future instrument's markup *does* carry native per-clause anchors,
  nothing here forces synthesizing one anyway — but nothing yet needs that
  branch, so it isn't built until it does.
