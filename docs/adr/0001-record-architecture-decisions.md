# ADR-0001 — Record architecture decisions

- Status: accepted
- Date: 2026-09-20

## Context

This project locks a small number of architecture decisions early and treats
reopening one as an event, not a preference. `DECISIONS.md` is the register: a
one-line statement of each decision and the reason it is locked. That table is
deliberately terse, so it cannot carry the reasoning, the alternatives weighed,
or the consequences accepted.

## Decision

We record architecture decisions as ADRs in `docs/adr/`, in the style Michael
Nygard describes. One file per decision, numbered sequentially, never deleted.

`DECISIONS.md` stays the index and the authority on whether a decision is
locked; the ADR carries the argument. `ADR-0002` to `ADR-0017` correspond to
`D-01` to `D-16`.

Changing anything in `DECISIONS.md` requires a new ADR that supersedes the old
one. The superseded ADR is marked `superseded by ADR-NNNN` and left in place.

## Consequences

- The reasoning behind a locked decision survives the people who made it.
- An agent or a new contributor can read one file to understand why an
  apparently reasonable alternative is off the table.
- Reopening a decision costs a written argument, which is the intended friction.
- The register and the ADRs can drift. `DECISIONS.md` wins; a mismatch is a bug.
