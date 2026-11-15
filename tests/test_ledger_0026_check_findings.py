"""Ledger 0026 (#299, ADR-0053 F1-F6): `assessment.check_id`, the `finding`
columns for a check's findings (the claim by number, the outcome, `settle_by`,
`blocked`, `derived_from`) and `check_findings_run`. Needs a Postgres server
(`DATABASE_URL`); CI sets it and `REQUIRE_LEDGER_DB`.

The database refuses again what `check_findings_write` refuses first: a finding
with a verdict only when its outcome is one, `settle_by` for `ambiguous` and no
other verdict, a withheld finding with no verdict and no citations, conditional
exactly when not assessable, one target, and one finding per claim and target.
"""

from __future__ import annotations

import pathlib
from typing import Any, LiteralString, cast

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.ledger_db import needs_ledger_db, scratch_database

MIGRATIONS = pathlib.Path(__file__).parents[1] / "db" / "ledger"
CHECK = "c" * 32
CITATION = {
    "clause_id": "privacy-act-1988:C2026C00227:sch1/app8/1",
    "version_id": "C2026C00227",
    "quoted_text": "reasonable steps",
    "canonicalisation_version": 1,
}


def _sql(text: str) -> LiteralString:
    """A migration file this repository ships: never input."""
    return cast("LiteralString", text)


def _seed(scratch: psycopg.Connection[Any]) -> None:
    """A tenant, a checked document with two claims, and an assessment of it."""
    scratch.execute("INSERT INTO tenant (tenant_id, name) VALUES ('acme', 'Acme')")
    scratch.execute(
        "INSERT INTO instrument (tenant_id, instrument_id, title, jurisdiction, scope, "
        "document_class) VALUES ('acme', 'acme/check-1', 'T', 'AU', 'tenant', "
        "'submitted_document')"
    )
    scratch.execute(
        "INSERT INTO instrument_version (tenant_id, instrument_id, version_id, title, valid_from) "
        "VALUES ('acme', 'acme/check-1', 'v1', 'T', '2026-11-15')"
    )
    scratch.execute(
        "INSERT INTO submitted_check (tenant_id, check_id, user_id, instrument_id, version_id, "
        "filename, document_format, trace_id) VALUES ('acme', %s, 'uid-1', 'acme/check-1', 'v1', "
        "'p.md', 'markdown', %s)",
        (CHECK, "a" * 32),
    )
    scratch.execute(
        "INSERT INTO check_extraction (tenant_id, check_id, as_of, trace_id, status, "
        "completed_at) VALUES ('acme', %s, '2026-11-15', %s, 'done', now())",
        (CHECK, "a" * 32),
    )
    for index in (1, 2):
        scratch.execute(
            "INSERT INTO check_claim (tenant_id, check_id, claim_index, passage, quote_start, "
            "quote_end, quote_sha256, summary, obligation_ids, policy_clause_ids, provenance, "
            "trace_id) VALUES ('acme', %s, %s, 'p3', 10, 40, %s, 'A claim.', '{}', '{}', "
            "'inferred', %s)",
            (CHECK, index, "ab" * 32, "a" * 32),
        )


def _assessment(
    scratch: psycopg.Connection[Any],
    assessment_id: str,
    check_id: str | None,
    *,
    facts: Any = None,  # Any: the facts column's JSON, or its absence
    facts_sha256: str | None = None,
) -> None:
    """A check's assessment carries the facts it was decided on; a coverage assessment
    need not."""
    if check_id is not None and facts is None:
        facts, facts_sha256 = [], "c" * 64
    scratch.execute(
        "INSERT INTO assessment (tenant_id, assessment_id, user_id, corpus_version, corpus_sha, "
        "mapping_version, mapping_sha, as_of, trace_id, check_id, facts, facts_sha256) "
        "VALUES ('acme', %s, 'a@x', NULL, %s, NULL, %s, '2026-11-15', %s, %s, %s, %s)",
        (
            assessment_id,
            "a" * 40,
            "b" * 40,
            "a" * 32,
            check_id,
            None if facts is None else Jsonb(facts),
            facts_sha256,
        ),
    )


def _finding(scratch: psycopg.Connection[Any], **changes: Any) -> None:  # Any: column values
    values: dict[str, Any] = {  # Any: column values
        "finding_id": "f1",
        "assessment_id": "as1",
        "check_id": CHECK,
        "claim_index": 1,
        "obligation_id": "OB-PA-APP8-1",
        "policy_clause_id": None,
        "verdict": "not_addressed",
        "settle_by": None,
        "assessable": True,
        "unassessable_reason": None,
        "citations": Jsonb([CITATION]),
        "outcome": "verdict",
        "outcome_detail": None,
        "blocked": Jsonb([]),
        "derived_from": None,
        "claim_text": None,
        **changes,
    }
    scratch.execute(
        "INSERT INTO finding (tenant_id, finding_id, assessment_id, check_id, claim_index, "
        "obligation_id, policy_clause_id, verdict, provenance_tier, assessable, "
        "unassessable_reason, citations, settle_by, outcome, outcome_detail, blocked, "
        "derived_from, claim_text, trace_id) VALUES ('acme', %(finding_id)s, "
        "%(assessment_id)s, %(check_id)s, %(claim_index)s, %(obligation_id)s, "
        "%(policy_clause_id)s, %(verdict)s, 'inferred', %(assessable)s, %(unassessable_reason)s, "
        "%(citations)s, %(settle_by)s, %(outcome)s, %(outcome_detail)s, %(blocked)s, "
        "%(derived_from)s, %(claim_text)s, %(trace_id)s)",
        {**values, "trace_id": "a" * 32},
    )


@needs_ledger_db
def test_0026_adds_its_columns_idempotently_and_refuses_what_a_finding_must_not_be() -> None:
    files = sorted(MIGRATIONS.glob("*.sql"))
    migration = next(f for f in files if f.name.startswith("0026"))
    before = [f for f in files if f.name < migration.name]
    assert before[-1].name.startswith("0025")
    with scratch_database() as dsn, psycopg.connect(dsn, autocommit=True) as scratch:
        for earlier in before:
            scratch.execute(_sql(earlier.read_text()))
        _seed(scratch)
        # a finding written before this migration: it names its claim by its words
        scratch.execute(
            "INSERT INTO assessment (tenant_id, assessment_id, user_id, corpus_version, "
            "corpus_sha, mapping_version, mapping_sha, as_of, trace_id) VALUES ('acme', 'old', "
            "'a@x', NULL, %s, NULL, %s, '2026-11-15', %s)",
            ("a" * 40, "b" * 40, "a" * 32),
        )
        scratch.execute(
            "INSERT INTO finding (tenant_id, finding_id, assessment_id, claim_text, verdict, "
            "provenance_tier, citations, trace_id) VALUES ('acme', 'old-1', 'old', 'Kept 7 years.',"
            " 'consistent', 'parsed', %s, %s)",
            (Jsonb([{"clause_id": "a:v:1", "canonicalisation_version": 1}]), "a" * 32),
        )
        for _ in range(2):  # and again: idempotent
            scratch.execute(_sql(migration.read_text()))
        row = scratch.execute(
            "SELECT outcome, verdict, check_id, settle_by, blocked FROM finding "
            "WHERE finding_id = 'old-1'"
        ).fetchone()
        assert row == ("verdict", "consistent", None, None, [])  # untouched, and a verdict row

        # an assessment points at one check, and a check has one assessment
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _assessment(scratch, "ghost", "d" * 32)
        with pytest.raises(psycopg.errors.CheckViolation, match="assessment_check_id"):
            _assessment(scratch, "blank", " ")
        # a check's assessment records the facts it was decided on (ADR-0034): present,
        # an array, and hashed, the two together
        with pytest.raises(psycopg.errors.CheckViolation, match="assessment_facts"):
            scratch.execute(
                "INSERT INTO assessment (tenant_id, assessment_id, user_id, corpus_sha, "
                "mapping_sha, as_of, trace_id, check_id) VALUES ('acme', 'nofacts', 'a@x', %s, %s, "
                "'2026-11-15', %s, %s)",
                ("a" * 40, "b" * 40, "a" * 32, CHECK),
            )
        bad: list[tuple[Any, str | None]] = [
            ([], "xyz"),
            ([], "A" * 64),
            ({"a": 1}, "c" * 64),
            ([], None),
        ]
        for facts, sha in bad:
            with pytest.raises(psycopg.errors.CheckViolation, match="assessment_facts"):
                _assessment(scratch, "bad", CHECK, facts=facts, facts_sha256=sha)
        _assessment(scratch, "as1", CHECK)
        with pytest.raises(psycopg.errors.UniqueViolation, match="assessment_one_per_check"):
            _assessment(scratch, "as2", CHECK)
        _assessment(scratch, "other", None)  # a coverage assessment names no check, and any number

        # a finding of a check names a claim of its extraction, by number
        _finding(scratch)
        with pytest.raises(psycopg.errors.ForeignKeyViolation, match="finding_claim_fk"):
            _finding(scratch, finding_id="f9", claim_index=3)
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_names_its_claim"):
            _finding(scratch, finding_id="f9", claim_index=None)
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_names_its_claim"):
            _finding(scratch, finding_id="f9", check_id=None, claim_index=None)
        # one per claim and target; another claim, or another target, is another finding
        with pytest.raises(psycopg.errors.UniqueViolation, match="finding_one_per_claim"):
            _finding(scratch, finding_id="f2")
        _finding(scratch, finding_id="f3", claim_index=2)
        _finding(
            scratch,
            finding_id="f4",
            obligation_id=None,
            policy_clause_id="acme/privacy-policy:v1:3/1",
        )
        # one target, not both and not neither
        for obligation, policy in (("OB-PA-APP8-1", "acme/privacy-policy:v1:3/1"), (None, None)):
            with pytest.raises(psycopg.errors.CheckViolation, match="finding_one_target"):
                _finding(
                    scratch,
                    finding_id="f9",
                    claim_index=2,
                    obligation_id=obligation,
                    policy_clause_id=policy,
                )

        # a verdict only on a verdict row; the four verdicts and no others
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_verdict_iff_verdict"):
            _finding(scratch, finding_id="f9", claim_index=2, obligation_id="OB-X", verdict=None)
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_verdict_iff_verdict"):
            _finding(
                scratch,
                finding_id="f9",
                claim_index=2,
                obligation_id="OB-X",
                outcome="withheld",
                outcome_detail="citation_blocked",
                citations=Jsonb([]),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            _finding(scratch, finding_id="f9", claim_index=2, obligation_id="OB-X", verdict="fine")
        with pytest.raises(psycopg.errors.CheckViolation):  # (several constraints refuse it)
            _finding(scratch, finding_id="f9", claim_index=2, obligation_id="OB-X", outcome="lost")

        # F3: settle-by for `ambiguous` and for no other verdict, and one line
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_settle_by"):
            _finding(
                scratch, finding_id="f9", claim_index=2, obligation_id="OB-X", verdict="ambiguous"
            )
        for text in ("  ", "two\nlines"):
            with pytest.raises(psycopg.errors.CheckViolation, match="finding_settle_by"):
                _finding(
                    scratch,
                    finding_id="f9",
                    claim_index=2,
                    obligation_id="OB-X",
                    verdict="ambiguous",
                    settle_by=text,
                )
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_settle_by"):
            _finding(
                scratch,
                finding_id="f9",
                claim_index=2,
                obligation_id="OB-X",
                verdict="conflicts",
                settle_by="Something.",
            )
        _finding(
            scratch,
            finding_id="f5",
            claim_index=2,
            obligation_id="OB-X",
            verdict="ambiguous",
            settle_by="Whether the 30 days is reasonable.",
        )

        # a verdict row cites, and a row without a verdict cites nothing and says why
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_citations_iff_verdict"):
            _finding(
                scratch, finding_id="f9", claim_index=2, obligation_id="OB-Y", citations=Jsonb([])
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_citations_iff_verdict"):
            _finding(
                scratch,
                finding_id="f9",
                claim_index=2,
                obligation_id="OB-Y",
                verdict=None,
                outcome="withheld",
                outcome_detail="citation_blocked",
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_detail_iff"):
            _finding(
                scratch,
                finding_id="f9",
                claim_index=2,
                obligation_id="OB-Y",
                verdict=None,
                outcome="withheld",
                citations=Jsonb([]),
            )
        # a citation still carries its canonicalisation version (0003)
        with pytest.raises(psycopg.errors.CheckViolation, match="canonicalisation_version"):
            _finding(
                scratch,
                finding_id="f9",
                claim_index=2,
                obligation_id="OB-Y",
                citations=Jsonb([{"clause_id": "a:v:1"}]),
            )
        # a blocked clause is kept on a withheld row alone, never among the citations
        _finding(
            scratch,
            finding_id="f6",
            claim_index=2,
            obligation_id="OB-Y",
            verdict=None,
            outcome="withheld",
            outcome_detail="citation_blocked",
            citations=Jsonb([]),
            blocked=Jsonb([{"clause_id": "a:v:1", "outcome": "not_in_context"}]),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_blocked_only_withheld"):
            _finding(
                scratch,
                finding_id="f9",
                claim_index=2,
                obligation_id="OB-Z",
                blocked=Jsonb([{"clause_id": "a:v:1", "outcome": "not_in_context"}]),
            )

        # F4: conditional is the same fact as not assessable, with Track A's reason
        _finding(
            scratch,
            finding_id="f7",
            claim_index=2,
            obligation_id="OB-W",
            verdict=None,
            outcome="conditional",
            assessable=False,
            unassessable_reason="turnover is not known",
            citations=Jsonb([]),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="finding_conditional_iff"):
            _finding(
                scratch,
                finding_id="f9",
                claim_index=2,
                obligation_id="OB-V",
                assessable=False,
                unassessable_reason="x",
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            _finding(
                scratch,
                finding_id="f9",
                claim_index=2,
                obligation_id="OB-V",
                verdict=None,
                outcome="conditional",
                citations=Jsonb([]),
            )

        # F6: a derived finding names the finding it comes from, which must exist
        _finding(
            scratch,
            finding_id="f8",
            claim_index=2,
            obligation_id="OB-U",
            derived_from="f1",
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation, match="finding_derived_from_fk"):
            _finding(
                scratch,
                finding_id="f9",
                claim_index=2,
                obligation_id="OB-T",
                derived_from="nope",
            )

        # the rules above are a check's findings': a finding recorded without a check (as
        # before this migration) is held to the rules it was written under, not these
        _finding(
            scratch,
            finding_id="legacy",
            assessment_id="old",
            check_id=None,
            claim_index=None,
            claim_text="Kept 7 years.",
            obligation_id=None,
            policy_clause_id=None,
            settle_by="not a check's finding, so not held to settle-by",
            citations=Jsonb([{"clause_id": "a:v:1", "canonicalisation_version": 1}]),
        )

        # the reservation: one run per check, pending, done or failed, done exactly when timed
        scratch.execute(
            "INSERT INTO check_findings_run (tenant_id, check_id, trace_id) VALUES "
            "('acme', %s, %s)",
            (CHECK, "a" * 32),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            scratch.execute(
                "INSERT INTO check_findings_run (tenant_id, check_id, trace_id) VALUES "
                "('acme', %s, %s)",
                (CHECK, "a" * 32),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # no extraction, no run
            scratch.execute(
                "INSERT INTO check_findings_run (tenant_id, check_id, trace_id) VALUES "
                "('acme', %s, %s)",
                ("d" * 32, "a" * 32),
            )
        for status, completed in (("later", "NULL"), ("done", "NULL"), ("pending", "now()")):
            with pytest.raises(psycopg.errors.CheckViolation):
                scratch.execute(
                    _sql(
                        f"UPDATE check_findings_run SET status = '{status}', "
                        f"completed_at = {completed}"
                    )
                )
        with pytest.raises(psycopg.errors.CheckViolation, match="check_findings_run_trace_id"):
            scratch.execute("UPDATE check_findings_run SET trace_id = ' '")
