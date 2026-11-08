"""The CAG prefix's size cap (ADR-0054, amending D-11 / ADR-0012).

The prefix is the system prompt, the obligation digest and the tenant's policy
corpus, read in full on every call. It is capped at `PREFIX_CAP_TOKENS`, and a
prefix over the cap is refused, loudly, before any generator call: never
truncated, and never quietly replaced by a subset of the policies
(`PolicyCorpusTooLarge`). A real customer with a large policy set is therefore
refused today; what replaces the refusal is #274's question, not this module's.

**The size is counted here, locally.** A token-counting API would be a call
outside Australia carrying the tenant's policy text (ADR-0054 point 4), so the
count is an estimate stated in code: `estimate_tokens`.

- An ASCII character counts as `1 / CHARS_PER_TOKEN` of a token, and
  `CHARS_PER_TOKEN` is 2.5. Claude's tokenizer averages roughly 3.5 to 4.5
  characters a token on English prose, and nearer 3 on text full of
  identifiers, numbers and long hex strings (the fences' boundaries and the
  stable ids in every marker). 2.5 is below all of those, so the estimate is
  high, and by design: the cap protects the cost of a document check, so the
  error that matters is counting too few.
- A character that isn't ASCII counts as one token per UTF-8 byte. A token is at
  least one byte, so for those characters this can't count too few.
- `tests/test_prefix_cap.py` states the bound it holds to on the six Acme
  policies: the estimate is at least 1.15 times the count of an independent
  real subword tokenizer (`count_reranker_tokens`, which splits text into
  fewer pieces than Claude's does), and at most three times it.

The cap is a constant with its ADR named, not a setting, so it can't be raised
quietly: raising it is a new ADR.
"""

from __future__ import annotations

import math
from typing import Final

from opentelemetry.trace import Span

PREFIX_CAP_TOKENS: Final = 30_000
"""The most the CAG prefix may be, by `estimate_tokens`."""
PREFIX_CAP_ADR: Final = "ADR-0054"
"""The decision that fixed it. Changing the number changes that ADR."""
CHARS_PER_TOKEN: Final = 2.5
"""ASCII characters per token assumed, deliberately below any real tokenizer's."""


def estimate_tokens(*parts: str | None) -> int:
    """A conservative count of the tokens in `parts`, together: see the module
    docstring. `None` is a part that isn't there."""
    ascii_characters = 0
    other_bytes = 0
    for part in parts:
        if part is None:
            continue
        encoded = part.encode("utf-8")
        ascii_in_part = sum(1 for byte in encoded if byte < 0x80)
        ascii_characters += ascii_in_part
        other_bytes += len(encoded) - ascii_in_part
    return math.ceil(ascii_characters / CHARS_PER_TOKEN) + other_bytes


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


def check_prefix_cap(
    system: str, policy_corpus: str | None, *, digest: str | None = None, current: Span
) -> int:
    """The prefix's estimated size, if it is within the cap; otherwise
    `PolicyCorpusTooLarge`. `system` is the system prompt, `digest` the
    obligation digest (None until a document check builds one) and
    `policy_corpus` the fenced policies. The estimate and the cap are recorded
    on `current`, the request's span, whether the prefix passes or not."""
    estimated = estimate_tokens(system, digest, policy_corpus)
    current.set_attribute("prefix.estimated_tokens", estimated)
    current.set_attribute("prefix.cap_tokens", PREFIX_CAP_TOKENS)
    if estimated > PREFIX_CAP_TOKENS:
        raise PolicyCorpusTooLarge(estimated)
    return estimated
