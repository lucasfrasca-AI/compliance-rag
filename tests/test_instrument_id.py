"""Tenant-prefixed instrument ids (`CONTEXT.md` **Instrument id**, issue #4).

An instrument id is globally unique on its own (public law), or prefixed by
its owning tenant as `tenant/instrument` (a company's own policy) — at most
one `/`. The tenant part obeys the tenant-id rules from issue #3: no `:` or
`/`, not empty, and `_public` is reserved, so a public instrument can never
be prefixed.
"""

from __future__ import annotations

import pytest

from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    instrument_tenant_prefix,
    parse_stable_id,
    stable_id,
    validate_instrument_id,
)


def test_unprefixed_instrument_id_passes() -> None:
    assert validate_instrument_id("privacy-act-1988") == "privacy-act-1988"
    assert instrument_tenant_prefix("privacy-act-1988") is None


def test_tenant_prefixed_instrument_id_passes() -> None:
    assert validate_instrument_id("acme/hr-policy") == "acme/hr-policy"
    assert instrument_tenant_prefix("acme/hr-policy") == "acme"


def test_two_slashes_are_rejected() -> None:
    """Only one `/` — `tenant/instrument`, never a deeper path."""
    with pytest.raises(ValueError, match="invalid instrument_id"):
        validate_instrument_id("acme/beta/hr-policy")


def test_reserved_public_id_is_never_a_valid_tenant_prefix() -> None:
    """A public instrument is never prefixed (CONTEXT.md: Instrument id)."""
    with pytest.raises(ValueError):
        validate_instrument_id(f"{PUBLIC_TENANT_ID}/hr-policy")


def test_stable_id_and_parse_stable_id_round_trip_a_prefixed_id() -> None:
    built = stable_id("acme/hr-policy", "v3", "sec2/1")
    assert built == "acme/hr-policy:v3:sec2/1"
    assert parse_stable_id(built) == ("acme/hr-policy", "v3", "sec2/1")


def test_stable_id_and_parse_stable_id_round_trip_an_unprefixed_id() -> None:
    built = stable_id("privacy-act-1988", "TEST-FIXTURE-001", "sch1/app11/1")
    assert parse_stable_id(built) == ("privacy-act-1988", "TEST-FIXTURE-001", "sch1/app11/1")
