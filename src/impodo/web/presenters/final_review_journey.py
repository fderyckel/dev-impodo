"""Select Final review locations without creating workflow decisions."""

from __future__ import annotations

from collections.abc import Mapping


def final_review_journey(
    query: Mapping[str, str],
    *,
    comparison_available: bool,
    preparation_approved: bool,
    comparison_recovery: bool = False,
    attention_required: bool = False,
) -> dict[str, str]:
    """Resolve explicit and older review links using existing saved state."""

    step = query.get("step", "")
    view = query.get("view", "")
    if step not in {"prepared", "compare", "results"}:
        if any(name in query for name in ("quality_status", "quality_dataset", "quality_page")):
            step = "prepared"
        elif any(name in query for name in ("status", "dataset", "issue", "page")):
            step = "results"
        elif comparison_recovery:
            step = "compare"
        elif comparison_available:
            step = "results"
        elif preparation_approved:
            step = "compare"
        else:
            step = "prepared"
    if view not in {"attention", "changes", "outside"}:
        status = query.get("status", "")
        if status in {"attention", "needs_review", "blocked"} or query.get("issue"):
            view = "attention"
        elif status in {"ready", "create", "update", "unchanged"}:
            view = "changes"
        else:
            view = "attention" if attention_required else "changes"
    return {"step": step, "view": view}
