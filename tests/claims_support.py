"""What the claim-extraction tests share (#298): a submitted document cut into
passages, a digest and a policy prefix to pick from, a generator scripted with
what a (possibly compromised) model returns, and a ledger holding the check the
claims are recorded against. Nothing here calls a model."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from compliance_rag.adapters.fakes import FakeGenerator, FakeLedgerStore
from compliance_rag.adapters.generation_residency import TenantTextGate
from compliance_rag.adapters.interfaces import (
    CheckRecord,
    Generation,
    Profile,
    ReplyKind,
)
from compliance_rag.assessment.claims import ClaimExtractor
from compliance_rag.assessment.coverage import Outcome
from compliance_rag.assessment.obligation_digest import ObligationDigest
from compliance_rag.assessment.prompt import SourceClause
from compliance_rag.corpus.models import ClauseRecord, DocumentClass
from compliance_rag.corpus.submitted_parser import SubmittedFormat, segment_submitted_document
from compliance_rag.corpus.synthetic_registry import SyntheticRegistry
from compliance_rag.settings import REGION
from tests.test_obligation_digest import coverage_of, obligation

TENANT = "acme"
SUBMITTER = "uid-second"
CHECK_ID = "c" * 32
INSTRUMENT = f"{TENANT}/check-{CHECK_ID}"
VERSION = "v1"
AS_OF = dt.date(2026, 11, 15)
DIGEST_IDS = ("OB-PA-APP11-1", "OB-PA-APP8-1")
POLICY_IDS = ("acme/privacy-policy:v1:3/1", "acme/vendor-policy:v1:2/4")

DOCUMENT = """# Customer insights programme

We will keep every customer's purchase history for 7 years after the account closes.

Purchase histories will be exported weekly to CloudAnalytics Inc, a vendor overseas.

Customers can ask for a copy of their profile, and we will respond within 30 days.
"""
"""A small document of three claims, in Markdown: a heading and three paragraphs."""


def passages_of(
    source: str = DOCUMENT, fmt: SubmittedFormat = SubmittedFormat.MARKDOWN
) -> tuple[ClauseRecord, ...]:
    return segment_submitted_document(
        source,
        document_format=fmt,
        instrument_id=INSTRUMENT,
        version_id=VERSION,
        tenant_id=TENANT,
        user_id=SUBMITTER,
    )


def digest_of(*obligation_ids: str) -> ObligationDigest:
    """A digest listing exactly these obligations (applicable ones)."""
    ids = obligation_ids or DIGEST_IDS
    return ObligationDigest.of(
        coverage_of(*(obligation(i, Outcome.APPLICABLE, summary=f"Summary of {i}.") for i in ids))
    )


def policies_of(*clause_ids: str) -> tuple[SourceClause, ...]:
    ids = clause_ids or POLICY_IDS
    return tuple(SourceClause(i, f"Policy text of {i}.") for i in ids)


def span_of(source: str, quote: str) -> tuple[int, int]:
    start = source.index(quote)
    return start, start + len(quote)


def claim(passage: ClauseRecord, quote: str, /, **more: object) -> dict[str, object]:
    """One claim as the model replies it, naming `passage` by its id."""
    return {
        "passage": passage.clause_id,
        "quote": quote,
        "summary": "What the document says.",
        "obligation_ids": [],
        "policy_clause_ids": [],
        **more,
    }


def reply_of(*claims: dict[str, object]) -> str:
    return json.dumps({"claims": list(claims)})


_BOUNDARY = r"<{label} boundary=([0-9a-f]{{24}})>"


def boundary_in(prompt: str, label: str) -> str:
    """The boundary of a fenced section of a prompt the generator was sent."""
    found = re.search(_BOUNDARY.format(label=label), prompt)
    assert found is not None, f"no {label} section in the prompt"
    return found.group(1)


@dataclass(frozen=True, slots=True)
class Prompt:
    """What a scripted reply may depend on: the prompt the generator was sent."""

    system: str
    cached_prefix: str
    user: str

    @property
    def document_boundary(self) -> str:
        return boundary_in(self.user, "submitted_document")

    @property
    def digest_boundary(self) -> str:
        return boundary_in(self.cached_prefix, "obligation_digest")

    @property
    def policies_boundary(self) -> str:
        return boundary_in(self.cached_prefix, "tenant_policies")


class ScriptedGenerator(FakeGenerator):
    """What a compromised model returns: a fixed reply, or one built from the
    prompt it was sent (a forged fence needs the real boundary). Records every
    call, as `FakeGenerator` does."""

    def __init__(self, reply: str | Callable[[Prompt], str]) -> None:
        super().__init__("")
        self._script = reply
        self.replies: list[ReplyKind] = []

    async def generate(
        self,
        *,
        system: str,
        cached_prefix: str | None,
        user: str,
        max_tokens: int,
        reply: ReplyKind = ReplyKind.ANSWER,
    ) -> Generation:
        self.replies.append(reply)
        prompt = Prompt(system, cached_prefix or "", user)
        self._reply = self._script if isinstance(self._script, str) else self._script(prompt)
        return await super().generate(
            system=system,
            cached_prefix=cached_prefix,
            user=user,
            max_tokens=max_tokens,
            reply=reply,
        )


async def checked_ledger(*, trace_id: str = "a" * 32) -> FakeLedgerStore:
    """A ledger holding acme and the check the extraction is recorded against."""
    ledger = FakeLedgerStore()
    await ledger.put_profile(Profile(TENANT))
    await ledger.record_version(
        tenant_id=TENANT,
        instrument_id=INSTRUMENT,
        version_id=VERSION,
        valid_from=AS_OF,
        supersedes=None,
        document_class=DocumentClass.SUBMITTED_DOCUMENT,
        title="Customer insights programme",
    )
    await ledger.record_check(
        CheckRecord(
            tenant_id=TENANT,
            check_id=CHECK_ID,
            user_id=SUBMITTER,
            instrument_id=INSTRUMENT,
            version_id=VERSION,
            filename="proposal.md",
            document_format=SubmittedFormat.MARKDOWN,
            trace_id=trace_id,
        )
    )
    return ledger


def extractor_over(
    generator: FakeGenerator, ledger: FakeLedgerStore, *, region: str = REGION
) -> ClaimExtractor:
    return ClaimExtractor(
        generator, ledger, tenant_text=TenantTextGate(region, SyntheticRegistry.load())
    )
