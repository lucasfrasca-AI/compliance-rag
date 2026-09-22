"""Tenant id validation (`CONTEXT.md` **Tenant id**, issue #3).

Two validators, deliberately different strictness:

- `validate_tenant_segment` (field type `TenantSegment`) — safe to embed in a
  stable ID. Rejects `:` and `/`, the two delimiters a stable ID depends on,
  and empty. Allows `PUBLIC_TENANT_ID`, because that value is a legitimate
  sentinel wherever a record's scope is public — it is the type of
  `ClauseRecord.tenant_id` and `Chunk.tenant_id`.
- `validate_tenant_id` — a *real* tenant id, applied where a tenant enters the
  system (`Profile.__post_init__`; `Profile` is a plain dataclass, so it calls
  the function directly rather than using an Annotated field type). Same
  rejections, plus `_public` itself: a company may never register under the
  reserved name.
"""

from __future__ import annotations

import pytest

from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    TenantSegment,
    validate_tenant_id,
    validate_tenant_segment,
)


class _Holder:
    """Exercises `TenantSegment` through real pydantic validation, in isolation

    from `ClauseRecord`/`Chunk` — those models cover the same type in practice
    (their `tenant_id` field *is* `TenantSegment`), but a failure here
    attributes to the type itself rather than to a model's other invariants.
    """

    tenant_id: str

    def __init__(self, tenant_id: str) -> None:
        from pydantic import TypeAdapter

        self.tenant_id = TypeAdapter(TenantSegment).validate_python(tenant_id)


@pytest.mark.parametrize("value", ["acme", "acme-corp", "acme_corp", "a1"])
def test_valid_tenant_ids_pass_both_validators(value: str) -> None:
    assert validate_tenant_segment(value) == value
    assert validate_tenant_id(value) == value


def test_empty_tenant_id_is_rejected_by_both() -> None:
    with pytest.raises(ValueError, match="empty"):
        validate_tenant_segment("")
    with pytest.raises(ValueError, match="empty"):
        validate_tenant_id("")


def test_colon_is_rejected_by_both() -> None:
    """':' is the only stable-ID delimiter; a tenant id must never carry one."""
    with pytest.raises(ValueError, match=":"):
        validate_tenant_segment("acme:corp")
    with pytest.raises(ValueError, match=":"):
        validate_tenant_id("acme:corp")


def test_slash_is_rejected_by_both() -> None:
    """'/' appears inside clause paths and tenant-prefixed instrument ids."""
    with pytest.raises(ValueError, match="/"):
        validate_tenant_segment("acme/corp")
    with pytest.raises(ValueError, match="/"):
        validate_tenant_id("acme/corp")


def test_reserved_public_id_passes_segment_but_fails_tenant_validation() -> None:
    """`_public` is a legitimate sentinel value, but never a real tenant's id."""
    assert validate_tenant_segment(PUBLIC_TENANT_ID) == PUBLIC_TENANT_ID
    with pytest.raises(ValueError, match="reserved"):
        validate_tenant_id(PUBLIC_TENANT_ID)


def test_tenant_segment_rejects_the_stable_id_delimiters() -> None:
    for bad in ("", "a:b", "a/b"):
        with pytest.raises(ValueError):
            _Holder(bad)


def test_tenant_segment_allows_the_reserved_public_sentinel() -> None:
    """Unlike `validate_tenant_id`, the field type on ClauseRecord and Chunk
    must accept `_public` — it is the value those records carry for law."""
    assert _Holder(PUBLIC_TENANT_ID).tenant_id == PUBLIC_TENANT_ID


def test_tenant_segment_accepts_a_real_tenant() -> None:
    assert _Holder("acme").tenant_id == "acme"
