# ADR-0025 — The library owner is one seeded `tenant` row

- Status: accepted
- Date: 2026-10-10
- Issue: #67
- Extends: ADR-0020 (library and subscriptions), D-09 (the ledger is the non-derivable store)

## Context

Library law is owned by `_public` (ADR-0020). Every corpus table in the ledger
(`instrument`, `instrument_version`, `clause`, `clause_version`) chains back
to `tenant` through foreign keys, and migration 0002 forbade `_public` there,
so that no company could register under the reserved ID. Together these made
library law impossible to record. The verifier now treats a version the ledger
has never recorded as not in force (ADR-0021 mechanism 3, issue #30), so the
gap became concrete: no Privacy Act citation could ever verify against a
Postgres ledger.

There were two options:
- **Seed one `_public` row in `tenant`**, flagged as the library.
- **Drop the corpus tables' foreign keys to `tenant`**, since public rows have
  no tenant to reference.

## Decision

**Seed one `_public` row, flagged `is_library`, in migration 0006.** The
existing guarantees survive, and are now structural rather than resting on
one CHECK:

- **The flag is tied to the ID in both directions.** A CHECK makes
  `tenant_id = '_public'` equivalent to `is_library`, so no other row can be
  the library and `_public` can't be anything else.
- **No company can register as `_public`.** The seeded row holds the primary
  key, so any later insert collides. A mutant without the CHECK still fails
  for that reason.
- **The library never subscribes**, because it sees its own instruments by
  ownership. That is now a CHECK on `tenant_instrument`. The foreign key to
  `tenant` no longer excludes it on its own.
- **A subscription must name an instrument the library holds.**
  `tenant_instrument` gains a foreign key to `instrument ('_public',
  instrument_id)`, through a generated column fixed at `'_public'`. The
  open item from ADR-0020 is closed.

Dropping the foreign keys was rejected. It would leave public rows
referencing nothing, and the ledger is the one store that can't be rebuilt
(D-09), so it should be the most constrained, not the least.

## Consequences

- Library-law instruments and versions can be recorded, so the Postgres
  ledger adapter (#69) can give the verifier a real record to check a
  Privacy Act citation against.
- `_public` can now own rows in every table that references `tenant`,
  including `usage`. Spend on public-law questions can be recorded against the
  library.
- **Existing data.** A database that already holds subscriptions to
  instruments it never recorded would fail the new foreign key when 0006 is
  applied. No such database exists; every ledger to date is a fresh CI or
  local one.
