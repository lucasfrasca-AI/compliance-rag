# Synthetic submitted documents

Invented documents of the kind a member submits to be checked (ADR-0053: a
submitted document), for the tests of the submitted-document segmenter and of
the lifecycle's submit path (#296). Every one is registered in
`corpus/synthetic/registry.json` by `scripts/register_synthetic_fixtures.py`, as
tenant `acme`, so the residency gate treats it as synthetic.

- `acme-customer-insights-proposal.md`: Markdown. The five claims of the worked
  example in `docs/research/phase3-product-options.md`, with headings, a code
  fence that holds a blank line, and a quoted line that tries to instruct the
  checker (it is data; the segmenter does nothing with it).
- `acme-vendor-onboarding-request.txt`: plain text, with an indented paragraph.

author: model:claude-opus-5-5 (drafted for #296)
reviewed_by: PENDING (the human)
date: 2026-11-15
