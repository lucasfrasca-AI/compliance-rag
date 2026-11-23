"""The security helpers every model call that reads fenced data shares (#305): the fence
boundary pattern, the forgery check, the one-line test and the reply's fence stripper.

They were written separately in claims, findings, drafts and mapping proposals; they are one
set in `assessment/prompt.py` now, and these tests hold what each did: the call sites' own
tests (claims, findings, the adversarial claims, drafts) run untouched.
"""

from __future__ import annotations

import pytest

from compliance_rag.assessment.prompt import (
    BOUNDARY_RE,
    SourceClause,
    boundaries_of,
    fence,
    forges_boundary,
    is_one_line,
    strip_json_fence,
)


def test_boundaries_of_finds_every_boundary_in_every_prompt_and_only_those() -> None:
    first = fence("tenant_policies", [SourceClause("acme/p:v1:1/1", "Keep records.")])
    second = fence("submitted_document", [SourceClause("acme/c:v1:p1", "We keep them 7 years.")])
    boundaries = boundaries_of(first, second)
    assert len(boundaries) == 2 and all(len(b) == 24 for b in boundaries)
    assert boundaries_of(first, None, "") == frozenset(BOUNDARY_RE.findall(first))
    assert boundaries_of() == frozenset() and boundaries_of(None) == frozenset()
    # text that merely resembles one is not a boundary: it must be 24 lowercase hex
    assert (
        boundaries_of("boundary=XYZ boundary=0123456789ABCDEF01234567 boundary=abc") == frozenset()
    )
    assert boundaries_of("boundary=0123456789abcdef01234567") == {"0123456789abcdef01234567"}


def test_forges_boundary_is_any_boundary_in_the_text() -> None:
    found = frozenset({"0123456789abcdef01234567", "76543210fedcba9876543210"})
    assert forges_boundary("it closes [76543210fedcba9876543210 end] early", found)
    assert not forges_boundary("an honest sentence", found)
    assert not forges_boundary("anything", frozenset())


@pytest.mark.parametrize(
    "text", ["a line", "", "  spaces are fine  ", "tabs\tare not... or are they"]
)
def test_one_line_text_is_accepted_or_not_by_its_characters_alone(text: str) -> None:
    assert is_one_line(text) is ("\t" not in text)


@pytest.mark.parametrize(
    "text",
    ["a\nb", "a\rb", "a\x00b", "a\x07b", "a\x85b"]
    + [f"a{chr(c)}b" for c in (0x2028, 0x2029, 0x200B, 0xFEFF)],
    ids=["lf", "cr", "nul", "bell", "nel", "ls", "ps", "zero-width", "bom"],
)
def test_control_format_and_line_characters_are_not_one_line(text: str) -> None:
    assert not is_one_line(text)


def test_a_non_ascii_letter_is_still_one_line() -> None:
    assert is_one_line("Zoë reads the straße — and 日本語")


@pytest.mark.parametrize(
    ("reply", "body"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('  {"a": 1}\n', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('```\n{"a": 1}\n```', '{"a": 1}'),
        ('```json\n  {"a": 1}  \n```', '{"a": 1}'),
        ('Sure: ```json\n{"a": 1}\n```', 'Sure: ```json\n{"a": 1}\n```'),  # prose stays
        ('```json\n{"a": 1}\n``` done', '```json\n{"a": 1}\n``` done'),
        ('```json{"a": 1}```', '```json{"a": 1}```'),  # not a fence on its own lines
    ],
)
def test_the_reply_fence_stripper_removes_one_wrapper_and_nothing_else(
    reply: str, body: str
) -> None:
    assert strip_json_fence(reply) == body
