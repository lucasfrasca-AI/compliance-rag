# ADR-0039 — A definition's closing words stay with it; a connector leads the paragraph it governs

- Status: accepted. Extended on 2026-10-14 to connectors outside definitions
  (issue #186; see "Extended to ordinary subsections" below).
- Date: 2026-10-14
- Issue: #178 (part 1, the parser); #186 (the same shape in ordinary subsections)
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
3. **Closing words outside a Definition are unchanged.** A `subsection2` in a
   subsection with no definition that no paragraph follows still joins that
   subsection's text (`s8/2`, `s6e/1a`, `sch1/app11/2`). (A `subsection2`
   *between* two paragraphs of such a subsection is a connector, and leads the
   paragraph after it: see "Extended to ordinary subsections".)
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

**Not modelled:** a held `subsection2` followed by a token-less paragraph that
continues the paragraph above (the s6FA(a) shape) stays held until the next
clause opens or the definition ends. No definition in C2026C00227 has that
shape, so it isn't guessed at.

## Consequences

- **Five clauses change text; none is added, removed or re-pathed.** `s6/1`
  (back to the bare chapeau), `s6/1/credit-reporting-body`,
  `s6/1/commonwealth-officer`, `s6/1/court-tribunal-order` and
  `s6/1/record/d`. Over the whole pinned compilation (sha256 `6d0a43e5…`, 4114
  clauses) nothing else differs, the stored HTML is identical, and the
  multiset of words is identical.
- **A conservation test now guards the class of bug, not the four cases.**
  Every operative word of the Act, read off the markup without parser code, is
  stored exactly once. It runs on both checked-in fixtures, and on the pinned
  compilation when `PRIVACY_ACT_COMPILATION_HTML` is set, where the only
  exception is the s80U heading set in subsection style ("Enforceable civil
  penalty provisions"), which is not a provision. A word on the wrong clause
  still needs a test of its own, as the four here have.
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
- **The same shape exists outside definitions.** Where a non-definition
  subsection has a `subsection2` between its paragraphs, the words used to join
  the subsection's text. The clearest case is s7(1) ("… is a reference to: but
  does not include a reference to an act done … received from:"), whose (f)–(h)
  are exclusions that read as inclusions. Fixed by the same rule in #186: see
  the next section.

## Extended to ordinary subsections (issue #186)

### Context

The class was bigger than s6(1). A `subsection2` between two `paragraph` (or
`paragraphsub`) blocks of a subsection with no definition joined the
subsection's text too. There are 21, in 15 sections (`s7/1`, `s11/2`,
`s20k/4`, `s23c/2`–`3`, `s26wc/1`–`2`, `s26we/2`, `s26wf/1`–`5`, `s26wg`,
`s26wn`, `s26wq/1`, `s30/4`, `s46/2`, `s50/2`, `sch1/app7/6`, `sch2/s11/5`).
Every one splits a single lettered list: the letters carry on across it ((ee)
then (f); (b) then (c)). None closes a list, and none is followed by a
paragraph without its own letter. They are of four kinds:

- **An exclusion** (s7(1)): "… a reference to: (a) … (ee) …; **but does not
  include a reference to an act done … received from:** (f) an intelligence
  agency; …". On the subsection, (f)–(h) read as acts and practices the Act
  covers.
- **A second chapeau for the group that carries the consequence** (15 of them:
  "the body must:", "the respondent must, in writing:", "this Part has effect
  as if:", "then:", "commits an offence punishable on conviction:", "the
  individual may:", "must not exceed the greater of:", s26WQ(1)'s "the
  Commissioner may, by written notice given to the entity:").
- **A connector with closing words after the second group** (s26WF(2), s26WF(5),
  s30(4)): "this Part does not require: (e) …; (f) …; **to take steps to notify
  the individual …**".
- **A connector that also finishes the first group's sentence** (s26WG, s50(2)):
  "… information: (a) would be likely; or (b) would not be likely; **to result
  in serious harm …, have regard to the following:** (c) …".

In seven of the 21 the connector carries a negation: s7(1), s26WF(1)–(5) ("the
access or disclosure is not, and is taken never to have been:", "this Part does
not require:") and s26WN ("do not apply in relation to:"). On the subsection, a
bare "(e) an eligible data breach of the APP entity …; or" reads as what the
access *is*.

### Decision

The same rule, and only that. A `subsection2` in a subsection with no
definition waits (`_SubsectionNode.unclaimed`, as `_DefinitionNode.unclaimed`
does) until the next block is known:

1. A paragraph (or sub-paragraph) opens: the words lead it. `s7/1/f` is "but
   does not include a reference to an act done, or a practice engaged in, in
   relation to a record that has originated with, or has been received from:
   an intelligence agency;", and `s7/1` is "Except so far as the contrary
   intention appears, a reference in this Act (other than section 8) to an act
   or to a practice is a reference to:".
2. Nothing opens: the words close the subsection, after its other text, as
   they always have (`s8/2`, `sch1/app11/2`). **Closing words are not changed.**
3. A Definition opens in the subsection: whatever waited settles into the
   subsection's text first, so a shape with words before a definition keeps
   the order it had.

This is an extension of ADR-0039, not a new ADR, because the rule does not
differ. The reasons in "Why the connector leads" are about what a connector
governs, not about definitions, and every alternative rejected there fails in
an ordinary subsection for the same reason (on the subsection it is the
inversion again; trailing the paragraph before it leaves a dangling clause and
a bare noun phrase; repeating it writes words the Act prints once; a clause of
its own needs a path no source word supplies, and re-paths the paragraphs).

| Clause | Before | After |
| --- | --- | --- |
| `s20k/4` | If: the body must: | If: |
| `s20k/4/d` | extend the ban period by such period as the body considers is reasonable in the circumstances; and | the body must: extend the ban period by such period as the body considers is reasonable in the circumstances; and |
| `s26wf/1/e` | an eligible data breach of the APP entity, credit reporting body, credit provider or file number recipient, as the case may be; or | the access or disclosure is not, and is taken never to have been: an eligible data breach of the APP entity, credit reporting body, credit provider or file number recipient, as the case may be; or |

### Judgement calls

- **Three subsections have a connector and closing words** (s26WF(2), s26WF(5),
  s30(4)). The connector leads its paragraph and the closing words stay on the
  subsection, where all closing words go. Their sentence is therefore split
  across the subsection and the paragraph: `s26wf/2` is "If: to take steps to
  notify the individual of the contents of a statement that relates to the
  access or disclosure." and `s26wf/2/e` is "this Part does not require: the
  APP entity, … ; or". Read in clause order it is still every word once, but the
  subsection no longer reads as a sentence on its own, as it did when it also
  held "this Part does not require:". Rejected, for these three only: leaving
  their connector on the subsection. That makes the rule depend on words
  further down the subsection, and (e) and (f) are then bare noun phrases under
  "does not require", which is the problem being fixed. Rejected: moving their
  closing words to the second group, which changes closing words, the thing
  that was not to change, and puts the end of a sentence in the middle of a
  list.
- **Two connectors finish the sentence before them** (s26WG: "to result in
  serious harm to any of the individuals to whom the information relates, have
  regard to the following:"; s50(2): "and that that matter could be more
  conveniently or effectively dealt with by the alternative complaint body, the
  Commissioner may decide not to investigate …, and, if the Commissioner so
  decides, he or she shall:"). The first part belongs to the group before and
  the last part to the group after. They are one block in the source, the parser
  does not cut a block, and a rule that looked at the words to guess where to
  cut would be a guess. They lead the paragraph after them whole, like the
  rest. `s26wg/c` and `s50/2/c` carry both halves; the first group (`s26wg/a`
  "would be likely; or", `s26wg/b` "would not be likely;") is stored without
  the words that complete its sentence, which were on the subsection before and
  are now on (c).
- **Only the first paragraph of the second group carries the connector**, as
  in *record*. A retrieved `s7/1/g` is still "the Defence Intelligence
  Organisation or the Australian Geospatial‑Intelligence Organisation; or".
  `s7/1/f`, the one the issue names, reads as what it is.
- **Not modelled**: a held `subsection2` followed by a paragraph with no letter
  of its own (the s6FA(a) shape) keeps waiting until a paragraph with a letter
  opens or the subsection ends. No connector in C2026C00227 is followed by
  one. A test keeps every word stored once in that shape.

### Consequences

- **42 clauses change text; none is added, removed or re-pathed.** The 21
  subsections lose the connector (and are the chapeau, plus any closing words),
  and the 21 paragraphs after them gain it. Over the whole pinned compilation
  (sha256 `6d0a43e5…`, 4114 clauses): same paths in the same order with the
  same ids, `source.html` byte-identical, the multiset of words identical, and
  nothing else differs.
- **The golden corpus is unchanged.** Its law is the excerpt, which has no
  connector of this kind, so the embedding cache stays valid.
- **No chunk budget is at risk.** The longest changed clause is 76 reranker
  tokens (the budget is 420); the longest clause in the Act is 150.
- **No signed mapping cites any of the 21 subsections or paragraphs.**
- **A deployed C2026C00227 is further behind the parser.** Its bucket, Aura and
  Qdrant hold the old text of these 42 clauses as well as #178's five (47 in
  all). `ingest_compilation` refuses the drift, as it should; republishing is
  #178 part 2, the human's decision, and now covers these clauses.
- **The test fixture holds every connector in the Act**, cut verbatim from the
  pinned source. The whole-compilation test checks that there are 21, that each
  leads its paragraph, and that the fixture reads each one exactly as the whole
  compilation does, so a fixture that holds fewer than the Act has is a failing
  test.
