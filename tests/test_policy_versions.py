"""What a policy version is, said once (#303): status, the next id, the version a new one
replaces, and the lifecycle's two additions for an admin's upload (a backdate reason on the
ledger's record, and the whole-file residency gate)."""

from __future__ import annotations

import datetime as dt

import pytest

from compliance_rag import telemetry
from compliance_rag.adapters.corpus_lifecycle import CorpusLifecycle
from compliance_rag.adapters.fakes import FakeClauseStore, FakeLedgerStore, FakeRetrievalStore
from compliance_rag.adapters.generation_residency import GenerationResidencyError
from compliance_rag.adapters.interfaces import DocumentClass, PolicyVersionRow, Profile
from compliance_rag.corpus.models import ClauseRecord
from compliance_rag.corpus.policy_parser import parse_policy_markdown
from compliance_rag.corpus.policy_versions import (
    VersionStatus,
    group_policies,
    next_version_id,
    superseded_by_new,
    version_status,
)
from compliance_rag.corpus.reranker_tokenizer import count_reranker_tokens
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.settings import BUILD_PHASE_OFFSHORE_REGION, REGION
from tests.test_api_admin_policies import PRIVACY, THE_TEXT

D = dt.date
TODAY = D(2026, 11, 23)
JAN = D(2026, 1, 1)


def row(
    version: str = "v1",
    *,
    start: dt.date = JAN,
    end: dt.date | None = None,
    withdrawn: bool = False,
    purged: bool = False,
    instrument: str = "acme/p",
    title: str = "P",
) -> PolicyVersionRow:
    return PolicyVersionRow(
        instrument_id=instrument,
        version_id=version,
        title=title,
        valid_from=start,
        valid_to=end,
        withdrawn=withdrawn,
        purged=purged,
        backdate_reason=None,
    )


@pytest.mark.parametrize(
    ("version", "status"),
    [
        (row(), VersionStatus.IN_FORCE),
        (row(start=TODAY), VersionStatus.IN_FORCE),
        (row(start=TODAY + dt.timedelta(days=1)), VersionStatus.FUTURE),
        (row(end=TODAY + dt.timedelta(days=1)), VersionStatus.IN_FORCE),  # ends tomorrow
        (row(end=TODAY), VersionStatus.SUPERSEDED),  # valid_to is exclusive
        (row(end=TODAY - dt.timedelta(days=1)), VersionStatus.SUPERSEDED),
        (row(withdrawn=True), VersionStatus.WITHDRAWN),
        (row(withdrawn=True, end=D(2026, 2, 1)), VersionStatus.WITHDRAWN),
        (row(purged=True), VersionStatus.PURGED),
        (row(purged=True, withdrawn=True), VersionStatus.PURGED),  # purged outranks withdrawn
        (row(start=TODAY + dt.timedelta(days=9), withdrawn=True), VersionStatus.WITHDRAWN),
    ],
)
def test_a_versions_status_is_derived_from_what_the_ledger_records(
    version: PolicyVersionRow, status: VersionStatus
) -> None:
    assert version_status(version, TODAY) is status


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        ([], "v1"),
        (["v1"], "v2"),
        (["v1", "v2", "v3"], "v4"),
        (["v1", "v3"], "v4"),  # the highest, not the first gap: an id is not reused
        (["v9", "v10"], "v11"),  # numerically, not as text
        (["C2026C00227"], "v1"),  # another name is stepped over
        (["v1", "c1", "version-2", "v02"], "v3"),  # v02 counts as 2
        (["v1", "V2"], "v2"),
    ],
)
def test_the_next_version_id_is_one_past_the_highest(existing: list[str], expected: str) -> None:
    assert next_version_id(existing) == expected


def test_a_new_version_replaces_the_open_ended_one_that_is_not_removed() -> None:
    assert superseded_by_new([]) is None
    v1, v2 = row("v1", end=D(2026, 6, 1)), row("v2", start=D(2026, 6, 1))
    assert superseded_by_new([v1, v2]) == v2
    assert superseded_by_new([v1, row("v2", start=D(2026, 6, 1), withdrawn=True)]) is None
    assert superseded_by_new([v1, row("v2", start=D(2026, 6, 1), purged=True)]) is None
    # a future-dated one that has not ended is the latest: the new one replaces that
    future = row("v3", start=D(2027, 1, 1))
    assert superseded_by_new([row("v1", end=D(2027, 1, 1)), future]) == future
    # two open-ended ones (never made by the routes): the later date, then the later id
    later = superseded_by_new([row("v1"), row("v2", start=D(2026, 3, 1))])
    assert later is not None and later.version_id == "v2"


def test_policies_are_grouped_by_instrument_and_never_by_title() -> None:
    rows = [
        row("v1", instrument="acme/a", title="Same"),
        row("v1", instrument="acme/b", title="Same"),
        row("v2", instrument="acme/a", start=D(2026, 6, 1), title="Same, revised"),
    ]
    groups = group_policies(rows)
    assert [(g.instrument_id, [v.version_id for v in g.versions]) for g in groups] == [
        ("acme/b", ["v1"]),  # "Same" sorts before "Same, revised"
        ("acme/a", ["v1", "v2"]),
    ]
    assert [g.slug for g in groups] == ["b", "a"]
    assert groups[1].title == "Same, revised"  # the latest version's title, as recorded


def test_a_group_with_every_version_purged_still_has_a_title() -> None:
    (group,) = group_policies([row(purged=True, title="Gone")])
    assert group.title == "Gone"


# ---- the lifecycle's two additions -----------------------------------------------------------


def lifecycle(region: str) -> tuple[CorpusLifecycle, FakeLedgerStore, FakeRetrievalStore]:
    ledger = FakeLedgerStore()
    retrieval = FakeRetrievalStore(ledger=ledger)
    return (
        CorpusLifecycle(
            ledger=ledger,
            retrieval=retrieval,
            clauses=FakeClauseStore(),
            generation_region=region,
            synthetic_registry=SyntheticRegistry.load(),
        ),
        ledger,
        retrieval,
    )


def clauses_of(text: str) -> tuple[ClauseRecord, ...]:
    return parse_policy_markdown(
        text, instrument_id="acme/privacy-policy", version_id="v1", tenant_id="acme"
    )


async def publish(
    subject: CorpusLifecycle,
    text: str,
    *,
    backdate_reason: str | None = None,
    uploaded_bytes: bytes | None = None,
) -> None:
    await subject.publish_version(
        clauses_of(text),
        count_tokens=count_reranker_tokens,
        source_html=text,
        instrument_id="acme/privacy-policy",
        tenant_id="acme",
        version_id="v1",
        effective_from=D(2026, 1, 1),
        supersedes=None,
        document_class=DocumentClass.INTERNAL_POLICY,
        title="Privacy Policy",
        backdate_reason=backdate_reason,
        uploaded_bytes=uploaded_bytes,
    )


async def test_the_lifecycle_records_a_backdate_reason_with_the_version() -> None:
    subject, ledger, _ = lifecycle(REGION)
    await ledger.put_profile(Profile("acme"))
    await publish(subject, THE_TEXT, backdate_reason="approved by the board in January")
    (version,) = await ledger.policy_version_rows("acme")
    assert version.backdate_reason == "approved by the board in January"
    # a publish that gives none (a seed, an operator script) records none
    other, ledger2, _ = lifecycle(REGION)
    await ledger2.put_profile(Profile("acme"))
    await publish(other, THE_TEXT)
    assert (await ledger2.policy_version_rows("acme"))[0].backdate_reason is None


async def test_offshore_the_whole_uploaded_file_must_be_a_registered_policy() -> None:
    subject, ledger, retrieval = lifecycle(BUILD_PHASE_OFFSHORE_REGION)
    await ledger.put_profile(Profile("acme"))
    source = PRIVACY.read_text("utf-8")
    raw = source.encode("utf-8")
    # clauses that are all registered, from a file that is not the registered file
    excerpt = "\n\n".join(source.split("\n\n")[:-1]) + "\n"
    with pytest.raises(GenerationResidencyError, match="byte for byte"):
        await publish(subject, excerpt, uploaded_bytes=excerpt.encode("utf-8"))
    with pytest.raises(GenerationResidencyError, match="byte for byte"):
        await publish(subject, source, uploaded_bytes=raw + b"\n")
    assert not await ledger.policy_version_rows("acme") and not retrieval._chunks  # pyright: ignore[reportPrivateUsage]
    await publish(subject, source, uploaded_bytes=raw)
    assert len(await ledger.policy_version_rows("acme")) == 1
    # without the bytes (a script holding parsed clauses) the chunk and clause rule is all there is
    other, _, _ = lifecycle(BUILD_PHASE_OFFSHORE_REGION)
    await publish(other, source)
    with pytest.raises(GenerationResidencyError, match="real customer data"):
        await publish(other, THE_TEXT.replace("seven", "eight"))


async def test_the_whole_file_gate_checks_before_anything_is_written_and_is_free_in_sydney() -> (
    None
):
    sydney, ledger, retrieval = lifecycle(REGION)
    await publish(sydney, THE_TEXT, uploaded_bytes=b"whatever the bytes are")
    assert len(await ledger.policy_version_rows("acme")) == 1
    offshore, ledger2, retrieval2 = lifecycle(BUILD_PHASE_OFFSHORE_REGION)
    await offshore.check_publishable(
        clauses_of(PRIVACY.read_text("utf-8")),
        count_tokens=count_reranker_tokens,
        instrument_id="acme/privacy-policy",
        tenant_id="acme",
        version_id="v1",
        document_class=DocumentClass.INTERNAL_POLICY,
        title="Privacy Policy",
        uploaded_bytes=PRIVACY.read_bytes(),
    )
    with pytest.raises(GenerationResidencyError):
        await offshore.check_publishable(
            clauses_of(PRIVACY.read_text("utf-8")),
            count_tokens=count_reranker_tokens,
            instrument_id="acme/privacy-policy",
            tenant_id="acme",
            version_id="v1",
            document_class=DocumentClass.INTERNAL_POLICY,
            title="Privacy Policy",
            uploaded_bytes=b"not the file",
        )
    assert not await ledger2.policy_version_rows("acme") and not retrieval2._chunks  # pyright: ignore[reportPrivateUsage]
    assert retrieval._chunks  # pyright: ignore[reportPrivateUsage]


async def test_a_removal_audit_row_carries_the_trace_it_was_made_in() -> None:
    subject, ledger, _ = lifecycle(REGION)
    await ledger.put_profile(Profile("acme"))
    await publish(subject, THE_TEXT)
    with telemetry.span("test.withdraw"):
        trace_id = telemetry.current_trace_id()
        await subject.withdraw(
            tenant_id="acme",
            instrument_id="acme/privacy-policy",
            version_id="v1",
            actor="a@acme.example",
            reason="retired",
        )
    outside = await ledger.removal_audit("acme")
    assert [e.trace_id for e in outside] == [trace_id, trace_id]  # authorized, completed
    # outside any span there is no trace to store
    await subject.purge(
        tenant_id="acme",
        instrument_id="acme/privacy-policy",
        version_id="v1",
        actor="a@acme.example",
        reason="gone",
        acknowledged_findings_citing=0,
    )
    assert [e.trace_id for e in (await ledger.removal_audit("acme"))[2:]] == [None, None]
