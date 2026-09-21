#!/usr/bin/env python3
"""Phase 0 trace check: prove a span reaches Sydney and does NOT reach _Default.

Both halves are required (`PHASES.md` Phase 0 exit criteria). A span that
arrives in `compliance-rag-logs` while a copy also sits in the global `_Default`
bucket is a residency breach, so a one-sided pass is a failure here (D-16).

Emits one span through the same `telemetry.py` path the application uses, then
reads it back out of each bucket by `trace_id`.

Exit codes
    0  span in compliance-rag-logs, absent from _Default
    1  assertion failed (missing where required, or present where forbidden)
    2  setup problem: no credentials, missing env, API error
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

REGION = "australia-southeast1"
BUCKET = "compliance-rag-logs"
LOG_NAME = "compliance_rag.spans"
POLL_SECONDS = 15
POLL_ATTEMPTS = 12  # up to 3 minutes; Cloud Logging ingest is not instant


def fail(code: int, message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)
    sys.exit(code)


def gcloud(*args: str) -> str:
    result = subprocess.run(
        ["gcloud", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "CLOUDSDK_CORE_DISABLE_PROMPTS": "1"},
    )
    if result.returncode != 0:
        fail(2, f"gcloud {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def require_auth(project: str) -> None:
    """Fail loudly on auth problems rather than silently reporting no entries.

    An unauthenticated run would find zero entries in both buckets, which would
    look like the _Default half passing. That must never read as success.
    """
    account = gcloud("auth", "list", "--filter=status:ACTIVE", "--format=value(account)")
    if not account:
        fail(2, "no active gcloud credentials: Workload Identity Federation auth did not complete")
    print(f"authenticated as {account}")
    if not project:
        fail(2, "GCP_PROJECT_ID is empty")
    probe = gcloud(
        "logging", "buckets", "describe", BUCKET, f"--location={REGION}", "--format=value(name)"
    )
    if BUCKET not in probe:
        fail(2, f"cannot read log bucket {BUCKET}: {probe!r}")
    print(f"can read {BUCKET}")


def emit_span(project: str) -> str:
    """Emit one span through telemetry.py and return its trace_id."""
    sys.path.insert(0, "src")
    from compliance_rag.settings import Settings
    from compliance_rag.telemetry import configure, current_trace_id, span

    settings = Settings(env="ci", gcp_project_id=project)
    configure(settings)
    with span("ci.trace_check", **{"ci.run_id": os.environ.get("GITHUB_RUN_ID", "local")}):
        trace_id = current_trace_id()
    print(f"emitted span with trace_id={trace_id}")
    return trace_id


def count_entries(project: str, trace_id: str, bucket: str, location: str) -> int:
    out = gcloud(
        "logging",
        "read",
        f'jsonPayload.trace_id="{trace_id}"',
        f"--bucket={bucket}",
        f"--location={location}",
        "--view=_AllLogs",  # a bare view name; gcloud builds the full path
        "--format=value(insertId)",
        "--limit=10",
    )
    return len([line for line in out.splitlines() if line.strip()])


def main() -> None:
    project = os.environ.get("GCP_PROJECT_ID", "")
    require_auth(project)
    trace_id = emit_span(project)

    found_regional = 0
    for attempt in range(1, POLL_ATTEMPTS + 1):
        found_regional = count_entries(project, trace_id, BUCKET, REGION)
        if found_regional:
            print(f"found in {BUCKET} after {attempt * POLL_SECONDS}s")
            break
        time.sleep(POLL_SECONDS)

    # _Default is global. Read it only after the regional read succeeded, so a
    # slow ingest cannot make the exclusion look effective.
    found_default = count_entries(project, trace_id, "_Default", "global")

    print(f"\ntrace_id       : {trace_id}")
    print(f"{BUCKET}: {found_regional} entries (want >= 1)")
    print(f"_Default       : {found_default} entries (want 0)")

    problems: list[str] = []
    if found_regional < 1:
        problems.append(
            f"span {trace_id} never arrived in {BUCKET}. The sink "
            "compliance-rag-to-regional may not match logName "
            f'"projects/{project}/logs/{LOG_NAME}".'
        )
    if found_default > 0:
        problems.append(
            f"span {trace_id} also landed in the global _Default bucket. The exclusion "
            "compliance-rag-not-default is missing or its filter does not match. This is a "
            "residency breach (D-16) regardless of what reached Sydney."
        )
    if problems:
        for problem in problems:
            print(f"::error::{problem}", file=sys.stderr)
        sys.exit(1)

    print("\nPASS: span reached Sydney and did not reach _Default")


if __name__ == "__main__":
    main()
