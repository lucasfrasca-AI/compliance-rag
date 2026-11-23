"""How a check's decisions are shown (#301): one presentation of a decision, shared by
the decisions endpoint (`check_decisions.py`) and the report (`check_report.py`), so the page
and the JSON say the same thing from one value, and neither imports the other's router."""

from __future__ import annotations

from compliance_rag.adapters.interfaces import CheckDecision
from compliance_rag.api.deps import sydney_label


def standing_json(decision: CheckDecision | None) -> dict[str, object] | None:
    """A decision as a member is shown it: what, by whom, when. The time is converted here,
    once: `at` is the ISO instant, `at_label` the same moment in Sydney time as the page
    prints it. None for no decision."""
    if decision is None:
        return None
    moment = decision.decided_at
    return {
        "kind": decision.kind.value,
        "by": decision.email,
        "at": moment.isoformat() if moment else None,
        "at_label": sydney_label(moment) if moment else None,
        "trace_id": decision.trace_id,
    }
