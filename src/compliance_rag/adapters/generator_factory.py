"""The one place `GENERATION_BACKEND` is turned into a `Generator` (ADR-0057).

`gemini` (the default) is Gemini 3.5 Flash on Vertex; `claude` is Claude on
Vertex, dormant until it is approved on this project. Both are offshore at
`us`, and both are built through the same residency gate (ADR-0023): it asks
the store whether it holds real tenant data. Each adapter is imported only
when it is chosen, so a process on one backend never loads the other's SDK.

The API (`api/main.py`), the live eval (`eval/live.py`) and the operator
scripts all build through here, so none of them can be pointed at the wrong
backend by a different `if`. `demo` is not built here: it is the in-process
stand-in the API builds for itself (`api/main.py`), and asking for it here is
a mistake.
"""

from __future__ import annotations

from compliance_rag.adapters.interfaces import Generator, RetrievalAdmin
from compliance_rag.settings import Settings


async def build_generator(settings: Settings, *, corpus: RetrievalAdmin) -> Generator:
    generation = settings.generation
    if generation.backend == "gemini":
        from compliance_rag.adapters.gemini_generator import GeminiGenerator

        return await GeminiGenerator.create(settings, corpus=corpus)
    if generation.backend == "claude":
        from compliance_rag.adapters.vertex_claude_generator import VertexClaudeGenerator

        return await VertexClaudeGenerator.create(settings, corpus=corpus)
    raise ValueError(
        f"GENERATION_BACKEND={generation.backend} builds no model: only `gemini` and `claude` do"
    )


def generation_label(settings: Settings) -> str:
    """What generates, in a line for an operator: the model and where it runs."""
    generation = settings.generation
    return f"{generation.model or '(no model configured)'} at {generation.region} (Vertex)"
