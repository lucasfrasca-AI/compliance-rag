"""The capability a store-level delete requires (ADR-0021).

Purge and replace-in-place guardrails — refuse library law, show the count of
findings citing the target before acting, audit every outcome — live in
`CorpusLifecycle`, the one adapter that issues a `RemovalAuthorization`. The
store-level operations that actually delete or withdraw (`RetrievalAdmin`,
`ClauseStore.purge`) take one and act only on the target it names.

This makes bypassing the guardrails deliberate and visible, not impossible —
nothing in one Python process is. Reaching a store-level delete without them
means importing `_issue` (or `_ISSUER`) under a `reportPrivateUsage`
suppression: `CorpusLifecycle` does, and so does the store's own purge test,
which is exercising the store's half of the contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    instrument_tenant_prefix,
    validate_instrument_id,
)

_ISSUER = object()


@dataclass(frozen=True, slots=True)
class RemovalAuthorization:
    """One authorised removal of a tenant-owned instrument, or one version of
    it (`version_id` None means every version), tied to the audit row that
    recorded it being authorised."""

    tenant_id: str
    instrument_id: str
    version_id: str | None
    audit_id: str
    _issuer: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuer is not _ISSUER:
            raise PermissionError("a RemovalAuthorization is issued only by CorpusLifecycle")
        validate_instrument_id(self.instrument_id)
        if self.tenant_id == PUBLIC_TENANT_ID or (
            instrument_tenant_prefix(self.instrument_id) != self.tenant_id
        ):
            raise PermissionError(
                f"{self.instrument_id!r} is not owned by tenant {self.tenant_id!r}; "
                "library law is never removed, only unsubscribed from"
            )

    @property
    def stable_id_prefix(self) -> str:
        return stable_id_prefix(self.instrument_id, self.version_id)


def stable_id_prefix(instrument_id: str, version_id: str | None) -> str:
    """What every clause id under one instrument (`version_id` None) or one
    instrument version starts with."""
    if version_id is None:
        return f"{instrument_id}:"
    return f"{instrument_id}:{version_id}:"


def _issue(  # pyright: ignore[reportUnusedFunction] — called only by CorpusLifecycle
    *, tenant_id: str, instrument_id: str, version_id: str | None, audit_id: str
) -> RemovalAuthorization:
    return RemovalAuthorization(
        tenant_id=tenant_id,
        instrument_id=instrument_id,
        version_id=version_id,
        audit_id=audit_id,
        _issuer=_ISSUER,
    )
