"""Present Stage 3 relationship-resolution coverage without business keys."""

from __future__ import annotations

from typing import Any, Iterable

from impodo.domain.mapping.contracts import ResolverOrigin
from impodo.domain.matching_order import MatchingOrderCheck
from impodo.domain.relationship_health import (
    RelationshipHealthNotCheckableReason,
    RelationshipHealthResult,
)


_UNCHECKED_MESSAGES = {
    RelationshipHealthNotCheckableReason.MAPPING_INCOMPLETE: (
        "Save a complete relationship provider and matching rule first."
    ),
    RelationshipHealthNotCheckableReason.UNCONFIRMED_MATCHING_RULE: (
        "Confirm the related record's Odoo matching rule before testing values."
    ),
    RelationshipHealthNotCheckableReason.UNSUPPORTED_RELATIONSHIP: (
        "This relationship shape cannot be simulated safely yet."
    ),
    RelationshipHealthNotCheckableReason.ONE2MANY_INVERSE_REQUIRED: (
        "Odoo fills this list from child records. Map the child table's inverse "
        "Many2one field instead."
    ),
    RelationshipHealthNotCheckableReason.RELATED_DATASET_UNAVAILABLE: (
        "Choose the incoming parent table that owns these related records."
    ),
    RelationshipHealthNotCheckableReason.RELATED_IDENTITY_UNAVAILABLE: (
        "Save a direct identity for the incoming related table before testing it."
    ),
    RelationshipHealthNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE: (
        "Impodo could not read the required columns from the frozen source."
    ),
    RelationshipHealthNotCheckableReason.EVIDENCE_LIMIT_EXCEEDED: (
        "This relationship has more distinct keys than the bounded authoring "
        "check supports. Final review remains required."
    ),
    RelationshipHealthNotCheckableReason.ODOO_SCHEMA_CHANGED: (
        "The Odoo fields changed. Return to Odoo data before checking again."
    ),
}


def relationship_health_view(
    *,
    workspace_id: str,
    dataset_views: Iterable[dict[str, Any]],
    live_check: MatchingOrderCheck | None,
    live_check_current: bool,
    check_attempt: Any,
    working_draft_is_current: bool,
) -> dict[str, object] | None:
    """Return one business-facing card per saved relationship mapping."""

    views = tuple(dataset_views)
    view_by_id = {view["source"].dataset_id: view for view in views}
    mappings = tuple(
        (view, relationship)
        for view in views
        if not bool(view.get("odoo_pinned")) and view.get("mapping") is not None
        for relationship in view["mapping"].relationships
    )
    if not mappings:
        return None
    current_results = (
        {
            (item.owner_dataset_id, item.target_field): item
            for item in live_check.relationship_health_results
        }
        if live_check is not None and live_check_current
        else {}
    )
    running = bool(check_attempt is not None and check_attempt.active)
    schema_changed = bool(live_check is not None and live_check.schema_changed)
    cards = tuple(
        _relationship_card(
            view,
            relationship,
            current_results.get(
                (view["source"].dataset_id, relationship.target_field)
            ),
            view_by_id=view_by_id,
            has_check=live_check is not None,
            live_check_current=live_check_current,
            running=running,
            schema_changed=schema_changed,
        )
        for view, relationship in mappings
    )
    return {
        "cards": cards,
        "running": running,
        "can_start": working_draft_is_current and not running,
        "check_url": f"/workspaces/{workspace_id}/mapping/order/check",
        "ready_count": sum(bool(card["ready"]) for card in cards),
        "attention_count": sum(bool(card["blocked_count"]) for card in cards),
        "message": (
            check_attempt.message
            if running
            else (
                "Save the current matching choices before checking relationships."
                if not working_draft_is_current
                else "One read-only check tests every saved relationship."
            )
        ),
    }


def _relationship_card(
    view: dict[str, Any],
    relationship: Any,
    result: RelationshipHealthResult | None,
    *,
    view_by_id: dict[str, dict[str, Any]],
    has_check: bool,
    live_check_current: bool,
    running: bool,
    schema_changed: bool,
) -> dict[str, object]:
    source = view["source"]
    field = next(
        (
            item
            for item in (view["model"].fields if view.get("model") is not None else ())
            if item.name == relationship.target_field
        ),
        None,
    )
    related_view = view_by_id.get(relationship.resolver.dataset_id)
    related_model_label = _related_model_label(view, result, relationship, field)
    field_label = field.label if field is not None else relationship.target_field
    origin_label = _origin_label(
        relationship.resolver.origin,
        related_model_label=related_model_label,
        related_dataset_name=(
            related_view["source"].name if related_view is not None else "incoming table"
        ),
    )
    status_label, status_class, message = _relationship_state(
        result,
        source_name=source.name,
        field_label=field_label,
        related_model_label=related_model_label,
        origin_label=origin_label,
        has_check=has_check,
        live_check_current=live_check_current,
        running=running,
        schema_changed=schema_changed,
    )
    checked = result if result is not None and result.checked else None
    inverse_action = _one2many_inverse_action(
        field,
        view_by_id,
    )
    edit_url = (
        f"{view['edit_url']}#relationship-"
        f"{view['index']}-{relationship.target_field}"
    )
    return {
        "dataset_name": source.name,
        "field_label": field_label,
        "technical_field": relationship.target_field,
        "related_model_label": related_model_label,
        "relationship_kind": relationship.kind,
        "required": bool(relationship.required or relationship.required_on_create),
        "origin_label": origin_label,
        "show_target_count": (
            relationship.resolver.origin is not ResolverOrigin.DATASET
        ),
        "show_incoming_count": (
            relationship.resolver.origin is not ResolverOrigin.TARGET_CATALOG
        ),
        "status_label": status_label,
        "status_class": status_class,
        "message": message,
        "checked": checked is not None,
        "ready": bool(checked is not None and checked.ready),
        "source_row_count": checked.source_row_count if checked else 0,
        "populated_choice_count": checked.populated_choice_count if checked else 0,
        "blank_row_count": checked.blank_row_count if checked else 0,
        "incomplete_row_count": checked.incomplete_row_count if checked else 0,
        "target_count": checked.target_count if checked else 0,
        "incoming_count": checked.incoming_count if checked else 0,
        "missing_count": checked.missing_count if checked else 0,
        "ambiguous_count": checked.ambiguous_count if checked else 0,
        "case_mismatch_count": checked.case_mismatch_count if checked else 0,
        "blocked_count": checked.blocked_count if checked else 0,
        "edit_url": edit_url,
        "edit_label": (
            f"Review {checked.blocked_count:,} unresolved choice"
            f"{'s' if checked.blocked_count != 1 else ''}"
            if checked is not None and checked.blocked_count
            else "Review relationship"
        ),
        "related_url": (
            related_view["edit_url"] if related_view is not None else None
        ),
        "related_action_label": (
            f"Open {related_view['source'].name}"
            if related_view is not None
            else None
        ),
        "inverse_action": inverse_action,
    }


def _relationship_state(
    result: RelationshipHealthResult | None,
    *,
    source_name: str,
    field_label: str,
    related_model_label: str,
    origin_label: str,
    has_check: bool,
    live_check_current: bool,
    running: bool,
    schema_changed: bool,
) -> tuple[str, str, str]:
    if running:
        return (
            "Checking Odoo",
            "review",
            "Impodo is testing the saved relationship against current data.",
        )
    if has_check and not live_check_current:
        return (
            "Needs refresh",
            "review",
            (
                "The Odoo fields changed. Return to Odoo data before checking again."
                if schema_changed
                else "The source or saved relationship changed. Check again."
            ),
        )
    if result is not None and result.checked:
        if result.blocked_count:
            return (
                "Needs attention",
                "review",
                (
                    f"{source_name} has {result.blocked_count:,} unresolved "
                    f"{field_label} choice"
                    f"{'s' if result.blocked_count != 1 else ''}. "
                    f"The expected parent is {origin_label}."
                ),
            )
        return (
            "Ready",
            "registered",
            (
                f"Every populated {field_label} choice resolves to exactly one "
                f"{related_model_label}."
            ),
        )
    if result is not None:
        return (
            "Chosen - not tested",
            "review",
            _UNCHECKED_MESSAGES.get(
                result.not_checkable_reason,
                "This saved relationship could not be tested safely.",
            ),
        )
    return (
        "Chosen - not tested",
        "review",
        "Run the read-only check to test this saved relationship.",
    )


def _origin_label(
    origin: ResolverOrigin,
    *,
    related_model_label: str,
    related_dataset_name: str,
) -> str:
    origin = ResolverOrigin(origin)
    if origin is ResolverOrigin.TARGET_CATALOG:
        return f"an existing Odoo {related_model_label}"
    if origin is ResolverOrigin.DATASET:
        return f"one record in {related_dataset_name}"
    return (
        f"an existing Odoo {related_model_label}, or otherwise one record in "
        f"{related_dataset_name}"
    )


def _related_model_label(
    view: dict[str, Any],
    result: RelationshipHealthResult | None,
    relationship: Any,
    field: Any,
) -> str:
    technical = (
        result.related_model
        if result is not None and result.related_model != "unknown"
        else relationship.resolver.model
        or (field.relation if field is not None else "")
    )
    model = next(
        (item for item in view.get("models", ()) if item.name == technical),
        None,
    )
    return model.label if model is not None else technical or "related record"


def _one2many_inverse_action(
    field: Any,
    view_by_id: dict[str, dict[str, Any]],
) -> dict[str, str] | None:
    if field is None or field.type != "one2many" or not field.relation_field:
        return None
    child = next(
        (
            view
            for view in view_by_id.values()
            if view.get("selected_model") == field.relation
        ),
        None,
    )
    if child is None:
        return None
    return {
        "url": (
            f"{child['edit_url']}#relationship-"
            f"{child['index']}-{field.relation_field}"
        ),
        "label": (
            f"Open {child['source'].name} -> {field.relation_field}"
        ),
    }
