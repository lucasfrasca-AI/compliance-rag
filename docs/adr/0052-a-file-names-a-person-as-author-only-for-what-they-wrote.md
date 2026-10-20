# ADR-0052 — A file names a person as author only for what they wrote

- Status: proposed. The human decided the rule on 2026-10-21 (quoted below).
  The split and its shape are the model's. The split takes effect when he
  signs `part_year.yaml`; until then the PR that carries it stays a draft.
- Date: 2026-10-21
- Issue: #254
- Amends: ADR-0047 Decision 9 (the signed reading), and the corpus rules'
  D-01 practice for model-drafted artefacts (`CLAUDE.md`, Corpus rules)

## Context

`corpus/instruments/privacy-act-1988/C2026C00227/incomplete.yaml` (#211)
recorded `author: "Lucas Frasca"` and `reviewed_by: "Lucas Frasca"` over two
fields:

- **`shown`**: his wording, verbatim (#211, decision 3). His name belongs on it.
- **`part_year_only: {started: business_started_on}`**: the model's encoding
  of his decision 2 ("Mark them unassessable only when the facts leave open
  that the business ran for part of a year"). He decided the rule. The model
  chose the field, its shape, and which profile fact `started` names. ADR-0047
  said the model "transcribed both and drafted nothing in it". For `shown`
  that was true; for `part_year_only` it was not.

The human, 2026-10-21:

> incomplete.yaml: split it out as a model-authored entry pending my
> signature. You drafted the shape, so my name shouldn't be on it — show me
> the field and I'll sign it properly. That's the right call and I want it to
> stay the default whenever a file records me as author for something you
> shaped.

## Decision

**A reviewed artefact that names a person as `author` holds only what that
person wrote.** Anything the model shaped goes in its own artefact:
- `author: "model:<model id>"`, `reviewed_by: "PENDING — <person>"` and a
  `date`;
- used only once that person signs it (D-01);
- presented to them as a table of field, value and what it does.

This applies even when the model is encoding the person's own decision.
Deciding a rule is not writing the field that encodes it. This is the default
from now on, not a one-off.

**For the incomplete-clause readings, two files rather than per-entry
signatures:**

- `incomplete.yaml` keeps `shown`, author and reviewer Lucas Frasca,
  2026-10-20. Its entries no longer accept any other field (`extra="forbid"`),
  so nothing the model shapes can ride in a file under his name.
- `part_year.yaml`, beside it, carries `part_year_only`. Its author is
  `model:claude-opus-5-5`, it is `PENDING — Lucas Frasca` until he signs, and
  it has the same front matter and the same checks.

Why separate files, not a signature on each entry or field:
- Every reviewed artefact in this repo carries one review block per file:
  mapping YAML, selection manifests, the golden set. `validate_review` and
  "a pending file is refused" are file-level rules, and both files reuse
  them unchanged.
- A per-entry signature would not have been enough here anyway. One entry
  held both a human-written field and a model-shaped one, so it would have
  needed a signature per *field*. That would be a second review mechanism
  for one file.

**Pending is refused, not skipped**, as for every reviewed artefact. While
`part_year.yaml` is pending, `load_incomplete_readings` raises
`UnsignedIncompleteReading`. The API, the demo and Track A refuse to build,
and CI is red. The PR stays a draft until it is signed. `main` keeps the
previous single file meanwhile, unchanged.

## Consequences

- **Signing changes no outcome.** Signed, the two files read exactly as the
  single file did: the same words are shown and the same years are part
  years. The full suite passes with the file signed locally.
- **Unsigned, the field would not merely go missing; the whole load fails.**
  Had a pending file been skipped instead of refused, every turnover leaf
  resting on s6DA(2) would be unassessable whatever year it reads.
  - GA-04 (started 2 March 2015, every figure under $3,000,000) would lose its
    decided previous year. Its obligations would not move: its start year is
    a part year either way.
  - GA-01 and GA-05 would go from 35 APPs applicable to 35 unassessable, with
    10 questions.

  That is why a pending file is refused rather than skipped: a silent change
  to an outcome is worse than a red build.
- **`CLAUDE.md`'s corpus rules should carry the default.** Only the human
  edits `CLAUDE.md`. Suggested wording for him: "A file that records a person
  as author holds only what they wrote; anything the model shaped, even
  encoding their decision, goes in a model-authored file pending their
  signature (ADR-0052)."
- **The corpus version moves** (ADR-0034): a new file under
  `corpus/instruments` and a changed reader under `src/compliance_rag/corpus`.
  Nothing is written to any store.
