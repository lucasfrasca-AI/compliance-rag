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
columns nullable, checks both SHAs are 40 lowercase hex characters, and
requires both on every new row. That last check is `NOT VALID`: rows written
before it (tests only; no deployed ledger holds one) aren't rejected, and every
new row is. The adapter refuses to read such a row back, rather than guess.

**2. Resolution is from git, at load time, by content (the human's mechanism;
the matching rule is the model's).** `resolve_source_version(kind, repo=...)`
runs in the loader before it writes anything:

- the candidates are the tags named with the kind's prefix, newest first by
  creation date;
- the first whose commit holds the same tree for the kind's path as `HEAD` does
  wins, and the version is that tag with the commit it points at (an annotated
  tag is peeled to its commit);
- with none, the version is `HEAD`'s commit, untagged.

The match is on the path's *content*, not on `HEAD`. `CONTEXT.md` defines the
version as a tag over that path, so a commit that changes only code leaves the
version where it was; comparing `HEAD` to the tag's commit would call every
code change a new, untagged version. A tag that moved resolves to where it
points now, and what a load already recorded doesn't change: that is the reason
the SHA is kept.

**3. The tag names are a convention: `corpus-<version>` and
`mappings-<version>`.** The prefix is how the two kinds are told apart, so
`mappings-2026.10.14` can never name a corpus.

**4. A path with uncommitted changes is refused (the model's call).** Staged,
unstaged and untracked changes under the path all count; changes elsewhere
don't. The content a load would write is then in no commit, so no SHA could name
it, and recording `HEAD`'s would say something false. Both loaders check this
before touching the graph, as `load_mappings` already refuses a file awaiting
review. This is the one case in which "record the SHA alone rather than
refusing" doesn't apply: that sentence is about a missing tag, and here there
is nothing to record.

**5. The graph records each load, in the graph.** `begin_load(version)` creates
a `Load` node (`load_id`, `kind`, `tag` if any, `sha`, `started_at`) and
`finish_load(load_id)` sets `finished_at`, once; an unknown or already finished
load is refused. Like the law the loads write, the record is append-only, and
`Load` is metadata: not in the bitemporal vocabulary, no valid time, never
retired, unreachable by the query builder. `loaded_sources()` returns, for each
kind, the latest *finished* load's version (latest by when it started) and
`loading`: a load of that kind started after it and hasn't finished.

It lives in the graph, not the ledger, because it describes the graph. The graph
is derived and can be rebuilt from the repository (D-09); a ledger row saying
what the graph held would survive the rebuild and then lie. And the loaders
have Aura credentials but no reason to hold the ledger's.

**6. A load that doesn't finish blocks assessments (the model's call).**
`recorded_load(graph, version)` wraps a loader's writes: `begin_load` before,
`finish_load` only if the body returns. A body that raises leaves the load
unfinished, so `loading` stays true, as the graph may be part-written, until a
later load of that kind finishes. It also holds for a load refused before its
first write, which the wrapper can't tell from one that died halfway; the
cost is that a refused load must be re-run, once its refusal is dealt with,
before assessments resume. Erring toward refusing is the point.

**7. The API records through one ledger call.**
`POST /v1/coverage` (`{as_of?}`; nothing else, in particular no version)
reads `loaded_sources()`, computes the coverage on the tenant's evidence, reads
`loaded_sources()` again, and records the assessment and every obligation's
row in one transaction (`LedgerStore.record_coverage_assessment`; a refused row
leaves no assessment, which two separate calls could not promise), as the
member (`user_id` is their email) and with the request's `trace_id`.

- Either kind with no finished load, or either unfinished: **503**. The first
  names the loaders to run; the second adds `Retry-After`.
- The second read differing from the first, because a load started or
  finished meanwhile: **503**, nothing recorded. The coverage may mix two
  versions.

`GET /v1/coverage/{assessment_id}` returns it row for row, with each version as
`{tag, sha, untagged}`. An id that isn't a uuid4 is a 422; another tenant's, or
an unknown one, is a 404.

**8. Only the graph records loads, for now.** The corpus bucket (ingest) and
Qdrant (embed and upsert) loaders record nothing. Nothing records an *answer*
as an assessment yet, so the one thing an assessment is computed from, Track A
over the graph, is the one source whose version is recorded. When answers are
recorded, whether the bucket and the vector store record loads too is a
decision for then.

**9. What the ledger doesn't keep, the read-back doesn't show.** `coverage`
rows hold the outcome, reason, citations and questions, not an obligation's
`instrument_id` or `summary`. The recorded shape therefore drops those two from
`GET /v1/coverage`'s, so that what is returned on recording and on reading back
are the same. Recording them is a ledger change of its own, if wanted.

## Consequences

- **Every load is untagged today.** No tags exist. Until the human tags, every
  assessment records `tag: null, untagged: true` with a SHA. To name the current
  state: `git tag corpus-2026.10.14` and `git tag mappings-2026.10.14`, then
  re-run both loaders. They are idempotent apart from the new `Load` node.
- **The deployed graph needs both loaders re-run before `POST /v1/coverage`
  works**, and `scripts/apply_graph_schema.py` for the `load_id` uniqueness
  constraint (idempotent). Until then it is a 503 naming the loaders. This
  change runs none of that.
- **A `Load` node is added to the graph per loader run**, and counts toward
  `counts()` and so toward Aura's limits. At a few a day that is not a concern;
  nothing compacts them.
- **Reproducing an assessment** is checking out its two SHAs. A tag that has
  moved since can be told apart by the SHA it was recorded with.
- A reviewer reading a version sees `mappings-2026.10.14 @ 1a2b3c4` (a tag and
  seven characters of the commit) or `untagged @ 1a2b3c4`.
