# ADR-0050 — A mapping edge that was never true is removed as an acknowledged erratum

- Status: accepted. The human decided on 2026-10-20 (quoted below); the
  mechanism's shape is the model's.
- Date: 2026-10-20
- Issue: #245
- Amends: `D-05` and ADR-0031 (a change of meaning is a new id), and through
  them ADR-0037 and ADR-0044 (an obligation's exceptions and qualifiers grow
  and never shrink)

## Context

The qualifiers file signed on 2026-10-15 (ADR-0044's sign-off) reads
`OB-PA-APP12-5` with APP 12.6: "Without limiting subclause 12.5, access may be
given through the use of a mutually agreed intermediary." It is loaded into
Aura as a `READ_WITH` edge. On 2026-10-20 the human trimmed it:

> APP 12.6: trim it. If it says "may", it's a permission like 4.2 and 12.10,
> and I'd rather the qualifier category stay precise than keep one borderline
> member. My signature was on the batch, not on that judgement.

The loader refused the trimmed file. `DERIVED_FROM`, `EXCEPTED_BY` and
`READ_WITH` only grow (D-05), and a change of meaning is a new id with
`supersedes`. Supersession works in valid time with no gap: the old obligation
ends today, and every `as_of` before today would still read 12.5 with 12.6.
Asked to choose, the human decided:

> APP 12.6: option (b), remove it as an acknowledged erratum under a short
> ADR. It was my error to sign a batch containing a permission, and your
> draft flagged it. Superseding from today would leave a qualifier
> relationship in force for every earlier date that was never correct in the
> first place — an erratum is what this is, so record it as one. Write the
> ADR.

## Decision

**An erratum, not a supersession.** Supersession records that the law, or our
reading of it, changed on a date. Here nothing changed: 12.6 never qualified
12.5 at any valid time. So the edge is removed outright, for every `as_of`.

**What an erratum may touch:** only a mapping item's grow-only edges to a
clause (`ERRATUM_EDGES`: `DERIVED_FROM`, `EXCEPTED_BY`, `READ_WITH`), from an
Obligation, Condition, Actor or Control. Never a node, Clause nodes included.
Never an edge in `MEANING` (`APPLIES_TO`, `REQUIRES`, `QUALIFIED_BY`,
`HAS_OPERAND`, `SUPERSEDES`). Never a property. Those are still a new id.

**How, modelled on ADR-0031's acknowledged correction:**

- **Named exactly.** `check_mappings` finds every stored grow-only edge the
  mappings no longer give. The load goes ahead only if an
  `ErratumAcknowledgement` names exactly those edges (source, edge type,
  target), plus who and why. A missing or an extra edge refuses the load
  before any write. An edge an erratum already removed may be named again, so
  the same command re-run is a no-op.
- **One transaction.** `GraphAdmin.remove_by_erratum` deletes all of a load's
  edges, or none.
- **Audited on the node.** The source node's `corrections` trail gains one
  entry: when, by whom, why, `"erratum": true` and the edges removed. The
  graph itself still shows what an earlier answer was given.
- **Never given again.** A load that would give an edge back after an erratum
  removed it is refused. Giving it back would undo the erratum without anyone
  deciding to.
- **Dry run first.** `scripts/load_mappings.py --erratum SOURCE EDGE_TYPE
  TARGET --by NAME --reason WHY` runs the checks against the graph, prints
  exactly what it would remove, and writes nothing. `--apply` removes and
  loads.

## Consequences

- **Reconstructable.** An assessment records its mapping version as a tag
  and a commit (ADR-0034). `mappings-2026.10.15` (d1b3d55) holds the file as
  signed, 12.6 included, and the trail on `OB-PA-APP12-5` names the removal,
  when, and by whom. So an answer given before the erratum can still be
  explained, though the graph no longer reproduces it at an earlier `as_of`.
  That is the point: it was wrong at that date too.
- The qualifiers file keeps `reviewed_by: "Lucas Frasca"`, dated 2026-10-20,
  since the human made the change. Its header records the erratum.
- As signed and trimmed: **4 qualifier pairs** (5.2→5.1, 7.6→7.7, 11.3→11.1,
  11.3→11.2). 12.6 has no paragraphs, so the erratum removes one edge.
- The mapping version moves (ADR-0034): the loader changed, and so did the
  YAML.
- An erratum is for an edge that was never true. A reading that was right
  until the law changed is still a supersession.
