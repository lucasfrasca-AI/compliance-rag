"""The one place `GENERATION_BACKEND` is turned into a `Generator` (ADR-0056).

`bedrock` (the default) is Claude Sonnet 5 on Bedrock's Australian inference
profile, called from Sydney: nothing it does leaves Australia, so it asks the
store nothing. `vertex` is the build-phase path, built through its residency
gate exactly as before (ADR-0023). Each adapter is imported only when it is
chosen, so a process on one backend never loads the other's SDK path.

The API (`api/main.py`), the live eval (`eval/live.py`) and the operator
scripts all build through here, so none of them can be pointed at the wrong
backend by a different `if`.
"""

from __future__ import annotations

from compliance_rag.adapters.interfaces import Generator, RetrievalAdmin
from compliance_rag.settings import Settings


async def build_generator(settings: Settings, *, corpus: RetrievalAdmin) -> Generator:
    if settings.generation_backend == "vertex":
        from compliance_rag.adapters.vertex_claude_generator import VertexClaudeGenerator

        return await VertexClaudeGenerator.create(settings, corpus=corpus)
    from compliance_rag.adapters.bedrock_claude_generator import BedrockClaudeGenerator

    return BedrockClaudeGenerator.create(settings)


def generation_label(settings: Settings) -> str:
    """What generates, in a line for an operator: the model and where it runs."""
    if settings.generation_backend == "vertex":
        return (
            f"{settings.vertex_claude_model or '(no model configured)'} "
            f"at {settings.vertex_claude_region} (Vertex)"
        )
    return (
        f"{settings.bedrock_inference_profile} from {settings.bedrock_region} "
        "(Bedrock, processed in Australia)"
    )
