# Handoff 0000 — setup and Phase 0

**Date:** 2026-09-22
**Phase at handoff:** 1 — Corpus, retrieval, verifier
**Branch:** `chore/scaffold`, 15 commits ahead of `main`, PR #1 open
**CI:** run `35680489579` green — both `ci` and `trace-check`

Phase 0 is closed. This document covers what a fresh agent needs that is *not*
already in the repo. Everything else is referenced, not repeated.

## Read first

`CLAUDE.md` → `CONTEXT.md` → `DECISIONS.md` → `PHASES.md` → `SERVICES.md` →
`BUILD_LOG.md`. `BUILD_LOG.md` is the narrative of what exists and why; it now
records failures as well as successes, which is the point of reading it.

## Phase 0 closed — the evidence

CI run **35554816201** satisfied the last exit criterion, and the current run
**35680489579** still does. Proof is in the run, not in a claim here:

| Criterion | Where it was proven |
| --- | --- |
| `uv run pytest` green in CI | job `ci` — ruff, ruff format, pyright strict, 42 tests |
| Trace in the Sydney bucket from a CI run | job `trace-check` — 1 entry in `compliance-rag-logs` |
| Nothing matching the filter in `_Default` | same job — 0 entries |
| Interfaces have fakes and a contract test each | `tests/contracts/test_interfaces.py` |

The trace came from CI authenticated keylessly as `compliance-rag-ci` through
Workload Identity Federation. No service-account key exists in this project and
creating one is hard-denied by the guard.

## The billable guard — read this before running any `gcloud`

The mechanism is documented in `SERVICES.md` ("Protocol for anything billable")
and `CLAUDE.md` step 3. Two things about it are easy to get wrong:

**It is built from `deny`, not `ask`.** A `PreToolUse` hook returning `ask` is
not enforced in every session type — in this project it collapsed into an allow
and two billable commands ran unprompted. Every billable command is now denied
unless an unused line in `.claude/billable-approvals.txt` *equals* it, after
whitespace normalisation. `<approved> && anything` does not match.

**You can read that file but not write it.** `Edit`, `Write` and any `Bash`
command naming the path are denied; the `Read` tool is deliberately open, so a
refused approval can be diagnosed. The same asymmetry applies to
`ci/wif-provider-flags.yaml`, which holds the OIDC attribute condition.

**Approval lines with more than one command must be tested before being
offered** — `bash -n` plus an argv dump, per `CLAUDE.md`. This is not
ceremony: three consecutive hand-written attempts at one exclusion line failed
(a literal `\"` inside `bash -c '…'`, then a comma splitting a comma-separated
dict flag), and each failure burned a single-use approval.

The guard's catalogue was extended four times during Phase 0, each time because
a command that should have been gated was not. Expect to extend it again; a
command that creates or grants anything and passes through silently is a bug.
It was also narrowed once, after over-matching blocked read-only `describe` and
`list` calls — the verification that proves what was created.

## Two operational findings that will otherwise waste a session

**Cloud Logging sink exclusions take about 11 minutes to propagate.** Checks at
1 and 7 minutes still saw the leak. Before concluding an exclusion is broken,
check its age. `ci/trace_check.py` now reads the exclusion's own configuration
and distinguishes "missing or mismatched filter" (hard failure) from "correct
but written inside the 20-minute grace window" (fails, but says so plainly).
Log *ingest*, by contrast, is seconds — the two budgets are separate constants
and should not be conflated again.

**The span exporter's transport depends on where it runs.** Cloud Run's logging
agent turns stdout into log entries; a GitHub runner has no agent, so a span
printed to stdout reaches nothing. `telemetry.py` selects by `env`:
`cloud`=stdout, `ci`=Cloud Logging API, `local`=stderr. A CI job that emits
spans must run with `env=ci` or it will silently log nothing — and "nothing in
either bucket" reads as a residency pass unless the check guards against it,
which `require_auth()` does by exiting 2.

## Standing constraint on Phase 1 CI jobs

`compliance-rag-ci` holds `roles/logging.viewAccessor` on the `_AllLogs` view of
**both** `compliance-rag-logs` and `_Default`. The `_Default` binding is wider
than it looks: it lets CI read *everything* routed to the default bucket, not
just its own spans. It exists because the absence assertion is the residency
claim (D-16) and you cannot prove a log is not somewhere without being allowed
to look.

**The constraint that follows: CI must never run code that could exfiltrate
what it reads.** Phase 1 adds `ingest` and `eval` Cloud Run jobs and a CI
regression gate. If any of them gain network egress to somewhere untrusted, or
start echoing fetched log content into a public artefact, this binding needs
revisiting. Recorded in `BUILD_LOG.md` 2026-09-22 with the same wording.

## Vocabulary changed under you

`CONTEXT.md` was grilled in two rounds (commits `f75c343`, `7f5436f`). Five
terms added, eleven sharpened. Read the file; the changes are not summarised
here. The ones most likely to bite:

- **Chunk** is capped at **420 tokens by the reranker's tokenizer**, not 500 by
  an unspecified one. The embedder never binds; the cross-encoder's 512-token
  query+passage window does, and overflow truncates the clause tail silently.
  The cap is therefore a function of the reranker model — see #6.
- **Canonicalisation** is now specified exactly and **versioned**. It defines
  what counts as a matching citation under D-02.
- Public law carries the reserved tenant id `_public`, never `NULL`.
- Instrument ids are globally unique; tenant-scoped ones are **tenant-prefixed**
  (`acme/hr-policy`). The code does not accept this yet — see #4.

## D-14 amended

`DECISIONS.md` D-14 read flatly as "documents never get graph nodes", which
forbade the tenant subgraph `CONTEXT.md` already defined and would have blocked
Phase 2 from traversing an approved policy-to-law mapping. Read loosely it
permitted a persisted policy node — the thing it exists to prevent.

Resolved in `docs/adr/0018-policy-clauses-are-projected-not-persisted.md`,
which supersedes ADR-0015 (marked superseded, not deleted). Policy clauses are
nodes only inside the ephemeral tenant subgraph, projected per request at
`as_of`. Uploaded documents get no node at all.

**This has a Phase 2 consequence with a Phase 1 cost:** "never persisted" has
to be enforced in the graph adapter, not by convention. A graph write touching a
policy clause is a bug and needs a test that proves the loader refuses it.

## Open work

Issues #2–#6, all `needs-triage`, all opened because building Phase 0 forced a
guess. Bodies carry the reasoning; not repeated here.

| # | Title | Note |
| --- | --- | --- |
| #2 | Version the canonical form and record it on every citation | D-02 |
| #3 | Validate tenant ids: no `:` or `/`, `_public` reserved | no `Tenant` model exists yet |
| #4 | Accept tenant-prefixed instrument ids in the stable-ID grammar | `INSTRUMENT_ID_RE` has no `/` |
| #5 | Clause-ID router regex must accept the tenant/ prefix | **blocked by #3 and #4** |
| #6 | Derive the chunk token cap from the reranker window | couples to the reranker A/B |

Not yet ticketed:

- **`CONTEXT.md` has no term for an uploaded document.** D-14 states that
  proposals and user-private additions get no graph node; the glossary is
  silent. Not a contradiction — a gap. Adding a **Document** term would close
  it.
- **PR #1 is open and unmerged**, 15 commits, CI green.
- **Branch protection on `main` is not configured.** The `ci` check exists and
  has passed, so it can now be referenced — but the GitHub API returned 403:
  branch protection needs GitHub Pro or a public repo. `SETUP.md` §1 assumes it
  is available. The human must either upgrade, make the repo public, or accept
  that "CI must be green" is a convention rather than an enforced gate.

## Suggested skills

Call the `Skill` tool for these. Note that most `mattpocock-skills` carry
`disable-model-invocation` — the **human** must type them as slash commands;
you cannot start them yourself.

- `mattpocock-skills:grill-with-docs` — start every Phase 1 feature with it,
  per `CLAUDE.md`. Human-invoked.
- `mattpocock-skills:to-spec` → `to-tickets` → `implement`, with
  `mattpocock-skills:tdd` at the seams. All human-invoked.
- `mattpocock-skills:code-review` — **must** be called with the plugin prefix.
  Bare `/code-review` resolves to a different, built-in skill.
- `mattpocock-skills:domain-modeling` — model-invocable. Use when changing
  `CONTEXT.md` or writing an ADR.
- `mattpocock-skills:diagnosing-bugs` — model-invocable.

Project configuration for these skills is in `docs/agents/`: GitHub Issues as
the tracker, the five canonical triage roles kept separate from the six topic
labels (`bug`, `feature`, `corpus`, `eval`, `later`, `blocked`), single-context
domain docs.

## Where to start

Phase 1's first deliverable is the legislation.gov.au parser. Its exit criteria
are in `PHASES.md`. Before writing it, note that #4 and #6 change the shape of
what the parser produces — clause ids and chunk boundaries — so triaging those
two first will avoid re-chunking the corpus later.

Nothing in Phase 1 is unlocked billable-wise until asked for: the Cloud Storage
bucket, Vertex embeddings, Claude on Vertex and the reranker service all need
approval lines. `SERVICES.md` has the rows and the estimates.
