"""The citation verifier (issue #30, D-02): the deterministic check that makes
"every citation verbatim, versioned and clickable" true.

Every citation a draft makes is checked, in this order, and the first failure
is its outcome:

1. **In context.** The cited clause was in the retrieved or cached set this
   request actually showed the model. A citation to anything else blocks.
2. **Version in force — from the ledger, not from retrieval.** The cited
   `version_id` must be recorded, in force at the request's `as_of`, and not
   withdrawn (ADR-0021 mechanism 3). Retrieval's own filter is mechanism 1;
   this check holds even when retrieval leaked a retired clause. A version the
   ledger has never recorded blocks: unknown is not in force.
3. **Canonical text.** The clause is read from `ClauseStore` — canonical text
   in Cloud Storage — and the quote, canonicalised, must be a substring of it,
   under the same canonicalisation version (`CONTEXT.md`: Canonicalisation
   version). Curly quotes and collapsed whitespace therefore still match.

Only a quote mismatch is repairable (`CONTEXT.md`: Repair): the clause is
legitimate, the model just didn't copy it exactly. Out-of-context, unknown,
retired and withdrawn citations block outright — no regeneration makes them
true.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from compliance_rag.adapters.interfaces import (
    CanonicalisationVersionMismatch,
    CitationVerifier,
    ClauseStore,
    LedgerStore,
)
from compliance_rag.assessment.draft import DraftCitation
from compliance_rag.corpus.models import (
    CANONICALISATION_VERSION,
    ClauseRecord,
    canonicalise,
    instrument_owner,
    parse_stable_id,
)


class Outcome(StrEnum):
    VERIFIED = "verified"
    NOT_IN_CONTEXT = "not_in_context"
    VERSION_UNKNOWN = "version_unknown"
    VERSION_NOT_IN_FORCE = "version_not_in_force"
    WITHDRAWN = "withdrawn"
    CLAUSE_MISSING = "clause_missing"
    CANONICALISATION_MISMATCH = "canonicalisation_mismatch"
    QUOTE_MISMATCH = "quote_mismatch"

    @property
    def repairable(self) -> bool:
        return self is Outcome.QUOTE_MISMATCH


@dataclass(frozen=True, slots=True)
class CheckedCitation:
    citation: DraftCitation
    outcome: Outcome
    clause: ClauseRecord | None = None
    """Set when the clause was read: what a user is shown is
    `clause.canonical_text` — never the model's text (D-02)."""

    @property
    def verified(self) -> bool:
        return self.outcome is Outcome.VERIFIED


class CanonicalCitationVerifier(CitationVerifier):
    """A canonicalised quote must be a substring of the clause's canonical
    text, compared only under the same canonicalisation version."""

    async def verify(
        self,
        clause_id: str,
        quoted_text: str,
        *,
        clauses: tuple[ClauseRecord, ...],
        canonicalisation_version: int,
    ) -> bool:
        quote = canonicalise(quoted_text)
        if not quote:
            return False
        for clause in clauses:
            if clause.clause_id != clause_id:
                continue
            if canonicalisation_version != clause.canonicalisation_version:
                raise CanonicalisationVersionMismatch(
                    f"cannot verify a canonicalisation v{canonicalisation_version} citation "
                    f"against {clause_id!r}, canonicalised under "
                    f"v{clause.canonicalisation_version}"
                )
            return quote in clause.canonical_text
        return False


class Verifier:
    """Checks one request's citations against its context, the ledger and
    canonical text."""

    def __init__(
        self, clause_store: ClauseStore, ledger: LedgerStore, comparator: CitationVerifier
    ) -> None:
        self._clause_store = clause_store
        self._ledger = ledger
        self._comparator = comparator

    async def check(
        self,
        citation: DraftCitation,
        *,
        context: frozenset[str],
        as_of: dt.date,
        user_id: str | None = None,
    ) -> CheckedCitation:
        """`user_id` is the member whose answer this is: canonical text of a
        user-scoped clause is read for its owner alone (ADR-0036). Retrieval's
        filter keeps another's clause out of `context`; this holds even when
        it didn't, like the version check above."""
        if citation.clause_id not in context:
            return CheckedCitation(citation, Outcome.NOT_IN_CONTEXT)
        instrument_id, version_id, _ = parse_stable_id(citation.clause_id)
        owner = instrument_owner(instrument_id)

        validity = await self._ledger.version_validity(
            tenant_id=owner, instrument_id=instrument_id, version_id=version_id
        )
        if validity is None:
            return CheckedCitation(citation, Outcome.VERSION_UNKNOWN)
        valid_from, valid_to = validity
        if not (valid_from <= as_of and (valid_to is None or as_of < valid_to)):
            return CheckedCitation(citation, Outcome.VERSION_NOT_IN_FORCE)
        if await self._ledger.is_withdrawn(
            tenant_id=owner, instrument_id=instrument_id, version_id=version_id
        ):
            return CheckedCitation(citation, Outcome.WITHDRAWN)

        clause = await self._clause_store.get_clause(
            citation.clause_id, tenant_id=owner, user_id=user_id
        )
        if clause is None:
            return CheckedCitation(citation, Outcome.CLAUSE_MISSING)
        # The quote is canonicalised now, under the current version; a clause
        # stored under an older one is not re-canonicalised to fit — it
        # blocks until re-ingested (`CONTEXT.md`: Canonicalisation version).
        try:
            matches = await self._comparator.verify(
                citation.clause_id,
                citation.quote,
                clauses=(clause,),
                canonicalisation_version=CANONICALISATION_VERSION,
            )
        except CanonicalisationVersionMismatch:
            return CheckedCitation(citation, Outcome.CANONICALISATION_MISMATCH, clause)
        return CheckedCitation(
            citation, Outcome.VERIFIED if matches else Outcome.QUOTE_MISMATCH, clause
        )

    async def check_all(
        self,
        citations: Iterable[DraftCitation],
        *,
        context: frozenset[str],
        as_of: dt.date,
        user_id: str | None = None,
    ) -> tuple[CheckedCitation, ...]:
        return tuple(
            [await self.check(c, context=context, as_of=as_of, user_id=user_id) for c in citations]
        )
