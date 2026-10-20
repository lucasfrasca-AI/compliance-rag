"""The golden set's out-of-environment cases (#252, ADR-0051): the loader,
the gate that refuses an unsigned file, the check the answer eval runs, and
the eval's row for one case through `answer()` with a fake generator (the
live run needs the model, whose quota is zero today).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from scripts.run_answer_eval import environment_row

from compliance_rag.eval.environment_cases import (
    EnvironmentCase,
    UnsignedEnvironmentCases,
    check_case,
    load_environment_cases,
    read_environment_cases,
)
from tests.test_not_in_environment import (
    APP7_2,
    APP7_8_B,
    CONFIDENT,
    SPAM_QUESTION,
    confident_spam_act_answer,
    spam_pipeline,
)

CASES = f"""\
author: "model: Claude Opus 5.5 — a test fixture"
reviewed_by: "Lucas Frasca"
date: "2026-10-21"
cases:
  - id: GE-01
    query: "{SPAM_QUESTION}"
    tenant_id: acme
    subscribes: [privacy-act-1988]
    as_of: 2026-10-21
    not_in_environment: ["Spam Act 2003"]
    expected_clause_ids: [{APP7_8_B}]
    cites_instead: "Privacy Act 1988"
"""


def _write(tmp_path: Path, text: str = CASES) -> Path:
    path = tmp_path / "not_in_environment.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _case(tmp_path: Path) -> EnvironmentCase:
    cases = load_environment_cases(_write(tmp_path))
    assert cases is not None
    (case,) = cases.cases
    return case


def test_the_committed_cases_are_signed() -> None:
    """CI's gate: what the answer eval refuses, CI refuses. Red while a
    drafted file awaits the human's signature; green with none at all."""
    cases = read_environment_cases()
    assert cases is None or cases.is_signed_off, "not_in_environment.yaml awaits review (D-01)"
    if cases is not None:
        assert load_environment_cases() == cases


def test_a_pending_file_is_refused_and_none_is_none(tmp_path: Path) -> None:
    assert load_environment_cases(tmp_path / "absent.yaml") is None
    pending = CASES.replace('"Lucas Frasca"', '"PENDING — Lucas Frasca"')
    with pytest.raises(UnsignedEnvironmentCases, match="awaiting review"):
        load_environment_cases(_write(tmp_path, pending))
    read = read_environment_cases(_write(tmp_path, pending))
    assert read is not None and not read.is_signed_off


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (("subscribes: [privacy-act-1988]", "subscribes: []"), "not in 'acme'"),
        (('reviewed_by: "Lucas Frasca"', 'reviewed_by: "model: Claude"'), "never a model"),
        (("id: GE-01", "id: GQ-01"), "GE-"),
        (('not_in_environment: ["Spam Act 2003"]', "not_in_environment: []"), "at least 1"),
        (("subscribes: [privacy-act-1988]", "subscribes: [acme/policy]"), "library"),
    ],
)
def test_a_malformed_case_is_refused(tmp_path: Path, change: tuple[str, str], why: str) -> None:
    with pytest.raises(ValueError, match=why):
        load_environment_cases(_write(tmp_path, CASES.replace(*change)))


def _check(case: EnvironmentCase, answer: str | None, **changes: object) -> dict[str, bool]:
    fields: dict[str, object] = {
        "flagged": ["Spam Act 2003"],
        "named": ["spam act"],
        "delivered": [APP7_8_B, APP7_2],
        "answer": answer,
        "cited": [APP7_2],
        **changes,
    }
    result = check_case(case, **fields)  # pyright: ignore[reportArgumentType]
    return {"acceptable": result.acceptable, "says": result.says_not_in_environment}


def test_either_accepted_answer_passes_and_the_confident_one_fails(tmp_path: Path) -> None:
    case = _case(tmp_path)
    says = "The Spam Act 2003 isn't in this environment, so I can't say what it requires."
    from_held = (
        "The Spam Act is not something I can answer from. The Privacy Act 1988's APP 7.2 "
        "permits direct marketing where an opt-out is offered."
    )
    names_held = "Answering from the Privacy Act 1988 only: APP 7.2 allows it with an opt-out."
    assert _check(case, says) == {"acceptable": True, "says": True}
    assert _check(case, from_held)["acceptable"]
    assert _check(case, names_held) == {"acceptable": True, "says": False}
    # The failure the human named: a confident Spam Act answer from APP 7 text.
    assert _check(case, CONFIDENT) == {"acceptable": False, "says": False}
    # Naming the Privacy Act doesn't save an answer that speaks as the Spam Act.
    speaks_as = "Under the Spam Act you need consent; see APP 7 of the Privacy Act 1988."
    assert not _check(case, speaks_as)["acceptable"]
    declines = "I can't say what the Spam Act requires. The Privacy Act 1988's APP 7.2 permits it."
    assert _check(case, declines)["acceptable"]
    # The flag and the label are required whatever the answer says.
    assert not _check(case, says, flagged=[None])["acceptable"]
    assert not _check(case, says, delivered=[APP7_2])["acceptable"]
    # Answering from held law only counts if every citation is of held law.
    assert not _check(case, names_held, cited=["spam-act-2003:C1:s16/1"])["acceptable"]
    # Withheld, with no text, only the flag is said.
    assert not _check(case, None, cited=[])["acceptable"]


async def test_the_eval_row_subscribes_answers_and_checks_one_case(tmp_path: Path) -> None:
    case = _case(tmp_path)
    pipeline, _ = await spam_pipeline(confident_spam_act_answer())
    await pipeline.ledger.unsubscribe(tenant_id="acme", instrument_id="privacy-act-1988")
    row = await environment_row(pipeline, case)
    assert await pipeline.ledger.subscribed_instruments("acme") == {"privacy-act-1988"}
    assert row["id"] == "GE-01" and row["verified"] is True
    assert row["not_in_environment"] == [{"named": "spam act", "known_as": None, "part_of": None}]
    assert APP7_8_B in row["delivered"]
    # Without the signed register the flag has no `known_as`, and the fake's
    # answer is the confident one, so the case fails on both counts.
    assert row["check"] == {
        "flagged": False,
        "delivered": True,
        "says_not_in_environment": False,
        "names_held_instrument": False,
        "cites_only_held": True,
        "speaks_as_named": True,
        "acceptable": False,
    }
    json.dumps(row)  # what the script records
    assert dt.date.fromisoformat(row["as_of"]) == case.as_of
