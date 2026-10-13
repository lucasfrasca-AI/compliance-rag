// Neo4j constraints and indexes only. No data (PHASES.md Phase 0).
//
// Bitemporal properties on every node (D-05):
//
//   valid_from  : date       when the node's content became true in the world
//   valid_to    : date       when it stopped being true; never null — an
//                            open-ended node carries the NO_END_DATE sentinel
//                            (9999-12-31, CONTEXT.md: Bitemporal), so "in force
//                            on as_of" is always valid_from <= as_of < valid_to
//   end_reason  : string     'superseded' | 'repealed' | 'expired' — only on an
//                            ended node, set with valid_to
//   recorded_at : datetime   when we learned it (set by the writer)
//
// No status is stored (ADR-0029): not yet commenced, in force, or ended (for
// its end_reason) is derived from the dates on each as_of.
//
// Nodes are immutable. An amendment creates a new version node, a SUPERSEDES
// edge to the old, and sets valid_to and end_reason on the old — once, from
// NO_END_DATE to a date. Nothing else is deleted or mutated
// (adapters/neo4j_graph.py enforces it).
//
// Applied by Neo4jGraph.apply_schema, one statement at a time, idempotently.
//
// Every traversal takes as_of and a path-depth cap, enforced by the query
// builder (D-05). An unscoped query is a test failure.
//
// stable_id is unique per label. For Clause it is the CONTEXT.md stable ID:
// {instrument_id}:{version_id}:{clause_path}.

// --- uniqueness -------------------------------------------------------------

CREATE CONSTRAINT instrument_stable_id IF NOT EXISTS
FOR (n:Instrument) REQUIRE n.stable_id IS UNIQUE;

CREATE CONSTRAINT instrument_version_stable_id IF NOT EXISTS
FOR (n:InstrumentVersion) REQUIRE n.stable_id IS UNIQUE;

CREATE CONSTRAINT clause_stable_id IF NOT EXISTS
FOR (n:Clause) REQUIRE n.stable_id IS UNIQUE;

CREATE CONSTRAINT obligation_stable_id IF NOT EXISTS
FOR (n:Obligation) REQUIRE n.stable_id IS UNIQUE;

CREATE CONSTRAINT condition_stable_id IF NOT EXISTS
FOR (n:Condition) REQUIRE n.stable_id IS UNIQUE;

CREATE CONSTRAINT actor_stable_id IF NOT EXISTS
FOR (n:Actor) REQUIRE n.stable_id IS UNIQUE;

CREATE CONSTRAINT control_stable_id IF NOT EXISTS
FOR (n:Control) REQUIRE n.stable_id IS UNIQUE;

CREATE CONSTRAINT jurisdiction_stable_id IF NOT EXISTS
FOR (n:Jurisdiction) REQUIRE n.stable_id IS UNIQUE;

CREATE CONSTRAINT sector_stable_id IF NOT EXISTS
FOR (n:Sector) REQUIRE n.stable_id IS UNIQUE;

// Load is the record that a loader ran (#153, ADR-0034): metadata, not law, so
// it has a load_id rather than a stable_id and no valid time. It is in no
// vocabulary the query builder reads.

CREATE CONSTRAINT load_id IF NOT EXISTS
FOR (n:Load) REQUIRE n.load_id IS UNIQUE;

// --- existence of the bitemporal properties ---------------------------------
// Property-existence constraints are an Enterprise/Aura feature. Community
// (the CI service container) refuses them; apply_schema reports each refused
// statement by name rather than failing or skipping silently, and the writer
// (Neo4jGraph.put_node) enforces the same invariant on every edition: no node
// is written without valid_from, valid_to and recorded_at.

CREATE CONSTRAINT obligation_valid_from IF NOT EXISTS
FOR (n:Obligation) REQUIRE n.valid_from IS NOT NULL;

CREATE CONSTRAINT obligation_valid_to IF NOT EXISTS
FOR (n:Obligation) REQUIRE n.valid_to IS NOT NULL;

CREATE CONSTRAINT instrument_version_valid_from IF NOT EXISTS
FOR (n:InstrumentVersion) REQUIRE n.valid_from IS NOT NULL;

CREATE CONSTRAINT instrument_version_valid_to IF NOT EXISTS
FOR (n:InstrumentVersion) REQUIRE n.valid_to IS NOT NULL;

// --- retired by ADR-0029 (status is derived, never stored) -----------------
// Dropped before Neo4jGraph.drop_stored_status removes the property, which
// these constraints would refuse on Aura. IF EXISTS: a no-op once dropped.

DROP CONSTRAINT obligation_status IF EXISTS;

DROP CONSTRAINT instrument_version_status IF EXISTS;

DROP INDEX obligation_status_idx IF EXISTS;

// --- lookup indexes ---------------------------------------------------------
// as_of filtering reads valid_from / valid_to on every traversal, so they are
// indexed on the labels the traversal starts from and passes through.

CREATE INDEX obligation_temporal IF NOT EXISTS
FOR (n:Obligation) ON (n.valid_from, n.valid_to);

CREATE INDEX instrument_version_temporal IF NOT EXISTS
FOR (n:InstrumentVersion) ON (n.valid_from, n.valid_to);

CREATE INDEX clause_instrument IF NOT EXISTS
FOR (n:Clause) ON (n.instrument_id, n.version_id);

CREATE INDEX obligation_instrument IF NOT EXISTS
FOR (n:Obligation) ON (n.instrument_id);

CREATE INDEX condition_temporal IF NOT EXISTS
FOR (n:Condition) ON (n.valid_from, n.valid_to);

CREATE INDEX actor_temporal IF NOT EXISTS
FOR (n:Actor) ON (n.valid_from, n.valid_to);

CREATE INDEX jurisdiction_code IF NOT EXISTS
FOR (n:Jurisdiction) ON (n.code);

CREATE INDEX sector_code IF NOT EXISTS
FOR (n:Sector) ON (n.code);

// --- retired by ADR-0027 --------------------------------------------------
// Conditions have an `operator`, not a `kind`, and an Actor is identified by
// its stable ID ({instrument_id}/actor/{slug}), not a `role` property.

DROP INDEX condition_kind IF EXISTS;

DROP INDEX actor_role IF EXISTS;

// --- edges ------------------------------------------------------------------
// PART_OF       (Clause)->(InstrumentVersion)
// VERSION_OF    (InstrumentVersion)->(Instrument)
// SUPERSEDES    (X)->(X), X one of InstrumentVersion, Obligation, Condition,
//               Actor, Control, Jurisdiction, Sector
// DERIVED_FROM  (Obligation|Condition|Actor|Control)->(Clause) — one per
//               compilation the item is grounded in (ADR-0028)
// QUALIFIED_BY  (Actor)->(Condition) — the root of the actor's condition tree
// HAS_OPERAND   (Condition)->(Condition) — the operator (all_of, any_of, not,
//               leaf) is the parent node's `operator` property (ADR-0027)
// APPLIES_TO    (Obligation)->(Actor|Jurisdiction|Sector)
// REQUIRES      (Obligation)->(Condition|Control)
// CROSSWALKS_TO (Obligation|Control)->(Obligation|Control)
//
// Tenant subgraph nodes (Organisation, HANDLES, OPERATES_IN, ACTS_AS,
// REGULATED_BY) are built per request from the ledger and are not persisted
// during the build (CONTEXT.md), so they get no constraints here.
