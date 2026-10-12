"""The keep-alive workflows (issue #119; the Aura one is checked in
tests/test_neo4j_graph.py): each holds its own store's credentials and no
other workflow does, runs no repository code, and leaves nothing behind.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
QDRANT = WORKFLOWS / "qdrant-keepalive.yml"


def test_only_the_qdrant_keepalive_holds_qdrant_cloud_credentials() -> None:
    """CI's Qdrant is a service container: no other workflow names a Qdrant
    secret or the Qdrant Cloud host (the same guard as Aura's)."""
    cloud = re.compile(r"secrets\.\w*QDRANT|cloud\.qdrant\.io", re.IGNORECASE)
    for workflow in WORKFLOWS.glob("*.yml"):
        if workflow != QDRANT:
            assert not cloud.search(workflow.read_text()), workflow.name
    assert "secrets.QDRANT_KEEPALIVE_API_KEY" in QDRANT.read_text()


def test_the_qdrant_keepalive_runs_no_code_and_deletes_what_it_writes() -> None:
    text = QDRANT.read_text()
    assert "actions/checkout" not in text and "uv " not in text
    assert "permissions: {}" in text
    point = re.search(r"POINT: (\S+)", text)
    assert point is not None
    # One fixed point: upserted without vectors, deleted, then checked gone.
    assert re.search(r"vector[^a-z]*:[^a-z]*\{\}", text)
    assert (
        text.index("-X PUT") < text.index("/delete?wait=true") < text.index('test "$status" = 404')
    )
    # The key never reaches argv: it goes to curl on stdin.
    assert "-K -" in text and "api-key: %s" in text
