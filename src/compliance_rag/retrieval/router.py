"""The router: the single fixed decision between exact lookup and the full
hybrid pipeline (`CONTEXT.md`: Router, D-04, D-12, issue #5).

A clause-ID query — an exact stable ID, tenant-prefixed or not — has one
correct answer, so it is sent to `RetrievalStore.lookup_clause` on the
keyword channel and never reaches the reranker: a cross-encoder can only
degrade a query that isn't ambiguous (D-04). Anything else runs the full
pipeline unchanged. `STABLE_ID_RE` already accepts a tenant-prefixed
instrument id (issue #3's tenant-id rules, folded into the instrument-id
grammar in issue #4), so this module adds no grammar of its own — only the
routing decision.
"""

from __future__ import annotations

from compliance_rag.corpus.models import STABLE_ID_RE


def is_clause_id_query(query: str) -> bool:
    """True iff `query`, stripped of surrounding whitespace, is in its
    entirety a stable clause ID.

    Anchored full-string match, not a search: prose that happens to mention
    a clause ID mid-sentence must fall through to the full pipeline rather
    than short-circuit to a single lookup (issue #5's near-miss case).
    """
    return STABLE_ID_RE.match(query.strip()) is not None
