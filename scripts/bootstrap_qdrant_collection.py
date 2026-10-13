"""Create the deployed Qdrant cluster's corpus collection if it is missing
(issue #139): the vector config and payload indexes a service would create on
start, so the keep-alive (`.github/workflows/qdrant-keepalive.yml`) has a
collection to write to before anything is deployed. Idempotent: an existing
collection is never recreated or emptied, only given any payload index it
lacks.

Reads QDRANT_URL / QDRANT_API_KEY / QDRANT_COLLECTION through `Settings`, and
prints what it found or made, never the URL or the key.

    uv run python -m scripts.bootstrap_qdrant_collection
"""

from __future__ import annotations

import asyncio

from compliance_rag.adapters.qdrant_retrieval_store import bootstrap_collection
from compliance_rag.settings import Settings


def main() -> None:
    status = asyncio.run(bootstrap_collection(Settings()))
    print(
        f"{status.name}: {'created' if status.created else 'already there'}; "
        f"{status.points} points; payload indexes: {', '.join(status.payload_indexes)}"
    )


if __name__ == "__main__":
    main()
