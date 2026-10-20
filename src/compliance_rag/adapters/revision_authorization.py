"""The capability a store-level corpus revision requires (ADR-0042).

A revision rewrites stored library law, which nothing else may do: the bucket's
clause objects and manifest hashes, the Clause nodes' `canonical_sha256`, and
the text and vectors of Qdrant points — and, in each, whether the clause is
incomplete and why (ADR-0047). The guardrails (the plan, the exact
acknowledgement, the pin, the findings count) live in `CorpusRevision`, the one
issuer of a `RevisionAuthorization`. The store-level writes take one and touch
only the clauses its record names, of the version it names, with the hashes
it records.

As with `RemovalAuthorization` (ADR-0021), this makes a bypass deliberate and
visible, not impossible: it means importing `_issue` under a
`reportPrivateUsage` suppression.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    Incomplete,
    canonical_sha256,
    instrument_tenant_prefix,
    parse_stable_id,
    validate_instrument_id,
)
from compliance_rag.corpus.revision import RevisionRecord, described

_ISSUER = object()


@dataclass(frozen=True, slots=True)
class RevisionAuthorization:
    """One authorised revision of one library-law instrument version, carrying
    its record: what each named clause's hash was and becomes."""

    record: RevisionRecord
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise PermissionError("a RevisionAuthorization is issued only by CorpusRevision")
        validate_instrument_id(self.record.instrument_id)
        if instrument_tenant_prefix(self.record.instrument_id) is not None:
            raise PermissionError(
                f"{self.record.instrument_id!r} is a tenant's document; a corpus revision is of "
                "library law (a tenant's document is versioned or replaced, ADR-0021)"
            )

    @property
    def tenant_id(self) -> str:
        return PUBLIC_TENANT_ID

    @property
    def instrument_id(self) -> str:
        return self.record.instrument_id

    @property
    def version_id(self) -> str:
        return self.record.version_id

    def sha256_for(self, clause_id: str) -> str:
        """The hash this revision gives `clause_id`, or `PermissionError` if
        the clause isn't one it writes in its version."""
        instrument_id, version_id, path = parse_stable_id(clause_id)
        written = self.record.written
        if (instrument_id, version_id) != (self.instrument_id, self.version_id) or (
            path not in written
        ):
            raise PermissionError(
                f"{clause_id} is not a clause revision {self.record.revision_id} writes"
            )
        return written[path]

    def check_form(self, clause_id: str, canonical_text: str, incomplete: Incomplete) -> None:
        """`canonical_text` is the text this revision records for `clause_id`,
        and `incomplete` the incompleteness it records (ADR-0047)."""
        if canonical_sha256(canonical_text) != self.sha256_for(clause_id):
            raise PermissionError(
                f"{clause_id}: the text given is not the text revision "
                f"{self.record.revision_id} records (its hash differs)"
            )
        recorded = self.record.written_incomplete[parse_stable_id(clause_id)[2]]
        if incomplete != recorded:
            raise PermissionError(
                f"{clause_id}: given as {described(incomplete)}, but revision "
                f"{self.record.revision_id} records it {described(recorded)}"
            )


def _issue(record: RevisionRecord) -> RevisionAuthorization:  # pyright: ignore[reportUnusedFunction] — called only by CorpusRevision
    return RevisionAuthorization(record=record, _issuer=_ISSUER)
