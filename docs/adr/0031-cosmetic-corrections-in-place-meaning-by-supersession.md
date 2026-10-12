# ADR-0031 — Cosmetic corrections in place; a change of meaning is a new id

- Status: accepted
- Date: 2026-10-13
- Issue: #120 (from the #93 design session, Q4)
- Amends: ADR-0006 / `D-05` ("nodes are immutable")

## Context

D-05 makes every loaded node immutable, so the loader refuses a mapping file
whose loaded item differs at all. The mapping README used to say "correcting a
mapping edits the existing entry", which the loader then refused. A typo in an
obligation's summary would have to be superseded by a new obligation id. That
multiplies ids for nothing, and it puts a misspelling on the same footing as
pointing an obligation at the wrong clause.

D-05 exists because "deleting superseded law silently changes issued reports".
That is about *meaning*. A misspelt summary doesn't change what applies to
anyone. And every assessment records the mapping version (a git tag), so the
wording an issued assessment showed can always be recovered from git.

## Decision

**Split by meaning (#93 Q4, recommendation b):**

- **Meaning is immutable.** That covers an item's clauses, dates, actors,
  jurisdictions and sectors, required conditions or controls, operands, a
  leaf's field, period, test and value, `supersedes`, and a crosswalk's kind.
  A change to any of them is a new id with `supersedes` (D-05), refused as
  before.
- **Wording may be corrected in place.** That covers a closed list
  (`graph_schema.COSMETIC`): `summary`, `description`, `question`,
  `unassessable_reason`, `name`, `descriptor` and a crosswalk's `note`.
- **Only when acknowledged exactly.** `load_mappings` finds every wording
  difference before writing. It applies them only if the caller's
  `CorrectionAcknowledgement` names exactly those item ids, plus who made the
  correction and why. Missing or extra acknowledgements are refused, as
  `acknowledged_additions` refuses for ingest.
- **Audited on the node.** `GraphAdmin.correct` locks the node (or crosswalk
  edge), checks every key is cosmetic, sets the new wording, and appends to the
  node's own `corrections` trail: when, by whom, why, and the previous wording.
  It is one transaction, and repeating the same correction is a no-op. The
  trail is writer-kept like `recorded_at`, so a loader never authors it and
  comparisons ignore it.

## Consequences

- Typos cost a reviewed YAML edit and one acknowledged load, not a new id.
- A loaded node still says what it said before: the trail keeps every previous
  wording, so the graph itself, not only git, shows what an earlier answer
  displayed.
- The list of cosmetic fields is closed and lives in one place. Adding a field
  to it is a change to this ADR.
