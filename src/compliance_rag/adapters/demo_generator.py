"""The demo generator: a `Generator` that calls no model (#292).

It reads the clauses the prompt fences and quotes the top-ranked retrieved
clause and the first clause of the tenant's policies verbatim, so the verifier,
the citations and click-to-clause can all be exercised. A question containing
"withhold" makes it misquote, and it never repairs, so the answer is withheld
(ADR-0003).

It runs in this process. Nothing it handles goes anywhere, so the residency
gates are told generation is in Sydney (`GENERATOR=demo` requires
`VERTEX_CLAUDE_REGION=australia-southeast1`, `Settings`). Because it is not a
model, every page the API serves says so while it is on (`api/banner.py`), and
every answer event reports `"generator": "demo"`.

The throwaway local demo (#247, under `scripts/`) and the deployed API
(`GENERATOR=demo`, `api/main.py`) build the same class. Nothing here imports
the script: it imports this.
"""

from __future__ import annotations

import json
import re

from compliance_rag.adapters.interfaces import Generation, Generator
from compliance_rag.assessment.prompt import REPAIR_SYSTEM_PROMPT

FAKE_BANNER = "FAKE GENERATOR — not a real answer"
"""The words every page carries while the demo generator is on."""

# The fence and question format `assessment/prompt.py` writes (`fence`,
# `user_prompt`). Should it change, the fake cites nothing and
# tests/test_demo_generator.py's end-to-end test fails.
_BLOCK_RE = re.compile(
    r"\[(?P<boundary>[0-9a-f]{24}) clause (?P<clause_id>\S+)[^\n]*\]\n"
    r"(?P<text>.*?)\n\[(?P=boundary) end\]",
    re.DOTALL,
)
_QUOTE_CHARS = 120
_MISQUOTE = "words the fake generator made up, which no clause contains"


def _fenced(prompt: str | None) -> list[tuple[str, str]]:
    """Each fenced clause in a prompt, in order: its id and its text."""
    return [(m["clause_id"], m["text"]) for m in _BLOCK_RE.finditer(prompt or "")]


def _opening(text: str) -> str:
    """A verbatim quote: the clause's opening, cut at a word."""
    if len(text) <= _QUOTE_CHARS:
        return text
    return text[:_QUOTE_CHARS].rsplit(" ", 1)[0]


class FakeDemoGenerator(Generator):
    """No model is called (see the module docstring)."""

    async def generate(
        self, *, system: str, cached_prefix: str | None, user: str, max_tokens: int
    ) -> Generation:
        if system == REPAIR_SYSTEM_PROMPT:
            clause_id = next(iter(_fenced(user)), ("", ""))[0]
            reply: dict[str, object] = {"clause_id": clause_id, "quote": _MISQUOTE}
        else:
            question = user.rsplit("\nQuestion: ", 1)[-1]
            withhold = "withhold" in question.lower()
            quoted = [*_fenced(user)[:1], *_fenced(cached_prefix)[:1]]
            reply = {
                "answer": f"{FAKE_BANNER}. No model was called. This stand-in quotes "
                + (
                    "words no clause contains, because the question says 'withhold', "
                    "so the verifier blocks the answer."
                    if withhold
                    else "the top-ranked retrieved clause and the first clause of Acme's "
                    "policies verbatim, so the citations, the verifier and "
                    "click-to-clause can be exercised."
                ),
                "citations": [
                    {"clause_id": clause_id, "quote": _MISQUOTE if withhold else _opening(text)}
                    for clause_id, text in quoted
                ],
            }
        return Generation(
            text=json.dumps(reply),
            input_tokens=0,
            output_tokens=0,
            stop_reason="end_turn",
            model="fake-demo-generator",
        )
