// Neo4j constraints and indexes only. No data (PHASES.md Phase 0).
//
// Bitemporal properties on every node (D-05):
//
//   valid_from  : date       when the node's content became true in the world
//   valid_to    : date       when it stopped being true; never null — an
//                            open-ended node carries the NO_END_DATE sentinel
//                            (9999-12-31, CONTEXT.md: Bitemporal), so "in force
//                            on as_of" is always valid_from <= as_of < valid_to
//   status      : string     'in_force' | 'not_yet_commenced' | 'repealed'
//   recorded_at : datetime   when we learned it (set by the writer)
//
// Nodes are immutable. An amendment creates a new version node, a SUPERSEDES
// edge to the old, and sets valid_to on the old — once, from NO_END_DATE to a
// date. Nothing else is deleted or mutated (adapters/neo4j_graph.py enforces it).
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

// --- existence of the bitemporal properties ---------------------------------
// Property-existence constraints are an Enterprise/Aura feature. Community
// (the CI service container) refuses them; apply_schema reports each refused
// statement by name rather than failing or skipping silently, and the writer
// (Neo4jGraph.put_node) enforces the same invariant on every edition: no node
// is written without valid_from, valid_to, status and recorded_at.

CREATE CONSTRAINT obligation_valid_from IF NOT EXISTS
FOR (n:Obligation) REQUIRE n.valid_from IS NOT NULL;

CREATE CONSTRAINT obligation_status IF NOT EXISTS
FOR (n:Obligation) REQUIRE n.status IS NOT NULL;

CREATE CONSTRAINT instrument_version_valid_from IF NOT EXISTS
FOR (n:InstrumentVersion) REQUIRE n.valid_from IS NOT NULL;

CREATE CONSTRAINT instrument_version_status IF NOT EXISTS
FOR (n:InstrumentVersion) REQUIRE n.status IS NOT NULL;

// --- lookup indexes ---------------------------------------------------------
// as_of filtering reads valid_from / valid_to / status on every traversal, so
// they are indexed on the labels the traversal starts from and passes through.

CREATE INDEX obligation_temporal IF NOT EXISTS
FOR (n:Obligation) ON (n.valid_from, n.valid_to);

CREATE INDEX obligation_status_idx IF NOT EXISTS
FOR (n:Obligation) ON (n.status);

CREATE INDEX instrument_version_temporal IF NOT EXISTS
FOR (n:InstrumentVersion) ON (n.valid_from, n.valid_to);

CREATE INDEX clause_instrument IF NOT EXISTS
FOR (n:Clause) ON (n.instrument_id, n.version_id);

CREATE INDEX obligation_instrument IF NOT EXISTS
FOR (n:Obligation) ON (n.instrument_id);

CREATE INDEX condition_kind IF NOT EXISTS
FOR (n:Condition) ON (n.kind);

CREATE INDEX jurisdiction_code IF NOT EXISTS
FOR (n:Jurisdiction) ON (n.code);

CREATE INDEX sector_code IF NOT EXISTS
FOR (n:Sector) ON (n.code);

CREATE INDEX actor_role IF NOT EXISTS
FOR (n:Actor) ON (n.role);

// --- edges ------------------------------------------------------------------
// PART_OF      (Clause)->(InstrumentVersion)
// VERSION_OF   (InstrumentVersion)->(Instrument)
// SUPERSEDES   (InstrumentVersion)->(InstrumentVersion), (Obligation)->(Obligation)
// APPLIES_TO   (Obligation)->(Actor|Jurisdiction|Sector)
// REQUIRES     (Obligation)->(Condition|Control)
// CROSSWALKS_TO (Obligation|Control)->(Obligation|Control)
//
// Tenant subgraph nodes (Organisation, HANDLES, OPERATES_IN, ACTS_AS,
// REGULATED_BY) are built per request from the ledger and are not persisted
// during the build (CONTEXT.md), so they get no constraints here.
