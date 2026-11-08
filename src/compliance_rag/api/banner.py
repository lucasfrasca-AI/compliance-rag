"""The banner a page carries while the demo generator is on (#292).

Rendered by the server into every page it serves (sign-in, chat list, chat,
the error page, and each answer turn of the turns fragment), never by
`ask.js`: it doesn't depend on a script running, and a withheld or failed turn
carries it as a verified one does. The JSON API says the same with
`"generator": "demo"` on each answer event (`answers.py`).
"""

from __future__ import annotations

from compliance_rag.adapters.demo_generator import FAKE_BANNER
from compliance_rag.settings import Settings


def banner_text(settings: Settings) -> str | None:
    """The words to show on every page, or `None` when a model is answering."""
    return FAKE_BANNER if settings.generator == "demo" else None
