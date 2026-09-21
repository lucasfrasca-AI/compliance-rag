#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash). Reads the tool call as JSON on stdin.

The gate is built entirely out of `deny`, because `ask` decisions are not
enforced in this session type (see BUILD_LOG 2026-09-20): a hook returning
"ask" in a non-interactive session collapses into an allow, so the billable
protocol silently passed everything through. `deny` is enforced, so the guard
now fails closed.

- Denies destructive or forbidden commands outright.
- Denies anything billable UNLESS an unused approval line in
  .claude/billable-approvals.txt equals the command. On a match the line is
  marked `# used` and the command passes through once.
- Stays silent (exit 0, no output) for everything else, which lets the normal
  permission rules in .claude/settings.json decide.

Two properties keep this honest:

*Boundary anchoring.* A billable rule matches only at the start of a shell
segment — the start of the command, or immediately after `;`, `&&`, `||`, `|`
or a newline, with leading environment assignments stripped. Prose that merely
mentions a billable command (a heredoc writing BUILD_LOG, an ADR body) no
longer trips the guard.

*Full-command equality.* An approval authorises exactly one command string,
compared after whitespace normalisation. `<approved> && anything` does not
match; a chain must be approved as the whole chain.

Only the human writes approval lines. This script never adds one; it only
marks an existing line used. Claude is denied Edit/Write/Bash access to the
approvals file in .claude/settings.json; the Read tool is left open on purpose,
so a refused approval can be diagnosed. Reading grants nothing — only a line
already in the file authorises a command.
"""

import datetime
import json
import os
import pathlib
import re
import sys

APPROVALS = (
    pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")) / ".claude" / "billable-approvals.txt"
)

try:
    cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "") or ""
except Exception:
    sys.exit(0)


def out(decision: str, reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def m(pattern: str) -> bool:
    """Unanchored search. Used by the hard denies, which fail closed on purpose."""
    return re.search(pattern, cmd, re.I) is not None


# --- hard denies (unchanged: unanchored, deny-only, safe to over-match) ----
if m(r"gcloud\s+(projects|sql\s+instances|run\s+services|storage\s+buckets)\s+delete"):
    out(
        "deny",
        "Deleting cloud resources is not allowed from Claude Code. Ask the human to do it in the console.",
    )
if m(r"gcloud\s+iam\s+service-accounts\s+keys\s+create"):
    out(
        "deny",
        "Service-account keys are forbidden (SETUP.md §2). Cloud Run uses the SA directly; local dev uses ADC.",
    )
if m(r"dangerously-skip-permissions"):
    out("deny", "Never disable permissions.")
if m(r"gcloud\s+services\s+enable\s+\S*documentai"):
    out(
        "deny",
        "documentai is enabled only when a scanned PDF actually needs it (SERVICES.md). Ask the human.",
    )

# --- forbidden services (SERVICES.md 'Explicitly forbidden') ----------------
if m(
    r"gcloud\s+(ai\s+index|ai\s+index-endpoints|spanner|alloydb|discovery-engine|compute\s+instances\s+create)|matchingengine|vectorsearch"
):
    out(
        "deny",
        "That service is on the forbidden list in SERVICES.md (Vector Search, Spanner, AlloyDB, Agent Builder datastore, VMs).",
    )


# --- shell segmentation -----------------------------------------------------
SEGMENT_SPLIT = re.compile(r";|&&|\|\||\||\n")
ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)*")
LEADING_NOISE = re.compile(r"^(?:sudo|command|env|nohup|time|exec)\s+")


def segments(command: str) -> list[str]:
    """Shell segments, each stripped of env assignments and common prefixes."""
    result: list[str] = []
    for raw in SEGMENT_SPLIT.split(command):
        seg = raw.strip().lstrip("(){}& \t")
        seg = ENV_PREFIX.sub("", seg)
        while LEADING_NOISE.match(seg):
            seg = LEADING_NOISE.sub("", seg)
        if seg:
            result.append(seg)
    return result


SEGMENTS = segments(cmd)


def at_boundary(pattern: str) -> bool:
    """True if a rule matches at the start of any shell segment."""
    return any(re.match(pattern, seg, re.I) for seg in SEGMENTS)


# --- billable catalogue -----------------------------------------------------
# (regex, service, resource, region, est $/mo, SERVICES.md row / authority)
BILLABLE_RULES: list[tuple[str, str, str, str, str, str]] = [
    (
        r"gcloud\s+services\s+enable",
        "GCP API enablement",
        "one or more googleapis.com services",
        "n/a (project-wide)",
        "$0 — enabling an API bills nothing; the resources it unlocks do",
        "SETUP.md §2 (API enablement). Each unlocked service still needs its own approval.",
    ),
    (
        r"gcloud\s+iam\s+service-accounts\s+create",
        "IAM",
        "compliance-rag-runtime service account",
        "n/a (global)",
        "$0",
        "SETUP.md §2 (runtime service account). No key is ever created — see the hard deny above.",
    ),
    (
        r"gcloud\s+projects\s+add-iam-policy-binding",
        "IAM",
        "role binding on the project",
        "n/a (global)",
        "$0",
        "SETUP.md §2 (runtime service account roles).",
    ),
    (
        # Mutating verbs only. `describe`, `list` and `--help` create nothing, and
        # gating them blocks the verification that proves what was created.
        r"gcloud\s+iam\s+(?:workload-identity-pools\s+(?:(?:create|update|delete|undelete)\b"
        r"|providers\s+(?:create-\w+|update|delete|undelete)\b)"
        r"|service-accounts\s+add-iam-policy-binding\b)",
        "Workload Identity Federation",
        "pool / OIDC provider / impersonation binding",
        "global",
        "$0",
        "SERVICES.md: Workload Identity Federation row — Phase 0. Federation decides who "
        "outside GCP may act as a service account, so it is gated like any other grant.",
    ),
    (
        r"gcloud\s+run\s+deploy\s+reranker|gcloud\s+run\s+services\s+update\s+reranker",
        "Cloud Run",
        "reranker",
        "australia-southeast1",
        "$0–15 (scale-to-zero; min-instances 1 is a separate ask)",
        "SERVICES.md: 'Cloud Run service `reranker`' — Phase 1.",
    ),
    (
        r"gcloud\s+run\s+(deploy|services\s+update)",
        "Cloud Run",
        "service (name in the command)",
        "australia-southeast1",
        "$0–8 (scale-to-zero, max 2)",
        "SERVICES.md: 'Cloud Run service `compliance-rag-api`' — Phase 3.",
    ),
    (
        r"gcloud\s+run\s+jobs\s+(create|deploy|update|execute)",
        "Cloud Run jobs",
        "job (name in the command)",
        "australia-southeast1",
        "$0 (scale-to-zero, included in the Cloud Run row)",
        "SERVICES.md: 'Cloud Run jobs (`ingest`, `eval`, `feed-poll`)' — Phase 1.",
    ),
    (
        r"gcloud\s+storage\s+buckets\s+create|gsutil\s+mb",
        "Cloud Storage",
        "<project>-corpus (versioned)",
        "australia-southeast1",
        "<$1",
        "SERVICES.md: 'Cloud Storage bucket `<project>-corpus`' — Phase 1.",
    ),
    (
        r"gcloud\s+logging\s+buckets\s+create",
        "Cloud Logging",
        "compliance-rag-logs (regional log bucket)",
        "australia-southeast1",
        "$0 (50 GiB free tier)",
        "SERVICES.md: 'Cloud Logging regional log bucket' — Phase 0. The only telemetry sink (D-16).",
    ),
    (
        r"gcloud\s+logging\s+(sinks\s+(create|update|delete)|buckets\s+update)",
        "Cloud Logging",
        "log router sink / bucket configuration",
        "australia-southeast1",
        "$0 (routing is free; storage stays inside the 50 GiB free tier)",
        "SERVICES.md: 'Cloud Logging regional log bucket' — Phase 0. Where logs are routed "
        "decides residency, so a sink change is gated exactly like creating the bucket (D-16).",
    ),
    (
        r"gcloud\s+sql\s+(instances\s+(create|patch)|databases\s+create)",
        "Cloud SQL",
        "db-f1-micro, 10 GB SSD, no HA, no replica",
        "australia-southeast1",
        "~$9–10",
        "SERVICES.md: 'Cloud SQL Postgres `db-f1-micro`' — Phase 3.",
    ),
    (
        r"gcloud\s+artifacts\s+repositories\s+create",
        "Artifact Registry",
        "compliance-rag (Docker repository)",
        "australia-southeast1",
        "$0 (free tier)",
        "SERVICES.md: 'Cloud Build + Artifact Registry' — Phase 1 (needed for the reranker image).",
    ),
    (
        r"gcloud\s+builds\s+submit|docker\s+push",
        "Cloud Build / Artifact Registry",
        "image build and push",
        "australia-southeast1",
        "$0 (free tier)",
        "SERVICES.md: 'Cloud Build + Artifact Registry' — Phase 1.",
    ),
    (
        r"gcloud\s+secrets\s+(create|versions\s+add)",
        "Secret Manager",
        "secret (name in the command)",
        "australia-southeast1",
        "$0 (≤6 secrets free)",
        "SERVICES.md: 'Secret Manager' — Phase 3.",
    ),
    (
        r"gcloud\s+scheduler\s+jobs\s+(create|update)",
        "Cloud Scheduler",
        "job (name in the command)",
        "australia-southeast1",
        "$0 (≤3 jobs free)",
        "SERVICES.md: 'Cloud Scheduler (≤3 jobs)' — Phase 4.",
    ),
    (
        r"gcloud\s+workflows\s+deploy",
        "Cloud Workflows",
        "workflow (name in the command)",
        "australia-southeast1",
        "$0 (free tier)",
        "SERVICES.md: 'Cloud Workflows' — Phase 4.",
    ),
    (
        r"gcloud\s+pubsub\s+topics\s+create",
        "Pub/Sub",
        "budget-alerts",
        "australia-southeast1",
        "$0",
        "SERVICES.md: 'Cloud Billing budget + Pub/Sub topic `budget-alerts`' — Phase 1.",
    ),
    (
        r"gcloud\s+billing\s+budgets\s+create",
        "Cloud Billing",
        "budget",
        "n/a (global)",
        "$0",
        "SERVICES.md: 'Cloud Billing budget' — created by the human, not by Claude Code.",
    ),
    (
        r"(?:terraform\s+(apply|destroy)|firebase\s+deploy)",
        "Infrastructure deploy",
        "whatever the plan or firebase.json declares",
        "australia-southeast1",
        "unknown — depends on the plan",
        "SERVICES.md: every resource in the plan must have its own row and be phase-unlocked.",
    ),
]


def classify() -> tuple[str, str, str, str, str] | None:
    for pattern, service, resource, region, est, row in BILLABLE_RULES:
        if at_boundary(pattern):
            return service, resource, region, est, row
    return None


def norm(text: str) -> str:
    """Whitespace-normalised form used for approval equality."""
    return " ".join(text.split())


def read_lines() -> list[str]:
    try:
        APPROVALS.parent.mkdir(parents=True, exist_ok=True)
        if not APPROVALS.exists():
            APPROVALS.write_text(
                "# Billable approvals. ONLY the human edits this file.\n"
                "# One approval per line:  <ISO date> | <exact full command>\n"
                "# Matching is full-command equality after whitespace normalisation.\n"
                "# The guard marks a line `# used` after it authorises one command.\n"
            )
        return APPROVALS.read_text().splitlines()
    except Exception:
        return []


def find_approval(command: str, lines: list[str]) -> tuple[int, str] | None:
    """First unused approval line whose command equals this one."""
    target = norm(command)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "# used" in line:
            continue
        if "|" not in line:
            continue
        _date, _, approved = line.partition("|")
        approved = approved.strip()
        if approved and norm(approved) == target:
            return i, approved
    return None


def mark_used(index: int, lines: list[str]) -> None:
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    lines[index] = f"{lines[index]}  # used {stamp}"
    APPROVALS.write_text("\n".join(lines) + "\n")


meta = classify()
if meta is not None:
    service, resource, region, est, row = meta
    lines = read_lines()
    hit = find_approval(cmd, lines)
    if hit is not None:
        index, _approved = hit
        try:
            mark_used(index, lines)
        except Exception:
            out(
                "deny",
                "An approval matched but the approvals file could not be updated. "
                "Refusing to run a billable command without consuming its approval.",
            )
        sys.exit(0)  # approved once — pass through

    today = datetime.date.today().isoformat()
    out(
        "deny",
        "BILLABLE — BLOCKED. No unused approval line matches this command.\n\n"
        f"  Service:      {service}\n"
        f"  Resource:     {resource}\n"
        f"  Region:       {region}\n"
        f"  Est. $/month: {est}\n"
        f"  Authority:    {row}\n\n"
        "To approve exactly this one command, paste the following line into\n"
        ".claude/billable-approvals.txt by hand (Claude cannot write that file):\n\n"
        f"{today} | {norm(cmd)}\n\n"
        "Matching is full-command equality, so appending anything to the command\n"
        "invalidates the approval. The guard consumes the line on first use.",
    )

sys.exit(0)
