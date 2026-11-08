"""The CAG prefix's size cap (ADR-0054, amending D-11 / ADR-0012).

The prefix is the system prompt, then, when a document check passes one, the
obligation digest (#295), then the tenant's policy corpus (ADR-0012), read in
full on every call.
It is capped at `PREFIX_CAP_TOKENS`, and a prefix over the cap is refused,
loudly, before any generator call: never truncated, and never quietly replaced
by a subset of the policies (`PolicyCorpusTooLarge`). A real customer with a
large policy set is therefore refused today; what replaces the refusal is
#274's question, not this module's.

**The size is counted here, locally.** A token-counting API would be a call
outside Australia carrying the tenant's policy text (ADR-0054 point 4), so the
count is an estimate stated in code: `estimate_tokens`. It is high on purpose,
because the cap protects the cost of a document check, so the error that
matters is counting too few. It counts by character class, so structure-heavy
text (the fences' 24-character hex boundaries, stable ids, numbers,
punctuation) is not counted as if it were prose:

- A run of letters and digits that contains a digit (a number, an id, most hex)
  counts one token per character.
- A run of letters only counts `ceil(length / CHARS_PER_TOKEN)`, at least one,
  with `CHARS_PER_TOKEN` 2.5. A run of eight or more characters that is all
  hex digits (a hex string that happens to hold no digit) counts one per
  character instead.
- Any other ASCII character that isn't whitespace counts `SYMBOL_TOKENS`
  (1.25), and so does each newline. A space or tab counts nothing.
- A character that isn't ASCII counts one token per UTF-8 byte.

**What the bound is measured against.** No Claude tokenizer is available
offline, so the tests hold the estimate to a proxy: the vendored reranker
tokenizer's count (`count_reranker_tokens`, a real subword tokenizer, not
Claude's). `tests/test_prefix_cap.py` requires the estimate to be at least 1.15
times that count on the six Acme policies and on adversarial shapes (many
short clauses, numeric, hex-heavy, punctuation-heavy and non-ASCII text), and
at most three times it on the Acme policies. That is a measured bound against
a proxy, not a guarantee about Claude's own count; it should be re-checked
against a real count when one is available (ADR-0054: the cap is revisited
once a real tenant's corpus is measured).

**What is measured is what is sent.** `check_prefix_cap` takes the system prompt
and the built prefix exactly as the generator is given them (`prompt.cag_prefix`
builds it, digest and joining included), so the digest counts like the policies
do and no caller reconstructs the bytes to measure them.

The cap is a constant with its ADR named, not a setting, so it can't be raised
quietly: raising it is a new ADR.
"""

from __future__ import annotations

import math
import re
from typing import Final

from opentelemetry.trace import Span

PREFIX_CAP_TOKENS: Final = 30_000
"""The most the CAG prefix may be, by `estimate_tokens`."""
PREFIX_CAP_ADR: Final = "ADR-0054"
"""The decision that fixed it. Changing the number changes that ADR."""
CHARS_PER_TOKEN: Final = 2.5
"""Characters per token assumed for a run of letters only, deliberately low."""
SYMBOL_TOKENS: Final = 1.25
"""What a punctuation or symbol character, or a newline, counts. Above one,
because the proxy gives one token to each and a symbol is never worth less."""
HEX_RUN_MIN: Final = 8
"""A run of this many hex digits or more is counted a token per character."""

_RUN_OR_SYMBOL = re.compile(r"[A-Za-z0-9]+|[^\sA-Za-z0-9]|\n", re.ASCII)
_HEX_RUN = re.compile(r"[0-9a-fA-F]+")


def _run_tokens(run: str) -> int:
    if any(c.isdigit() for c in run) or (len(run) >= HEX_RUN_MIN and _HEX_RUN.fullmatch(run)):
        return len(run)
    return max(1, math.ceil(len(run) / CHARS_PER_TOKEN))


def estimate_tokens(*parts: str | None) -> int:
    """A conservative count of the tokens in `parts`, summed: see the module
    docstring. `None` is a part that isn't there."""
    total = 0.0
    for part in parts:
        if part is None:
            continue
        for match in _RUN_OR_SYMBOL.finditer(part):
            text = match.group()
            if text[0].isalnum() and text.isascii():
                total += _run_tokens(text)
            elif text.isascii():
                total += SYMBOL_TOKENS
            else:
                total += len(text.encode("utf-8"))
    return math.ceil(total)


class PolicyCorpusTooLarge(RuntimeError):
    """The CAG prefix is over the cap (ADR-0054). Raised before any generator
    call; carries the measured estimate and the cap, so what a client is told
    is a measurement, not a guess."""

    def __init__(self, estimated_tokens: int, cap_tokens: int = PREFIX_CAP_TOKENS) -> None:
        self.estimated_tokens = estimated_tokens
        self.cap_tokens = cap_tokens
        super().__init__(
            f"the policy corpus is too large to read in full: the prompt prefix is about "
            f"{estimated_tokens:,} tokens against a cap of {cap_tokens:,} ({PREFIX_CAP_ADR}); "
            "nothing was truncated and no model was called"
        )


def check_prefix_cap(system: str, prefix: str | None, *, current: Span) -> int:
    """The prefix's estimated size, if it is within the cap; otherwise
    `PolicyCorpusTooLarge`. `system` is the system prompt and `prefix` the
    cached prefix as the generator receives it (`prompt.cag_prefix`: the
    obligation digest, if any, then the fenced policies), or `None` for none.
    The estimate and the cap are recorded on `current`, the request's span,
    whether the prefix passes or not."""
    estimated = estimate_tokens(system, prefix)
    current.set_attribute("prefix.estimated_tokens", estimated)
    current.set_attribute("prefix.cap_tokens", PREFIX_CAP_TOKENS)
    if estimated > PREFIX_CAP_TOKENS:
        raise PolicyCorpusTooLarge(estimated)
    return estimated
