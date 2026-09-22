#!/usr/bin/env python3
"""Phase 0 trace check: prove a span reaches Sydney and does NOT reach _Default.

Both halves are required (`PHASES.md` Phase 0 exit criteria). A span that
arrives in `compliance-rag-logs` while a copy also sits in the global `_Default`
bucket is a residency breach, so a one-sided pass is a failure here (D-16).

Emits one span through the same `telemetry.py` path the application uses, then
reads it back out of each bucket by `trace_id`.

Two different clocks matter, and conflating them produces a misleading failure:

  Log ingest      seconds. A span written now is readable within about half a
                  minute, so INGEST_BUDGET_SECONDS covers the regional read.
  Sink exclusion  minutes. Measured at roughly 11 minutes in this project
                  (BUILD_LOG 2026-09-21); checks at 1 and 7 minutes still saw
                  the leak. A span appearing in _Default shortly after the
                  exclusion was written therefore says nothing about whether
                  the exclusion is correct.

So the check reads the exclusion's configuration directly. Missing exclusion,
or a filter that does not match, is a hard failure. A correct exclusion written
inside PROPAGATION_GRACE_SECONDS still fails the run — routing is not proven
until a span stays out — but says plainly that the cause is propagation and
that re-running later is the fix, rather than sending someone to debug a filter
that is already right.

Exit codes
    0  span in compliance-rag-logs, absent from _Default
    1  assertion failed (missing where required, or present where forbidden)
    2  setup problem: no credentials, missing env, API error
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time

REGION = "australia-southeast1"
BUCKET = "compliance-rag-logs"
LOG_NAME = "compliance_rag.spans"
EXCLUSION_NAME = "compliance-rag-not-default"

POLL_SECONDS = 15
INGEST_BUDGET_SECONDS = 180  # log ingest only; nothing to do with the exclusion
PROPAGATION_GRACE_SECONDS = 20 * 60  # measured ~11 min; 20 gives honest headroom


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


def exclusion_state(project: str) -> tuple[bool, bool, float]:
    """(exists, filter_matches, seconds_since_the__Default_sink_was_updated)."""
    sink = json.loads(gcloud("logging", "sinks", "describe", "_Default", "--format=json"))
    expected = f'logName="projects/{project}/logs/{LOG_NAME}"'
    match = next(
        (e for e in sink.get("exclusions", []) if e.get("name") == EXCLUSION_NAME),
        None,
    )
    try:
        updated = dt.datetime.fromisoformat(sink.get("updateTime", "").replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.UTC) - updated).total_seconds()
    except ValueError:
        age = float("inf")
    if match is None:
        return False, False, age
    return True, match.get("filter", "") == expected, age


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


def count_entries(trace_id: str, bucket: str, location: str) -> int:
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

    exists, filter_ok, age = exclusion_state(project)
    age_minutes = age / 60
    print(
        f"exclusion {EXCLUSION_NAME}: exists={exists} filter_matches={filter_ok} "
        f"_Default sink updated {age_minutes:.1f} min ago"
    )

    trace_id = emit_span(project)

    found_regional = 0
    deadline = time.monotonic() + INGEST_BUDGET_SECONDS
    while True:
        found_regional = count_entries(trace_id, BUCKET, REGION)
        if found_regional or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    if found_regional:
        print(f"found in {BUCKET}")

    # _Default is global. Read it only after the regional read, so slow ingest
    # cannot make the exclusion look effective.
    found_default = count_entries(trace_id, "_Default", "global")

    print(f"\ntrace_id            : {trace_id}")
    print(f"{BUCKET} : {found_regional} entries (want >= 1)")
    print(f"_Default            : {found_default} entries (want 0)")

    problems: list[str] = []
    if found_regional < 1:
        problems.append(
            f"span {trace_id} never arrived in {BUCKET} within {INGEST_BUDGET_SECONDS}s. "
            "The sink compliance-rag-to-regional may not match logName "
            f'"projects/{project}/logs/{LOG_NAME}".'
        )
    if found_default > 0:
        if not exists:
            problems.append(
                f"span {trace_id} landed in the global _Default bucket and the exclusion "
                f"{EXCLUSION_NAME} does not exist. Residency breach (D-16)."
            )
        elif not filter_ok:
            problems.append(
                f"span {trace_id} landed in the global _Default bucket. The exclusion "
                f"{EXCLUSION_NAME} exists but its filter does not match "
                f'logName="projects/{project}/logs/{LOG_NAME}". Residency breach (D-16).'
            )
        elif age < PROPAGATION_GRACE_SECONDS:
            problems.append(
                f"span {trace_id} landed in the global _Default bucket, but the exclusion "
                f"{EXCLUSION_NAME} is correctly configured and the _Default sink was updated "
                f"only {age_minutes:.1f} minutes ago. Sink exclusions have taken about 11 "
                "minutes to take effect in this project, so this is most likely propagation "
                "rather than a misconfiguration: re-run once the sink is at least "
                f"{PROPAGATION_GRACE_SECONDS // 60} minutes old. Failing anyway, because "
                "routing is not proven until a span stays out of _Default."
            )
        else:
            problems.append(
                f"span {trace_id} landed in the global _Default bucket even though the "
                f"exclusion {EXCLUSION_NAME} is correct and {age_minutes:.0f} minutes old, "
                "well past any propagation window. Residency breach (D-16)."
            )

    if problems:
        for problem in problems:
            print(f"::error::{problem}", file=sys.stderr)
        sys.exit(1)

    print("\nPASS: span reached Sydney and did not reach _Default")


if __name__ == "__main__":
    main()
