# ADR-0017 — Telemetry follows the same residency rule as data

- Status: accepted
- Date: 2026-09-20
- Decision ID: `D-16` (`DECISIONS.md`)

## Context

A trace of this system contains the prompt, the retrieved chunks and the answer — the same customer data the stores are chosen to protect. Telemetry SaaS is where residency rules are most often quietly broken, usually starting with 'just for synthetic data'.

## Decision

OpenTelemetry instrumentation is written once; exporters are configuration in `telemetry.py` and adding one never touches a call site. The single sink is a Cloud Logging regional bucket in `australia-southeast1`, from day one. No offshore telemetry endpoint is ever configured, not even for synthetic data. Cloud Trace receives metadata only, because its regional storage is unverified. A trace UI is added later only by ADR — Phoenix on Cloud Run Sydney is the planned choice, self-hosted Langfuse the unlikely alternative. A startup assertion refuses to boot if an exporter endpoint is outside the region.

## Consequences

- One sink is simpler to defend to an auditor than a cut-over.
- Debugging happens in Logs Explorer until a named need justifies a trace UI.
- Span attributes are structured and queryable, which the eval harness depends on; a BigQuery sink is added when it needs SQL over spans.
- The residency assertion must run before any exporter is constructed.
