# ADR-0048 — Publishing takes a version's clauses and writes the clause store first

- Status: accepted. The model's calls throughout, within #233's spec.
- Date: 2026-10-20
- Issue: #233 (gap 5 and U5 item 2 of `docs/research/phase3-product-options.md`)
- Amends: ADR-0021 (its consequence "`publish_version` writes Qdrant only"
  no longer holds)
- Touches: ADR-0026 (the writer allowlist is unchanged), ADR-0036 (the
  owner travels with the clause), ADR-0042 (the amend path's vector load)

## Context

`CorpusLifecycle.publish_version` took chunks and wrote two stores: the
chunks to retrieval and the version's record to the ledger. It wrote nothing
to the clause store. The verifier reads a cited clause's canonical text from
the clause store, and so does the cached policy prefix (`tenant_policies`
reads the manifest, then each clause). A policy published through the
lifecycle, which is what an upload API will call, would have been retrievable
and in the ledger, and every citation of it would have blocked
(`CLAUSE_MISSING`). Its prefix contribution would have been silently empty.
Only `replace_in_place`, compilation ingest (library law) and operator
scripts wrote clause text.

## Decision

**1. `publish_version` and `complete_version` take clauses, not chunks.** The
signature is now `(clauses, *, count_tokens, source_html, …)`. The lifecycle
derives the chunks itself with `chunk_clauses`.

- **Why clauses.** The clause store holds clauses, and the chunks are a pure
  function of them (`chunk_clause`, one chunk per clause, its text the
  clause's canonical text).
- **Why derive the chunks rather than take both and check them.** Taking both
  would let a caller pass a pair that disagrees, and the check would have to
  restate the chunker's rule. Derived, a published chunk's text is its
  clause's canonical text by construction.
- **No synthetic claim on publish.** A clause has no `synthetic` field, so
  publish has no flag for an upload to set (#78). The registry marks the
  derived chunks, and the residency gate counts an unregistered clause as
  real, as `replace_in_place` already did.
- **The cost.** The lifecycle needs the reranker tokenizer's count
  (`count_tokens`), passed per call as `vector_load` already did. It is a
  parameter, not a constructor argument, so the many places that build a
  lifecycle don't change.
- **The source.** `source_html` is the document the clauses were parsed
  from, stored as the version's `source.html` with its hash on the manifest.
  This is the same parameter, with the same meaning, that `replace_in_place`
  takes. For a Markdown policy it is the Markdown as uploaded. How a policy
  citation is rendered for click-to-clause is I2's question; this ADR doesn't
  answer it.
- **What publishing now refuses.** A version with no clauses, a clause id
  given twice, and clauses under mixed canonicalisation versions. The
  manifest records one canonicalisation version per version.

`replace_in_place` is unchanged. It already took both, and its tests use
mismatched pairs to prove the registry and the gate.

**2. The order, and what makes it resumable.** All checks run before any write:

- the class check;
- the payload: one version, one owner, and private text never in a policy;
- the registry and the residency gate;
- the refusal of a version retrieval already holds;
- the clause store's own comparison (below).

Then the writes:

1. **The clause store.** First the clauses it lacks: `put_clauses`, which in
   Cloud Storage lists every path on the manifest before writing the objects,
   as an intent record. Then `put_manifest`, the source and the
   canonicalisation version, which marks the version complete, as ingest
   does. The bucket is authoritative, and the derived stores come from it
   (ADR-0042's reason for going first).
2. **Retrieval.** The chunks.
3. **The ledger.** The version's record. This is the commit point. Until it is
   written, the verifier blocks every citation of the version
   (`VERSION_UNKNOWN`), and the prefix doesn't list it.

The clause-store step writes nothing it already holds, and never writes over
anything. Of the version, the clause store may hold only some of the clauses
given, each one identical to its given clause, with the same source and
canonicalisation version once those are written. Anything else is refused
before any write:

- a stored path that isn't given;
- a manifest hash that differs from the given text;
- a stored clause that differs in any field;
- a different source;
- a clause missing from a version already complete.

A different document under a stored `version_id` is a new version, or a
replace in place, never a resume. So a part-way state is finished as follows:

- **The clause store part-written, nothing in retrieval.** Run
  `publish_version` again. It writes what is missing, then the rest. Still
  "not yet published", because retrieval holds none of the version.
- **Some chunks in retrieval.** `publish_version` is refused, as
  re-publishing always was. `complete_version` repeats the clause-store step,
  which is idempotent, and upserts the missing chunks. A second call writes
  nothing.
- **Every chunk in retrieval, no ledger record.** Call
  `record_published_version`. It now also refuses a version the clause store
  doesn't hold complete (no manifest metadata), because the record is what
  makes a version citable and puts a policy in the prefix. A version whose
  chunks were published before this change, with no clause text, is finished
  by `complete_version`, which writes the text and returns 0, and then by the
  record.

**3. Library law is checked, not rewritten.** `load_version_vectors` publishes
library law through the same lifecycle. It now takes `source_html`, read back
with `read_version_source`, beside the clauses `read_version_clauses` reads
back. Ingest wrote both, so the clause-store step finds everything identical
and writes nothing. A versioned bucket would otherwise keep a noncurrent
generation of each of 779 objects on every load. The operator script and the
amend path of a corpus revision read the stored, anchored source. A version
whose ingest never finished has no source, and is an error.

**4. User scope.** `ClauseRecord.user_id` is stored as given. The comparison
reads each clause as its owner, so the owner's own partial publish resumes.
`get_clause` is owner-only (ADR-0036), so a reader who isn't the owner still
gets nothing, and the verifier blocks their citation.

**5. The writer allowlist is unchanged.** The new writes go through the
lifecycle's existing `ClauseStore` calls (`put_clauses`, `put_manifest`), in
the one module the allowlist already names for the write path
(`test_synthetic_registry.py`).

## Consequences

- A policy published through the lifecycle can be cited and verified. The
  proof is `tests/test_publish_clause_text.py`, run on `FakeClauseStore` and
  on `GCSClauseStore` over the versioned in-memory bucket, never a real one.
  All six Acme fixtures are published offshore with the real registry, and for
  every clause:
  - the prefix lists it, and the tenant-text gate passes it;
  - the verifier passes a correct quote and blocks a wrong one;
  - an unpublished path is `CLAUSE_MISSING`.

  The lifecycle contract asserts the clause store's contents and now also
  runs against `GCSClauseStore`.
- **Mutant check.** With the clause write removed, 22 tests fail.
- **What it can't tell apart.** `get_clause` is owner-only, so a path whose
  object belongs to another member reads as absent. If its manifest hash is
  the same text, it would be written over, with the same words and a new
  owner. That needs two members publishing one `version_id` of one
  instrument. ADR-0036's version-owner follow-up closes it, along with the
  manifest and `source.html`, which are keyed by tenant and not yet
  owner-only. No confidential text crosses: the overwriting member supplied
  those exact words.
- **Reads.** Each publish reads every given clause back once, 16 at a time.
  For a policy this is a handful. For the 779-clause compilation it is 779
  Class B operations per vector load, a fraction of a cent.
