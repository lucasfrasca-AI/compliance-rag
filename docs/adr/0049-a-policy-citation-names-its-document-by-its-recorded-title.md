# ADR-0049 — A policy citation names its document by its recorded title

- Status: accepted. The coordinator chose option (a) of the #239 comment
  (2026-10-20). The details below are the model's calls, made within that.
- Date: 2026-10-20
- Issue: #239 (gap 6 and I2 of `docs/research/phase3-product-options.md`)
- Amends: `CONTEXT.md`'s Display form entry ("a pure function of the path"
  now holds for library law only)
- Touches: ADR-0003 (blocked citations), ADR-0019 (anchors: unchanged),
  ADR-0021 (the version record), ADR-0048 (the publish path)
- Leaves open: #217 (headings and opening prose in clauses), and I2's
  rendering question (click-to-clause)

## Context

A policy citation read "3/1". `display_form` falls back to the raw path for
any path it doesn't recognise, and a policy's path is just its section and
paragraph numbers. Exit criterion 2 needs a policy citation a reader can
follow, whatever I2 decides about rendering.

The issue assumed the ledger already held each document's title. It didn't:
`record_version` was the only writer of `instrument` and
`instrument_version`, and it wrote the instrument id into both `title`
columns. Nothing upstream carried a title either. The real title was only in
each policy's Markdown, in its front matter and its `#` heading, and the
policy parser drops both by design.

## Decision

**1. A policy clause's display form is "{title}, {number}".** For example
`acme/data-security-policy:v1:3/1` reads "Acme Pty Ltd — Data Security and
Retention Policy (Internal), 3.1".

- **The title** is the one recorded with the clause's version, word for word.
- **The number** is the document's own numbering: `## 3.` and `3.1`, so the
  path `3/1` reads "3.1". A comma separates the two, the way a pinpoint
  follows the work it is in. There is no "§": the policies never write one.
- **A path of another shape** in a tenant's document keeps its law form after
  the title, for example "A supplied Act, s 6C(1)(a)".
- **Library law is unchanged** ("APP 11.1", "s 6(1)"). Its form is still
  `display_form` of the path alone and never reads a title.
- **A policy is recognised** by its tenant-prefixed instrument id. Library
  law is unprefixed (`CONTEXT.md`, Instrument id).

**2. The title is document metadata, supplied by whoever publishes the
version.** It is not clause text, so #217's question is untouched, and no
corpus revision is needed.

- **The lifecycle.** `publish_version`, `record_published_version`,
  `load_version_vectors` and the amend path each take `title`, and
  `record_version` writes it.
- **No default.** An empty title, or one with a control, format or
  line-separator character, is refused (`validate_title`) before anything is
  written. Migration 0017 also refuses a blank title in the table, whatever
  writes it. `instrument.title` never again holds the instrument id by
  default.
- **Where the titles come from today:**
  - **The synthetic fixtures:** fixture code reads each fixture's
    front-matter `title:` (`golden_corpus.front_matter_title`). That code is
    what records them in the eval harness's ledger, and what an operator
    script that publishes them will call. It is not the parser. No operator
    script publishes the fixtures to a ledger yet;
    `register_synthetic_fixtures` writes only the registry.
  - **The Act:** "Privacy Act 1988", its compilation's own `<title>`. It is a
    constant beside `INSTRUMENT_ID` in `scripts/ingest_compilation.py`, and a
    test checks it against the source.
  - **A future upload API:** a request field.
- **No backfill.** No ledger is deployed (#174).
- **`complete_version` takes no title.** It never writes the ledger.
  `record_published_version`, which records the version after it, does.

**3. Each version keeps its own title.** The displayed title is read from
`instrument_version.title`, and `instrument.title` holds the most recently
recorded version's, by when it was recorded, not by `valid_from`. Nothing
displays `instrument.title`.

- A citation names a version, and a chat read back shows a turn as it was
  shown (#162).
- If a policy is renamed in v2, a v1 citation keeps the name it was cited
  under.
- A version's title is written once, with its record, so the form of a
  recorded version never changes.
- **Why no `PolicyVersion` key.** A version is keyed by a plain
  `(instrument_id, version_id)` pair, not by `PolicyVersion`. That type means
  "an internal policy in the cached prefix", and a title is also read for an
  external upload or a member's private addition.

**4. Titles are read once per request, never once per citation.** There is
one `LedgerStore.version_titles(tenant_id, versions)` read:

- **In `answer()`, before generation,** for every policy version in the
  context (the cached prefix and what was retrieved). The answer carries what
  was read, and the answer event cites from it.
- **In the chat read-back,** for every clause the chat names, cited or blocked.

Alternatives considered:
- A read per citation: rejected by the spec.
- Titles carried on `PolicyVersion` from `policy_versions`: free for the
  prefix, but it misses retrieved tenant clauses (external uploads, user
  scope) and the read-back.
- A title stored on clauses or chunks: a corpus revision, and #217's
  territory.

One batched read covers every surface. The Postgres adapter makes no query at
all when no policy version is asked for.

**5. One function makes the form, and every surface uses it.**
`display_form.cite(clause_id, titles)` serves:
- the answer event and the chat read-back, through `answers.citation_json`
  and the `incomplete` list;
- blocked and unreadable entries in a read-back;
- the model's fences.

Each clause's opening marker now says `cited as "<form>"`, law included, and
the system prompt tells the model to name a clause in prose by that form. The
form goes in as a JSON string, so a quote in a title is escaped and can't
close it early to pose as an exception, a qualifier or an incompleteness. It
is also hashed into the fence's boundary, so a title can't contain its
boundary, as clause text can't.

**The repair prompt's fence is marked the same way (amended by #243).** It
first left the repair's single-clause fence as it was, so the model saw one
clause under two labels between an attempt and its repair. The repair now
fences the very `SourceClause` the answer prompt did (`answer()` keeps what it
showed, and `_repair` takes only its text from the verifier's canonical text),
so the form, the exception and qualifier glosses (ADR-0037, ADR-0044) and the
incomplete flag (ADR-0047) come from the one `SourceClause.marker`, and are
JSON-quoted and hashed into the repair fence's boundary as they are here. The
failed-quote fence still marks by id alone. The repair's system prompt says
the marker may say more than the id and that it describes the clause rather
than being part of its text.

**6. A missing title gives the number alone.** A citation of a version the
ledger doesn't hold for this tenant (only a citation that failed
verification can name one) reads as just its number, for example "2.4".
Nothing is made up from the instrument id.

## Consequences

- **The prompt changes once.** The cached prefix's bytes, and therefore its
  boundary, change once for every tenant: one cache write each. After that,
  the same titles give the same prefix, byte for byte (D-11).
- **Law prompts gain the form too,** a few tokens per clause.
- **Coverage** (`/v1/coverage`, the Markdown report) cites only library law,
  because an obligation derives from a law clause (ADR-0028). It passes
  `NO_TITLES`. A findings surface that cites policy clauses will read titles
  as the read-back does.
- **The display form isn't stored anywhere.** The title is a fact recorded
  once, and the form is computed from it and the path each time. No two
  stored copies can drift.
- **Rendering and anchors are not touched.** Click-to-clause is I2's question
  and ADR-0019's anchors are unchanged.
