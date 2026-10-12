"""Track A's evaluation (issue #115, ADR-0027, D-06): three-valued, with the
deciding clauses as the citation chain and every unknown leaf as a question.

On the invented `test-act` miniature (`tests/fixtures/mappings/
test-applicability.yaml`), through `FakeGraphStore` — the same snapshot shape
and evaluator `GraphTrackA` uses over Neo4j (`tests/contracts/
test_graph_store.py` runs both).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from compliance_rag.adapters.fakes import FakeGraphStore
from compliance_rag.assessment.coverage import Coverage, Outcome, Truth
from compliance_rag.assessment.coverage_report import cite, render
from compliance_rag.assessment.evidence import Evidence, EvidenceProvenance, Facts, Period
from compliance_rag.corpus.models import NO_END_DATE
from compliance_rag.graph.applicability import Snapshot, explain
from compliance_rag.graph.mappings import MappingSet, load_mapping_set
from compliance_rag.graph.track_a import snapshot_of_mappings

FIXTURE = Path(__file__).parent / "fixtures" / "mappings" / "test-applicability.yaml"
AS_OF = dt.date(2026, 10, 12)  # FY2026-27; s6D(1)'s "previous" year is FY2025-26
LAST_FY = Period.financial_year(2025)
OPEN = (dt.date(2026, 1, 1), NO_END_DATE)
CLAUSES = {"test-act:v1:s1/1": OPEN, "test-act:v1:s2/1": OPEN}


def mappings(
    document: dict[str, Any] | None = None, tmp_path: Path | None = None
) -> MappingSet:  # Any: YAML
    if document is None or tmp_path is None:
        return load_mapping_set([FIXTURE])
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_mapping_set([path])


def fixture_document() -> dict[str, Any]:  # Any: YAML
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


_ids = iter(range(10_000))


def fact(
    field: str, value: Any, period: Period | None = None, **kw: Any
) -> Evidence:  # Any: test values
    return Evidence(
        tenant_id="acme",
        evidence_id=f"e{next(_ids)}",
        field=field,
        value=value,
        period=period or Period(dt.date(2020, 1, 1)),
        provenance=kw.pop("provenance", EvidenceProvenance.ASSERTED),
        source="test",
        asserted_by="cfo@acme.example",
        **kw,
    )


def snapshot(source: MappingSet | None = None, as_of: dt.date = AS_OF) -> Snapshot:
    return snapshot_of_mappings(source or mappings(), as_of, clause_validity=CLAUSES)


async def assess(
    *evidence: Evidence, as_of: dt.date = AS_OF, store: FakeGraphStore | None = None
) -> Coverage:
    store = store or FakeGraphStore(mappings(), clause_validity=CLAUSES)
    return await store.coverage(Facts.of(evidence), tenant_id="acme", as_of=as_of)


STARTED = fact("business_started_on", dt.date(2023, 3, 1), Period(dt.date(2023, 3, 1)))
SMALL_HISTORY = (
    STARTED,
    *(fact("annual_turnover_aud", 900, Period.financial_year(y)) for y in (2022, 2023, 2024)),
)


# ---- the three outcomes -------------------------------------------------------


async def test_over_the_threshold_applies_with_its_citation_chain() -> None:
    coverage = await assess(fact("annual_turnover_aud", 4000, LAST_FY))
    keep = coverage.obligation("OB-T-KEEP")
    assert keep.outcome is Outcome.APPLICABLE
    # The chain: the obligation's clause, the actor's, and the deciding leaf's.
    assert keep.citations == ("test-act:v1:s1/1", "test-act:v1:s2/1")
    deciding = keep.explanation.find("COND-T-UNDER-THRESHOLD")
    assert deciding is not None and deciding.truth is Truth.FALSE
    # Short-circuit: once the threshold decides, no exception is asked about,
    # and the one reason given is the first decisive one.
    assert keep.questions == ()
    assert keep.explanation.find("COND-T-HEALTH") is None
    assert keep.explanation.find("COND-T-EVER-OVER") is None


async def test_under_the_threshold_with_nothing_else_known_is_possibly() -> None:
    coverage = await assess(fact("annual_turnover_aud", 900, LAST_FY))
    keep = coverage.obligation("OB-T-KEEP")
    assert keep.outcome is Outcome.UNASSESSABLE
    asked = {q.condition_id for q in keep.questions}
    assert asked == {"COND-T-EVER-OVER", "COND-T-HEALTH", "COND-T-OPTED-IN"}
    assert all(q.clause_ids == ("test-act:v1:s2/1",) for q in keep.questions)


async def test_every_question_answered_no_is_not_applicable_and_still_cited() -> None:
    coverage = await assess(
        fact("annual_turnover_aud", 900, LAST_FY),
        *SMALL_HISTORY,
        fact("provides_health_service", False),
        fact("opted_in", False),
    )
    keep = coverage.obligation("OB-T-KEEP")
    assert keep.outcome is Outcome.NOT_APPLICABLE
    assert keep.citations  # "it doesn't apply to you" is just as cited
    assert keep.questions == ()


async def test_opting_in_covers_a_small_keeper_whatever_else_is_unknown() -> None:
    coverage = await assess(fact("annual_turnover_aud", 900, LAST_FY), fact("opted_in", True))
    assert coverage.obligation("OB-T-KEEP").outcome is Outcome.APPLICABLE


# ---- leaves ----------------------------------------------------------------------


async def test_the_threshold_reads_the_previous_financial_year_only() -> None:
    """FY2026-27's figure says nothing about s6D(1)-style "previous year"."""
    coverage = await assess(fact("annual_turnover_aud", 4000, Period.financial_year(2026)))
    question = coverage.obligation("OB-T-KEEP").questions[0]
    assert question.condition_id == "COND-T-UNDER-THRESHOLD"
    assert question.period == "FY2025-26"


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ((*SMALL_HISTORY, fact("annual_turnover_aud", 900, LAST_FY)), Truth.FALSE),  # all known
        ((STARTED, fact("annual_turnover_aud", 1500, Period.financial_year(2023))), Truth.TRUE),
        ((STARTED, *SMALL_HISTORY[1:3]), Truth.UNKNOWN),  # FY2024-25 missing
        (SMALL_HISTORY[1:], Truth.UNKNOWN),  # start date unknown
    ],
)
async def test_any_year_since_it_started(history: tuple[Evidence, ...], expected: Truth) -> None:
    ever = explain(snapshot(), Facts.of(history), "COND-T-EVER-OVER")
    assert ever.truth is expected


async def test_a_missing_history_names_the_years_it_needs() -> None:
    coverage = await assess(fact("annual_turnover_aud", 900, LAST_FY), *SMALL_HISTORY[:3])
    (question,) = [
        q
        for q in coverage.obligation("OB-T-KEEP").questions
        if q.condition_id == "COND-T-EVER-OVER"
    ]
    assert question.missing_periods == ("FY2024-25",)


async def test_conflicting_evidence_is_unknown_and_says_so() -> None:
    coverage = await assess(
        fact("annual_turnover_aud", 900, LAST_FY), fact("annual_turnover_aud", 4000, LAST_FY)
    )
    question = coverage.obligation("OB-T-KEEP").questions[0]
    assert question.conflict and len(question.evidence_ids) == 2


async def test_an_inferred_claim_never_settles_a_leaf() -> None:
    claim = fact("annual_turnover_aud", 4000, LAST_FY, provenance=EvidenceProvenance.INFERRED)
    assert (await assess(claim)).obligation("OB-T-KEEP").outcome is Outcome.UNASSESSABLE


async def test_evidence_of_the_wrong_type_is_unknown_not_a_guess() -> None:
    coverage = await assess(fact("annual_turnover_aud", "about four thousand", LAST_FY))
    assert coverage.obligation("OB-T-KEEP").questions[0].conflict


# ---- obligations ----------------------------------------------------------------


async def test_a_required_condition_gates_one_obligation() -> None:
    covered = fact("annual_turnover_aud", 4000, LAST_FY)
    no_health = await assess(covered, fact("provides_health_service", False))
    assert no_health.obligation("OB-T-HEALTH").outcome is Outcome.NOT_APPLICABLE
    assert no_health.obligation("OB-T-KEEP").outcome is Outcome.APPLICABLE


@pytest.mark.parametrize(
    ("places", "expected"),
    [
        (("AU-NSW",), Outcome.APPLICABLE),
        (("NZ",), Outcome.NOT_APPLICABLE),
        (None, Outcome.UNASSESSABLE),
    ],
)
async def test_jurisdiction_narrows_an_obligation(
    places: tuple[str, ...] | None, expected: Outcome
) -> None:
    covered = fact("annual_turnover_aud", 4000, LAST_FY)
    evidence = (covered,) if places is None else (covered, fact("operates_in", places))
    assert (await assess(*evidence)).obligation("OB-T-KEEP-AU").outcome is expected


async def test_with_no_text_in_force_an_obligation_is_unassessable_not_applicable() -> None:
    """ADR-0028: before the earliest compilation loaded there is nothing to cite."""
    store = FakeGraphStore(
        mappings(), clause_validity=dict.fromkeys(CLAUSES, (dt.date(2026, 6, 1), NO_END_DATE))
    )
    coverage = await assess(
        fact("annual_turnover_aud", 4000, LAST_FY), as_of=dt.date(2026, 3, 1), store=store
    )
    keep = coverage.obligation("OB-T-KEEP")
    assert keep.outcome is Outcome.UNASSESSABLE and "No compilation" in keep.reason


# ---- operands over time (ADR-0027, Q1) --------------------------------------------


async def test_repealing_an_operand_is_one_retirement(tmp_path: Path) -> None:
    """The opt-in is repealed in July: from then it is false, and nothing above it changed id."""
    document = fixture_document()
    opted = next(c for c in document["conditions"] if c["id"] == "COND-T-OPTED-IN")
    opted |= {"valid_to": "2026-07-01", "end_reason": "repealed"}
    store = FakeGraphStore(mappings(document, tmp_path), clause_validity=CLAUSES)
    evidence = (
        fact("annual_turnover_aud", 900, Period.financial_year(2024)),
        *SMALL_HISTORY,
        fact("opted_in", True),
        fact("provides_health_service", False),
    )
    before = await assess(*evidence, as_of=dt.date(2026, 6, 1), store=store)
    after = await assess(fact("annual_turnover_aud", 900, LAST_FY), *evidence, store=store)
    assert before.obligation("OB-T-KEEP").outcome is Outcome.APPLICABLE
    assert after.obligation("OB-T-KEEP").outcome is Outcome.NOT_APPLICABLE
    repealed = after.obligation("OB-T-KEEP").explanation.find("COND-T-OPTED-IN")
    assert repealed is not None and repealed.absent


async def test_amending_an_operand_is_one_supersession(tmp_path: Path) -> None:
    """The threshold rises to 5000 in July: the successor stands in for the
    old leaf in the unchanged parent."""
    document = fixture_document()
    old = next(c for c in document["conditions"] if c["id"] == "COND-T-UNDER-THRESHOLD")
    old |= {"valid_to": "2026-07-01", "end_reason": "superseded"}
    document["conditions"].append(
        dict(
            old,
            id="COND-T-UNDER-THRESHOLD-2",
            valid_from="2026-07-01",
            valid_to=None,
            end_reason=None,
            supersedes="COND-T-UNDER-THRESHOLD",
            leaf=old["leaf"] | {"value": 5000},
        )
    )
    amended = mappings(document, tmp_path)
    facts = Facts.of([fact("annual_turnover_aud", 4000, LAST_FY)])
    before = explain(snapshot(amended, dt.date(2026, 6, 1)), facts, "COND-T-UNDER-THRESHOLD")
    after = explain(snapshot(amended), facts, "COND-T-UNDER-THRESHOLD")
    assert (before.condition_id, before.truth) == ("COND-T-UNDER-THRESHOLD", Truth.UNKNOWN)
    assert (after.condition_id, after.truth) == ("COND-T-UNDER-THRESHOLD-2", Truth.TRUE)


# ---- the report D-06 requires ----------------------------------------------------


async def test_the_report_lists_every_unassessable_obligation_with_clause_and_missing_fact() -> (
    None
):
    coverage = await assess(fact("annual_turnover_aud", 900, LAST_FY))
    report = render(coverage)
    for obligation in coverage.with_outcome(Outcome.UNASSESSABLE):
        assert obligation.obligation_id in report
        for question in obligation.questions:
            assert f"Missing: {question.unassessable_reason}" in report
    assert "## Questions that would settle it" in report
    assert "%" not in report and "score" not in report.lower()  # D-06: no scores


async def test_track_b_gets_the_clauses_of_what_applies_or_might_and_nothing_else() -> None:
    coverage = await assess(
        fact("annual_turnover_aud", 4000, LAST_FY), fact("provides_health_service", False)
    )
    assert coverage.obligation("OB-T-HEALTH").outcome is Outcome.NOT_APPLICABLE
    assert coverage.track_a_clause_ids == {"test-act:v1:s1/1", "test-act:v1:s2/1"}
    cited_only_by_health = set(coverage.obligation("OB-T-HEALTH").clause_ids) - {
        c
        for o in coverage.obligations
        if o.outcome is not Outcome.NOT_APPLICABLE
        for c in o.citations
    }
    assert not cited_only_by_health & coverage.track_a_clause_ids


def test_a_citation_reads_as_a_reader_cites_it() -> None:
    assert cite("privacy-act-1988:C2026C00227:s6d/4/b") == "s 6D(4)(b)"
    assert cite("privacy-act-1988:C2026C00227:sch1/app11/1") == "APP 11.1"


# ---- review of #125 ----------------------------------------------------------------


async def test_possibly_cites_the_conditions_already_met() -> None:
    """#93 Trace 2: "under the small-business threshold (s6D(1)), but…" — the
    met threshold is why the answer is possibly, not no, so it is cited."""
    coverage = await assess(fact("annual_turnover_aud", 900, LAST_FY))
    keep = coverage.obligation("OB-T-KEEP")
    met = keep.explanation.find("COND-T-UNDER-THRESHOLD")
    assert met is not None and met.truth is Truth.TRUE
    assert "COND-T-UNDER-THRESHOLD" not in {q.condition_id for q in keep.questions}


async def test_the_tenant_subgraph_is_projected_not_stored() -> None:
    coverage = await assess(
        fact("annual_turnover_aud", 900, LAST_FY),
        fact("operates_in", ("AU-NSW",)),
        fact("data_categories", ("health", "contact")),
        fact("regulators", ("OAIC",)),
    )
    subgraph = coverage.subgraph
    assert subgraph is not None
    assert subgraph.organisation == "acme"
    assert subgraph.operates_in == ("AU-NSW",)
    assert subgraph.handles == ("health", "contact")
    assert subgraph.regulated_by == ("OAIC",)
    assert subgraph.acts_as == (("test-act/actor/keeper", Truth.UNKNOWN),)  # might hold it


async def test_the_report_pairs_each_missing_fact_with_its_clause() -> None:
    report = render(await assess(fact("annual_turnover_aud", 900, LAST_FY)))
    assert "Missing: Nobody has said whether the business opted in. (s 2(1))" in report


async def test_an_operand_amended_twice_resolves_to_its_latest_version(tmp_path: Path) -> None:
    """A → B → C: on C's dates, A (the id the parent names) is C — never
    "absent" because B, the middle link, is no longer in force."""
    document = fixture_document()
    first = next(c for c in document["conditions"] if c["id"] == "COND-T-UNDER-THRESHOLD")
    first |= {"valid_to": "2026-04-01", "end_reason": "superseded"}
    second = dict(
        first,
        id="COND-T-UNDER-2",
        valid_from="2026-04-01",
        valid_to="2026-07-01",
        end_reason="superseded",
        supersedes="COND-T-UNDER-THRESHOLD",
        leaf=first["leaf"] | {"value": 2000},
    )
    third = dict(
        first,
        id="COND-T-UNDER-3",
        valid_from="2026-07-01",
        valid_to=None,
        end_reason=None,
        supersedes="COND-T-UNDER-2",
        leaf=first["leaf"] | {"value": 5000},
    )
    document["conditions"] += [second, third]
    facts = Facts.of([fact("annual_turnover_aud", 4000, LAST_FY)])
    resolved = explain(snapshot(mappings(document, tmp_path)), facts, "COND-T-UNDER-THRESHOLD")
    assert (resolved.condition_id, resolved.truth) == ("COND-T-UNDER-3", Truth.TRUE)


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        # Start unknown, but every year since not_before (2022-07-01) known and
        # under: the real floor is never earlier, so "no" is sound.
        ((*SMALL_HISTORY[1:], fact("annual_turnover_aud", 900, LAST_FY)), Truth.FALSE),
        # Started on the last day of FY2022-23: that year ended after it, so it counts.
        (
            (
                fact("business_started_on", dt.date(2023, 6, 30), Period(dt.date(2023, 6, 30))),
                fact("annual_turnover_aud", 1500, Period.financial_year(2022)),
            ),
            Truth.TRUE,
        ),
    ],
)
async def test_any_year_since_it_started_edges(
    history: tuple[Evidence, ...], expected: Truth
) -> None:
    assert explain(snapshot(), Facts.of(history), "COND-T-EVER-OVER").truth is expected


# ---- a commencement date as a fixture (#134, PHASES.md Phase 2 graph eval) -------


async def test_a_provision_loaded_ahead_of_commencement_applies_only_from_it(
    tmp_path: Path,
) -> None:
    """Tranche 1's pattern (items commencing 10 Dec 2026, ADR-0029): a new
    obligation and the compilation carrying its text are loaded before they
    commence. The same question on either side of that date gets different,
    correct obligation sets, and nothing else moves."""
    commencement = dt.date(2026, 12, 10)
    document = fixture_document()
    new_clause = "test-act:v2:s9/1"
    document["source_clause_ids"].append(new_clause)
    document["obligations"].append(
        {
            "id": "OB-T-AUTOMATED-DECISIONS",
            "clause_ids": [new_clause],
            "summary": "Say in the policy when decisions are made by computer.",
            "valid_from": commencement.isoformat(),
            "applies_to": {"actors": ["test-act/actor/keeper"]},
        }
    )
    store = FakeGraphStore(
        mappings(document, tmp_path),
        clause_validity={**CLAUSES, new_clause: (commencement, NO_END_DATE)},
    )
    evidence = (fact("annual_turnover_aud", 4000, LAST_FY),)
    before = await assess(*evidence, as_of=dt.date(2026, 12, 9), store=store)
    after = await assess(*evidence, as_of=commencement, store=store)
    new = "OB-T-AUTOMATED-DECISIONS"
    assert new not in {o.obligation_id for o in before.obligations}
    assert after.obligation(new).outcome is Outcome.APPLICABLE
    assert new_clause in after.obligation(new).citations  # the text that commenced with it
    unchanged = {o.obligation_id: o.outcome for o in before.obligations}
    assert len(unchanged) == 3
    assert {o.obligation_id: o.outcome for o in after.obligations if o.obligation_id != new} == (
        unchanged
    )
