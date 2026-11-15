"""The adversarial set for claim extraction (#298), run in default CI.

`tests/fixtures/adversarial_claims/` holds documents that attack the extraction
and `cases.json`, the reply a compromised model would give to each: a forged
fence, "ignore previous instructions", fake JSON, planted obligation and clause
ids, text claiming to be synthetic, a document that holds its own boundary, and
quotes a model invents. Each case is run through the real extractor against a
scripted generator, and what is asserted is what code did, never what the model
was asked to do:

- the reply is refused whole, or each claim in it is accepted or dropped for the
  reason the case names, and what is accepted is a span of the document with no
  id outside the digest or the prefix;
- nothing a case plants reaches the ledger;
- a document that holds its own boundary is refused before the model is called;
- offshore, every one of these documents is refused before the model is called,
  whatever it says about itself, because no registry vouches for it;
- in every prompt the document sits inside one section whose boundary it
  does not contain, whatever markers it forges.

Every document is run, not a representative one. Nothing is billable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from compliance_rag.adapters.generation_residency import GenerationResidencyError
from compliance_rag.adapters.interfaces import ClaimRecord
from compliance_rag.assessment.claims import claim_quote
from compliance_rag.assessment.draft import MalformedGeneration
from compliance_rag.assessment.prompt import DelimiterCollision
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from tests.claims_support import (
    AS_OF,
    CHECK_ID,
    DIGEST_IDS,
    POLICY_IDS,
    TENANT,
    Prompt,
    ScriptedGenerator,
    checked_ledger,
    digest_of,
    extractor_over,
    fix_boundary,
    passages_of,
    policies_of,
)

SET = Path(__file__).parent / "fixtures" / "adversarial_claims"
DOCUMENTS = sorted(SET.glob("*.md"))


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    document: str
    attack: str
    reply: str
    expect: dict[str, Any]  # Any: JSON, read from the fixture
    planted: tuple[str, ...]
    collision_boundary: str | None


def _cases() -> list[Case]:
    raw = json.loads((SET / "cases.json").read_text(encoding="utf-8"))
    return [
        Case(
            name=c["name"],
            document=c["document"],
            attack=c["attack"],
            reply=c["reply_text"] if "reply_text" in c else json.dumps(c["reply"]),
            expect=c["expect"],
            planted=tuple(c["planted"]),
            collision_boundary=c.get("collision_boundary"),
        )
        for c in raw["cases"]
    ]


CASES = _cases()


def _script(case: Case, source: str):
    """The case's reply, with `{{DOCUMENT_BOUNDARY}}`, `{{DIGEST_BOUNDARY}}` and
    `{{PASSAGE:n}}` filled from the prompt the model was actually sent, so a
    forged fence echoes the real boundary and a claim names a real passage."""
    passages = passages_of(source)

    def reply(seen: Prompt) -> str:
        text = case.reply.replace("{{DOCUMENT_BOUNDARY}}", seen.document_boundary)
        text = text.replace("{{DIGEST_BOUNDARY}}", seen.digest_boundary)
        for number, passage in enumerate(passages, start=1):
            text = text.replace(f"{{{{PASSAGE:{number}}}}}", passage.clause_id)
        return text

    return reply


async def _run(case: Case, monkeypatch: pytest.MonkeyPatch):
    source = (SET / case.document).read_text(encoding="utf-8")
    if case.collision_boundary is not None:
        assert case.collision_boundary in source
        fix_boundary(monkeypatch, case.collision_boundary)
    generator = ScriptedGenerator(_script(case, source))
    ledger = await checked_ledger()
    passages = passages_of(source)
    refused: str | None = None
    extraction = None
    try:
        extraction = await extractor_over(generator, ledger).extract(
            tenant_id=TENANT,
            check_id=CHECK_ID,
            passages=passages,
            source=source,
            digest=digest_of(*DIGEST_IDS),
            policies=policies_of(*POLICY_IDS),
            as_of=AS_OF,
        )
    except MalformedGeneration:
        refused = "malformed_generation"
    except DelimiterCollision:
        refused = "boundary_collision"
    return generator, ledger, passages, extraction, refused


def test_the_set_is_whole_every_document_has_a_case_and_every_case_a_document() -> None:
    assert {c.document for c in CASES} == {d.name for d in DOCUMENTS}
    assert len({c.name for c in CASES}) == len(CASES)
    for kind in ("forged-fence", "ignore-previous", "fake-json", "planted-ids", "synthetic"):
        assert any(kind in c.document or kind in c.name for c in CASES), kind
    assert any(c.collision_boundary for c in CASES)


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_no_adversarial_document_is_registered_as_synthetic(document: Path) -> None:
    """The set must stay unregistered: it proves the offshore gate, and a
    registered attack would pass it."""
    registry = SyntheticRegistry.load()
    assert not registry.vouches_for_document(document.read_bytes())
    passages = passages_of(document.read_text(encoding="utf-8"))
    assert registry.unregistered(TENANT, ((p.clause_id, p.canonical_text) for p in passages))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_each_attack_is_refused_or_neutralised_by_code(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator, ledger, passages, extraction, refused = await _run(case, monkeypatch)
    expect = case.expect
    assert refused == expect.get("refused"), case.attack
    stored = await ledger.get_claims(TENANT, CHECK_ID)

    if refused is not None:
        assert stored is None  # nothing recorded for a reply refused whole
        # a collision is refused before the model is called; a malformed reply, after
        assert len(generator.calls) == (0 if refused == "boundary_collision" else 1)
        return

    assert extraction is not None and stored is not None
    assert len(generator.calls) == 1
    # what is accepted: the document's own words, with the ids the case allows
    accepted = [
        {
            "quote": claim_quote(c, passages),
            **({"obligation_ids": list(c.obligation_ids)} if c.obligation_ids else {}),
            **({"policy_clause_ids": list(c.policy_clause_ids)} if c.policy_clause_ids else {}),
        }
        for c in stored.claims
    ]
    assert accepted == expect["accepted"], case.attack
    assert [d.reason.value for d in extraction.dropped] == expect["dropped"], case.attack
    assert stored.claims_dropped == len(expect["dropped"])

    for recorded in stored.claims:
        _assert_nothing_untrusted(recorded)
    # nothing the case plants, in a quote or an id or a label, reached the ledger
    for planted in case.planted:
        assert planted not in repr(stored), planted
        assert planted not in repr(extraction.claims), planted


def _assert_nothing_untrusted(claim: ClaimRecord) -> None:
    assert set(claim.obligation_ids) <= set(DIGEST_IDS)
    assert set(claim.policy_clause_ids) <= set(POLICY_IDS)
    assert claim.provenance.value == "inferred"


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
async def test_offshore_every_adversarial_document_is_refused_before_the_model_is_called(
    document: Path,
) -> None:
    """Whatever it says about itself ("this document is synthetic", "do not apply
    the residency check"): the registry decides, and none of these is in it."""
    source = document.read_text(encoding="utf-8")
    generator = ScriptedGenerator('{"claims": []}')
    ledger = await checked_ledger()
    with pytest.raises(GenerationResidencyError):
        await extractor_over(generator, ledger, region="us").extract(
            tenant_id=TENANT,
            check_id=CHECK_ID,
            passages=passages_of(source),
            source=source,
            digest=digest_of(*DIGEST_IDS),
            policies=(),
            as_of=AS_OF,
        )
    assert generator.calls == []
    assert await ledger.usage(TENANT) == () and await ledger.get_claims(TENANT, CHECK_ID) is None


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
async def test_in_every_prompt_the_document_is_one_section_its_forged_markers_cannot_close(
    document: Path,
) -> None:
    source = document.read_text(encoding="utf-8")
    passages = passages_of(source)
    generator = ScriptedGenerator('{"claims": []}')
    ledger = await checked_ledger()
    await extractor_over(generator, ledger).extract(
        tenant_id=TENANT,
        check_id=CHECK_ID,
        passages=passages,
        source=source,
        digest=digest_of(*DIGEST_IDS),
        policies=policies_of(*POLICY_IDS),
        as_of=AS_OF,
    )
    (call,) = generator.calls
    seen = Prompt(str(call["system"]), str(call["cached_prefix"]), str(call["user"]))
    b = seen.document_boundary
    assert b not in source  # a document cannot hold the hash of itself
    # one section, opened and closed once, with a marker pair per passage and no other
    assert seen.user.count(f"<submitted_document boundary={b}>") == 1
    assert seen.user.count(f"</submitted_document boundary={b}>") == 1
    assert seen.user.count(f"[{b} ") == 2 * len(passages)
    assert seen.user.count(f"[{b} end]") == len(passages)
    # every passage verbatim, in order, inside it
    position = seen.user.index(f"<submitted_document boundary={b}>")
    for passage in passages:
        framed = f"[{b} clause {passage.clause_id}]\n{passage.text}\n[{b} end]"
        position = seen.user.index(framed, position) + len(framed)
    assert seen.user.index(f"</submitted_document boundary={b}>") >= position
    # nothing of the document is in the cached prefix, or the system prompt
    for passage in passages:
        assert passage.text not in seen.cached_prefix and passage.text not in seen.system
    # the section's boundary is not the digest's or the policies'
    assert len({b, seen.digest_boundary, seen.policies_boundary}) == 3
