"""OpenTelemetry bootstrap. Sinks are configuration; call sites never change (D-16).

The single sink is a Cloud Logging regional bucket in `australia-southeast1`.
Every span becomes a structured log entry keyed by `trace_id`. Adding a sink
later — Phoenix on Cloud Run Sydney, by ADR — is a change to `SINK_BUILDERS` and
nothing else.

The residency assertion runs before any exporter is constructed: a configured
endpoint outside the region refuses to start the process, not even for synthetic
data.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from compliance_rag.settings import REGION, Settings

_ALLOWED_HOST = re.compile(
    rf"(^|\.)({re.escape(REGION)})[-.]|^{re.escape(REGION)}-|\.{re.escape(REGION)}\."
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


class ResidencyError(RuntimeError):
    """Raised when a telemetry endpoint would send data outside the region."""


def assert_in_region(endpoint: str) -> None:
    """Refuse any exporter endpoint outside `australia-southeast1` (D-16).

    Localhost is allowed so tests and local development work. Anything else must
    name the region in its host.
    """
    if not endpoint:
        return
    host = urlparse(endpoint if "//" in endpoint else f"//{endpoint}").hostname or ""
    if host in _LOCAL_HOSTS:
        return
    if not _ALLOWED_HOST.search(host):
        raise ResidencyError(
            f"telemetry endpoint {endpoint!r} resolves to host {host!r}, which is not in "
            f"{REGION}. Telemetry follows the same residency rule as data (D-16): the only "
            "sink is a Cloud Logging regional bucket in Sydney. No offshore endpoint is "
            "configured at any phase, including for synthetic data."
        )


class StructuredLogSpanExporter(SpanExporter):
    """Writes every span as one structured log record keyed by `trace_id`.

    In Cloud Run this is picked up by the Cloud Logging agent and routed to the
    regional bucket. Locally it is a JSON line on stderr. Either way the span
    attributes — both channel candidate lists, fused list, rerank scores,
    cached-prefix hit, tokens, cost, verifier result — stay queryable.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("compliance_rag.spans")

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        for readable in spans:
            self._logger.info(json.dumps(self._record(readable), default=str))
        return SpanExportResult.SUCCESS

    @staticmethod
    def _record(span: ReadableSpan) -> dict[str, Any]:  # Any: span attributes are heterogeneous
        context = span.get_span_context()
        return {
            "severity": "INFO",
            "message": span.name,
            "trace_id": format(context.trace_id, "032x") if context else None,
            "span_id": format(context.span_id, "016x") if context else None,
            "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
            "start_time": span.start_time,
            "end_time": span.end_time,
            "status": span.status.status_code.name if span.status else None,
            "attributes": dict(span.attributes or {}),
        }

    def shutdown(self) -> None:
        logging.shutdown()


def _cloud_logging_exporter(settings: Settings) -> SpanExporter:
    """The only sink. Structured logs into the Sydney regional bucket."""
    handler: logging.Handler
    if settings.env == "cloud":
        # Cloud Run collects stdout into Cloud Logging; the regional bucket is a
        # sink configuration on the project, not a client setting.
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.StreamHandler(sys.stderr)
    logger = logging.getLogger("compliance_rag.spans")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return StructuredLogSpanExporter(logger)


SINK_BUILDERS: dict[str, Callable[[Settings], SpanExporter]] = {
    "cloud-logging": _cloud_logging_exporter,
}
"""Config only. A trace UI is added here by ADR, never at a call site (D-16)."""


def configure(settings: Settings) -> TracerProvider:
    """Build the tracer provider from `settings.sinks`. Idempotent per process."""
    for sink in settings.sinks:
        if sink not in SINK_BUILDERS:
            raise ValueError(
                f"unknown telemetry sink {sink!r}. Known sinks: {sorted(SINK_BUILDERS)}. "
                "Adding one requires an ADR (D-16)."
            )

    # Residency check before any exporter exists.
    assert_in_region(settings.reranker_url)
    assert_in_region(settings.qdrant_url)

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.otel_service_name,
                "cloud.region": settings.gcp_region,
                "deployment.environment": settings.env,
            }
        )
    )
    for sink in settings.sinks:
        provider.add_span_processor(SimpleSpanProcessor(SINK_BUILDERS[sink](settings)))
    trace.set_tracer_provider(provider)
    return provider


def tracer(name: str = "compliance_rag") -> trace.Tracer:
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any) -> Generator[trace.Span]:  # Any: OTel attribute values
    """Start a span with structured attributes.

    Every function touching an LLM, a retriever or the graph uses this
    (`CLAUDE.md` code standards).
    """
    with tracer().start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield current


def current_trace_id() -> str:
    """Hex trace ID for the active span, for storing on ledger rows."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x")
