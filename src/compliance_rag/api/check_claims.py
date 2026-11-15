"""Extracting the claims of a submitted document (#298, ADR-0053 F2, ADR-0059 Q2
and Q4): `POST /v1/checks/{check_id}/claims`, streamed as server-sent events as
`/v1/answers` is, and `GET` of what it recorded.

**Why a run endpoint of its own.** `POST /v1/checks` (#297) stores a document and
calls no model, and its tests prove it. Extraction is the first call that sends a
submitted document to one, so it is a separate request a member makes for a check
they can see: the smallest design consistent with how `/v1/answers` streams, with
nothing about submission changed. The check page and the findings (#299, #300)
build on it.

The stream carries stages and a result, never anything unvalidated:
- `coverage` once Track A has answered (the obligation digest is built from it);
- `claims`: the claims that passed `assessment/claims.py`'s checks, each a
  verbatim quote of the document with its summary, its picked obligation and
  policy clause ids and provenance `inferred`, and how many were dropped, and
  why each was (`dropped_reasons`: its place in the reply and our word for the
  reason, never the model's words);
- or `error`.

There is no `retrieval` stage because there is no retrieval: a check never calls
`RetrievalStore.search`, `route` or `lookup_clause`, and no word of a submitted
document becomes a query (CLAUDE.md; a test proves it with a spy).

**Gates, in order, each before the next costs anything.** The sign-in; then the
check: not theirs or not there is a 404, as for the document itself
(`checks.visible_check`, ADR-0059 Q5); then the extraction is **reserved**
(`LedgerStore.reserve_extraction`), atomically: a check is extracted once, its
claims are never replaced, and of any number of concurrent requests exactly one
holds the reservation. The rest are a 409 before the stream starts and call
nothing, so a double click or a retry spends no second call. The reservation is
released as failed (`fail_extraction`) by every path that ends without recording
the claims: a refusal, a malformed reply, a model that is down, any error, or a
client that left before the model was called (the extraction task itself
releases it if it fails). The check can then be extracted again. A request that
died holding one (the process was killed) is taken over after
`RESERVATION_TTL`. Inside the stream, before Track A
or anything else: offshore, only a registered build-phase tester may send a
document to be checked (`assert_submission_allowed`, ADR-0032), as for Ask. The
synthetic-text gate over the prefix, every passage and the whole document is the
extractor's, before it builds a prompt, so a document the registry does not vouch
for never reaches the generator (`claims.ClaimExtractor.extract`).

**Recorded before it is shown.** The extraction and its claims are in the ledger
(with the request's trace id) before the `claims` event is sent, so nothing is
shown that the ledger lacks. The extraction runs in a task the client leaving
does not cancel, so the tokens it spends and the claims it makes are recorded
either way.

An error event names our word for what went wrong, never the model's or the
provider's: `residency`, `boundary_collision`, `malformed_generation`,
`generation_unavailable`, `policy_corpus_too_large` or `internal`. A failed
extraction records no claims, so the check can be extracted again.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from compliance_rag.adapters.generation_residency import (
    GenerationResidencyError,
    assert_submission_allowed,
)
from compliance_rag.adapters.interfaces import (
    ClaimExtractionRecord,
    ExtractionAlreadyReserved,
    GenerationUnavailable,
    LedgerStore,
)
from compliance_rag.api.checks import NOT_FOUND, read_passages, visible_check
from compliance_rag.api.deps import (
    CurrentCaller,
    counts,
    coverage_on,
    services_of,
    today,
)
from compliance_rag.assessment.claims import Extraction, claim_quote
from compliance_rag.assessment.draft import MalformedGeneration
from compliance_rag.assessment.obligation_digest import ObligationDigest
from compliance_rag.assessment.policies import tenant_policies
from compliance_rag.assessment.prefix_cap import PolicyCorpusTooLarge
from compliance_rag.assessment.prompt import DelimiterCollision
from compliance_rag.corpus.models import ClauseRecord, clause_anchor_id
from compliance_rag.telemetry import current_trace_id

router = APIRouter(prefix="/v1")
log = logging.getLogger(__name__)

ALREADY_EXTRACTED: Final = "this check's claims were already extracted"
NOT_EXTRACTED: Final = "this check has not been extracted"

_running: set[asyncio.Task[Extraction]] = set()
"""Extractions still running for a client that left: kept until they finish, so
the tokens they spend are recorded, and so are the claims."""


def _error(failed: Exception) -> tuple[dict[str, object], bool]:
    """What an `error` event carries for a failure, and whether it is one the
    server's operator should see in the log (an unexpected one)."""
    if isinstance(failed, GenerationResidencyError):
        return {"error": "residency", "message": str(failed)}, False
    if isinstance(failed, DelimiterCollision):
        return {
            "error": "boundary_collision",
            "message": "the document contains the text that fences it, so it was not sent "
            "to the model",
        }, False
    if isinstance(failed, MalformedGeneration):
        return {
            "error": "malformed_generation",
            "message": "the model's reply wasn't a valid set of claims; nothing in it is used",
        }, False
    if isinstance(failed, GenerationUnavailable):
        return {
            "error": "generation_unavailable",
            "message": "the model isn't taking requests, so nothing was generated",
            "reason": failed.reason.value,
            "trace_id": current_trace_id(),
        }, False
    if isinstance(failed, PolicyCorpusTooLarge):
        return {
            "error": "policy_corpus_too_large",
            "message": str(failed),
            "estimated_tokens": failed.estimated_tokens,
            "cap_tokens": failed.cap_tokens,
            "trace_id": current_trace_id(),
        }, False
    return {"error": "internal", "message": "the claims couldn't be extracted"}, True


def event(name: str, data: dict[str, object]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def claims_json(
    record: ClaimExtractionRecord, passages: tuple[ClauseRecord, ...]
) -> dict[str, object]:
    """A recorded extraction as a member is shown it. The quote is read back from
    the stored passage (the ledger holds only its span and hash, `claim_quote`),
    so it is the document's own words, and a passage that no longer matches is an
    integrity failure, never a different quote."""
    return {
        "check_id": record.check_id,
        "as_of": record.as_of.isoformat(),
        "accepted": len(record.claims),
        "dropped": record.claims_dropped,
        "trace_id": record.trace_id,
        "claims": [
            {
                "index": c.claim_index,
                "passage": c.passage,
                "anchor": clause_anchor_id(c.passage),
                "char_start": c.quote_start,
                "char_end": c.quote_end,
                "quote": claim_quote(c, passages),
                "summary": c.summary,
                "obligation_ids": list(c.obligation_ids),
                "policy_clause_ids": list(c.policy_clause_ids),
                "provenance": c.provenance.value,
            }
            for c in record.claims
        ],
    }


@router.post("/checks/{check_id}/claims")
async def extract_claims(request: Request, who: CurrentCaller, check_id: str) -> StreamingResponse:
    services = services_of(request)
    # Before anything is read, sent or written: a check that isn't theirs is not
    # found, whoever's it is (ADR-0059 Q5); one already extracted is not run again.
    record = await visible_check(services, who, check_id)
    if record is None:
        raise HTTPException(404, NOT_FOUND)
    day = today()
    try:
        await services.ledger.reserve_extraction(
            who.tenant_id, check_id, as_of=day, trace_id=current_trace_id()
        )
    except ExtractionAlreadyReserved:
        raise HTTPException(
            409, {"error": "already_extracted", "message": ALREADY_EXTRACTED}
        ) from None

    async def stages() -> AsyncIterator[str]:
        extracting: asyncio.Task[Extraction] | None = None
        try:
            # First: offshore, only a registered tester's document leaves
            # Australia (ADR-0032), as for a question.
            assert_submission_allowed(
                generation_region=services.settings.generation_region,
                submitter=who.member.email,
                testers=services.settings.offshore_testers,
            )
            coverage = await coverage_on(services, who.tenant_id, day)
            yield event(
                "coverage",
                {"check_id": check_id, "as_of": day.isoformat(), "counts": counts(coverage)},
            )
            digest = ObligationDigest.of(coverage)
            policies = await tenant_policies(
                services.ledger, services.clauses, tenant_id=who.tenant_id, as_of=day
            )
            passages = await read_passages(services.clauses, record)
            source = await services.clauses.get_source_html(
                record.instrument_id, record.version_id, tenant_id=record.tenant_id
            )
            if source is None:
                raise LookupError(f"{record.instrument_id}:{record.version_id}: no stored source")

            extractor = services.claim_extractor()

            async def run() -> Extraction:
                """Extracts and records, and releases the reservation if it
                does not get as far as recording, whatever stops it."""
                try:
                    return await extractor.extract(
                        tenant_id=who.tenant_id,
                        check_id=check_id,
                        passages=passages,
                        source=source,
                        digest=digest,
                        policies=policies,
                        as_of=day,
                    )
                except BaseException:
                    await _release(services.ledger, who.tenant_id, check_id)
                    raise

            # A child task carries a copy of this request's context, so its spans
            # and rows carry the request's trace_id, and it isn't cancelled if the
            # client leaves: the tokens it spends and the claims it makes are
            # recorded either way.
            extracting = asyncio.ensure_future(run())
            _running.add(extracting)
            extracting.add_done_callback(_finished)
            extraction = await asyncio.shield(extracting)
            stored = await services.ledger.get_claims(who.tenant_id, check_id)
            if stored is None:  # recorded by `extract`, so this is the ledger lying
                raise LookupError(f"check {check_id}: the extraction was not recorded")
            shown = claims_json(stored, passages)
            yield event(
                "claims",
                {
                    **shown,
                    "provenance": "inferred",
                    "dropped_reasons": [
                        {"position": d.position, "reason": d.reason.value}
                        for d in extraction.dropped
                    ],
                    "generator": services.settings.generator,
                    "cost_usd": extraction.generation.cost_usd,
                },
            )
        except Exception as failed:
            if extracting is None:  # nothing was handed the reservation: release it here
                await _release(services.ledger, who.tenant_id, check_id)
            body, unexpected = _error(failed)
            if unexpected:
                log.exception("extracting a check's claims failed")
            else:
                log.warning("a check's claims were not extracted: %s", body["error"])
            yield event("error", {"check_id": check_id, **body})
        except BaseException:  # the client left before the model was called
            if extracting is None:
                await _release(services.ledger, who.tenant_id, check_id)
            raise

    return StreamingResponse(stages(), media_type="text/event-stream")


async def _release(ledger: LedgerStore, tenant_id: str, check_id: str) -> None:
    """Release a reservation as failed. A ledger that cannot take it does not hide
    the error being reported: the reservation then lapses after `RESERVATION_TTL`."""
    try:
        await ledger.fail_extraction(tenant_id, check_id)
    except Exception:
        log.exception("a check's extraction could not be released")


def _finished(task: asyncio.Task[Extraction]) -> None:
    _running.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning(
            "an extraction finished with an error after its client left",
            exc_info=task.exception(),
        )


@router.get("/checks/{check_id}/claims")
async def get_claims(request: Request, who: CurrentCaller, check_id: str) -> dict[str, object]:
    """What was recorded for a check the caller may see: 404 for one that isn't
    theirs or doesn't exist (the one answer for both), and for one not yet
    extracted."""
    services = services_of(request)
    record = await visible_check(services, who, check_id)
    if record is None:
        raise HTTPException(404, NOT_FOUND)
    extraction = await services.ledger.get_claims(who.tenant_id, check_id)
    if extraction is None:
        raise HTTPException(404, NOT_EXTRACTED)
    return claims_json(extraction, await read_passages(services.clauses, record))
