"""Shared pytest fixtures.

One `TracerProvider` for the whole test process: OpenTelemetry's global API
accepts only the first `trace.set_tracer_provider()` call in a process and
silently no-ops every call after it, so installing an in-memory exporter has
to happen exactly once, here — not per test module — for any test that needs
to inspect what a span actually contains (`CLAUDE.md`: every function
touching an LLM, a retriever or the graph emits one).
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from compliance_rag.adapters.interfaces import LawScope
from compliance_rag.corpus.models import Scope

SPAN_EXPORTER = InMemorySpanExporter()

_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
trace.set_tracer_provider(_provider)


ALL_LAW = LawScope.unscoped("test: retrieval with no tenant profile")
"""For tests of retrieval itself, not of Track A's scope (D-03, ADR-0030)."""


PUBLIC_AND_TENANT = (Scope.PUBLIC, Scope.TENANT)
"""The scopes `RetrievalPipeline.retrieve` asks for unless told otherwise, for
a test of something else that calls `lookup_clause`, which takes them
explicitly (ADR-0036)."""


@pytest.fixture(autouse=True)
def _clear_spans() -> Iterator[None]:
    SPAN_EXPORTER.clear()
    yield
    SPAN_EXPORTER.clear()


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """The process-wide in-memory exporter, for any test that asserts on a
    span's name or attributes. Already cleared before and after every test
    by `_clear_spans` above."""
    return SPAN_EXPORTER


def qdrant_reachable(url: str) -> bool:
    """Whether a real Qdrant server answers at `url` — the gate for every
    test that needs one."""
    if not url:
        return False
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 6333), timeout=1):
            return True
    except OSError:
        return False
