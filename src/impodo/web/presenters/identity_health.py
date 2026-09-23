"""Project aggregate Stage 3 identity evidence for data managers."""

from __future__ import annotations

from typing import Any, Iterable

from impodo.domain.matching_order import (
    MatchingIdentityNotCheckableReason,
    MatchingIdentityResult,
    MatchingOrderCheck,
)


_UNCHECKED_MESSAGES = {
    MatchingIdentityNotCheckableReason.MAPPING_INCOMPLETE: (
        "Save a complete source identifier and Odoo matching rule first."
    ),
    MatchingIdentityNotCheckableReason.UNCONFIRMED_MATCHING_RULE: (
        "Confirm this Odoo matching rule before testing current records."
    ),
    MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY: (
        "This identity depends on a related Odoo record. Impodo will prove "
        "that complete scope in the relationship-checking slice."
    ),
    MatchingIdentityNotCheckableReason.UNSUPPORTED_TARGET_FIELD: (
        "This matching field type is not included in the direct identity "
        "check yet."
    ),
    MatchingIdentityNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE: (
        "Impodo could not read the required columns from the current frozen "
        "source."
    ),
    MatchingIdentityNotCheckableReason.ODOO_PINNED_SELECTION: (
        "These rows stay bound through protected Odoo selection evidence."
    ),
    MatchingIdentityNotCheckableReason.ODOO_SCHEMA_CHANGED: (
        "The Odoo fields changed. Return to Odoo data before checking again."
    ),
}


def identity_health_view(
    *,
    workspace_id: str,
    dataset_views: Iterable[dict[str, Any]],
    live_check: MatchingOrderCheck | None,
    live_check_current: bool,
    check_attempt: Any,
    working_draft_is_current: bool,
) -> dict[str, object] | None:
    """Return one card per file-backed mapping without exposing key values."""

    eligible = tuple(
        view for view in dataset_views if not bool(view.get("odoo_pinned"))
    )
    if not eligible:
        return None
    current_results = (
        {item.dataset_id: item for item in live_check.identity_results}
        if live_check is not None and live_check_current
        else {}
    )
    running = bool(check_attempt is not None and check_attempt.active)
    schema_changed = bool(live_check is not None and live_check.schema_changed)
    cards = tuple(
        _identity_card(
            view,
            current_results.get(view["source"].dataset_id),
            has_check=live_check is not None,
            live_check_current=live_check_current,
            running=running,
            schema_changed=schema_changed,
        )
        for view in eligible
    )
    return {
        "cards": cards,
        "running": running,
        "can_start": working_draft_is_current and not running,
        "check_url": f"/workspaces/{workspace_id}/mapping/order/check",
        "checked_count": sum(bool(card["checked"]) for card in cards),
        "attention_count": sum(bool(card["blocked_count"]) for card in cards),
        "message": (
            check_attempt.message
            if running
            else (
                "Save the current matching choices before checking Odoo."
                if not working_draft_is_current
                else "One read-only check tests every saved direct identity."
            )
        ),
    }


def _identity_card(
    view: dict[str, Any],
    result: MatchingIdentityResult | None,
    *,
    has_check: bool,
    live_check_current: bool,
    running: bool,
    schema_changed: bool,
) -> dict[str, object]:
    source = view["source"]
    mapping = view.get("mapping")
    selected_key = view.get("selected_key")
    labels = view.get("matching_rule_labels", {})
    rule_label = (
        labels.get(selected_key.key_id, selected_key.description)
        if selected_key is not None
        else "No confirmed matching rule"
    )
    if not rule_label and selected_key is not None:
        rule_label = " + ".join(
            (*selected_key.key_fields, *selected_key.scope_fields)
        )
    chosen = bool(mapping is not None and selected_key is not None)
    status_label, status_class, message = _identity_state(
        result,
        chosen=chosen,
        has_check=has_check,
        live_check_current=live_check_current,
        running=running,
        schema_changed=schema_changed,
    )
    result_counts = result if result is not None and result.checked else None
    return {
        "dataset_id": source.dataset_id,
        "dataset_name": source.name,
        "model_label": (
            view["model"].label
            if view.get("model") is not None
            else view.get("selected_model", "Odoo")
        ),
        "rule_label": rule_label,
        "rule_basis": _rule_basis(selected_key),
        "status_label": status_label,
        "status_class": status_class,
        "message": message,
        "checked": result_counts is not None,
        "source_row_count": (
            result_counts.source_row_count if result_counts else 0
        ),
        "source_unique_row_count": (
            result_counts.source_unique_row_count if result_counts else 0
        ),
        "source_blank_row_count": (
            result_counts.source_blank_row_count if result_counts else 0
        ),
        "source_repeated_row_count": (
            result_counts.source_repeated_row_count if result_counts else 0
        ),
        "source_repeated_group_count": (
            result_counts.source_repeated_group_count if result_counts else 0
        ),
        "target_unique_key_count": (
            result_counts.target_unique_key_count if result_counts else 0
        ),
        "target_ambiguous_key_count": (
            result_counts.target_ambiguous_key_count if result_counts else 0
        ),
        "expected_new_count": (
            result_counts.expected_new_count if result_counts else 0
        ),
        "expected_existing_count": (
            result_counts.expected_existing_count if result_counts else 0
        ),
        "blocked_count": result_counts.blocked_count if result_counts else 0,
        "edit_url": f"{view['edit_url']}#target-identity-{view['index']}",
    }


def _identity_state(
    result: MatchingIdentityResult | None,
    *,
    chosen: bool,
    has_check: bool,
    live_check_current: bool,
    running: bool,
    schema_changed: bool,
) -> tuple[str, str, str]:
    if running:
        return (
            "Checking Odoo",
            "review",
            "Impodo is checking the saved identity against current data.",
        )
    if has_check and not live_check_current:
        return (
            "Needs refresh",
            "review",
            (
                "The Odoo fields changed. Return to Odoo data before checking "
                "again."
                if schema_changed
                else "The source or saved matching choices changed. Check again."
            ),
        )
    if result is not None and result.checked:
        if result.blocked_count:
            return (
                "Tested - needs attention",
                "review",
                f"{result.blocked_count:,} source row"
                f"{'s' if result.blocked_count != 1 else ''} cannot be "
                "identified safely with this rule.",
            )
        return (
            "Tested on current data",
            "registered",
            "Every included source row has one usable identity outcome.",
        )
    if result is not None:
        return (
            "Chosen - not tested",
            "review",
            _UNCHECKED_MESSAGES[result.not_checkable_reason],
        )
    if chosen:
        return (
            "Chosen - not tested",
            "review",
            "Run the read-only Odoo check to test this saved rule.",
        )
    return (
        "Suggested",
        "",
        "Save the source identifier and Odoo matching rule before testing it.",
    )


def _rule_basis(selected_key: Any) -> str:
    if selected_key is None:
        return "No governed rule"
    if selected_key.recommendation_basis == "ODOO_ENFORCED":
        return "Odoo-enforced rule"
    if selected_key.recommendation_basis == "CURATED_CONVENTION":
        return "Reviewed Odoo convention"
    return "Chosen by the data manager"
