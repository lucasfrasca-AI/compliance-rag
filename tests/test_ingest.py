"""Ingest orchestration: pinned compilation + selection manifest -> Cloud
Storage, via `ClauseStore` (issue #19).

Real fixture bytes throughout (`docs/research/fixtures/privacy-act-1988-
C2026C00227.excerpt.html`, issue #9's spike), per `CLAUDE.md`'s testing
standard — a synthetic HTML snippet would prove nothing about the actual
markup ingest has to survive.

The drift check is the point of this ticket, not an afterthought: a
re-ingest is only ever a no-op or a hard stop before anything is written,
never a silent overwrite (`CLAUDE.md` corpus rules: "canonical text never
edited after write").
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from compliance_rag.adapters.fakes import FakeClauseStore
from compliance_rag.corpus.ingest import IngestDriftError, ingest_compilation
from compliance_rag.corpus.models import (
    CANONICALISATION_VERSION,
    PUBLIC_TENANT_ID,
    ClauseRecord,
    Scope,
    canonical_sha256,
    stable_id,
)
from compliance_rag.corpus.selection_manifest import SelectionManifest

FIXTURE = (
    Path(__file__).parent.parent
    / "docs"
    / "research"
    / "fixtures"
    / "privacy-act-1988-C2026C00227.excerpt.html"
)
INSTRUMENT_ID = "privacy-act-1988"
VERSION_ID = "C2026C00227"


@pytest.fixture(scope="module")
def fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _manifest(*prefixes: str) -> SelectionManifest:
    return SelectionManifest(
        author="jane.doe@example.com",
        reviewed_by="sam.smith@example.com",
        date="2026-09-24",
        prefixes=prefixes,
    )


async def _run(fixture_html: str, store: FakeClauseStore, selection: SelectionManifest):
    return await ingest_compilation(
        fixture_html,
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        tenant_id=PUBLIC_TENANT_ID,
        scope=Scope.PUBLIC,
        selection=selection,
        store=store,
    )


# --------------------------------------------------------------------------
# First run
# --------------------------------------------------------------------------


async def test_first_ingest_writes_every_in_scope_clause(fixture_html: str) -> None:
    store = FakeClauseStore()
    result = await _run(fixture_html, store, _manifest("s6c/"))
    assert result.first_ingest is True
    assert result.clauses_written > 0

    manifest = await store.get_manifest(INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID)
    assert set(manifest) == {
        "s6c/1",
        "s6c/1/a",
        "s6c/1/b",
        "s6c/1/c",
        "s6c/1/d",
        "s6c/1/e",
        "s6c/2",
        "s6c/3",
        "s6c/3/a",
        "s6c/3/b",
        "s6c/3/c",
        "s6c/3/c/i",
        "s6c/3/c/ii",
        "s6c/3/d",
        "s6c/3/d/i",
        "s6c/3/d/ii",
        "s6c/3/d/iii",
        "s6c/3/d/v",
        "s6c/3/e",
        "s6c/3/f",
        "s6c/3/f/i",
        "s6c/3/f/ii",
        "s6c/3/f/iii",
        "s6c/3/f/v",
        "s6c/3/g",
        "s6c/4",
        "s6c/4/a",
        "s6c/4/b",
        "s6c/4/b/i",
        "s6c/4/b/ii",
        "s6c/4/b/iii",
        "s6c/4/c",
        "s6c/5",
    }
    assert result.clauses_written == len(manifest)

    # sch1/ and s6/ clauses exist in the fixture but were out of scope.
    assert (
        await store.get_clause(
            stable_id(INSTRUMENT_ID, VERSION_ID, "sch1/app11/1"), tenant_id=PUBLIC_TENANT_ID
        )
        is None
    )


async def test_first_ingest_records_manifest_metadata(fixture_html: str) -> None:
    store = FakeClauseStore()
    await _run(fixture_html, store, _manifest("s6c/"))

    metadata = await store.get_manifest_metadata(
        INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID
    )
    assert metadata is not None
    assert metadata.canonicalisation_version == CANONICALISATION_VERSION

    stored_html = await store.get_source_html(INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID)
    assert stored_html is not None
    assert metadata.source_html_sha256 == hashlib.sha256(stored_html.encode("utf-8")).hexdigest()


async def test_a_clause_object_carries_its_own_canonical_sha256_consistently(
    fixture_html: str,
) -> None:
    """The manifest's hash for a path is exactly `canonical_sha256` of that
    clause's own `canonical_text` (issue #19: "confirm manifest.json's
    canonical_sha256 is over canonical text, not raw")."""
    store = FakeClauseStore()
    await _run(fixture_html, store, _manifest("s6c/"))
    manifest = await store.get_manifest(INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID)
    clause = await store.get_clause(
        stable_id(INSTRUMENT_ID, VERSION_ID, "s6c/1"), tenant_id=PUBLIC_TENANT_ID
    )
    assert clause is not None
    assert manifest["s6c/1"] == canonical_sha256(clause.canonical_text)


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


async def test_re_running_with_no_change_is_idempotent(fixture_html: str) -> None:
    store = FakeClauseStore()
    first = await _run(fixture_html, store, _manifest("s6c/"))
    second = await _run(fixture_html, store, _manifest("s6c/"))

    assert first.first_ingest is True
    assert second.first_ingest is False
    assert second.clauses_written == first.clauses_written


# --------------------------------------------------------------------------
# Drift: scope change
# --------------------------------------------------------------------------


async def test_a_wider_prefix_set_stops_the_run_and_writes_nothing(fixture_html: str) -> None:
    """The core auditability requirement: a re-ingest whose prefixes now
    expand to a different set than the stored manifest fails loudly and
    waits for a human, rather than silently absorbing the new clauses."""
    store = FakeClauseStore()
    await _run(fixture_html, store, _manifest("s6c/"))

    with pytest.raises(IngestDriftError, match="sch1/app11/1"):
        await _run(fixture_html, store, _manifest("s6c/", "sch1/"))

    # Nothing from the wider run landed.
    assert (
        await store.get_clause(
            stable_id(INSTRUMENT_ID, VERSION_ID, "sch1/app11/1"), tenant_id=PUBLIC_TENANT_ID
        )
        is None
    )
    manifest = await store.get_manifest(INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID)
    assert "sch1/app11/1" not in manifest


async def test_a_narrower_prefix_set_also_stops_the_run(fixture_html: str) -> None:
    """A prefix quietly *removed* is just as much a scope change as one
    added — both must stop and wait, not just growth."""
    store = FakeClauseStore()
    await _run(fixture_html, store, _manifest("s6c/", "sch1/"))

    with pytest.raises(IngestDriftError, match="sch1/app11/1"):
        await _run(fixture_html, store, _manifest("s6c/"))


# --------------------------------------------------------------------------
# Drift: content change on an already-stored clause
# --------------------------------------------------------------------------


async def test_changed_canonical_sha256_on_a_pinned_compilation_stops_the_run_and_writes_nothing(
    fixture_html: str,
) -> None:
    """A `version_id` names one pinned compilation (`CONTEXT.md`); its text
    must never change under an already-ingested clause path. Simulated here
    by seeding the store with a clause at an in-scope path whose stored hash
    doesn't match what today's real parse of the pinned compilation
    produces — exactly the state a corrupted or hand-edited prior write
    would leave, which is what this check exists to catch."""
    store = FakeClauseStore()
    stale = ClauseRecord.build(
        instrument_id=INSTRUMENT_ID,
        version_id=VERSION_ID,
        clause_path="s6c/1",
        text="this is not what the pinned compilation actually says",
    )
    await store.put_clause(stale)

    with pytest.raises(IngestDriftError, match="s6c/1"):
        await _run(fixture_html, store, _manifest("s6c/"))

    # The stale clause is untouched...
    unchanged = await store.get_clause(stale.clause_id, tenant_id=PUBLIC_TENANT_ID)
    assert unchanged == stale
    # ...and nothing else from this run landed either.
    sibling = await store.get_clause(
        stable_id(INSTRUMENT_ID, VERSION_ID, "s6c/1/a"), tenant_id=PUBLIC_TENANT_ID
    )
    assert sibling is None
    metadata = await store.get_manifest_metadata(
        INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID
    )
    assert metadata is None


# --------------------------------------------------------------------------
# Selection manifest itself
# --------------------------------------------------------------------------


async def test_a_prefix_matching_nothing_in_the_real_compilation_fails_before_any_store_call(
    fixture_html: str,
) -> None:
    store = FakeClauseStore()
    with pytest.raises(ValueError, match="s99/"):
        await _run(fixture_html, store, _manifest("s6c/", "s99/"))
    manifest = await store.get_manifest(INSTRUMENT_ID, VERSION_ID, tenant_id=PUBLIC_TENANT_ID)
    assert manifest == {}


# ---- an interrupted first ingest (issue #99) ---------------------------------


async def _interrupted(fixture_html: str, store: FakeClauseStore, n: int) -> None:
    """What the first real ingest left behind: the first `n` selected clauses
    written, `put_manifest` (the last step) never reached."""
    from compliance_rag.corpus.parser import parse_compilation

    parsed = parse_compilation(
        fixture_html, instrument_id="privacy-act-1988", version_id="C2026C00227"
    )
    for clause in _manifest("s6c/").select(parsed.clauses)[:n]:
        await store.put_clause(clause)


async def test_an_interrupted_first_ingest_resumes(fixture_html: str) -> None:
    store = FakeClauseStore()
    await _interrupted(fixture_html, store, 10)
    result = await _run(fixture_html, store, _manifest("s6c/"))
    assert result.first_ingest
    complete = FakeClauseStore()
    await _run(fixture_html, complete, _manifest("s6c/"))
    tenant = {"tenant_id": "_public"}
    assert await store.get_manifest("privacy-act-1988", "C2026C00227", **tenant) == (
        await complete.get_manifest("privacy-act-1988", "C2026C00227", **tenant)
    )


async def test_an_interrupted_ingest_from_another_selection_is_drift(fixture_html: str) -> None:
    """Resuming is only for the same selection: a stored clause this
    selection doesn't include means two runs disagree — a human looks."""
    store = FakeClauseStore()
    await _interrupted(fixture_html, store, 10)
    with pytest.raises(IngestDriftError, match="interrupted first ingest"):
        await _run(fixture_html, store, _manifest("s6/"))


async def test_an_interrupted_ingest_with_different_text_is_drift(fixture_html: str) -> None:
    store = FakeClauseStore()
    await _interrupted(fixture_html, store, 3)
    edited = fixture_html.replace("a body corporate; or", "a body politic; or", 1)
    assert edited != fixture_html
    with pytest.raises(IngestDriftError, match="changed_content"):
        await _run(edited, store, _manifest("s6c/"))
