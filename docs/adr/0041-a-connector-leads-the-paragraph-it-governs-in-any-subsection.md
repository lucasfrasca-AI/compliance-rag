# ADR-0041 — A connector leads the paragraph it governs in any subsection, unless closing words complete its sentence

- Status: accepted. The exception for closing words (below) is provisional: it
  leaves three subsections as they were, and what their closing words should
  attach to is the human's decision.
- Date: 2026-10-14
- Issue: #186
- Amends: ADR-0039 (a definition's closing words stay with it; a connector leads
  the paragraph it governs), which this extends from definitions to every
  subsection
- Relates to: ADR-0019 (anchors are synthesized, clause ids are what a citation
  carries)

## Context

ADR-0039 stopped s6(1)'s definitions losing their closing words, and gave a
*connector* (the `subsection2` words between two paragraph groups, "but does
not include:") a home: it leads the paragraph that follows. It left subsections
with no definition as they were, where a `subsection2` between two paragraphs
joined the subsection's text.

There are 21 such connectors, in 15 sections (`s7/1`, `s11/2`, `s20k/4`,
`s23c/2`–`3`, `s26wc/1`–`2`, `s26we/2`, `s26wf/1`–`5`, `s26wg`, `s26wn`,
`s26wq/1`, `s30/4`, `s46/2`, `s50/2`, `sch1/app7/6`, `sch2/s11/5`). Every one
splits a single lettered list: the letters carry on across it ((ee) then (f);
(b) then (c)). None closes a list, and none is followed by a paragraph without
a letter of its own. Of four kinds:

- **An exclusion** (s7(1)): "… a reference to: (a) … (ee) …; **but does not
  include a reference to an act done … received from:** (f) an intelligence
  agency; …". On the subsection, (f)–(h) read as acts and practices the Act
  covers.
- **A second chapeau for the group that carries the consequence** (15): "the
  body must:", "the respondent must, in writing:", "this Part has effect as
  if:", "then:", "commits an offence punishable on conviction:", "the
  individual may:", "must not exceed the greater of:", and others.
- **A connector with closing words after the second group** (3: s26WF(2),
  s26WF(5), s30(4)): "this Part does not require: (e) …; (f) …; **to take
  steps to notify the individual …**".
- **A connector that also finishes the first group's sentence** (2: s26WG,
  s50(2)): "… information: (a) would be likely; or (b) would not be likely;
  **to result in serious harm …, have regard to the following:** (c) …".

In seven of the 21 the connector carries a negation: s7(1), s26WF(1)–(5)
("the access or disclosure is not, and is taken never to have been:", "this
Part does not require:") and s26WN ("do not apply in relation to:"). On the
subsection, a bare "(e) an eligible data breach of the APP entity …; or" reads
as what the access *is*.

## Decision

The rule of ADR-0039, in a subsection with no definition. A `subsection2` waits
for the next block (`_SubsectionNode.unclaimed`, as `_DefinitionNode.unclaimed`
does):

1. **A paragraph (or sub-paragraph) opens: the words lead it.** `s7/1/f` is
   "but does not include a reference to an act done, or a practice engaged in,
   in relation to a record that has originated with, or has been received
   from: an intelligence agency;", and `s7/1` is "Except so far as the contrary
   intention appears, a reference in this Act (other than section 8) to an act
   or to a practice is a reference to:". `s20k/4/d` is "the body must: extend the
   ban period by such period as the body considers is reasonable in the
   circumstances; and". Nothing is added, removed, re-pathed or repeated; a
   clause id, a path and `source.html` are as they were.
2. **Nothing opens: they close the subsection**, after its other text, as they
   always have (`s8/2`, `sch1/app11/2`). Closing words are not changed.
3. **The exception: a subsection with closing words keeps its connectors with
   them.** When words close the subsection after a connector, they complete the
   connector's sentence: s26WF(2) is "If: (a) … (d); **this Part does not
   require:** (e) the APP entity …; or (f) any other entity; **to take steps to
   notify the individual …**". Closing words stay on the subsection (2), so a
   connector leading (e) would put "does not require" on (e) and leave the
   subsection as "If: to take steps to notify the individual …", with the
   negation gone from it. That is the defect this ADR exists to remove, moved to
   the subsection. So `settle()` hands such a subsection's connectors back to
   its text, in source order, ahead of the closing words: `s26wf/2` is "If: this
   Part does not require: to take steps to notify the individual of the contents
   of a statement that relates to the access or disclosure." and `s26wf/2/e` is
   "the APP entity, credit reporting body, credit provider or file number
   recipient, as the case may be; or", both exactly as they were. Three
   subsections: s26WF(2), s26WF(5), s30(4).
4. A Definition opening in the subsection settles whatever waits first, by the
   same rule, so a shape with words before a definition keeps the order it had.

This is a new ADR rather than an edit to ADR-0039 because the rule differs: it
has an exception ADR-0039 never met (a definition has no sentence-completing
closing words after a connector), and the exception is provisional. The reasons
in ADR-0039's "Why the connector leads" are about what a connector governs, not
about definitions, and each alternative it rejected fails in an ordinary
subsection for the same reason: on the subsection it is the inversion again;
trailing the paragraph before it leaves a dangling clause and a bare noun
phrase; repeating it writes words the Act prints once; a clause of its own needs
a path no source word supplies and re-paths the paragraphs.

| Clause | Before | After |
| --- | --- | --- |
| `s20k/4` | If: the body must: | If: |
| `s20k/4/d` | extend the ban period by such period as the body considers is reasonable in the circumstances; and | the body must: extend the ban period by such period as the body considers is reasonable in the circumstances; and |
| `s26wf/1/e` | an eligible data breach of the APP entity, credit reporting body, credit provider or file number recipient, as the case may be; or | the access or disclosure is not, and is taken never to have been: an eligible data breach of the APP entity, credit reporting body, credit provider or file number recipient, as the case may be; or |
| `s26wf/2`, `s26wf/2/e` | unchanged | unchanged |

## Judgement calls, and what is not decided

- **The three held subsections are not fixed.** Their (e) and (f) are still bare
  noun phrases under a connector that is on the subsection (`s26wf/2/f` is "any
  other entity;"), as they were. The alternatives, for the human:
  - *Lead (e) and accept the split sentence.* Rejected here: the negation moves
    to (e) and leaves the subsection without it. It applies the rule and
    regresses the subsection chunk, which is worse than leaving it.
  - *Change what closing words attach to when they follow a connector.* The
    closing words complete the second group's sentence, not the whole list's;
    they could go with the connector. That is a change to closing words, which
    #186 was told not to touch, and it puts the end of a sentence in the middle
    of a list. Not decided here.
- **Two connectors finish the sentence before them** (s26WG: "to result in
  serious harm to any of the individuals to whom the information relates, have
  regard to the following:"; s50(2): "and that that matter could be more
  conveniently or effectively dealt with by the alternative complaint body, the
  Commissioner may decide not to investigate …, and, if the Commissioner so
  decides, he or she shall:"). The first part belongs to the group before and
  the last part to the group after. They are one block in the source, the parser
  does not cut a block, and a rule that looked at the words to find where to cut
  would be a guess. Nothing in the markup tells them from the 15 plain second
  chapeaux, so the rule applies to them: they lead (c) whole. `s26wg/c` and
  `s50/2/c` carry both halves, and the first group (`s26wg/a` "would be likely;
  or", `s26wg/b` "would not be likely;") is stored without the words that
  complete its sentence, which were on the subsection before. No negation is
  moved in either. Reverting these two would need a per-clause exception.
- **Only the first paragraph of the second group carries the connector**, as in
  *record*. A retrieved `s7/1/g` is still "the Defence Intelligence Organisation
  or the Australian Geospatial‑Intelligence Organisation; or". `s7/1/f`, the one
  the issue names, reads as what it is.
- **Not modelled**: a held `subsection2` followed by a paragraph with no letter
  of its own (the s6FA(a) shape) keeps waiting until a paragraph with a letter
  opens, or the subsection ends. No connector in C2026C00227 is followed by
  one. A synthetic test pins what happens, so a change is deliberate.

## Consequences

- **36 clauses change text; none is added, removed or re-pathed.** 18
  subsections lose their connector (and are the chapeau) and the 18 paragraphs
  after them gain it. Over the whole pinned compilation (sha256 `6d0a43e5…`,
  4114 clauses): same paths in the same order with the same ids, `source.html`
  byte-identical, the multiset of words identical, and the three held
  subsections (six clauses) identical to the parse before.
- **The golden corpus is unchanged.** Its law is the excerpt, which has no
  connector of this kind (0 of 75 clauses change), so the embedding cache stays
  valid.
- **No chunk budget is at risk.** The longest changed clause is 72 reranker
  tokens (the budget is 420); the longest clause in the Act is 150.
- **No signed mapping cites any of the 42 clauses involved.**
- **A deployed C2026C00227 is further behind the parser.** Its bucket, Aura and
  Qdrant hold the old text of these 36 clauses as well as #178's five (41 in
  all). `ingest_compilation` refuses the drift, as it should; republishing is
  #178 part 2, the human's decision, and now covers these clauses.
- **The test fixture holds every connector in the Act**, cut verbatim from the
  pinned source. The whole-compilation test checks that there are 21, that the
  three held are exactly the ones whose subsection has closing words, that each
  of the rest leads its paragraph, and that the fixture reads each one exactly
  as the whole compilation does, so a fixture that holds fewer than the Act has
  is a failing test.
