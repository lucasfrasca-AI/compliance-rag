"""`ClauseStore` over Cloud Storage (issue #19, `PHASES.md`: Cloud Storage
layout). The only module in the codebase that opens a `google.cloud.storage`
bucket (`CLAUDE.md`: "No Cypher, Qdrant, Vertex or SQL calls outside
adapters/").

Object layout, one instrument version per prefix:

- `{instrument_id}/{version_id}/clauses/{clause_path}.json` — one `ClauseRecord`
  each, so a citation lookup (`get_clause`) is a single GET.
- `{instrument_id}/{version_id}/source.html` — the exact bytes `parse_compilation`
  injected anchors into (`CONTEXT.md`: Position map).
- `{instrument_id}/{version_id}/manifest.json` — `{"clause_paths": {path:
  canonical_sha256}, "canonicalisation_version": int, "source_html_sha256": str}`.
  `clause_paths` is upserted by `put_clause` (one read-modify-write per
  clause) and by `put_clauses` (once per version, *before* its clause objects
  — an intent record, so a crash part-way leaves every intended path listed
  and the next ingest's drift check sees them). After a completed write the
  manifest matches what `FakeClauseStore` derives from its in-memory dict;
  during an interrupted batch it may list paths whose objects aren't written
  yet, which `get_clause` reports as absent.

A tenant-owned instrument's `instrument_id` is already prefixed with its
tenant (issue #4: `validate_instrument_id`), so the object key itself is the
tenant boundary — two tenants can never collide on one key the way
`FakeClauseStore`'s defense-in-depth tests deliberately force in memory.

`_BucketLike`/`_BlobLike` name only the two `google.cloud.storage` methods
this module actually calls: `google-cloud-storage` ships no inline type
stubs, so typing against the real `storage.Bucket`/`storage.Blob` propagates
`Unknown` through every call site under pyright strict, and a real `Bucket`
can't be swapped for a lightweight fake in tests without a shared,
minimally-typed shape either way. `storage.Client().bucket(...)` and its
`.blob(...)` satisfy this protocol structurally, so `for_bucket` needs no
cast.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Sequence
from typing import Any, Protocol, cast

from google.api_core.exceptions import NotFound
from google.cloud import storage

from compliance_rag.adapters.interfaces import ClauseStore, ManifestMetadata
from compliance_rag.adapters.removal_authorization import RemovalAuthorization
from compliance_rag.adapters.revision_authorization import RevisionAuthorization
from compliance_rag.corpus.models import (
    ClauseRecord,
    canonical_sha256,
    parse_stable_id,
    sha256_hex,
)
from compliance_rag.corpus.revision import RevisionDrift, RevisionRecord, check_revision_write


class _BlobLike(Protocol):
    def upload_from_string(self, data: str, content_type: str | None = None) -> None: ...
    def download_as_text(self) -> str: ...
    def delete(self) -> None: ...


class _BucketLike(Protocol):
    def blob(self, name: str, /) -> _BlobLike: ...
    def list_blobs(self, *, prefix: str, versions: bool) -> Iterable[_BlobLike]: ...


def _download_text_or_none(blob: _BlobLike) -> str | None:
    try:
        return blob.download_as_text()
    except NotFound:
        return None


class GCSClauseStore(ClauseStore):
    """Canonical clause text and per-version manifests in a Cloud Storage
    bucket (`SERVICES.md`: `<project>-corpus`)."""

    def __init__(self, bucket: _BucketLike) -> None:
        self._bucket = bucket

    @classmethod
    def for_bucket(cls, bucket_name: str) -> GCSClauseStore:
        # google-cloud-storage ships no inline stubs, so pyright can't verify
        # storage.Bucket's own methods satisfy _BucketLike structurally even
        # though they do at runtime (same shape, extra optional kwargs).
        bucket = cast(
            "_BucketLike",
            storage.Client().bucket(bucket_name),  # pyright: ignore[reportUnknownMemberType]
        )
        return cls(bucket)

    @staticmethod
    def _clauses_key(instrument_id: str, version_id: str, clause_path: str) -> str:
        return f"{instrument_id}/{version_id}/clauses/{clause_path}.json"

    @staticmethod
    def _source_html_key(instrument_id: str, version_id: str) -> str:
        return f"{instrument_id}/{version_id}/source.html"

    @staticmethod
    def _manifest_key(instrument_id: str, version_id: str) -> str:
        return f"{instrument_id}/{version_id}/manifest.json"

    async def _read_manifest(self, instrument_id: str, version_id: str) -> dict[str, Any] | None:
        # manifest.json's shape is this module's own contract (see the module
        # docstring), not validated by any schema at the JSON boundary — Any
        # here stands for "whatever json.loads hands back," narrowed by the
        # specific keys each caller below reads off it.
        blob = self._bucket.blob(self._manifest_key(instrument_id, version_id))
        data = await asyncio.to_thread(_download_text_or_none, blob)
        if data is None:
            return None
        parsed: dict[str, Any] = json.loads(data)
        return parsed

    async def _read_manifest_or_empty(self, instrument_id: str, version_id: str) -> dict[str, Any]:
        """`_read_manifest`, defaulting to a fresh `clause_paths` dict on a
        first-ever write — the read-modify-write starting point both
        `put_clause` and `put_manifest` share."""
        return await self._read_manifest(instrument_id, version_id) or {"clause_paths": {}}

    async def _write_manifest(
        self, instrument_id: str, version_id: str, manifest: dict[str, Any]
    ) -> None:
        blob = self._bucket.blob(self._manifest_key(instrument_id, version_id))
        await asyncio.to_thread(
            blob.upload_from_string,
            json.dumps(manifest, sort_keys=True),
            content_type="application/json",
        )

    async def get_clause(
        self, clause_id: str, *, tenant_id: str, user_id: str | None = None
    ) -> ClauseRecord | None:
        instrument_id, version_id, clause_path = parse_stable_id(clause_id)
        blob = self._bucket.blob(self._clauses_key(instrument_id, version_id, clause_path))
        data = await asyncio.to_thread(_download_text_or_none, blob)
        if data is None:
            return None
        record = ClauseRecord.model_validate_json(data)
        # Defense in depth only, matching FakeClauseStore's contract test:
        # a well-formed corpus (issue #4) can't actually produce this
        # mismatch, because a tenant-owned instrument_id already carries its
        # tenant prefix and is therefore already a distinct object key.
        if record.tenant_id != tenant_id:
            return None
        # One person's private addition (ADR-0036): the object key names the
        # tenant, not the owner, so the owner is checked on the record itself.
        # A reader who isn't the owner is told it isn't there.
        return record if record.visible_to(user_id) else None

    async def put_clause(self, clause: ClauseRecord) -> None:
        blob = self._bucket.blob(
            self._clauses_key(clause.instrument_id, clause.version_id, clause.clause_path)
        )
        await asyncio.to_thread(
            blob.upload_from_string,
            clause.model_dump_json(),
            content_type="application/json",
        )
        manifest = await self._read_manifest_or_empty(clause.instrument_id, clause.version_id)
        manifest.setdefault("clause_paths", {})[clause.clause_path] = canonical_sha256(
            clause.canonical_text
        )
        await self._write_manifest(clause.instrument_id, clause.version_id, manifest)

    async def put_clauses(self, clauses: Sequence[ClauseRecord]) -> None:
        """Every clause object, then each version's `manifest.json` once.

        `put_clause` rewrites the manifest after each clause, which is right
        for one clause but, for an ingest, means hundreds of writes to one
        object: Cloud Storage allows about one write per second to a single
        object and answers 429 beyond it — the first real ingest (issue #99)
        failed at the 11th clause. Clause objects are distinct keys and have
        no such limit. A failure part-way leaves clause objects the manifest
        doesn't list yet; the ingest re-runs them (idempotent keys).

        The manifest is written first, as an intent record: if the batch dies
        part-way, every path it meant to write is listed, so the next ingest's
        drift check compares a different selection against it instead of
        mistaking the bucket for empty and leaving this batch's objects behind
        as unlisted orphans `get_clause` would still serve."""
        versions = sorted({(c.instrument_id, c.version_id) for c in clauses})
        for instrument_id, version_id in versions:
            manifest = await self._read_manifest_or_empty(instrument_id, version_id)
            paths = manifest.setdefault("clause_paths", {})
            for clause in clauses:
                if (clause.instrument_id, clause.version_id) == (instrument_id, version_id):
                    paths[clause.clause_path] = canonical_sha256(clause.canonical_text)
            await self._write_manifest(instrument_id, version_id, manifest)
        for clause in clauses:
            blob = self._bucket.blob(
                self._clauses_key(clause.instrument_id, clause.version_id, clause.clause_path)
            )
            await asyncio.to_thread(
                blob.upload_from_string, clause.model_dump_json(), content_type="application/json"
            )

    async def get_manifest(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> dict[str, str]:
        manifest = await self._read_manifest(instrument_id, version_id)
        if manifest is None:
            return {}
        clause_paths: dict[str, str] = manifest.get("clause_paths", {})
        return clause_paths

    async def put_manifest(
        self,
        instrument_id: str,
        version_id: str,
        *,
        tenant_id: str,
        canonicalisation_version: int,
        source_html: str,
    ) -> None:
        html_blob = self._bucket.blob(self._source_html_key(instrument_id, version_id))
        await asyncio.to_thread(
            html_blob.upload_from_string, source_html, content_type="text/html; charset=utf-8"
        )
        manifest = await self._read_manifest_or_empty(instrument_id, version_id)
        manifest["canonicalisation_version"] = canonicalisation_version
        manifest["source_html_sha256"] = sha256_hex(source_html)
        await self._write_manifest(instrument_id, version_id, manifest)

    async def get_manifest_metadata(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> ManifestMetadata | None:
        manifest = await self._read_manifest(instrument_id, version_id)
        if manifest is None:
            return None
        if "canonicalisation_version" not in manifest or "source_html_sha256" not in manifest:
            return None
        return ManifestMetadata(
            canonicalisation_version=manifest["canonicalisation_version"],
            source_html_sha256=manifest["source_html_sha256"],
        )

    async def get_source_html(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> str | None:
        blob = self._bucket.blob(self._source_html_key(instrument_id, version_id))
        return await asyncio.to_thread(_download_text_or_none, blob)

    async def record_revision(
        self, authorization: RevisionAuthorization, clauses: Sequence[ClauseRecord]
    ) -> None:
        """The manifest first — the record and the new hashes in one write, the
        revision's commit point — then the clause objects that don't already
        hold their new text. The bucket is versioned, so each object written
        over keeps its previous text as a noncurrent generation, and so does
        the manifest (ADR-0042)."""
        record = authorization.record
        for clause in clauses:
            authorization.check_text(clause.clause_id, clause.canonical_text)
        manifest = await self._read_manifest(record.instrument_id, record.version_id)
        if manifest is None or "source_html_sha256" not in manifest:
            raise RevisionDrift(
                f"{record.instrument_id}:{record.version_id} has no completed ingest to revise"
            )
        paths: dict[str, str] = manifest.setdefault("clause_paths", {})
        recorded: list[dict[str, object]] = manifest.setdefault("revisions", [])
        on_record = any(r.get("revision_id") == record.revision_id for r in recorded)
        changed = check_revision_write(
            record,
            paths,
            given=frozenset(c.clause_path for c in clauses),
            on_record=on_record,
        )
        if changed or not on_record:
            for path in changed:
                paths[path] = record.written[path]
            if not on_record:
                recorded.append(record.to_json())
            await self._write_manifest(record.instrument_id, record.version_id, manifest)
        for clause in clauses:
            stored = await self.get_clause(clause.clause_id, tenant_id=clause.tenant_id)
            if stored is not None and stored.canonical_text == clause.canonical_text:
                continue  # already written: no noncurrent generation for nothing
            blob = self._bucket.blob(
                self._clauses_key(clause.instrument_id, clause.version_id, clause.clause_path)
            )
            await asyncio.to_thread(
                blob.upload_from_string, clause.model_dump_json(), content_type="application/json"
            )

    async def revisions(
        self, instrument_id: str, version_id: str, *, tenant_id: str
    ) -> tuple[RevisionRecord, ...]:
        manifest = await self._read_manifest(instrument_id, version_id)
        if manifest is None:
            return ()
        recorded: list[dict[str, object]] = manifest.get("revisions", [])
        return tuple(RevisionRecord.from_json(r) for r in recorded)

    async def purge(self, authorization: RemovalAuthorization) -> None:
        """Every generation under the target's prefix, live and noncurrent:
        `list_blobs(versions=True)` yields one blob per generation and
        `Blob.delete()` deletes that blob's own generation. The bucket's
        soft-delete retention still applies after this (ADR-0021)."""
        a = authorization
        prefix = (
            f"{a.instrument_id}/" if a.version_id is None else f"{a.instrument_id}/{a.version_id}/"
        )

        def delete_all() -> None:
            for blob in list(self._bucket.list_blobs(prefix=prefix, versions=True)):
                blob.delete()

        await asyncio.to_thread(delete_all)
