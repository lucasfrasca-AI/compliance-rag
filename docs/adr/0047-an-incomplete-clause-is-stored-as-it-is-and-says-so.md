# ADR-0047 — An incomplete clause is stored as it is, and says so

- Status: accepted. The human decided the state and refused the alt text
  (quoted below), then narrowed Track A to a part year and signed the words a
  reader is shown (Decision 9); the model made the other calls, each named as
  such.
- Date: 2026-10-20
- Issue: #211 (from the #206 audit)
- Extends: ADR-0042 (a corpus revision now compares, writes and records a
  clause's *form*: its text and its incompleteness), ADR-0003 / `D-02` (the
  verifier is unchanged; the flag is in addition to it)
- Touches: ADR-0045 (the provision is the unit marked), ADR-0027 (Track A's
  unassessable, for a part year only: Decision 6), D-01 (the reviewed reading,
  Decision 9)

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
reader is shown unless a person has signed others for that clause (Decision
9), plain lowercase words so it is safe in a prompt marker:

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

**6. Track A: unassessable where the missing content governs, derived from
the state.** A leaf whose clauses include an incomplete one is unknown, with
the clause's reason, for every reading the missing content governs: no figure
settles it. Derived from the stored clause state (the Clause node on the
graph, the committed corpus on the fake), never by editing the signed
mapping. Which readings that is:

- **By default, every one.** An incomplete clause nobody has read (no entry in
  Decision 9's file) governs whatever the leaf reads, so the leaf is unknown
  whatever the evidence, with no question — and so is a leaf that reads no
  financial year (`period: current`).
- **Only a part year, where a person has read it so.** The first version of
  this held s6DA(2)'s three conditions unassessable whenever they cited it,
  which made GA-01 and GA-05 — $4M in a full year, $3.2M in a full year —
  unassessable too, overstating the gap. The human's decision (#211,
  2026-10-20, decision 2):

  > Small-business conditions: agreed. Mark them unassessable only when the
  > facts leave open that the business ran for part of a year, not whenever
  > the formula is cited.

  The three leaves (`COND-PA-TURNOVER-PREVIOUS-YEAR-UNDER`,
  `COND-PA-TURNOVER-CURRENT-YEAR-UNDER`, `COND-PA-S6D4A-EVER-OVER-THRESHOLD`)
  read a tenant-supplied figure, `annual_turnover_aud`, and s6DA(2) governs
  that figure only for "a business [that] has been carried on for only part of
  a financial year" (its own stored words). So for each financial year a leaf
  reads, from the profile's `business_started_on`:

  | The year is | When | The leaf |
  |---|---|---|
  | a **part year** | the business started during it (after its 1 July) | unknown, the clause's reason, **no question** |
  | **open** | `business_started_on` unknown, conflicting or not a date | unknown, the clause's reason, and **asked** when it started |
  | a **full year** | it started on or before its 1 July, or after it ended (not carried on in it at all) | decided from the figure, as before |

  For s6D(4)(a), which reads every year ended since the business started (or
  21 December 2001, if later), the start year is the part year: it is never
  read from its figure, even one over the threshold (what s6DA(2) makes of it
  isn't in the corpus). Any other year over the threshold still decides it
  true; every other year known and under leaves it unknown with no question;
  another year without a figure is asked for, that year alone. With the start
  unknown, every year might be the start year, so nothing decides it and the
  start date is asked (`also_needs`).

**How part-year relevance is known** (the model's call, the reading signed):
not from the mapping, which says nothing of part years in structure (only in
its question prose), and not from the clause's words, which the code doesn't
interpret. A person records it, in the reviewed file of Decision 9, as
`part_year_only: {started: business_started_on}` on the clause and reason it
is for. `started` names the profile fact the signed mapping already reads for
when the business started (`COND-PA-CARRIED-ON-LAST-YEAR`'s leaf, s6D(4)(a)'s
`since_field`), taken, as the mapping takes it, as carried on since without a
break. A test holds each such entry to its clause: the clause's stored text
says "carried on for only part of a financial year", and the fact is a field
the mapping reads. Nothing is speculated past that: the part-year test is
dates only, and a figure in a part year decides nothing either way.

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

**9. The signed reading: what a reader is shown, and what the gap governs.**
The human's decision (#211, 2026-10-20, decision 3):

> The reason's wording: I'll sign wording. Use "the turnover formula in
> s 6DA(2) is published as an image in the compilation and has no text form
> in our corpus". That says what's wrong and why, which the parser's generic
> phrasing doesn't — a reader seeing "not available in text form" can't tell
> whether the clause is missing, unparsed, or genuinely imageless. Set
> reviewed_by to "Lucas Frasca" dated today on whatever file carries it.

- **The parser's detail stays what the parser emits**, in every store.
- **The reading is a reviewed artefact**:
  `corpus/instruments/privacy-act-1988/C2026C00227/incomplete.yaml`, beside
  the version's selection manifest, with `author`, `reviewed_by`, `date` and
  `source_clause_ids`. It maps each clause and reason to `shown`, the words a
  reader is shown, and optionally `part_year_only` (Decision 6). This one's
  `author` and `reviewed_by` are both Lucas Frasca, dated 2026-10-20: the
  wording is his, verbatim, and `part_year_only` records his decision 2; the
  model transcribed both and drafted nothing in it.
- **Refused while pending** (`compliance_rag.corpus.incomplete_review`): a
  file whose `reviewed_by` or `date` starts `PENDING` raises
  `UnreviewedIncompleteness` and is never used, as `load_mappings.py`
  refuses an unsigned mapping; the API reads the files when it is built, so an
  unsigned one stops the build rather than the first answer. A model never
  reviews one (`validate_review`), each entry must be a clause of the folder's
  version, and `source_clause_ids` must be exactly the clauses read. CI loads
  the committed file and checks each entry against the committed corpus: a
  reason the parser marks on that clause, and for `part_year_only`, the
  clause's own part-year words.
- **The words are safe in a fence marker.** `shown` is one line of letters,
  digits, spaces, balanced parentheses and plain punctuation: no `]` (closes
  the marker), no `;` (separates two reasons), no quote, no line break
  (#236). It is hashed into the fence's boundary with the rest of the marker.
- **Where readers see it**, for that clause and reason only, the parser's
  detail otherwise (another reason on the same clause, a clause nobody has
  read): the answer event's `incomplete` on each citation and at the top
  (each reason's `detail` is the words to show), a chat read back (the same
  builders, #232), the prompt's fence marker (answer and repair fences alike,
  #243), and Track A's reason, `<cite> is incomplete: <words>`.

**Joined at read time, not stored** (the model's call). ADR-0042's revision
path writes a parse of the pinned source to the bucket, Aura and Qdrant; the
signed words are not a parse and say nothing of the clause's text, and
storing them would put a reviewed reading in the corpus's form, so that
changing a word would take a cloud revision with an acknowledgement and a
re-signed plan. Joined at read time, a change is a reviewed PR, the stored
state stays exactly what the parser found (so a drift check still compares
parse with parse), the fake and the graph read one evaluator, and no live
step is needed. A chat read back still reads the *reasons* from the ledger,
as recorded with the turn (#228); only the words for a recorded reason are
joined, so an answer shown before the signing reads back in the signed words.
The files ship with the code, as `corpus/synthetic/registry.json` does for
the residency gate.

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
- **Track A, narrowed to a part year (Decision 6).** Run on the fake and on
  Neo4j, of the seven golden profiles only GA-04 moves: Acme started on
  2 March 2015, so FY2014-15, the first year s6D(4)(a) reads, is a part year.
  Its 35 APPs go from not applicable to unassessable, app-entity and
  organisation from false to unknown, with no question, and APP 11.1's chain
  cites s6D(4)(a) and s6DA(2), not s6E(1) and s6EA(1), which decide nothing
  once the role is unknown. The human authorised re-signing the labels that
  move (`corpus/golden/applicability.yaml`, 2026-10-20). GA-01 and GA-05 stay
  applicable (a full year decides), GA-02 keeps its s6D(4)(a) question (other
  years can be answered), GA-03's outcomes and questions don't move (its
  reasons now name s 6DA(2) too), GA-06 and GA-07 are unchanged. The first,
  held version (every citing leaf unassessable) moved GA-01, GA-02, GA-04 and
  GA-05, and was never merged.
- **The reason's words.** The parser can't know a formula computes turnover
  without reading the alt text, so its detail stays "the formula is not
  available in text form"; the reader is shown the signed words (Decision 9),
  and Track A's reason names the clause: "s 6DA(2) is incomplete: the
  turnover formula in s 6DA(2) is published as an image in the compilation
  and has no text form in our corpus".
- **The corpus version moves** again (ADR-0034): `incomplete.yaml` and its
  reader are under `corpus/instruments` and `src/compliance_rag/corpus`. The
  graph's loaded corpus is unchanged (nothing is written to it), so the next
  load records a new tag over the same clauses. The mapping version moves too
  (`src/compliance_rag/graph`). The prompt's marker for s6da/2 changed; it is
  in the retrieved fence, not the cached policy prefix, so no cache is missed.
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
- **A citation that can't be read back is marked, not fatal** (#232, the
  human's decision: "a corrupt row shouldn't make a member's chat history
  inaccessible"). A malformed element of `citations` reads back as an
  `UnreadableCitation` in its place, so nothing is dropped, and the chat is a
  200. The API shows `{"unreadable": true, "reason": …}` with `clause_id`,
  `cite` and `version_id` only when the stored id is well-formed. The model's
  calls: the reason is one of four fixed words (`not_an_object`, `no_clause`,
  `wrong_version`, `bad_incomplete`), never an exception message, because
  pydantic's quotes the input; an unreadable citation carries no `incomplete`
  (what the member was shown with it is lost, and the turn's `incomplete`
  list is built from the readable ones, so it can understate); the corruption
  is reported on the request's span (`chat.citation_unreadable`, ids and
  reason) and in the log, which is how an operator learns of it. The answer
  event and a chat build a citation with one function, `citation_json`.
- **The same for a `blocked` element** (#234). A stored element of `blocked`
  that isn't a stable clause id (prose, a number, null, an object, an id with a
  line break) reads back as an `UnreadableCitation` with reason `no_clause` in
  its place, and the API returns `{"unreadable": true, "reason": "no_clause"}`
  there; the withheld turn and the rest of the chat are intact (200). It never
  carries a clause id: a well-formed one is a readable entry. It is reported
  as `chat.blocked_unreadable` on the span and a warning log, ids, position and
  the fixed word only. No schema change (the column's constraints test the
  array, not its elements). `STABLE_ID_RE`, `INSTRUMENT_ID_RE`, `VERSION_ID_RE`
  and `CLAUSE_PATH_RE` now end in `\Z`, not `$`, which matched before a trailing
  newline, so `parse_stable_id` rejects an id ending in one. The read path keeps
  `isprintable()`: a tenant-prefixed id's tenant part (`[^/:]+`) still admits a
  line break, and a citation's kept id reaches a span attribute and the
  response.
- **Every clause the parse marks is reported**, in the test that pins the
  whole compilation; a selection that grows to s16A or Part IIIA ingests those
  clauses already marked.
