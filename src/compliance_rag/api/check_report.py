"""The findings report of a check (#300, ADR-0053 F7, F8, I2 and I3, ADR-0059 Q6 and
the 2026-11-22 amendment): `GET /v1/checks/{id}` and the page `/checks/{id}` (in
`check_pages.py`) both draw from `read_report`, so the two cannot differ.

**Read from the ledger, never computed.** Nothing here calls a model, a retriever
or the graph. The report is the check's stored document, its recorded claims
(`check_claims.claims_json`) and its recorded findings (`check_findings.findings_json`),
arranged: the same bytes after a reload, because they are the same rows.

**Three states**, by what the ledger holds, and the one place the API decides it:
`new` (no claims: the next step is to extract them), `extracted` (claims, no findings:
the next step is to make the findings) and `reported`. **A run in progress** is read from
the ledger's reservations (`LedgerStore.check_run`): `running` names the stage reserved
and still within its time-out (`claims` or `findings`), `failed` the stage whose last run
failed or was abandoned, so a page loaded mid-run shows the stage line and no "Run check"
button, and `static/check_run.js` polls `GET /v1/checks/{id}/run` until the run ends, then
reloads. A failed run says so and offers the run again, as the reservation allows.

**Order (F8).** Verdicts in the order `conflicts`, `not_addressed`, `ambiguous`,
`consistent`, then claim order, and no severity score or count of any kind that ranks
(D-06). The other kinds of finding have no verdict, so they have no place in that order
and sit **under their own headings after the four**, each in claim order:
1. **conditional** (ADR-0059 amendment): Track A can't decide the obligation; the
   questions that would decide it, a link to the questionnaire and the way to check
   again;
2. **withheld** (ADR-0003, ADR-0053 I3): a citation blocked or none given. Its verdict
   was never stored, so it cannot be placed in a verdict's group (placing it would say
   which verdict class the model chose); shown as withheld with our word for why and
   the clauses that blocked it, never as an error;
3. **failed**: no finding could be made, with our word for why.
A claim with no finding recorded against it is listed on its own, so its silence is
never read as compliance; the ledger holds its picks, so it says when there were none.

**The banner (Q6)** names the policies that disagree about a claim: one policy's
finding is `conflicts` and another policy's is `consistent` or `ambiguous`
(`policy_disagreements`, in the payload). `not_addressed` is silence, not disagreement.
Policies are named by title and version.

**The coverage statement (F7)**: the instruments and versions Track A assessed, the
policy versions the run sent (recorded with the assessment, ledger 0027, ADR-0034: exactly
those, with the titles recorded then), `as_of`, the corpus and mapping versions with their
SHAs, the facts the assessment read (their hash), what was not checked and that obligations
the model did not pick are not reported. An assessment recorded before the policy versions
were kept shows the policies its findings name and says so.

**Citations** open in the Sources panel (`_sources_panel.html`, shared with the chat page,
`source_panel`): one card per cited clause with
its canonical text (D-02), never the model's quote alone, and a link that opens the
clause in its source. A claim opens the document at its passage (`#clause-pN`, the
anchors `checks.passage_json` and the source route share) where its quote is marked.

**What members decided (#301, ADR-0059 Q3 and C1).** The report is still a pure read: it also
reads the check's decisions (`LedgerStore.check_decisions`, **one query**, not one per claim)
and applies them (`assessment/decisions.py`). Each claim carries its state (`confirmed`,
`rejected`, who and when) and its tier now; each finding its tier, **computed when read** as
the weakest of its claim's (`asserted` once confirmed) and its verdict's (`asserted` only when
a person accepted it): the stored claim and finding stay `inferred`. A **rejected claim's
findings leave the report**: out of the verdict groups, the conditional, withheld and failed
lists, the banner and the sources panel, and the claim is listed under `rejected_claims` with
who rejected it and how many findings were left out. Latest wins, so confirming it again
brings them back. The decisions are made by `POST /v1/checks/{id}/decisions`
(`check_decisions.py`); the page's buttons send them (`static/check_decisions.js`).

**Derived findings and the re-check (#306, ADR-0053 F6, ADR-0059 Q7 and C1).** A finding an
approved `implements` mapping derived (`assessment/mapping_derivation.py`) is shown among the
verdicts like any other, marked as derived, with the policy clause it comes from, the approved
mapping and who approved it, and cited to the policy clause and to the obligation. **The
model's own finding for the same claim and obligation is never dropped**: it is shown beneath
the derived one, "the model's direct reading disagrees" (or "agrees") with it. Its tier is the
weakest of its claim, the policy verdict and the mapping, computed when read
(`Decisions.finding_tier`); the mapping is `asserted` by the approval, and that raises nothing.
A derived finding's verdict is the policy finding's, so it is accepted through that finding.

**An issued report never changes** (ADR-0034). A report made before a mapping was approved
says "a mapping approved since would change this", naming each mapping and the claims it
touches, with a button that makes a new check of the same document (`check_recheck.py`). It is
computed from the recorded findings and the ledger's approved mappings by the rule that derives
them (`plan_derivations`), so it says exactly what a new check would add and never anything the
report already holds. It reads the ledger only.

**Everything is data.** A claim's quote and summary, and an ambiguous finding's
`settle_by`, are text of the document or of a model: they reach the page autoescaped,
never as markup, and `settle_by` is labelled as the model's.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import Any, Final, cast

from fastapi import APIRouter, HTTPException, Request

from compliance_rag import telemetry
from compliance_rag.adapters.interfaces import (
    CheckRun,
    ClaimExtractionRecord,
    FindingOutcome,
    FindingRecord,
    FindingsRecord,
    PolicyMapping,
    Verdict,
)
from compliance_rag.api.check_claims import claims_json
from compliance_rag.api.check_findings import facts_json, findings_with_titles
from compliance_rag.api.checks import (
    NOT_FOUND,
    CheckView,
    passage_json,
    read_check,
    source_path,
    visible_check,
)
from compliance_rag.api.decision_view import standing_json
from compliance_rag.api.deps import (
    ApiServices,
    Caller,
    CurrentCaller,
    services_of,
    sydney_label,
)
from compliance_rag.api.sources import source_href, source_page_href
from compliance_rag.api.tenant import version_json
from compliance_rag.assessment.coverage import CoverageRow, Outcome
from compliance_rag.assessment.decisions import Decisions
from compliance_rag.assessment.evidence import EvidenceProvenance
from compliance_rag.assessment.mapping_derivation import (
    applicable_obligations,
    plan_derivations,
)
from compliance_rag.corpus.display_form import NO_TITLES, Titles, cite
from compliance_rag.corpus.models import (
    PUBLIC_TENANT_ID,
    ClauseRecord,
    clause_anchor_id,
    parse_stable_id,
)

router = APIRouter(prefix="/v1")

Json = dict[str, Any]  # Any: JSON the sibling modules built (`claims_json`, `findings_json`)

VERDICT_ORDER: Final = (
    Verdict.CONFLICTS,
    Verdict.NOT_ADDRESSED,
    Verdict.AMBIGUOUS,
    Verdict.CONSISTENT,
)
"""ADR-0053 F8: the order a report lists its verdicts in."""
VERDICT_HEADINGS: Final = {
    Verdict.CONFLICTS: "Conflicts",
    Verdict.NOT_ADDRESSED: "Not addressed",
    Verdict.AMBIGUOUS: "Ambiguous",
    Verdict.CONSISTENT: "Consistent",
}
NOT_CHECKED: Final = "anything else"
"""The coverage statement's "not checked: anything else" (F7): what is not listed above it
was not checked."""
NOT_REPORTED: Final = (
    "Obligations the model did not pick for a claim are not reported: a claim is checked "
    "only against the obligations and policy clauses picked for it, and silence is never "
    "read as compliance."
)
QUESTIONNAIRE_HREF: Final = "/admin/questionnaire"
RECHECK_HREF: Final = "/checks/new"
"""Where a document is submitted again as a new check (ADR-0059 Q7, ADR-0034): a report
never changes, so a re-check is a new check and a new report."""


class State(StrEnum):
    NEW = "new"
    """No claims yet: the next step is to extract them."""
    EXTRACTED = "extracted"
    """Claims, no findings: the next step is to make the findings."""
    REPORTED = "reported"
    """Findings recorded: the report."""


@dataclass(frozen=True, slots=True)
class CheckState:
    """What the ledger holds of a check the caller may see."""

    view: CheckView
    extraction: ClaimExtractionRecord | None
    findings: FindingsRecord | None
    run: CheckRun
    """Where its run stands, from the reservations."""
    claims: tuple[Json, ...]
    """The recorded claims, each with its quote read back from the stored passage
    (`check_claims.claims_json`), made once when the state is read; none before the
    extraction."""
    decisions: Decisions
    """What members decided about the claims and findings (#301): read once, in one query,
    once there are findings to decide on; nothing before."""
    mappings: tuple[PolicyMapping, ...] = ()
    """The tenant's policy mappings in every status (#306), read once with the findings: the
    approved ones say which derived findings came from where, and which an older report would
    gain. Nothing before there are findings."""

    @property
    def state(self) -> State:
        if self.extraction is None:
            return State.NEW
        return State.EXTRACTED if self.findings is None else State.REPORTED


async def read_state(services: ApiServices, who: Caller, check_id: str) -> CheckState | None:
    """The check, its claims and its findings, as recorded; None when it is not the
    caller's or does not exist (`checks.read_check`'s one answer)."""
    view = await read_check(services, who, check_id)
    if view is None:
        return None
    extraction = await services.ledger.get_claims(who.tenant_id, check_id)
    findings = (
        None if extraction is None else await services.ledger.get_findings(who.tenant_id, check_id)
    )
    claims = (
        ()
        if extraction is None
        else tuple(cast("list[Json]", claims_json(extraction, view.passages)["claims"]))
    )
    run = await services.ledger.check_run(who.tenant_id, check_id)
    decisions = Decisions.of(
        () if findings is None else await services.ledger.check_decisions(who.tenant_id, check_id)
    )
    mappings = () if findings is None else await services.ledger.policy_mappings(who.tenant_id)
    return CheckState(view, extraction, findings, run, claims, decisions, mappings)


# ---- the document, with its claims marked ----------------------------------------------


@dataclass(frozen=True, slots=True)
class Segment:
    text: str
    claims: tuple[int, ...]
    """The claims whose quote covers this stretch, by number; empty for the rest."""


def segments_of(text: str, spans: Sequence[tuple[int, int, int]]) -> tuple[Segment, ...]:
    """`text` cut wherever a claim's quote begins or ends, each piece naming the claims
    that cover it. `spans` are `(start, end, claim number)` in the text's own offsets;
    a claim whose quotes overlap another's is named in every piece it covers."""
    cuts = sorted({0, len(text), *(s for s, _, _ in spans), *(e for _, e, _ in spans)})
    cuts = [c for c in cuts if 0 <= c <= len(text)]
    return tuple(
        Segment(text[a:b], tuple(n for s, e, n in spans if s <= a and b <= e))
        for a, b in pairwise(cuts)
        if a < b
    )


@dataclass(frozen=True, slots=True)
class PassageView:
    path: str
    anchor: str
    number: str
    segments: tuple[Segment, ...]
    claims: tuple[int, ...]
    """Every claim whose quote is in this passage, in claim order."""


def passage_views(
    passages: Sequence[ClauseRecord], claims: Sequence[Json]
) -> tuple[PassageView, ...]:
    """The document, passage by passage, each with its claims' quotes marked. A claim's
    span is in the document's characters, so it is cut against the passage's own offsets
    (`claims.claim_quote` read the same span back)."""
    views: list[PassageView] = []
    for p in passages:
        mine = [c for c in claims if c["passage"] == p.clause_path]
        start = p.char_start or 0
        spans = [
            (int(c["char_start"]) - start, int(c["char_end"]) - start, int(c["index"]))
            for c in mine
        ]
        views.append(
            PassageView(
                path=p.clause_path,
                anchor=clause_anchor_id(p.clause_path),
                number=p.clause_path[1:],
                segments=segments_of(p.text, spans),
                claims=tuple(n for _, _, n in spans),
            )
        )
    return tuple(views)


# ---- the report -------------------------------------------------------------------------


def claim_views(state: CheckState) -> list[dict[str, object]]:
    """The state's claims as a report names them, for a page that lists them before there
    are findings."""
    return [claim_view(c, state.view.record.check_id, state.decisions) for c in state.claims]


def claim_view(claim: Json, check_id: str, decisions: Decisions) -> dict[str, object]:
    """A claim as the report names it: its number, the model's label, the document's own
    words and where they are (the passage's anchor on the check's page), its tier and what a
    member decided about it (#301). The claim itself is as it was extracted: its decision is
    read from the rows beside it, so `tier` here is the tier **now** (`asserted` once
    confirmed), computed when read, while the stored claim stays `inferred`."""
    index = int(claim["index"])
    standing = decisions.claims.get(index)
    return {
        "index": index,
        "summary": claim["summary"],
        "quote": claim["quote"],
        "passage": claim["passage"],
        "anchor": claim["anchor"],
        "href": f"/checks/{check_id}#{claim['anchor']}",
        "tier": decisions.claim_tier(index).value,
        "decision": standing_json(standing),
        "confirmed": decisions.confirmed(index) is not None,
        "rejected": decisions.rejected(index) is not None,
    }


def _law_target(obligation_id: str, rows: dict[str, CoverageRow]) -> dict[str, object]:
    """The obligation a finding is against, named by the clause it is set out in (the first
    of its coverage row's citations is the obligation's own, `ObligationCoverage.citations`)
    and its id."""
    row = rows[obligation_id]
    first = row.citations[0] if row.citations else None
    return {
        "label": cite(first, NO_TITLES) if first else obligation_id,
        "href": source_href(first) if first else None,
    }


def _policy_ref(clause_id: str, titles: Titles) -> dict[str, object]:
    """A policy clause named by its document's title and version, and the clause."""
    instrument_id, version_id, _ = parse_stable_id(clause_id)
    return {
        "title": titles.get((instrument_id, version_id), instrument_id),
        "version_id": version_id,
        "cite": cite(clause_id, titles),
        "href": source_href(clause_id),
    }


def _disagreements(
    payload: Json,
    claims: dict[int, dict[str, object]],
    titles: Titles,
) -> list[dict[str, object]]:
    """The banner (Q6), one entry per claim the policies disagree about: the policies whose
    finding is `conflicts` and those whose finding is `consistent` or `ambiguous` (the
    verdict named), from the payload's `policy_disagreements`."""
    by_claim: dict[int, Json] = {}
    for d in payload["policy_disagreements"]:
        if int(d["claim_index"]) not in claims:  # a rejected claim's findings are not shown
            continue
        entry = by_claim.setdefault(int(d["claim_index"]), {"conflicts": {}, "others": {}})
        clause = str(d["conflicts"]["policy_clause_id"])
        entry["conflicts"][clause] = _policy_ref(clause, titles)
        other = str(d["with"]["policy_clause_id"])
        entry["others"][other] = {**_policy_ref(other, titles), "verdict": d["with"]["verdict"]}
    return [
        {
            "claim": claims[index],
            "conflicts": list(entry["conflicts"].values()),
            "others": list(entry["others"].values()),
        }
        for index, entry in sorted(by_claim.items())
    ]


def _mapping_ref(
    mapping: PolicyMapping, rows: dict[str, CoverageRow], titles: Titles
) -> dict[str, object]:
    """An approved mapping as a report names it: the policy clause that implements the
    obligation, the obligation, and who approved it and when (the ledger holds the request)."""
    return {
        "mapping_id": mapping.mapping_id,
        "policy": _policy_ref(mapping.policy_clause_id, titles),
        "obligation_id": mapping.obligation_id,
        "obligation": _law_target(mapping.obligation_id, rows)
        if mapping.obligation_id in rows
        else {"label": mapping.obligation_id, "href": None},
        "approved_by": mapping.approver_email,
        "approved_at": None if mapping.decided_at is None else mapping.decided_at.isoformat(),
        "approved_at_label": None
        if mapping.decided_at is None
        else sydney_label(mapping.decided_at),
    }


def _finding_view(
    finding: FindingRecord,
    shown: Json,
    claims: dict[int, dict[str, object]],
    rows: dict[str, CoverageRow],
    decisions: Decisions,
    by_id: dict[str, FindingRecord],
    mappings: dict[int, PolicyMapping],
    titles: Titles,
) -> dict[str, object]:
    if finding.obligation_id is not None:
        target = _law_target(finding.obligation_id, rows)
    else:
        target = {"label": shown["cite"], "href": shown["href"]}
    derived: dict[str, object] | None = None
    if finding.derived_from is not None:
        source = by_id[finding.derived_from]
        assert source.policy_clause_id is not None  # a derived finding comes from a policy one
        mapping = mappings.get(finding.mapping_id or 0)
        derived = {
            "from_finding_id": source.finding_id,
            "policy": _policy_ref(source.policy_clause_id, titles),
            "mapping": None if mapping is None else _mapping_ref(mapping, rows, titles),
            # its verdict is the policy finding's, so that is what a person accepts
            "source_accepted": standing_json(decisions.accepted(source.finding_id)),
        }
    return {
        # the run's recorded `provenance` is left out: the tier **now** is computed when
        # read (F5), the weakest of the claim's and the verdict's, and the two must not
        # both be said
        **{k: v for k, v in shown.items() if k != "provenance"},
        "tier": decisions.finding_tier(finding).value,
        # a derived finding's verdict is its source's: it is accepted through that finding
        "can_accept": finding.outcome is FindingOutcome.VERDICT and not finding.derived,
        "accepted": standing_json(decisions.accepted(finding.finding_id)),
        "claim": claims[finding.claim_index],
        "label": target["label"],
        "target_href": target["href"],
        "derived": derived,
        "direct": None,
    }


def _attach_direct(
    views: Sequence[tuple[FindingRecord, dict[str, object]]],
) -> set[str]:
    """Beneath each derived finding, the model's own finding for the same claim and obligation,
    as `direct`, with whether the two verdicts disagree (`direct_disagrees`: None when the
    model's own finding has no verdict to compare). **Neither is dropped** (ADR-0059 Q7): the
    direct finding is shown there, and is not listed again among the report's own groups.
    Returns the ids of the findings so attached."""
    direct = {
        (f.claim_index, f.obligation_id): (f, v)
        for f, v in views
        if not f.derived and f.obligation_id is not None
    }  # (claim, obligation id), so the lookup below is of a str
    attached: set[str] = set()
    for f, v in views:
        if not f.derived:
            continue
        own = direct.get((f.claim_index, f.obligation_id or ""))
        if own is None:
            continue
        other, shown = own
        v["direct"] = shown
        v["direct_disagrees"] = (
            None if other.verdict is None or f.verdict is None else other.verdict is not f.verdict
        )
        attached.add(other.finding_id)
    return attached


def _sources(findings: Sequence[Json]) -> list[Json]:
    """One card per clause the displayed findings cite, in the order they are first cited.
    Each citation is given the card's id (`source`), so a reader goes from a finding to the
    clause's own text. The card holds the clause's canonical text, never only the quote."""
    cards: dict[str, Json] = {}
    for finding in findings:
        for citation in finding["citations"]:
            clause_id = citation["clause_id"]
            card = cards.get(clause_id)
            if card is None:
                card = cards[clause_id] = {
                    "id": f"src-{len(cards) + 1}",
                    "clause_id": clause_id,
                    "cite": citation["cite"],
                    "version_id": citation["version_id"],
                    "href": citation["href"],
                    "text": citation["canonical_text"],
                    "quotes": [],
                    "incomplete": citation["incomplete"],
                }
            if citation["quote"] not in card["quotes"]:
                card["quotes"].append(citation["quote"])
            citation["source"] = card["id"]
    return list(cards.values())


async def _coverage_statement(
    services: ApiServices,
    record: FindingsRecord,
    titles: Titles,
    applied: Sequence[dict[str, object]] = (),
) -> dict[str, object]:
    """F7. The instruments are those of the obligations Track A assessed (the instrument of
    the clause each obligation is set out in), with how many of them apply or might and
    so were open to the model. The policies are exactly the versions the run sent, as
    recorded with the assessment. An assessment recorded before they were kept
    (`policies_recorded` false) lists the policies its findings name, from `titles`, and the
    page says so."""
    assessment = record.assessment
    in_scope: dict[tuple[str, str], int] = {}
    for row in record.coverage:
        if not row.citations:
            continue
        instrument_id, version_id, _ = parse_stable_id(row.citations[0])
        key = (instrument_id, version_id)
        in_scope[key] = in_scope.get(key, 0) + (row.outcome is not Outcome.NOT_APPLICABLE)
    law_titles = await services.ledger.version_titles(PUBLIC_TENANT_ID, in_scope)
    if assessment.policies is not None:
        policies = [(x.instrument_id, x.version_id, x.title) for x in assessment.policies]
    else:
        named = {
            parse_stable_id(f.policy_clause_id)[:2]
            for f in record.findings
            if f.policy_clause_id is not None
        }
        policies = [(i, v, titles.get((i, v), i)) for i, v in sorted(named)]
    return {
        "as_of": assessment.as_of.isoformat(),
        "instruments": [
            {
                "instrument_id": instrument_id,
                "version_id": version_id,
                "title": law_titles.get((instrument_id, version_id), instrument_id),
                "href": source_page_href(instrument_id, version_id),
                "obligations_open_to_the_model": in_scope[(instrument_id, version_id)],
            }
            for instrument_id, version_id in sorted(in_scope)
        ],
        "policies_recorded": assessment.policies is not None,
        "policies": [
            {
                "instrument_id": instrument_id,
                "version_id": version_id,
                "title": title,
                "href": source_page_href(instrument_id, version_id),
            }
            for instrument_id, version_id, title in sorted(policies)
        ],
        "corpus_version": version_json(assessment.corpus),
        "mapping_version": version_json(assessment.mapping),
        # the approved policy mappings the findings were derived through (#306)
        "policy_mappings_applied": list(applied),
        "facts": facts_json(assessment.facts),
        "obligations": {o.value: sum(r.outcome is o for r in record.coverage) for o in Outcome},
        "not_checked": NOT_CHECKED,
        "not_reported": NOT_REPORTED,
    }


def _mappings_applied(
    views: Sequence[tuple[FindingRecord, dict[str, object]]],
    mappings: dict[int, PolicyMapping],
    rows: dict[str, CoverageRow],
    titles: Titles,
) -> list[dict[str, object]]:
    """The approved mappings the displayed findings were derived through, once each, in the
    order they were proposed (F7: a report says what its findings rest on)."""
    used = sorted({f.mapping_id for f, _ in views if f.mapping_id is not None})
    return [_mapping_ref(mappings[i], rows, titles) for i in used if i in mappings]


def _would_change(
    state: CheckState,
    record: FindingsRecord,
    rows: dict[str, CoverageRow],
    claims: dict[int, dict[str, object]],
    titles: Titles,
) -> list[dict[str, object]]:
    """What a mapping approved since this assessment would change in it (ADR-0059 Q7): each
    approved `implements` mapping that touches one of its policy findings and has not been
    applied to it, with the claims it would derive a finding for. Computed by the rule that
    derives them (`plan_derivations`) from the recorded findings and the ledger's approved
    mappings, so it names nothing a new check would not add, and nothing the report already
    holds. The assessment itself is never changed; a new check is (`check_recheck.py`)."""
    plan = [
        d
        for d in plan_derivations(
            record.findings, state.mappings, applicable_obligations(record.coverage)
        )
        if d.claim_index in claims
    ]
    by_mapping: dict[int, list[int]] = {}
    for d in plan:
        by_mapping.setdefault(d.mapping.mapping_id, []).append(d.claim_index)
    held = {m.mapping_id: m for m in state.mappings}
    return [
        {
            **_mapping_ref(held[mapping_id], rows, titles),
            "claims": [claims[i] for i in sorted(set(indexes))],
        }
        for mapping_id, indexes in sorted(by_mapping.items())
    ]


async def report_json(services: ApiServices, state: CheckState) -> dict[str, object]:
    """The report: what `findings_json` recorded, ordered (F8), with the claims, the banner,
    the sources panel's cards and the coverage statement."""
    record = state.findings
    assert record is not None  # a report is of recorded findings
    check_id = state.view.record.check_id
    shown, titles = await findings_with_titles(
        services, state.view.record.tenant_id, check_id, record
    )  # the titles of every policy the findings name, read once
    payload = cast("Json", shown)
    decisions = state.decisions
    everything = {int(c["index"]): claim_view(c, check_id, decisions) for c in state.claims}
    # a rejected claim's findings leave the report (ADR-0059 Q3): the claim is shown under
    # "Rejected claims", with who rejected it, and nothing else of it is
    rejected = sorted(i for i, c in everything.items() if c["rejected"])
    claims = {i: c for i, c in everything.items() if i not in rejected}
    rows = {r.obligation_id: r for r in record.coverage}
    ordered = sorted(
        zip(record.findings, payload["findings"], strict=True), key=lambda pair: pair[0].claim_index
    )  # stable: within a claim, the ledger's order
    by_id = {f.finding_id: f for f in record.findings}
    mappings = {m.mapping_id: m for m in state.mappings}
    views = [
        (f, _finding_view(f, shown, claims, rows, decisions, by_id, mappings, titles))
        for f, shown in ordered
        if f.claim_index in claims
    ]
    # beneath a derived finding, the model's own for the same claim and obligation (Q7)
    beneath = _attach_direct(views)
    listed = [(f, v) for f, v in views if f.finding_id not in beneath]
    set_aside = Counter(f.claim_index for f in record.findings if f.claim_index in rejected)

    def of_outcome(outcome: FindingOutcome) -> list[dict[str, object]]:
        return [v for f, v in listed if f.outcome is outcome]

    by_verdict = {
        verdict: [
            v for f, v in listed if f.outcome is FindingOutcome.VERDICT and f.verdict is verdict
        ]
        for verdict in VERDICT_ORDER
    }
    applied = _mappings_applied(views, mappings, rows, titles)
    would_change = _would_change(state, record, rows, claims, titles)
    groups = [
        {"verdict": v.value, "heading": VERDICT_HEADINGS[v], "findings": by_verdict[v]}
        for v in VERDICT_ORDER
    ]
    answered = {f.claim_index for f in record.findings}
    # what the model picked to check each claim against is in the ledger with the claim
    picked = {
        c.claim_index: len(c.obligation_ids) + len(c.policy_clause_ids)
        for c in (state.extraction.claims if state.extraction else ())
    }
    return {
        "assessment_id": payload["assessment_id"],
        "as_of": payload["as_of"],
        "trace_id": payload["trace_id"],
        "tiers": {
            tier: sum(v["tier"] == tier for _, v in views)
            for tier in (EvidenceProvenance.ASSERTED.value, EvidenceProvenance.INFERRED.value)
        },
        "claims": [claims[i] for i in sorted(claims)],
        "rejected_claims": [
            {"claim": everything[i], "findings_set_aside": set_aside[i]} for i in rejected
        ],
        "disagreements": _disagreements(payload, claims, titles),
        "groups": groups,
        "conditional": of_outcome(FindingOutcome.CONDITIONAL),
        "withheld": of_outcome(FindingOutcome.WITHHELD),
        "failed": of_outcome(FindingOutcome.FAILED),
        "claims_without_findings": [
            {**c, "picked": picked[i]} for i, c in sorted(claims.items()) if i not in answered
        ],
        # as displayed: the verdict groups, and the model's own findings shown beneath a
        # derived one
        "sources": _sources(
            [
                *(f for v in VERDICT_ORDER for f in by_verdict[v]),
                *(
                    cast("Json", f["direct"])
                    for v in VERDICT_ORDER
                    for f in by_verdict[v]
                    if f.get("direct")
                ),
            ]
        ),
        "coverage_statement": await _coverage_statement(services, record, titles, applied),
        "would_change": would_change,
        "recheck": {"href": f"/v1/checks/{check_id}/recheck", "check_id": check_id},
        "questionnaire_href": QUESTIONNAIRE_HREF,
        "recheck_href": RECHECK_HREF,
    }


async def read_report(
    services: ApiServices, who: Caller, check_id: str
) -> tuple[CheckState, dict[str, object] | None] | None:
    """The check as recorded and, once its findings are, its report. None when the check is
    not the caller's or does not exist."""
    with telemetry.span("api.check.report", **{"check.check_id": check_id}) as span:
        found = await read_state(services, who, check_id)
        if found is None:
            span.set_attribute("check.found", False)
            return None
        span.set_attribute("check.found", True)
        span.set_attribute("check.state", found.state.value)
        span.set_attribute("check.running", found.run.running.value if found.run.running else "")
        if found.findings is None:
            return found, None
        for outcome in FindingOutcome:
            span.set_attribute(
                f"check.findings.{outcome.value}",
                sum(f.outcome is outcome for f in found.findings.findings),
            )
        return found, await report_json(services, found)


@router.get("/checks/{check_id}")
async def get_check(request: Request, who: CurrentCaller, check_id: str) -> dict[str, object]:
    """The check: its stored document, and what the ledger holds of its run. `state` says
    which, `claims` is the recorded extraction once there is one and `report` the findings
    report once there is one (`read_report`)."""
    found = await read_report(services_of(request), who, check_id)
    if found is None:
        raise HTTPException(404, NOT_FOUND)
    state, report = found
    record = state.view.record
    return {
        "check_id": record.check_id,
        "title": state.view.title,
        "filename": record.filename,
        "format": record.document_format.value,
        "submitted_at": record.created_at.isoformat() if record.created_at else None,
        "mine": state.view.mine,
        "href": f"/checks/{record.check_id}",
        "source_href": source_path(record.instrument_id),
        "trace_id": record.trace_id,
        "passages": [passage_json(p) for p in state.view.passages],
        "state": state.state.value,
        "running": state.run.running.value if state.run.running else None,
        "failed": state.run.failed.value if state.run.failed else None,
        "claims": None
        if state.extraction is None
        else claims_json(state.extraction, state.view.passages, state.decisions),
        "report": report,
    }


@router.get("/checks/{check_id}/run")
async def get_check_run(request: Request, who: CurrentCaller, check_id: str) -> dict[str, object]:
    """Where the check's run stands, for a page that is waiting on it: the stage running now
    and the stage whose last run failed (`LedgerStore.check_run`), and nothing else, so it
    is cheap to poll. The same 404 as the check for one that is not the caller's."""
    services = services_of(request)
    if await visible_check(services, who, check_id) is None:
        raise HTTPException(404, NOT_FOUND)
    run = await services.ledger.check_run(who.tenant_id, check_id)
    return {
        "check_id": check_id,
        "running": run.running.value if run.running else None,
        "failed": run.failed.value if run.failed else None,
    }
