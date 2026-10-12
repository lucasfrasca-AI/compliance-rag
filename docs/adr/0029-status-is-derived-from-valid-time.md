# ADR-0029 — Status is derived from valid time, never stored

- Status: accepted
- Date: 2026-10-12
- Issue: #112 (from the #93 design session, Q7)
- Amends: ADR-0006 / `D-05` ("three statuses")

## Context

D-05 gave every node a stored `status` (`in_force`, `not_yet_commenced`,
`repealed`) beside its valid time. The two say the same thing twice, and
they can disagree:
- a "repealed" node with an open `valid_to`;
- an "in_force" node whose `valid_to` has passed.

The second already happened. The ledger's `instrument_version.status` was
written as `in_force` on every insert and never changed. Every version that
version-and-retire ended (ADR-0021) still said `in_force`.

A stored status also can't load a passed-but-not-commenced provision ahead
of time without a second write on commencement day. The Privacy and Other
Legislation Amendment Act 2024 has such items: C2026C00227's Endnote 3 lists
Schedule 1 items 87–89 as commencing 10 Dec 2026.

## Decision

**Status is derived on each `as_of`, never stored.** `status_on` in
`corpus/models.py` derives it as follows:
- before `valid_from`: `not_yet_commenced`;
- from `valid_from` to `valid_to`: `in_force`;
- from `valid_to`: the node's `end_reason`.

What is stored instead is **why a record ended**:
- `end_reason` is one of `superseded`, `repealed` or `expired`;
- it is set together with `valid_to` and only with it, once, by the same
  locked retirement that already set `valid_to`;
- an open record has neither.

The model, the graph's `Bitemporal` and the ledger's
`instrument_version_end_together` check refuse one without the other.
A supersession (`GraphAdmin.amend`) records `superseded`.

The migration:
- **Neo4j:**
  - `schema.cypher` drops the two `status` existence constraints and the status
    index (`DROP … IF EXISTS`), and requires `valid_to` instead.
  - `Neo4jGraph.migrate_to_derived_status`, run after `apply_schema`, moves
    nodes written before this ADR: an ended node gains the `end_reason` its
    old status implies (`repealed` if it said so, otherwise `superseded`),
    then `status` is removed. A node stored as `repealed` but still open is
    refused, not guessed at.
  - The writer refuses `status` as a property, so it can't come back.
- **Ledger:**
  - `db/ledger/0008` drops `instrument_version.status` and adds `end_reason`.
  - It backfills `superseded` for already-retired rows, since version-and-retire
    is the only path that ended one.
- **Mapping YAML:** an obligation carries no `status`. An ended one carries
  `end_reason`, and a superseded one must say `superseded`.

D-05's last sentence now reads: *"Status (not yet commenced / in force /
superseded / repealed / expired) is derived from valid time and the stored
end reason, never stored."*

## Consequences

- One source of truth: no record can be "repealed" and in force at once.
- A provision loaded ahead of its commencement just works. `as_of` before
  `valid_from` sees it as not yet commenced, with nothing to update on the day.
- D-05's three statuses become five derived ones. `repealed` stays distinct
  from `superseded`, so "the law was replaced" and "the law was removed"
  remain different answers.
- The query builder is unchanged. It already filtered on
  `valid_from <= as_of < valid_to`, never on status.
