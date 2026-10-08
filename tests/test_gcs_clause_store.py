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
    def __init__(self, bucket: _FakeBucket, name: str, generation: int | None = None) -> None:
        self._bucket = bucket
        self.name = name
        self.generation = generation

    def upload_from_string(self, data: str, content_type: str | None = None) -> None:
        self._bucket.write(self.name, data)

    def download_as_text(self) -> str:
        try:
            return self._bucket.objects[self.name]
        except KeyError:
            raise NotFound(self.name) from None

    def delete(self) -> None:
        self._bucket.delete(self.name, self.generation)


class _FakeBucket:
    """Just enough of a *versioned* `storage.Bucket` for `GCSClauseStore`:
    an overwrite keeps the previous content as a noncurrent generation, and
    `list_blobs(versions=True)` yields one blob per generation, as the real
    bucket (versioning enabled, `SERVICES.md`) does."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.noncurrent: list[tuple[str, int, str]] = []
        self._live_generation: dict[str, int] = {}
        self._next_generation = 1

    def write(self, name: str, data: str) -> None:
        if name in self.objects:
            self.noncurrent.append((name, self._live_generation[name], self.objects[name]))
        self.objects[name] = data
        self._live_generation[name] = self._next_generation
        self._next_generation += 1

    def delete(self, name: str, generation: int | None) -> None:
        if generation is None or self._live_generation.get(name) == generation:
            self.objects.pop(name, None)
            self._live_generation.pop(name, None)
        self.noncurrent = [g for g in self.noncurrent if (g[0], g[1]) != (name, generation)]

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self, name)

    def list_blobs(self, *, prefix: str, versions: bool) -> list[_FakeBlob]:
        live = [
            _FakeBlob(self, n, self._live_generation[n])
            for n in self.objects
            if n.startswith(prefix)
        ]
        old = [_FakeBlob(self, n, g) for (n, g, _) in self.noncurrent if n.startswith(prefix)]
        return live + (old if versions else [])


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


# --------------------------------------------------------------------------
# purge (ADR-0021)
# --------------------------------------------------------------------------


async def test_purge_deletes_every_generation_under_the_target_and_nothing_else() -> None:
    from compliance_rag.adapters.removal_authorization import (
        _issue,  # pyright: ignore[reportPrivateUsage] — testing the store's half of the contract
    )
    from compliance_rag.corpus.models import Scope

    def tenant_clause(instrument_id: str, version_id: str, text: str) -> ClauseRecord:
        return ClauseRecord.build(
            instrument_id=instrument_id,
            version_id=version_id,
            clause_path="3/1",
            text=text,
            scope=Scope.TENANT,
            tenant_id="acme",
        )

    bucket = _FakeBucket()
    store = GCSClauseStore(bucket)
    await store.put_clause(tenant_clause("acme/retention-policy", "v1", "seven years"))
    await store.put_clause(
        tenant_clause("acme/retention-policy", "v1", "seven years, then destroyed")
    )
    await store.put_clause(tenant_clause("acme/retention-policy", "v2", "five years"))
    await store.put_clause(
        tenant_clause("acme/retention-policy-archive", "v1", "a near-miss prefix")
    )
    assert any(n.startswith("acme/retention-policy/v1/") for n, _, _ in bucket.noncurrent)

    deleted = await store.purge(
        _issue(
            tenant_id="acme", instrument_id="acme/retention-policy", version_id="v1", audit_id="a1"
        )
    )

    assert deleted > 0
    remaining = list(bucket.objects) + [n for n, _, _ in bucket.noncurrent]
    assert not [n for n in remaining if n.startswith("acme/retention-policy/v1/")]
    assert "acme/retention-policy/v2/clauses/3/1.json" in bucket.objects
    assert "acme/retention-policy-archive/v1/clauses/3/1.json" in bucket.objects
