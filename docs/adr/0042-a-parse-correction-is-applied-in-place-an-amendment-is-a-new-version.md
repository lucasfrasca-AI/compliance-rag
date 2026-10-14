# ADR-0042 — A parse correction of library law is applied in place, audited clause by clause; an amendment is a new version

- Status: accepted. The human decided the path (quoted below); the model made
  the other calls, each named as such.
- Date: 2026-10-15
- Issue: #178 (part 2)
- Amends: ADR-0021 / `D-18` ("law is never replaced in place under any
  circumstances"), ADR-0006 / `D-05` ("nodes are immutable")
- Extends: ADR-0031 (the `corrections` trail on a node), ADR-0034 (a write to
  the graph is a recorded load)

## Context

#178 found the most serious defect in the project. The parser attached the
words a definition's list ends with to `s6(1)`'s chapeau, so the stored
definition of *record* included what the Privacy Act excludes, and *credit
reporting body* read as every organisation. #186 found the same shape outside
definitions. The parser is fixed (#187, ADR-0039; #192, ADR-0041). The
deployed compilation is not.

That compilation, C2026C00227, is stored three times, all with the old parse:
the corpus bucket (canonical text, manifest hashes, object versioning on),
Aura (Clause nodes carrying `canonical_sha256`), and the deployed Qdrant
`clauses` collection (chunk text and vectors, point ids derived from the
clause id). Over the whole pinned compilation (sha256 `6d0a43e5…`, 4114
clauses) a fresh parse changes 41 clause texts. The deployed version holds
the 779 clauses its reviewed selection keeps (`sch1/`, `s6/`, `s6c/`, `s6d/`,
`s6da/`, `s6e/`, `s6ea/`, `s15`), and **seven** of them change: #178's five
(`s6/1`, `s6/1/commonwealth-officer`, `s6/1/court-tribunal-order`,
`s6/1/credit-reporting-body`, `s6/1/record/d`) and two of #186's
(`sch1/app7/6`, `sch1/app7/6/c`). The other 34 are in sections no store holds.
Clause ids, order and `source.html` are byte-identical before and after.

The verifier can't catch this. It compares a citation with the stored
canonical text, so a wrong stored text matches itself. And no sanctioned path
could correct it: `ingest_compilation` refuses a version whose content
drifted ("removals and edits can't be acknowledged"), `replace_in_place`
refuses library law (ADR-0021), and ADR-0031's in-place corrections cover a
mapping's wording only.

## The human's decision

> #178 part 2 — republish the corrected text, audited clause by clause as you
> recommend. This is the most serious defect found in the project: a
> definition whose stored text inverted the Act's meaning, in the corpus the
> verifier treats as authoritative. The verifier cannot catch it, because the
> wrong text matches itself. … The correction path you build here is the same
> one Phase 4 needs for amendments, so build it as the general mechanism
> rather than a one-off.

## Decision

**1. One mechanism: a corpus revision.** It is the only way stored library law
changes after ingest (`corpus/revision.py`, `adapters/corpus_revision.py`,
`scripts/revise_compilation.py`). It has three parts, shared by two modes:

- **The plan** is pure. A fresh parse of an instrument version's source,
  kept by its reviewed selection, compared clause by clause with what the
  bucket holds: added, removed and changed clauses, each changed one with both
  canonical texts and both hashes, and the source's sha256. It is computed
  from reads alone, before any store is written.
- **The acknowledgement** names exactly the clauses the revision writes or
  retires, who authorised it and why, and the number of findings citing the
  clauses it changes. A missing or extra id is refused, as
  `acknowledged_additions` refuses for ingest; a findings count that isn't the
  count now is refused, as `replace_in_place` refuses (ADR-0021).
- **The record** says what was done: each clause's previous and new hash, who,
  why, when, the source bytes' sha256, the commit of the code that parsed it
  (ADR-0034) and the findings count acknowledged.

**2. `correct`: the same source, the same version, in place.** A parse
correction keeps the `version_id`, because the law did not change: only how
it was parsed. It is refused unless all of these hold, checked before the
acknowledgement is looked at:

- the source parsed is the version's pinned bytes (its sha256 is the pin):
  that is what proves the law didn't change;
- the parse anchors `source.html` exactly as stored: anchors that moved are
  not a text correction;
- the clause set is the stored one. A removal is refused (a clause id is what
  a citation carries; a provision the law repealed is a new compilation). An
  addition is refused too (the model's call): a reviewed selection that grew
  is ingest's acknowledged-additions route, which works once the stored text
  matches the parse; a parser finding new clauses in the same source is not a
  text correction. Doing the correction first and the additions after breaks
  the only circularity (ingest refuses changes, the correction refuses
  additions).

Why in place, against ADR-0021's "law is never replaced in place": that rule
protects an issued citation from the law it verified against changing under
it. Here the law didn't change; the stored text misrepresented it. A new
`version_id` would assert a compilation the Register never published, leave
the wrong text citable at every `as_of` before it, and repoint every clause
id the signed mappings cite. The findings count is what keeps the
consequence in view: a correction changes what an issued citation of these
clauses verifies against, so a person sees how many there are first. (None
exist yet: nothing has been served from the deployment.)

**3. `amend`: a new compilation, a new version.** The same plan,
acknowledgement and record, applied as version and retire (ADR-0021) through
the existing paths: `ingest_compilation` writes the new version and its record
goes on the new version's manifest; `write_instrument_version` supersedes the
old version in the graph, retiring it and its clauses, under a recorded load;
`load_version_vectors(..., supersedes=old)` publishes the new chunks through
`CorpusLifecycle`, which retires the old ones and ends the old version in the
ledger. Every step is idempotent, so re-running the call finishes one that
stopped. It is built and tested on fixtures against a real Neo4j and a real
Qdrant. What remains is Phase 4 (#198): a second pinned compilation and its
reviewed selection, the operator wiring (`--mode amend` stops and names the
issue), re-grounding the mappings on the new clauses (ADR-0028), a deployed
ledger (#174), and acknowledging hundreds of ids from a reviewed file.

**4. The order of a correction, and its commit point (the model's call).**

1. **The bucket.** The manifest is written first: the new hashes and the
   revision's record, in one write. That write is the commit: once the record
   is on the manifest, the revision has happened. Then the clause objects. The
   bucket is versioned, so each object written over keeps its old text as a
   noncurrent generation, and so does the manifest.
2. **The graph**, as a recorded corpus load (ADR-0034) at the commit that
   parsed the new text: each changed Clause node's `canonical_sha256` moves
   from the recorded previous hash to the new one, all in one transaction;
   then the version's whole structure is written again, which is idempotent
   and refuses any node that disagrees with the parse. So the load the graph
   records — what an assessment names as its corpus version — is true of the
   graph, not only of the seven nodes.
3. **Qdrant.** Each changed chunk is re-embedded and written over itself:
   one upsert per point, carrying the stored payload with the new text, so the
   text and the vectors change and nothing else does (valid time, scope,
   `ancestors`, the synthetic and withdrawn flags stay the point's).

The bucket goes first because it is authoritative and the graph and Qdrant
are derived from it (`CONTEXT.md`: Derived store). Before the commit, nothing
is written, and every refusal is there: the pin, the anchors, the clause set,
the acknowledgement, the findings count, an open corpus load, and a derived
store that disagrees with the parse anywhere outside the revision (each must
hold every other clause at the parse's hash and each revised clause at its
previous or new one). A drifted derived store is a person's question, not
something a correction should paper over.

**5. Where the audit lives (the model's call).** Next to what changed, in two
places, because the ledger isn't durable yet (#174) and can't be the only one:

- **The bucket manifest**, `revisions`: one record per revision, each clause's
  previous and new hash, who, why, when, the source sha256, the code's commit
  and the findings count. It is written in the same object write as the new
  hashes, so the record and the change can't disagree: no revision is
  recorded that didn't happen, none happens unrecorded. With versioning, the
  old manifest and the old clause objects are kept beside it.
- **Each corrected Clause node's `corrections` trail**: ADR-0031's entry shape
  (when, by whom, why, the previous value), plus the revision's id and source.
  The node still says what it said before. The trail is writer-kept, so a
  loader's comparison ignores it (`WRITER_KEYS`).

Not the ledger: it isn't deployed, and a ledger row about what the bucket and
graph hold would outlive a rebuild of either and then lie (ADR-0034's reason
for keeping `Load` in the graph). Not Qdrant: it is derived, and its payload
changes only in its text. Every step emits a span (`corpus_revision.*`,
`neo4j_graph.revise_clauses`, `qdrant.revise_chunks`) naming the revision.

**6. Idempotent, and a part-way run is a state of its own (the model's call).**
`status` reads the version's latest revision and says, per store, which of its
clauses are still behind: bucket objects, graph nodes, Qdrant chunks, and
whether a corpus load is open. A re-run after a successful correction plans
nothing and is refused ("changes nothing"); `finish` finds nothing behind.

A correction that stopped after its commit is **unfinished**. A new
correction is refused until it is finished, and `finish` (`--apply --finish`)
brings every store up to the recorded revision. It takes no acknowledgement,
because it decides nothing: it writes only the text whose hash the record
holds, under the record's own id, so the graph's trail names the same revision
as the bucket. It is refused unless this checkout's parse, of the recorded
source bytes, reproduces every recorded hash. A graph step that died leaves
its load open (ADR-0034); the open load refuses the finish until a person
abandons it (`load_graph_structure --abandon`, #153), and the finish's graph
step is then the finished load that unblocks assessments.

**7. The writes take a capability.** `RevisionAuthorization`, issued only by
`CorpusRevision`, carries the record; the store-level writes
(`ClauseStore.record_revision`, `GraphAdmin.revise_clauses`,
`RetrievalAdmin.revise_chunks`) touch only the clauses it names, of its
version, with exactly the text whose hash it records, and refuse anything
else. As with `RemovalAuthorization`, a bypass is deliberate and visible, not
impossible. Library law only: a tenant's document has version and retire,
withdraw, purge and replace in place (ADR-0021).

**8. The findings count is every tenant's (the model's call).** Library law is
cited by every tenant, and a revision names exact clauses, so the count is the
findings of any tenant with a citation of one of them, by exact `clause_id`
(`count_findings_citing_clauses`, ledger 0015; a prefix would count `s6/10`
for `s6/1`). The operator script reads the configured ledger. None is deployed
yet (#174); `--no-ledger` says so, the count is then 0 and must still be
acknowledged, and the flag is refused if a ledger answers.

## Consequences

- `D-05` and `D-18` are amended to point here, and `CONTEXT.md`'s *Canonical
  text*, *Version and retire* and *Purge* entries say so; *Corpus revision* is
  a new entry.
- **Running the correction is the human's**, after merge, with the command in
  the PR. It reads the bucket, Aura, Qdrant Cloud and (to embed seven chunks)
  Vertex in australia-southeast1 through `Settings`, writes to existing
  resources only, and costs a fraction of a cent.
- **The signed mapping cites `s6/1/credit-reporting-body`** (s6D(4)(f)'s "is a
  credit reporting body"). Its id doesn't change; its text gains "that carries
  on a credit reporting business." (ADR-0039 asked the reviewer to re-read
  that condition against it.)
- **What the count doesn't see.** A chat turn (#162) records the clause ids an
  answer was shown with, and re-displays their canonical text; a correction
  changes what such a turn shows. The guardrails here, like ADR-0021's,
  count findings only. No chat exists in any deployed ledger.
- The other 34 changed clauses of the compilation are in sections no store
  holds; a selection that grows to include them ingests the corrected parse.
- `scripts/revise_compilation.py` is not in the corpus version's paths
  (ADR-0034): it applies the same pin and selection the ingest and graph
  scripts do, by importing them, and decides nothing about content.
