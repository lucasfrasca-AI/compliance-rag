# ADR-0047 — An incomplete clause is stored as it is, and says so

- Status: accepted. The human decided the state and refused the alt text
  (quoted below); the model made the other calls, each named as such.
- Date: 2026-10-20
- Issue: #211 (from the #206 audit)
- Extends: ADR-0042 (a corpus revision now compares, writes and records a
  clause's *form*: its text and its incompleteness), ADR-0003 / `D-02` (the
  verifier is unchanged; the flag is in addition to it)
- Touches: ADR-0045 (the provision is the unit marked), ADR-0027 (Track A's
  unassessable, held: see Consequences)

## Context

The pinned compilation (C2026C00227, sha256 `6d0a43e5…5c3b`) sets s6DA(2)'s
formula as an image:

```html
<p class="Formula"><img src="image.002.png" … alt="Start formula Amount that would be the annual turnover … end formula" /></p>
```

The parser reads text nodes only, so the deployed `s6da/2` ends "…the amount
worked out using the formula:" and stops. The stored text promises content it
doesn't hold, and nothing says so: the verifier can't see it (the truncated
text matches itself), and ADR-0039's conservation test reads text nodes of six
classes, which an `alt` attribute and a `Formula` block are outside. Three
signed conditions cite `s6da/2`. #211 offered three options: put the alt text
into the clause, carry it as metadata, or mark the gap.

## The human's decision

> s 6DA(2): no, alt text is not the Act's words. It's a transcription by
> whoever published the HTML, not the legislative text, and the verifier's
> claim is that a quoted span matches the authoritative source. Citing alt
> text as the Act would be the corpus asserting something it can't
> substantiate — the same class as #178.
>
> Instead: store the clause as it is and mark it INCOMPLETE with the reason,
> as a first-class clause state rather than a one-off. Any answer citing it
> carries the flag, and the three small-business conditions that depend on it
> return unassessable — "the turnover formula is not available in text form"
> — rather than a confident answer resting on a transcription. Images, tables
> and formulas will recur across instruments, so build it as the general
> state. Note the two honest routes to making it usable later: extracting
> from the PDF compilation, or a human transcribing and signing it under
> D-01.

## Decision

**1. Incomplete is a clause state, with a reason.** `ClauseRecord.incomplete`
is a tuple of `Incompleteness(reason, detail)`, empty for a complete clause.
The reasons are a closed enum (`IncompleteReason`), the detail the words a
reader is shown, plain lowercase words so it is safe in a prompt marker:

| Reason | Detail | What |
|---|---|---|
| `image_not_text` | "the formula is not available in text form" | an `<img>` in a `Formula` block |
| `image_not_text` | "an image is not available in text form" | any other `<img>` |
| `table_not_text` | "the table is not available in text form" | a `<table>` |
| `penalty_not_parsed` | "the penalty is not in the clause text" | a `Penalty` line (#216) |

**2. The parser detects it; the alt text never enters a clause.** An image, a
table or a penalty line marks the provision open where it occurs (ADR-0045's
provision: the open Definition, else the open subsection, and every clause
inside it). The model's calls:

- **The provision, not the last paragraph.** s16A(1)'s table follows (c) but
  belongs to the whole subsection: (a) names "column 1 of the table", (b)
  "column 2", (c) "column 3". Every clause of the provision is marked, so a
  citation of any of them carries the state with no propagation rule at read
  time. Several Definitions are each a provision of their own, so a table in
  one marks that one only.
- **What ends a provision.** A heading, a note, an example or an outline. No
  note or example in C2026C00227 is operative, so a dropped note doesn't make
  a clause incomplete. A `Formula` block, a penalty line and an empty block (a
  table's spacer) don't end it. A table's cells are the table's.
- **Penalty lines belong to the state** (the coordinator's notes and penalties
  check, 2026-10-20): they are content of the provision they follow, and the
  parse sets them in no clause. Unlike an image they are text, so a parse
  correction that takes them in completes the clause (#216).
- **Notes are not.** Headings, sub-headings, notes and examples are #212 and
  #214's, outside this state.

**Run over the whole pinned compilation** (`test_every_clause_the_whole_compilation_marks`,
`PRIVACY_ACT_COMPILATION_HTML`): 166 of 4114 clauses.

- `s6da/2`: the formula. **The only one of the 779 selected.**
- 15 clauses of the six operative tables, none selected: `s16a/1` and its
  (a)–(c), `s20f/1` and (a)–(b), `s20w` and (a)–(b), `s20x/1`, `s21h` and
  (a)–(b), `s37`. Each provision's own text names "the table".
- 150 clauses of the provisions the 61 penalty lines follow (s20C–s26XC, s65,
  s66, s80Q), none selected. Every penalty line follows a clause block, so
  every one is placed.
- The coat of arms (before s1) and the endnotes' four tables are in no
  provision and mark nothing.

**This is what #211 asked of the conservation test** (the model's call: in
its own test, not by widening ADR-0039's word count). ADR-0039's test counts
the words of six text classes and so can't see an image or a table. The new
test accounts for every image, table and penalty line: each one between
section 1 and the endnotes marks a provision, and the only others are named
(the coat of arms; the endnotes' four tables). The next compilation's formula
can't disappear unnoticed: it either marks its clause or fails that count.
It runs on the whole compilation only where the pinned source is on disk, as
the parser's other whole-compilation test does; in CI, the constructs cut
(which holds s6DA) and `clauses.json` carry it.

**3. Where the state lives: with the clause, in every store** (the model's
call). It is part of the stored corpus, not derived metadata: what a citation
of the clause means depends on it.

- **The bucket:** the clause object's `incomplete` field, and the manifest's
  `incomplete` map (path → reasons), beside `clause_paths`, so a drift check
  and a plan compare a clause's whole form without reading 779 objects.
  Absent, as on every manifest and object written before this, is complete.
- **The graph:** `incomplete_reasons` and `incomplete_details` on the Clause
  node, only when incomplete (Neo4j stores no null). A node is immutable, so
  loading today's parse over the deployed graph is refused: the state reaches
  it by a revision.
- **Qdrant:** `incomplete` in each point's payload, `[]` when complete, so what
  retrieval hands generation is marked. Absent on a deployed point is
  complete.
- **`clauses.json`**, the committed parser output, carries it on `s6da/2`
  only, regenerated by `scripts/export_compilation_clauses.py`.
- **Not the ledger's `clause` table**, which nothing writes.

**4. A revision carries the state (extends ADR-0042).** What a revision
compares, writes and records of each clause is its form: its canonical hash
and its incompleteness. A parse that marks a stored clause incomplete, or no
longer does, is a change: in the plan (with "text unchanged" when it is), in
the exact acknowledgement, on the record (`RevisedClause.previous_incomplete`
and `incomplete`, written only when present, so #178's record reads back
unchanged), and on each Clause node's `corrections` trail (the previous state,
beside the previous hash). Each store moves a clause from its previous form to
its new one or refuses; a state-only change re-embeds nothing (Qdrant
`set_payload`). An ingest whose parse marks a stored clause differently is
drift, routed to the revision.

**5. Answers carry it.** The verifier still checks every quote against the
stored text; the flag is in addition.

- `Answer.incomplete` lists each verified citation of an incomplete clause,
  read from the clause the verifier checked the quote against (the bucket),
  not from retrieval's copy.
- The API's answer event carries `incomplete` on each citation and a top-level
  `incomplete` list (clause id, display form, reasons).
- **The prompt shows it** (the model's call: yes, as the exception and
  qualifier markers do): `[B clause <id> incomplete (<detail>)]`, and the
  system prompt says an answer relying on an incomplete clause must say so,
  say what is missing, and never state, guess or reconstruct it. The boundary
  covers the marker. A fence with no incomplete clause keeps its bytes.

**6. Track A: unassessable, derived from the state.** A leaf whose clauses
include an incomplete one is unknown, with the clause's reason, whatever the
evidence: no answer to the questionnaire can settle it. Derived from the
clause state, never by editing the signed mapping. Built on its own branch and
**held**, because it changes signed golden labels (Consequences).

**7. Why the alt text is refused.** The verifier's claim is that a quoted span
matches the authoritative source. The alt text is an accessibility rendering
("Start formula … times start fraction … over … end fraction end formula") by
whoever published the HTML, not the words the Act prints. Storing it as clause
text would make the corpus assert what it can't substantiate (#178's class).
It isn't stored as metadata either: not as a field, not in a reason, not in a
prompt.

**8. Completing a clause: the two honest routes.**

- **Extraction from the PDF compilation**, the Register's other rendering of
  the same compilation. Whether its formula is extractable text, not an image
  there too, hasn't been checked. If it is, the extracted text is a parse of
  the Register's own rendering, pinned by its own sha256, and arrives through
  a corpus revision (ADR-0042). CONTEXT.md's Position map warns that PDF text
  fabricates whitespace; canonicalisation folds whitespace, but the extraction
  is tested against the source before it is trusted.
- **A human transcription signed under D-01**: a reviewed artefact with
  `author`, `reviewed_by` and `date`, naming the clause and the source it was
  read from, used only once signed, as mapping YAML is. The model may draft
  it; the human signs it.

**What un-marks a clause:** only a parse of the pinned source, by a corpus
revision, that no longer finds content missing — because one of those routes
put the content in the clause's text. Never a hand edit of any store, never
the model's say-so. The revision records the previous state, so the trail
shows the clause was incomplete and who completed it, when and why.

## Consequences

- **Deployed stores need the revision.** `scripts/revise_compilation.py`
  plans one change against the deployed C2026C00227 (`s6da/2`, text
  unchanged); the human applies it with the printed acknowledgement. It
  writes the bucket's manifest and object, one Clause node and one Qdrant
  payload, and calls no embedding.
- **The corpus version moves** (ADR-0034): the parser and `clauses.json` are
  under its paths.
- **The system prompt changed**, so the first answer after deploy misses the
  prompt cache once.
- **Track A, held.** With the state derived into Track A, GA-01, GA-04 and
  GA-05 go from decided to unassessable for 35 obligations each, and GA-02's
  questions lose `COND-PA-S6D4A-EVER-OVER-THRESHOLD`; GA-03, GA-06 and GA-07
  are unchanged. Those labels are the human's, so the derivation waits on
  their re-signing (#211 stays open).
- **The reason's words.** The parser can't know a formula computes turnover
  without reading the alt text, so the detail is "the formula is not
  available in text form", and Track A's reason names the clause
  ("s 6DA(2) is incomplete: the formula is not available in text form").
- **A chat read back** (#162) shows the flag the member was shown (#228). The
  turn records it with each citation, taken from the clause the verifier
  checked the quote against, in the citation's own element of the `chat`
  table's `citations` JSONB: `incomplete` is the same list of reasons as every
  other store, present only when there is any, so a row written before it
  reads back complete (those answers were shown without a flag). No
  structural migration, since the column's constraints test the array and not
  its elements; `db/ledger/0016_chat_citation_incomplete.sql` only re-sets the
  column's comment, which 0014 (applied, so not edited) left describing the
  old element shape. Read-back reads the ledger and never the corpus: a clause marked
  or completed after the answer doesn't change what the chat shows.
- **Every clause the parse marks is reported**, in the test that pins the
  whole compilation; a selection that grows to s16A or Part IIIA ingests those
  clauses already marked.
