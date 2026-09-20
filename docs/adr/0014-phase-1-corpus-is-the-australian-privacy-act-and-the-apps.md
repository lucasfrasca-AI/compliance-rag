# ADR-0014 — Phase 1 corpus is the Australian Privacy Act and the APPs

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-13` (`DECISIONS.md`)

## Context

The differentiator is answering policy-to-law in a single response. That needs one instrument done properly, with real policies written against it, rather than several done shallowly.

## Decision

Phase 1 ingests the Australian Privacy Act and the APPs, with a synthetic policy set of five to eight documents written against them. ISO 27001 Annex A contributes control IDs and titles only, no text. The EU AI Act is Phase 4.

## Consequences

- The first customers' policies are written against the APPs, so the demo is the product.
- Crosswalk machinery is exercised by ISO control IDs before a second full instrument arrives.
- ISO text is licensed and cannot be stored, which the corpus model must accommodate from the start.
- EU AI Act work waits, including its commencement-date fixtures.
