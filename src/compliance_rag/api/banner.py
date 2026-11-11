"""The banner a page carries while the demo generator is on (#292).

One partial, `templates/_banner.html`, and one stylesheet, `static/banner.css`,
for every place it appears: the Ask pages (`base.html`), each answer turn the
demo wrote (`_turns.html`), and the sign-in page, which is built in
`sign_in.py` and renders the same partial through `render_banner`. It is
written by the server, never by `ask.js`: it doesn't depend on a script
running, and a withheld or failed turn carries it as a verified one does.

Two rules decide where it shows:
- a **page** carries it while the deployment runs the demo generator
  (`banner_text`): whoever opens the app sees what it is running now;
- an **answer turn** carries it when the turn was written by the demo
  (`ChatTurn.generator`, ledger 0020), whatever the deployment runs now: a chat
  read back after a switch to a model still labels the stand-in's answers, and
  a model's answers are never labelled.

The JSON API says the same with `"generator"` on each answer event
(`answers.py`) and each answer turn of a chat (`chats.py`). The source pages
(`/v1/sources/...`) are exempt: they show stored law and policy text, never
generated text (`api/README.md`).
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from compliance_rag.adapters.demo_generator import FAKE_BANNER
from compliance_rag.settings import Settings

TEMPLATE_DIR = Path(__file__).parent / "templates"

_ENV = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True, undefined=StrictUndefined
)


def banner_text(settings: Settings) -> str | None:
    """The words to show on every page, or `None` when a model is answering."""
    return FAKE_BANNER if settings.generation_backend == "demo" else None


def render_banner(words: str | None) -> str:
    """The partial, for a page not made from a template."""
    return _ENV.get_template("_banner.html").render(banner=words)
