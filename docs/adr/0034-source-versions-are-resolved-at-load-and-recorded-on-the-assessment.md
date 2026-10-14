# ADR-0034 — Source versions are resolved at load and recorded on the assessment

- Status: accepted. The human chose the mechanism on 2026-10-14 (quoted below).
  The model made the other calls, each named as such, and the human can revise
  them.
- Date: 2026-10-14
- Issue: #153
- Extends: ADR-0006 / `D-05` (the graph only grows), ADR-0031 (a wording
  correction keeps an id and changes what is recorded)
- Refines: `CONTEXT.md` (Corpus version, Mapping version)

## Context

`CONTEXT.md` defines the **corpus version** and **mapping version** as git tags
over `corpus/instruments/` and `corpus/mappings/`, and says an assessment is
reproducible only with both. But nothing recorded which tags the deployed
stores were loaded from. `scripts/load_mappings.py` loads Aura from a checkout
and writes no tag; the corpus bucket's ingest writes the compilation, not the
repository state it came from. So the API couldn't say which versions an
assessment was computed against, and #147 took recording an assessment out for
that reason: a string derived from the coverage itself varied with the tenant's
evidence, and (ADR-0031) a wording correction keeps an id but changes what is
recorded, which only git recovers.

#153 asked the human two things: how the deployed stores carry the tags they
were loaded from, and what happens to an assessment requested while a load is in
progress, including whether the API refuses to record when the stores disagree
with what is configured.

## Decision

**The human's decision (2026-10-14, verbatim):**

> #153 — record the corpus and mapping versions as git tags resolved at load
> time, stored on the assessment row: the tag name plus the commit SHA it
> pointed at. A tag can move; a SHA can't, and reproducibility is the point. If
> no tag exists at load, record the SHA alone and mark it untagged rather than
> refusing.

**1. A version is a tag and a commit (`SourceVersion`).** The SHA is always
recorded; the tag is the name it was known by, or absent. The assessment row
holds `corpus_version` and `mapping_version` (the tags, NULL for untagged) and
`corpus_sha` and `mapping_sha` (always). Ledger migration `0013` makes the tag
columns nullable and says so in a column comment, checks both SHAs are 40
lowercase hex characters, and requires both on every new row, along with a
non-empty `trace_id` and a `user_id` that is NULL or non-blank. Those last
checks are `NOT VALID`: rows written before them (tests only; no deployed
ledger holds one) aren't rejected, and every new row is. The adapter refuses to
read such a row back, rather than guess.

**2. A version is a tag over the data and the code that builds the graph from
it.** The corpus is more than the selection manifest in `corpus/instruments/`:
its clauses come from the compilation pinned by `SOURCE_SHA256` in
`scripts/ingest_compilation.py`, parsed by `src/compliance_rag/corpus/` and
loaded by `scripts/load_graph_structure.py`. The mappings are more than the
YAML: `src/compliance_rag/graph/` compiles and loads it. A parser fix changes
what the graph holds with no manifest touched, and checking out a recorded SHA
would otherwise re-parse differently. So each kind is a set of paths
(`SOURCE_PATHS` in `adapters/git_source_versions.py`, which is where the
repository's layout is known, not in the interfaces):

- corpus: `corpus/instruments`, `src/compliance_rag/corpus`,
  `scripts/ingest_compilation.py`, `scripts/load_graph_structure.py`, and
  `src/compliance_rag/graph/loader.py`, which writes the clause nodes;
- mapping: `corpus/mappings`, `src/compliance_rag/graph` (compiled by
  `mappings.py`, loaded by `loader.py`), and `scripts/load_mappings.py`, whose
  glob and D-01 review filter decide which files load.

`graph/loader.py` is in both: it writes the corpus's clauses and the mappings'
nodes, so a change to it is a new version of each. A change to `mappings.py` is
a new mapping version only. `neo4j_graph.py` and `graph_schema.py` are in
neither, deliberately: they are how a node is stored, not what is written, and
a storage change that altered the written graph would be a change to what the
loaders write.

This was found in review, not in the human's decision, and is a widening of
`CONTEXT.md`'s definition, which now says so.

**3. Resolution is from git, at load time, by content (the human's mechanism;
the matching rule is the model's).** `resolve_source_version(kind, repo=...)`
runs in the loader before it writes anything:

- the candidates are the tags named with the kind's prefix, newest first by
  creation date;
- the first whose commit holds the same tree (or blob) as `HEAD` for *every*
  one of the kind's paths wins, and the version is that tag with the commit it
  points at (an annotated tag is peeled to its commit);
- with none, the version is `HEAD`'s commit, untagged.

The match is on the paths' *content*, not on `HEAD`. A commit that changes
nothing under them, an API change say, leaves the version where it was;
comparing `HEAD` to the tag's commit would call every such commit a new,
untagged version. A tag that moved resolves to where it points now, and what a
load already recorded doesn't change: that is the reason the SHA is kept.

**4. The tag names are a convention: `corpus-<version>` and
`mappings-<version>`.** The prefix is how the two kinds are told apart, so
`mappings-2026.10.14` can never name a corpus.

**5. Anything uncommitted under the paths is refused (the model's call).**
Staged, unstaged and untracked changes all count, whatever
`status.showUntrackedFiles` says, and so do files git ignores: a gitignored
`corpus/mappings/draft.yaml` is loaded all the same by the `*.yaml` glob. Files
git has been told to stop watching (`assume-unchanged`, `skip-worktree`) count
too, since a change to one shows nowhere. The content a load would write is then
in no commit, so no SHA could name it, and recording `HEAD`'s would say
something false. Both loaders check this before touching the graph, as
`load_mappings` already refuses a file awaiting review. This is the one case in
which "record the SHA alone rather than refusing" doesn't apply: that sentence
is about a missing tag, and here there is nothing true to record.

Three kinds of untracked or ignored file are not uncommitted source and are let
through (`NOT_SOURCE`): bytecode (`__pycache__/`, `*.pyc`), Finder's
`.DS_Store`, and the raw compilations of instrument versions whose source is
*pinned* (`PINNED_RAW`). Raw compilations are fetched and not committed
(`.gitignore`), and the ingest refuses any bytes that are not the pinned sha256
(`SOURCE_SHA256` in `scripts/ingest_compilation.py`), so what is in a pinned
version's `raw/` cannot change what is loaded. Today that is one directory,
`corpus/instruments/privacy-act-1988/C2026C00227/raw/`; a `raw/` under any
other instrument or version is an unpinned source and refuses the load. Tests
tie `PINNED_RAW` to the pin in the ingest script, and fail if an instrument
version with a manifest is added without one. Without the first two kinds every
load would be refused on any machine that had run the code. The list is closed
and in one place; anything else untracked or ignored under a path refuses the
load.

**6. The graph records each load (the model's call).** The human decided that
the versions are recorded on the assessment; where the record of what the graph
was loaded from lives is the model's call. `begin_load(version)` creates a
`Load` node (`load_id`, `kind`, `tag` if any, `sha`, `started_at`) in the graph,
and `finish_load(load_id)` sets `finished_at`, once; an unknown or already
closed load is refused. Like the law the loads write, the record is
append-only, and `Load` is metadata: not in the bitemporal vocabulary, no valid
time, never retired, unreachable by the query builder (`D-05` says so). Its
times are the Neo4j server's (`datetime()`), never the caller's, so a client
with a wrong clock can't reorder history. `loaded_sources()` returns, for each
kind, the latest *finished* load's version (latest by when it started) and what
stands in the way of assessing against it (point 7).

It lives in the graph, not the ledger, because it describes the graph. The graph
is derived and can be rebuilt from the repository (D-09); a ledger row saying
what the graph held would survive the rebuild and then lie. And the loaders
have Aura credentials but no reason to hold the ledger's.

**7. Loads of a kind are serialised, and a failed one is cleared by a person
(the model's call).** Two loads running at once make "the latest" meaningless:
B can finish while A is still writing, and the graph would report B, with
`loading` false, over a half-written A. So `begin_load` is refused while any
load of that kind is open, in the same transaction that would write it, and an
open load holds its kind through a property under a uniqueness constraint
(`schema.cypher`), so two that begin in the same instant can't both succeed
either.

A load that fails after it began stays open, so it blocks the next one and
every assessment. It is closed by `abandon_load(load_id, by, reason)`, which
records who, why and when, and by `--abandon LOAD_ID --by NAME --reason WHY` on
either loader script. It is never closed by time: whether a quiet load has
failed or is slow is a person's call.

Abandoning releases the hold, so the next load can begin. It does not make the
graph trustworthy: the abandoned load may have written, and nobody can tell a
load that died from one that never started writing. So an abandoned load keeps
assessments blocked (`LoadedSource.blocked_by_abandoned`, a 503 that names it)
until a *later load of that kind finishes*. Only that unblocks; the version the
graph holds is unchanged meanwhile. The one exception is a load closed by the
loader itself having written nothing (point 8), which blocks nothing.

Abandoning a load that is slow rather than dead lets a second loader begin
while the first is still writing, so two loaders write at once; the first's
`finish_load` then fails, after a full write. Abandon a load known to be dead,
not one that is merely quiet.

**8. A load refused before it writes leaves no record; the checks run again
under the hold (the model's call).** Each loader is split into its checks
(`check_mappings`, `check_instrument_version`: every refusal that precedes its
first write, including every D-05 refusal) and its writes, and `recorded_load`
runs the checks, takes the hold, and runs them again before the writes. A
routine refusal, an unacknowledged wording change say, is refused by the first
run before anything is recorded, so it is not an outage of the assessment
endpoint. The second run is for the race: another load of the kind may have
begun, written and finished between the first check and the hold, and writes
resting on a check made before that rest on a graph that has since changed. If
the second run refuses, nothing was written, so the load is closed as one that
wrote nothing (`abandon_load(..., wrote=False)`), which blocks no assessment. A
load that raises after its first write stays open (point 7).

**9. The API records through one ledger call.**
`POST /v1/coverage` (`{as_of?}`; nothing else, in particular no version)
reads `loaded_sources()`, computes the coverage on the tenant's evidence, reads
`loaded_sources()` again, and records the assessment and every obligation's row
in one transaction (`LedgerStore.record_coverage_assessment(record, rows)`; a
refused row leaves no assessment, which two separate calls could not promise),
as the member (`user_id` is their email) and with the request's `trace_id`.

The 503 policy is the model's call:

- Either kind with an open load: **503** with `Retry-After`.
- Either kind with a load abandoned after it may have written, and none
  finished since: **503** naming the loader, without `Retry-After`, since
  waiting won't help.
- Either kind with no finished load: **503** naming the loaders to run.
- The second read differing from the first, because a load started or
  finished meanwhile: **503**, nothing recorded. The coverage may mix two
  versions.

`GET /v1/coverage/{assessment_id}` returns it row for row, with each version as
`{tag, sha, untagged}`. An id that isn't a uuid4 is a 422; another tenant's, or
an unknown one, is a 404.

**10. #153's second question, in its own words (the model's reading).** The
human's decision, quoted at the top, is that the versions are git tags resolved
at load time and stored on the assessment. It doesn't say how the deployed
stores carry them or what happens during a load; the shape in points 6 to 9 is
the model's. There is no configured pin: `CORPUS_VERSION` and `MAPPING_VERSION`
do not exist, and nothing loaded is checked against one. The loaders write what
they loaded into the graph, and the API reads it. So an assessment requested
while a load is in progress, or after one failed, is refused with a 503 (point
9), and there is nothing configured for the stores to disagree with. What the
API records is what the graph says it holds.

Nothing compares the graph with the corpus bucket or with Qdrant, and the
bucket and Qdrant loaders record nothing. That is acceptable while the only
thing recorded as an assessment is Track A's coverage, which is computed from
the graph alone. It stops being acceptable when an answer is recorded, because
an answer reads clause text from the bucket and chunks from Qdrant: whether
those record their loads, and whether the API refuses on disagreement among the
three, is a decision for that ticket.

**11. What the ledger doesn't keep, the read-back doesn't show.** `coverage`
rows hold the outcome, reason, citations and questions, not an obligation's
`instrument_id` or `summary`, and an assessment holds no `roles`. The recorded
shape therefore drops those from `GET /v1/coverage`'s, so that what is
returned on recording and on reading back are the same. Recording them is a
ledger change of its own, if wanted.

## Consequences

- **Every load is untagged today.** No tags exist. Until the human tags, every
  assessment records `tag: null, untagged: true` with a SHA. To name the current
  state: `git tag corpus-2026.10.14` and `git tag mappings-2026.10.14`, then
  re-run both loaders. They are idempotent apart from the new `Load` node.
- **The deployed graph needs both loaders re-run before `POST /v1/coverage`
  works**, and `scripts/apply_graph_schema.py` for the two `Load` constraints
  (idempotent; the serialisation needs the second). Until then it is a 503
  naming the loaders. This change runs none of that.
- **A `Load` node is added to the graph per loader run**, and counts toward
  `counts()` and so toward Aura's limits. At a few a day that is not a concern;
  nothing compacts them.
- **Widening the paths means more things are new versions.** A parser change
  makes the corpus untagged until it is tagged and loaded again. That is the
  intent: it changes what the graph holds. A load must also be run from a
  checkout with those paths committed.
- **Reproducing an assessment** is checking out its two SHAs. A tag that has
  moved since can be told apart by the SHA it was recorded with.
- A reviewer reading a version sees `mappings-2026.10.14 @ 1a2b3c4` (a tag and
  seven characters of the commit) or `untagged @ 1a2b3c4`.
