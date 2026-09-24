"""`GCSClauseStore` (issue #19) against a tiny in-memory stand-in for
`google.cloud.storage.Bucket` — the same two methods (`blob`,
`upload_from_string`/`download_as_text`) the adapter actually calls, so
these tests exercise the adapter's own key layout, JSON shape and
read-modify-write manifest logic without a network call. Behavioural
parity with `FakeClauseStore` is already proven by the shared contract
tests in `tests/contracts/test_interfaces.py`; this file is about the
concrete object keys and manifest.json shape `PHASES.md` and issue #19
actually require.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from google.api_core.exceptions import NotFound

from compliance_rag.adapters.gcs_clause_store import GCSClauseStore
from compliance_rag.corpus.models import CANONICALISATION_VERSION, PUBLIC_TENANT_ID, ClauseRecord

INSTRUMENT_ID = "privacy-act-1988"
VERSION_ID = "C2026C00227"


class _FakeBlob:
    def __init__(self, objects: dict[str, str], name: str) -> None:
        self._objects = objects
        self.name = name

    def upload_from_string(self, data: str, content_type: str | None = None) -> None:
        self._objects[self.name] = data

    def download_as_text(self) -> str:
        try:
            return self._objects[self.name]
        except KeyError:
            raise NotFound(self.name) from None


class _FakeBucket:
    """Just enough of `storage.Bucket` for `GCSClauseStore` to talk to."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self.objects, name)


def _clause(clause_path: str, text: str = "some clause text") -> ClauseRecord:
    return ClauseRecord.build(
        instrument_id=INSTRUMENT_ID, version_id=VERSION_ID, clause_path=clause_path, text=text
    )


# --------------------------------------------------------------------------
# Object keys (PHASES.md)
# --------------------------------------------------------------------------


async def test_put_clause_writes_the_documented_key() -> None:
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    await store.put_clause(_clause("sch1/app11/1"))
    assert "privacy-act-1988/C2026C00227/clauses/sch1/app11/1.json" in bucket.objects


async def test_put_manifest_writes_source_html_and_manifest_at_the_documented_keys() -> None:
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    await store.put_manifest(
        INSTRUMENT_ID,
        VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html="<p>hello</p>",
    )
    assert "privacy-act-1988/C2026C00227/source.html" in bucket.objects
    assert "privacy-act-1988/C2026C00227/manifest.json" in bucket.objects


# --------------------------------------------------------------------------
# get_clause / put_clause
# --------------------------------------------------------------------------


async def test_get_clause_round_trips() -> None:
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    written = _clause("sch1/app11/1", "An APP entity must take reasonable steps.")
    await store.put_clause(written)
    read = await store.get_clause(written.clause_id, tenant_id=PUBLIC_TENANT_ID)
    assert read == written


async def test_get_unknown_clause_returns_none() -> None:
    store = GCSClauseStore(_FakeBucket())
    assert (
        await store.get_clause(
            "privacy-act-1988:C2026C00227:sch1/app11/1", tenant_id=PUBLIC_TENANT_ID
        )
        is None
    )


async def test_get_clause_with_the_wrong_tenant_returns_none() -> None:
    """Defense in depth, matching `FakeClauseStore`'s own contract test."""
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    written = _clause("sch1/app11/1")
    await store.put_clause(written)
    assert await store.get_clause(written.clause_id, tenant_id="some-other-tenant") is None


# --------------------------------------------------------------------------
# get_manifest reflects put_clause immediately (matches FakeClauseStore)
# --------------------------------------------------------------------------


async def test_get_manifest_reflects_every_put_clause_immediately() -> None:
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    first = _clause("sch1/app11/1")
    second = _clause("sch1/app11/2")
    await store.put_clause(first)
    manifest_after_one = await store.get_manifest(
        INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID
    )
    assert set(manifest_after_one) == {"sch1/app11/1"}

    await store.put_clause(second)
    manifest_after_two = await store.get_manifest(
        INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID
    )
    assert set(manifest_after_two) == {"sch1/app11/1", "sch1/app11/2"}


async def test_get_manifest_on_an_untouched_bucket_is_empty() -> None:
    store = GCSClauseStore(_FakeBucket())
    assert await store.get_manifest(INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID) == {}


# --------------------------------------------------------------------------
# put_manifest / get_manifest_metadata / get_source_html
# --------------------------------------------------------------------------


async def test_manifest_metadata_and_source_html_round_trip() -> None:
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    html = "<p>the exact bytes anchors were injected into</p>"
    await store.put_manifest(
        INSTRUMENT_ID,
        VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html=html,
    )

    metadata = await store.get_manifest_metadata(
        INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID
    )
    assert metadata is not None
    assert metadata.canonicalisation_version == CANONICALISATION_VERSION
    assert metadata.source_html_sha256 == hashlib.sha256(html.encode("utf-8")).hexdigest()
    assert (
        await store.get_source_html(INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID) == html
    )


async def test_get_manifest_metadata_is_none_before_put_manifest_even_if_clauses_exist() -> None:
    """`put_clause` alone upserts `clause_paths`; the version-level fields
    only exist once `put_manifest` has actually been called."""
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    await store.put_clause(_clause("sch1/app11/1"))
    assert (
        await store.get_manifest_metadata(INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID)
        is None
    )


async def test_put_manifest_preserves_clause_paths_already_written_by_put_clause() -> None:
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    await store.put_clause(_clause("sch1/app11/1"))
    await store.put_manifest(
        INSTRUMENT_ID,
        VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html="<p>hi</p>",
    )
    manifest = await store.get_manifest(INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID)
    assert set(manifest) == {"sch1/app11/1"}


async def test_put_clause_preserves_manifest_metadata_already_written_by_put_manifest() -> None:
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    await store.put_manifest(
        INSTRUMENT_ID,
        VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        canonicalisation_version=CANONICALISATION_VERSION,
        source_html="<p>hi</p>",
    )
    await store.put_clause(_clause("sch1/app11/1"))
    metadata = await store.get_manifest_metadata(
        INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID
    )
    assert metadata is not None
    assert metadata.canonicalisation_version == CANONICALISATION_VERSION


# --------------------------------------------------------------------------
# manifest.json is valid, sorted JSON (reviewable in a diff)
# --------------------------------------------------------------------------


async def test_manifest_json_is_deterministically_ordered() -> None:
    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    await store.put_clause(_clause("sch1/app11/2"))
    await store.put_clause(_clause("sch1/app11/1"))
    raw = bucket.objects["privacy-act-1988/C2026C00227/manifest.json"]
    assert raw == json.dumps(json.loads(raw), sort_keys=True)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests must never construct a real `storage.Client` — the fake
    bucket above is the only backend in play."""
    import google.cloud.storage as storage_module

    def _fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("GCSClauseStore tests must not touch a real storage.Client")

    monkeypatch.setattr(storage_module, "Client", _fail)
