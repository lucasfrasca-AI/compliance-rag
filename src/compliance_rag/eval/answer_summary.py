"""What a live answer-eval run measured (#31), computed from its recorded rows
— the numbers PHASES.md's exit criteria ask for, with the arithmetic shown."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

PLANNING_VOLUME = 2_000
"""Queries per month the cost projection is stated at (PHASES.md Phase 1)."""


def summarise(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:  # Any: recorded JSON rows
    answered = [r for r in rows if r.get("schema_valid")]
    citations = [c for r in answered for c in r["final_citations"]]
    verified_citations = sum(1 for c in citations if c["outcome"] == "verified")
    generations = [r["generation"] for r in answered]
    prefixed = [
        g for r, g in zip(answered, generations, strict=True) if r.get("tenant_id") == "acme"
    ]
    costs = [float(r["cost_usd"]) for r in answered]
    mean_cost = sum(costs) / len(costs) if costs else 0.0

    def mean(key: str) -> float:
        return (
            round(sum(int(g[key]) for g in generations) / len(generations), 1)
            if generations
            else 0.0
        )

    return {
        "queries": len(rows),
        "schema_valid": f"{len(answered)}/{len(rows)}",
        "answers_verified": f"{sum(1 for r in answered if r['verified'])}/{len(answered)}",
        "citations_verified": f"{verified_citations}/{len(citations)}",
        "repairs": sum(len(r["repairs"]) for r in answered),
        "mean_tokens": {
            "input_total": mean("input_tokens"),
            "cache_read": mean("cache_read_input_tokens"),
            "cache_write": mean("cache_creation_input_tokens"),
            "output": mean("output_tokens"),
        },
        "cache_hits_on_prefixed_requests": f"{sum(1 for g in prefixed if g['cached_prefix_hit'])}"
        f"/{len(prefixed)}",
        "mean_cost_per_query_usd": round(mean_cost, 6),
        "projected_monthly_usd": round(mean_cost * PLANNING_VOLUME, 2),
        "projection_arithmetic": f"{PLANNING_VOLUME} queries x ${mean_cost:.6f} mean "
        f"= ${mean_cost * PLANNING_VOLUME:.2f}/month",
    }
