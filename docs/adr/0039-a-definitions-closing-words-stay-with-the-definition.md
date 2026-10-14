# ADR-0039 — A definition's closing words stay with it; a connector leads the paragraph it governs

- Status: accepted
- Date: 2026-10-14
- Issue: #178 (part 1, the parser)
- Relates to: ADR-0019 (anchors are synthesized, clause ids are what a citation carries)

## Context

The compilation's markup is flat: a `Definition` block, its `paragraph`
blocks, and `subsection2` blocks for the words the drafter sets at the left
margin between or after them. The parser appended every `subsection2` to the
text of the open **subsection**. For most provisions that is the right clause.
In s6(1) it is not: one subsection holds 176 definitions, each its own clause
(`s6/1/<term>`), so "the open subsection" was the chapeau, and four
definitions lost words to it:

| Clause | Stored (before) | The Act says |
|---|---|---|
| `s6/1/record` | "record includes: (a) a document; or (b) an electronic or other device; (d) a generally available publication; or … (h) letters …" | (a)–(b), **"but does not include:"**, (d)–(h) |
| `s6/1/credit-reporting-body` | "means: (a) an organisation; or (b) an agency prescribed by the regulations;" | … **"that carries on a credit reporting business."** |
| `s6/1/commonwealth-officer` | "… and includes: (a)–(d)" | … **"but does not include a person … employed in the Australian Capital Territory Government Service or in the Public Service of the Northern Territory."** |
| `s6/1/court-tribunal-order` | "… made by: (a)–(e)" | … **"and includes an order, direction or other instrument that is of an interim or interlocutory nature."** |

`s6/1` itself read "In this Act, unless the contrary intention appears: but
does not include a person … and includes an order … that carries on a credit
reporting business. but does not include:". Nothing is lost from the stored
words in total, only put on the wrong clause, but the effect is a changed
meaning: the exclusions of *record* read as inclusions, and every organisation
read as a credit reporting body.

The four are two different shapes:

- **Closing words** follow the definition's last paragraph and govern the
  whole definition (*credit reporting body*, *Commonwealth officer*,
  *court/tribunal order*).
- **A connector** sits between two paragraph groups and governs only the
  second (*record*: "(a) …; (b) …; **but does not include:** (d) …").

What a fix may do:

- **Clause text is parsed, never written** (D-01, `CLAUDE.md`: Corpus rules).
  No word or label that isn't in the source, and no word twice.
- **Every word of a definition is in exactly one clause.**
- **One clause is one chunk** (`corpus/chunker.py`). A chunk is the clause's
  canonical text and nothing else: no parent context goes with it, so a
  retrieved paragraph says what its own clause says.
- **No clause is added, removed or moved.** A clause id is what a citation
  carries (ADR-0019, `CONTEXT.md`: Clause).

## Decision

Inside a `Definition`, a `subsection2` block belongs to the definition, not to
the subsection's chapeau. The parser holds its words (`unclaimed`) until it
knows which shape they are:

1. **A connector leads the paragraph that follows it.** If a paragraph (or
   subparagraph) opens before the definition ends, the waiting words become
   the first words of that clause. `s6/1/record` stays "record includes:", and
   `s6/1/record/d` is "but does not include: a generally available
   publication; or". Read in order, the stored clauses are the Act's sentence:
   "record includes:", "a document; or", "an electronic or other device;",
   "but does not include: a generally available publication; or", …
2. **Closing words join the definition's own text.** Words still waiting when
   the definition ends follow its lead-in, as a subsection's closing words
   always have (`s6c/1`, `sch1/app11/2`, `s8/2`). `s6/1/credit-reporting-body`
   is "credit reporting body means: that carries on a credit reporting
   business."
3. **Outside a Definition nothing changes.** A `subsection2` in a subsection
   with no definition still joins that subsection's text (`s8/2`, `s6e/1a`,
   `sch1/app11/2`).
4. **The anchor stays on the paragraph's own `<p>`.** The connector's `<p>`
   sits directly above it and has no id of its own, as no closing words'
   `<p>` ever had. The stored HTML (`source.html`) is therefore byte-identical
   before and after, and so is every clause id and path.

## Why the connector leads, and what was rejected

The connector governs (d) to (h) and nothing before them. Where it is stored
decides what a reader (or a retrieved chunk) takes (d) to be.

- **Chosen: it leads (d).** The words are in source order across the flat
  sequence (the definition, then (a), (b), (d) …), so reading the clauses in
  order gives the Act's sentence back. The chunk for (d) says "but does not
  include: a generally available publication; or" on its own, so the
  exclusion is in the text that is retrieved.
- **Rejected: it stays on the definition's own text** ("record includes: but
  does not include:"). This is what "closing words attach to the definition"
  gives if applied to a mid-list connector, and it is the original inversion
  moved. The definition would say includes and does not include in one
  breath. (a), (b) and (d)–(h) would be siblings under it with nothing
  telling them apart, so (d)–(h) read as inclusions. It also breaks source
  order: the connector would come before (a).
- **Rejected: it trails (b)** ("an electronic or other device; but does not
  include:"). It keeps the order, but the chunk for (b) ends in a dangling
  "but does not include:" and the chunk for (d) is still a bare noun phrase
  that reads as an inclusion.
- **Rejected: repeat it on (d)–(h).** Five copies of words the Act prints once.
  It breaks "parsed, never written" and "exactly once".
- **Rejected: a clause of its own for the exclusion group**, with (d)–(h)
  under it. It needs a path segment that no source word supplies, and (d)–(h)
  would change their paths and ids.

**What this does not fix:** only (d) carries the connector. (e) to (h) say
"anything kept in a library, art gallery or museum …; or" and so on, and read
on their own as inclusions. A chunk is one clause and the words are printed
once, so a retrieved (e) cannot say more. The definition and (d) are the
clauses that do.

**The closing words are not in source order across the flat sequence.** They
sit on the definition, ahead of its paragraphs. That is the convention every
subsection's closing words already follow (`sch1/app11/2` is "If: the entity
must take such steps … to destroy the information …", and its conditions are
clauses of their own), and for the reason that makes it right here: the words
govern the whole list, and the definition is the clause that heads it. Only a
connector, which governs what follows it and nothing else, is placed by source
order.

## Consequences

- **Five clauses change text; none is added, removed or re-pathed.** `s6/1`
  (back to the bare chapeau), `s6/1/credit-reporting-body`,
  `s6/1/commonwealth-officer`, `s6/1/court-tribunal-order` and
  `s6/1/record/d`. Over the whole pinned compilation (sha256 `6d0a43e5…`, 4114
  clauses) nothing else differs, the stored HTML is identical, and the
  multiset of words is identical.
- **The golden corpus is unchanged.** Its law is the excerpt, whose `s6/1`
  was already the bare chapeau, so the embedding cache (keyed by text) stays
  valid.
- **A deployed C2026C00227 is now behind the parser.** Its bucket, Aura and
  Qdrant hold the old text, and `ingest_compilation` refuses the drift
  ("removals and edits can't be acknowledged"). That is correct and is not
  changed here. Correcting it is #178 part 2, the human's decision.
- **The signed mapping cites `s6/1/credit-reporting-body`**, the source of its
  "is a credit reporting body" condition (s6D(4)(f)). The clause id is stable
  and its text gains the words "that carries on a credit reporting business."
  The reviewer may want to re-read that condition against the corrected
  definition.
- **The same shape exists outside definitions and is not fixed here.** Where a
  non-definition subsection has a `subsection2` between its paragraphs, the
  words still join the subsection's text. The clearest case is s7(1)
  ("… is a reference to: but does not include a reference to an act done …
  received from:"), whose (f)–(h) are exclusions that read as inclusions.
  Tracked in its own issue.
