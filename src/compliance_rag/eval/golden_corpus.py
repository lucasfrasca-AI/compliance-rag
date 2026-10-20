"""The corpora the golden set is labelled against (issue #27/#28, #180), parsed
and chunked exactly as ingest does it. Two of them, one law corpus each, both
with the six synthetic policies as `acme`'s own documents:

- **The gate corpus** (`gate_chunks()`): the whole compilation's 779 selected
  clauses as library law under `_public` — what production retrieves over. The
  recall gates (`tests/test_recall_gate.py`, `tests/test_recall_gate_reranked.py`)
  load it into a real Qdrant server and measure retrieval against the labels.
- **The golden excerpt** (`golden_chunks()`): the 75-clause Privacy Act excerpt
  instead. A subset of the gate's, same ids and same text
  (`tests/test_compilation_clauses.py`); small, and parsed from a checked-in
  fixture, so unit and contract tests, and numbers pinned on it (ADR-0035's),
  stay on it.

`tests/test_golden_set.py` proves every label resolves in both.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import yaml

from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Chunk, ClauseRecord, Scope
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.policy_parser import parse_policy
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.compilation_clauses import load_compilation_clauses
from compliance_rag.eval.golden import load_golden_set

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "research" / "fixtures"
LAW_FIXTURE = FIXTURES / "privacy-act-1988-C2026C00227.excerpt.html"
LAW_INSTRUMENT_ID = "privacy-act-1988"
LAW_VERSION_ID = "C2026C00227"
LAW_TITLE = "Privacy Act 1988"
"""The Act's title as the fixture's own `<title>` gives it (ADR-0049)."""
POLICY_TENANT_ID = "acme"
POLICY_VERSION_ID = "v1"

POLICY_INSTRUMENT_IDS = {
    "acme-privacy-policy.md": "acme/privacy-policy",
    "acme-vendor-cross-border-transfer-policy.md": "acme/vendor-policy",
    "acme-data-security-retention-policy.md": "acme/data-security-policy",
    "acme-marketing-communications-policy.md": "acme/marketing-policy",
    "acme-access-correction-procedure.md": "acme/access-correction-procedure",
    "acme-employee-data-handling-policy.md": "acme/employee-policy",
}
"""The instrument ids the golden set's policy labels use (agreed for #27)."""

_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def front_matter_title(source: str) -> str:
    """A synthetic policy's title, from its front matter's `title:`: what the
    operator records it under (ADR-0049). Document metadata, read here and
    never by the policy parser, whose clauses don't change (#217). A fixture
    without one is an error, not a title made up from its file name."""
    match = _FRONT_MATTER_RE.match(source)
    meta: object = yaml.safe_load(match.group(1)) if match else None
    title = meta.get("title") if isinstance(meta, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(title, str) or not title.strip():
        raise ValueError("a synthetic policy's front matter has no title")
    return title


def policy_source(filename: str) -> str:
    return (FIXTURES / "synthetic-policies" / filename).read_text(encoding="utf-8")


@cache
def policy_titles() -> dict[str, str]:
    """Each synthetic policy's instrument id to its front matter's title."""
    return {
        instrument_id: front_matter_title(policy_source(filename))
        for filename, instrument_id in POLICY_INSTRUMENT_IDS.items()
    }


def _policy_clauses() -> tuple[ClauseRecord, ...]:
    return tuple(
        clause
        for filename, instrument_id in POLICY_INSTRUMENT_IDS.items()
        for clause in parse_policy(
            policy_source(filename),
            instrument_id=instrument_id,
            version_id=POLICY_VERSION_ID,
            tenant_id=POLICY_TENANT_ID,
        )
    )


def golden_clauses() -> tuple[ClauseRecord, ...]:
    law = parse_compilation(
        LAW_FIXTURE.read_text(encoding="utf-8"),
        instrument_id=LAW_INSTRUMENT_ID,
        version_id=LAW_VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        scope=Scope.PUBLIC,
    ).clauses
    return (*law, *_policy_clauses())


def _all_chunks() -> tuple[Chunk, ...]:
    return chunk_clauses(golden_clauses(), count_tokens=count_reranker_tokens)


def synthetic_fixture_chunks() -> tuple[Chunk, ...]:
    """The six Acme policies' chunks, unmarked — what the operator registers
    as synthetic (`scripts/register_synthetic_fixtures.py`, issue #78)."""
    return tuple(c for c in _all_chunks() if c.scope is Scope.TENANT)


def synthetic_fixture_clauses() -> tuple[ClauseRecord, ...]:
    """The six Acme policies' clauses, registered alongside their chunks."""
    return tuple(c for c in golden_clauses() if c.scope is Scope.TENANT)


def golden_chunks() -> tuple[Chunk, ...]:
    """The six Acme policies are invented (docs/research/synthetic-policy-set.md).
    Their `synthetic` flag comes from the operator's registry, exactly as the
    write path derives it (issue #78) — the build-phase residency gate
    (ADR-0023) counts any unregistered tenant chunk as real customer data."""
    return SyntheticRegistry.load().mark(_all_chunks())


def golden_texts() -> tuple[str, ...]:
    """Every text a test on the golden corpus embeds: each chunk, then each
    query, once."""
    texts = [c.text for c in golden_chunks()] + [q.query for q in load_golden_set().queries]
    return tuple(dict.fromkeys(texts))


@cache
def gate_law_clauses() -> tuple[ClauseRecord, ...]:
    """The whole compilation's selected clauses, from the committed parser
    output (`eval/compilation_clauses.py`). Read once per process: 779 records."""
    return load_compilation_clauses().clauses


def gate_clauses() -> tuple[ClauseRecord, ...]:
    return (*gate_law_clauses(), *_policy_clauses())


@cache
def _gate_chunks_unmarked() -> tuple[Chunk, ...]:
    return chunk_clauses(gate_clauses(), count_tokens=count_reranker_tokens)


def gate_chunks() -> tuple[Chunk, ...]:
    """The recall gate's corpus: the whole compilation's law and the six Acme
    policies, marked synthetic from the registry as `golden_chunks()` is."""
    return SyntheticRegistry.load().mark(_gate_chunks_unmarked())


def gate_texts() -> tuple[str, ...]:
    """Every text the recall gates embed: each chunk, then each query, once."""
    texts = [c.text for c in gate_chunks()] + [q.query for q in load_golden_set().queries]
    return tuple(dict.fromkeys(texts))


def embedded_texts() -> tuple[str, ...]:
    """Every text the embedding cache must hold: the gate's and the golden
    corpus's (in practice the gate's, since the excerpt is a subset), once."""
    return tuple(dict.fromkeys((*gate_texts(), *golden_texts())))
