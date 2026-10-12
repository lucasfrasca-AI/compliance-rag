"""Apply `db/neo4j/schema.cypher` to the deployed graph, then the data
migrations it implies (Aura Free: public law only, D-09).

Idempotent: every schema statement is `IF NOT EXISTS` / `IF EXISTS`, and each
migration changes only nodes that still need it. Run after any change to the
schema file:

    uv run python -m scripts.apply_graph_schema
"""

from __future__ import annotations

import asyncio

from compliance_rag.adapters.neo4j_graph import Neo4jGraph
from compliance_rag.settings import Settings


async def main() -> None:
    async with Neo4jGraph.from_settings(Settings()) as graph:
        report = await graph.apply_schema()
        print(f"schema: {len(report.applied)} applied, unsupported {list(report.unsupported)}")
        # ADR-0029: status is derived. Constraints dropped above, then the property.
        print(f"status property removed from {await graph.drop_stored_status()} node(s)")
        counts = await graph.counts()
        print(f"graph: {counts.nodes} nodes, {counts.edges} edges")


if __name__ == "__main__":
    asyncio.run(main())
