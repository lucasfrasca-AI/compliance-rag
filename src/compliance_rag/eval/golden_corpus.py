"""The corpus the golden set is labelled against (issue #27/#28): the Privacy
Act excerpt as library law under `_public`, and the six synthetic policies as
`acme`'s own documents — parsed and chunked exactly as ingest does it.

`tests/test_golden_set.py` proves every label resolves here; the recall gate
(`tests/test_recall_gate.py`) loads these chunks into a real Qdrant server and
measures retrieval against the labels.
"""

from __future__ import annotations

from pathlib import Path

from compliance_rag.corpus.chunker import chunk_clauses
from compliance_rag.corpus.models import PUBLIC_TENANT_ID, Chunk, ClauseRecord, Scope
from compliance_rag.corpus.parser import parse_compilation
from compliance_rag.corpus.policy_parser import parse_policy
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.eval.golden import load_golden_set

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "research" / "fixtures"
LAW_FIXTURE = FIXTURES / "privacy-act-1988-C2026C00227.excerpt.html"
LAW_INSTRUMENT_ID = "privacy-act-1988"
LAW_VERSION_ID = "C2026C00227"
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


def golden_clauses() -> tuple[ClauseRecord, ...]:
    law = parse_compilation(
        LAW_FIXTURE.read_text(encoding="utf-8"),
        instrument_id=LAW_INSTRUMENT_ID,
        version_id=LAW_VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        scope=Scope.PUBLIC,
    ).clauses
    policies = tuple(
        clause
        for filename, instrument_id in POLICY_INSTRUMENT_IDS.items()
        for clause in parse_policy(
            (FIXTURES / "synthetic-policies" / filename).read_text(encoding="utf-8"),
            instrument_id=instrument_id,
            version_id=POLICY_VERSION_ID,
            tenant_id=POLICY_TENANT_ID,
        )
    )
    return (*law, *policies)


def synthetic_fixture_chunks() -> tuple[Chunk, ...]:
    """The six Acme policies' chunks, unmarked — what the operator registers
    as synthetic (`scripts/register_synthetic_fixtures.py`, issue #78)."""
    chunks = chunk_clauses(golden_clauses(), count_tokens=count_reranker_tokens)
    return tuple(c for c in chunks if c.scope is Scope.TENANT)


def golden_chunks() -> tuple[Chunk, ...]:
    """The six Acme policies are invented (docs/research/synthetic-policy-set.md).
    Their `synthetic` flag comes from the operator's registry, exactly as the
    write path derives it (issue #78) — the build-phase residency gate
    (ADR-0023) counts any unregistered tenant chunk as real customer data."""
    chunks = chunk_clauses(golden_clauses(), count_tokens=count_reranker_tokens)
    return SyntheticRegistry.load().mark(chunks)


def golden_texts() -> tuple[str, ...]:
    """Every text the recall gate embeds: each chunk, then each query, once."""
    texts = [c.text for c in golden_chunks()] + [q.query for q in load_golden_set().queries]
    return tuple(dict.fromkeys(texts))
