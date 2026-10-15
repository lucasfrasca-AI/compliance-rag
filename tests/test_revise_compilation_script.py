"""`scripts/revise_compilation.py` (#178 part 2): what it prints, what it
refuses, and that a dry run writes nothing — on the fakes, with no cloud
call. The script itself never runs in CI against a real store."""

from __future__ import annotations

import argparse
from typing import Any

import pytest
from scripts import revise_compilation
from scripts.revise_compilation import (
    acknowledgement_line,
    parse_arguments,
    render_plan,
    render_status,
)

from compliance_rag.adapters.corpus_revision import RevisionStatus
from compliance_rag.adapters.fakes import FakeLedgerStore
from compliance_rag.adapters.postgres_ledger_store import LedgerUnreachable
from compliance_rag.corpus.revision import RevisedClause, RevisionMode, RevisionRecord
from compliance_rag.settings import Settings
from tests.test_corpus_revision import CHANGED, corrected_clauses, deployed_clauses, plan_against


def _settings() -> Settings:
    return Settings(_env_file=None)  # pyright: ignore[reportCallIssue] — pydantic-settings init kwarg


def test_the_dry_run_table_shows_each_changed_clause_and_where_it_changes() -> None:
    plan = plan_against(deployed_clauses(), corrected_clauses())
    table = render_plan(plan)
    assert "7 changed, 0 added, 0 removed, 25 unchanged" in table
    assert "anchored source.html identical to the stored one" in table
    row = next(line for line in table.splitlines() if line.startswith(f"{min(CHANGED)} "))
    assert "In this Act, unless the contrary intention appears: but does" in row
    record_d = next(line for line in table.splitlines() if "s6/1/record/d" in line)
    assert "| a generally available publication; or" in record_d
    assert "| but does not include: a generally available publication; or" in record_d
    full = render_plan(plan, show_text=True)
    assert (
        "  new: credit reporting body means: that carries on a credit reporting business." in full
    )


def test_the_dry_run_prints_the_exact_acknowledgement_to_apply_it() -> None:
    plan = plan_against(deployed_clauses(), corrected_clauses())
    line = acknowledgement_line(plan, 0)
    assert line.startswith("--apply --mode correct --acknowledge ")
    assert line.endswith("--by NAME --reason WHY --acknowledge-findings 0")
    named = line.split("--acknowledge ")[1].split(" --by")[0].split()
    assert frozenset(named) == CHANGED


def test_an_unfinished_revision_is_reported_with_what_is_behind_and_how_to_finish() -> None:
    record = RevisionRecord(
        revision_id="rev-1",
        mode=RevisionMode.CORRECT,
        instrument_id="privacy-act-1988",
        version_id="C2026C00227",
        supersedes=None,
        at="2026-10-15T09:00:00+00:00",
        by="Piero Frasca",
        reason="#178",
        source_sha256="s",
        corpus_sha="b" * 40,
        corpus_tag=None,
        findings_citing=0,
        clauses=(RevisedClause("s6/1", "a", "b"),),
    )
    text = render_status(
        RevisionStatus(
            record=record,
            graph_behind=("privacy-act-1988:C2026C00227:s6/1",),
            graph_holds_version=True,
            retrieval_holds_version=True,
            corpus_load_open=True,
        )
    )
    assert "UNFINISHED" in text and "a corpus load is OPEN" in text
    assert "graph nodes behind:    ['privacy-act-1988:C2026C00227:s6/1']" in text
    assert "finish it with --apply --finish" in text
    assert "no revision on record" in render_status(RevisionStatus(record=None))


@pytest.mark.parametrize(
    "argv",
    [
        "--bucket b --apply --mode correct --by P --reason R",
        "--bucket b --apply --mode correct --acknowledge x --by P",
        "--bucket b --apply --mode correct --acknowledge x --by P --reason R",
        "--bucket b --apply --finish --acknowledge x",
        "--bucket b --apply --finish --mode correct",
    ],
)
def test_an_incomplete_or_mixed_command_line_is_refused(argv: str) -> None:
    with pytest.raises(SystemExit):
        parse_arguments(argv.split())


def test_a_complete_correction_and_a_finish_parse() -> None:
    command = "--bucket b --apply --mode correct --acknowledge x y --by P --reason R"
    correct = parse_arguments([*command.split(), "--acknowledge-findings", "0"])
    assert (correct.acknowledge, correct.acknowledge_findings) == (["x", "y"], 0)
    assert parse_arguments(["--bucket", "b", "--apply", "--finish"]).finish


async def test_amend_points_at_phase_4_and_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def touched(*_: object, **__: object) -> None:
        raise AssertionError("a store or the source was read")

    monkeypatch.setattr(revise_compilation, "Settings", _settings)
    monkeypatch.setattr(revise_compilation, "pinned_source", touched)
    arguments = parse_arguments(["--bucket", "b", "--mode", "amend"])
    with pytest.raises(SystemExit, match=f"Phase 4, {revise_compilation.AMEND_ISSUE}"):
        await revise_compilation.main(arguments)


async def test_no_ledger_is_refused_when_the_ledger_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Answers:
        location = "localhost:5432/compliance_rag"

        @classmethod
        def from_settings(cls, settings: Settings) -> Any:  # Any: a stand-in ledger
            return cls()

        async def ping(self) -> None:
            return None

    monkeypatch.setattr(revise_compilation, "PostgresLedgerStore", Answers)
    with pytest.raises(SystemExit, match="answers: its findings are the count"):
        await revise_compilation._ledger(_settings(), no_ledger=True)  # pyright: ignore[reportPrivateUsage]


async def test_an_unreachable_ledger_refuses_unless_no_ledger_says_none_is_deployed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Unreachable:
        @classmethod
        def from_settings(cls, settings: Settings) -> Any:  # Any: a stand-in ledger
            return cls()

        async def ping(self) -> None:
            raise LedgerUnreachable("the ledger at localhost:5432/compliance_rag can't be reached")

    monkeypatch.setattr(revise_compilation, "PostgresLedgerStore", Unreachable)
    with pytest.raises(SystemExit, match="pass --no-ledger"):
        await revise_compilation._ledger(_settings(), no_ledger=False)  # pyright: ignore[reportPrivateUsage]
    ledger, where = await revise_compilation._ledger(_settings(), no_ledger=True)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(ledger, FakeLedgerStore) and "#174" in where


async def test_an_apply_from_an_uncommitted_checkout_is_refused_before_anything_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from compliance_rag.adapters.git_source_versions import UncommittedSource

    def uncommitted(*_: object, **__: object) -> None:
        raise UncommittedSource("src/compliance_rag/corpus is edited: no SHA could name it")

    def touched(*_: object, **__: object) -> None:
        raise AssertionError("the source or a store was read")

    monkeypatch.setattr(revise_compilation, "Settings", _settings)
    monkeypatch.setattr(revise_compilation, "resolve_source_version", uncommitted)
    monkeypatch.setattr(revise_compilation, "pinned_source", touched)
    arguments = argparse.Namespace(
        mode=None, apply=True, finish=True, html=None, bucket="b", no_ledger=True, show_text=False
    )
    with pytest.raises(SystemExit, match="no SHA could name it"):
        await revise_compilation.main(arguments)
