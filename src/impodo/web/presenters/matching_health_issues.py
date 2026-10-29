"""Project current Stage 3 health evidence into the shared issue journey."""

from __future__ import annotations

from collections.abc import Mapping

from impodo.application.workspace.issues import (
    WorkflowIssueOrigin,
    WorkflowIssueProjection,
    WorkflowIssueSeverity,
    WorkflowIssueState,
    WorkflowIssueSummary,
    summarize_workflow_issues,
)
from impodo.application.workspace.mapping.health_issues import (
    identity_health_issue_severity,
    relationship_health_issue_severity,
)
from impodo.domain.matching_order import (
    MatchingIdentityNotCheckableReason,
    MatchingOrderCheck,
)
from impodo.domain.relationship_health import RelationshipHealthNotCheckableReason

_IDENTITY_REVIEW_CAUSES = {
    MatchingIdentityNotCheckableReason.MAPPING_INCOMPLETE.value: (
        "The saved identity is incomplete"
    ),
    MatchingIdentityNotCheckableReason.UNCONFIRMED_MATCHING_RULE.value: (
        "The Odoo matching rule is not confirmed"
    ),
    MatchingIdentityNotCheckableReason.INDIRECT_IDENTITY.value: (
        "The identity contains a related-record component"
    ),
    MatchingIdentityNotCheckableReason.UNSUPPORTED_TARGET_FIELD.value: (
        "The identity uses a field that this bounded check cannot test"
    ),
    MatchingIdentityNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE.value: (
        "Current source identity evidence is unavailable"
    ),
    MatchingIdentityNotCheckableReason.ODOO_SCHEMA_CHANGED.value: (
        "The captured Odoo identity fields changed"
    ),
}

_RELATIONSHIP_REVIEW_CAUSES = {
    RelationshipHealthNotCheckableReason.MAPPING_INCOMPLETE.value: (
        "The saved relationship is incomplete"
    ),
    RelationshipHealthNotCheckableReason.UNCONFIRMED_MATCHING_RULE.value: (
        "The related record's matching rule is not confirmed"
    ),
    RelationshipHealthNotCheckableReason.UNSUPPORTED_RELATIONSHIP.value: (
        "This relationship shape is not supported by the bounded check"
    ),
    RelationshipHealthNotCheckableReason.ONE2MANY_INVERSE_REQUIRED.value: (
        "This One2many needs the child table's inverse relationship"
    ),
    RelationshipHealthNotCheckableReason.RELATED_DATASET_UNAVAILABLE.value: (
        "The incoming related table is not selected"
    ),
    RelationshipHealthNotCheckableReason.RELATED_IDENTITY_UNAVAILABLE.value: (
        "The incoming related table has no usable identity"
    ),
    RelationshipHealthNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE.value: (
        "Current relationship source evidence is unavailable"
    ),
    RelationshipHealthNotCheckableReason.EVIDENCE_LIMIT_EXCEEDED.value: (
        "This relationship exceeds the bounded authoring-check limit"
    ),
    RelationshipHealthNotCheckableReason.ODOO_SCHEMA_CHANGED.value: (
        "The captured Odoo relationship fields changed"
    ),
}


def matching_health_workflow_issue_summary(
    *,
    check: MatchingOrderCheck | None,
    check_current: bool,
    identity_health: Mapping[str, object] | None,
    relationship_health: Mapping[str, object] | None,
) -> WorkflowIssueSummary:
    """Return current actionable Stage 3 cards from aggregate-only evidence."""

    if check is None or not check_current:
        return summarize_workflow_issues(())
    identity_results = {item.dataset_id: item for item in check.identity_results}
    relationship_results = {
        (item.owner_dataset_id, item.target_field): item
        for item in check.relationship_health_results
    }
    issues: list[WorkflowIssueProjection] = []
    if identity_health is not None:
        for card in _cards(identity_health):
            dataset_id = str(card.get("dataset_id", ""))
            result = identity_results.get(dataset_id)
            if result is None:
                continue
            severity = identity_health_issue_severity(result)
            if severity is None:
                continue
            issues.append(
                _identity_issue(
                    check,
                    card,
                    severity=severity,
                )
            )
    if relationship_health is not None:
        for card in _cards(relationship_health):
            result = relationship_results.get(
                (
                    str(card.get("dataset_id", "")),
                    str(card.get("technical_field", "")),
                )
            )
            if result is None:
                continue
            severity = relationship_health_issue_severity(result)
            if severity is None:
                continue
            issues.append(
                _relationship_issue(
                    check,
                    card,
                    severity=severity,
                )
            )
    return summarize_workflow_issues(issues)


def _cards(health: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    value = health.get("cards", ())
    if not isinstance(value, tuple):
        return ()
    return tuple(card for card in value if isinstance(card, Mapping))


def _identity_issue(
    check: MatchingOrderCheck,
    card: Mapping[str, object],
    *,
    severity: WorkflowIssueSeverity,
) -> WorkflowIssueProjection:
    dataset_id = str(card.get("dataset_id", ""))
    dataset_name = str(card.get("dataset_name", dataset_id))
    source_problem = bool(
        _count(card, "source_blank_row_count")
        or _count(card, "source_repeated_row_count")
    )
    target_problem = bool(_count(card, "target_ambiguous_key_count"))
    if severity is WorkflowIssueSeverity.REVIEW:
        reason = str(card.get("not_checkable_reason", ""))
        cause = _IDENTITY_REVIEW_CAUSES.get(
            reason,
            "The saved identity could not be tested on current data",
        )
    elif source_problem and target_problem:
        cause = f"{dataset_name} identity is not unique on current data"
    elif target_problem:
        cause = f"{dataset_name} matches more than one Odoo record"
    elif _count(card, "source_repeated_row_count"):
        cause = f"{dataset_name} has repeated identity values"
    else:
        cause = f"{dataset_name} has blank identity values"
    review_reason = str(card.get("not_checkable_reason", ""))
    schema_changed = (
        review_reason
        == MatchingIdentityNotCheckableReason.ODOO_SCHEMA_CHANGED.value
    )
    source_unavailable = (
        review_reason
        == MatchingIdentityNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE.value
    )
    origin = (
        WorkflowIssueOrigin.ODOO_SCHEMA
        if schema_changed
        else WorkflowIssueOrigin.SOURCE_DATA
        if source_problem or source_unavailable
        else WorkflowIssueOrigin.ODOO_DATA
        if target_problem
        else WorkflowIssueOrigin.IMPODO_ACTION
    )
    owner = (
        "Data manager and Odoo owner"
        if source_problem and target_problem
        else "Odoo owner"
        if target_problem or schema_changed
        else "Data manager"
    )
    correction_label = f"Review {dataset_name} identity"
    correction_route = str(card.get("edit_url", ""))
    recheck = "Save the correction, then run Check current data again."
    if schema_changed:
        correction_label = "Review Odoo fields"
        correction_route = f"/workspaces/{check.workspace_id}/schema"
        recheck = (
            "Refresh Odoo data, save the current matching choices, then run "
            "Check current data again."
        )
    return WorkflowIssueProjection(
        code=f"MATCH_IDENTITY_HEALTH:{dataset_id}",
        severity=severity,
        state=WorkflowIssueState.CURRENT,
        cause=cause,
        origin=origin,
        owner=owner,
        owning_stage="Match data",
        correction_label=correction_label,
        correction_route=correction_route,
        preserved_work=(
            "Accepted source rows and all other saved mapping decisions remain "
            "unchanged."
        ),
        recheck=recheck,
        evidence_revision=check.check_id,
        affected_record_types=(dataset_name,),
        affected_fields=_strings(card, "target_fields"),
        affected_record_count=(
            _count(card, "blocked_count")
            if severity is WorkflowIssueSeverity.MUST_FIX
            else None
        ),
    )


def _relationship_issue(
    check: MatchingOrderCheck,
    card: Mapping[str, object],
    *,
    severity: WorkflowIssueSeverity,
) -> WorkflowIssueProjection:
    dataset_id = str(card.get("dataset_id", ""))
    dataset_name = str(card.get("dataset_name", dataset_id))
    field = str(card.get("field_label", card.get("technical_field", "")))
    if severity is WorkflowIssueSeverity.REVIEW:
        reason = str(card.get("not_checkable_reason", ""))
        cause = _RELATIONSHIP_REVIEW_CAUSES.get(
            reason,
            f"{field} could not be tested on current data",
        )
    else:
        cause = _relationship_blocker_cause(card, field=field)
    inverse_action = card.get("inverse_action")
    correction_label = f"Review {field}"
    correction_route = str(card.get("edit_url", ""))
    if isinstance(inverse_action, Mapping):
        correction_label = str(inverse_action.get("label", correction_label))
        correction_route = str(inverse_action.get("url", correction_route))
    review_reason = str(card.get("not_checkable_reason", ""))
    schema_changed = (
        review_reason
        == RelationshipHealthNotCheckableReason.ODOO_SCHEMA_CHANGED.value
    )
    source_problem = bool(
        (_count(card, "blank_row_count") if bool(card.get("required")) else 0)
        or _count(card, "incomplete_row_count")
        or _count(card, "case_mismatch_count")
        or review_reason
        == RelationshipHealthNotCheckableReason.SOURCE_EVIDENCE_UNAVAILABLE.value
    )
    target_only = str(card.get("resolver_origin", "")) == "TARGET_CATALOG"
    if schema_changed:
        correction_label = "Review Odoo fields"
        correction_route = f"/workspaces/{check.workspace_id}/schema"
    recheck = (
        "Refresh Odoo data, save the relationship correction, then run Check "
        "current data again."
        if schema_changed
        else (
            "Save the relationship correction, then run Check current data "
            "again."
        )
    )
    return WorkflowIssueProjection(
        code=(
            f"MATCH_RELATIONSHIP_HEALTH:{dataset_id}:"
            f"{card.get('technical_field', '')}"
        ),
        severity=severity,
        state=WorkflowIssueState.CURRENT,
        cause=cause,
        origin=(
            WorkflowIssueOrigin.ODOO_SCHEMA
            if schema_changed
            else WorkflowIssueOrigin.SOURCE_DATA
            if source_problem
            else WorkflowIssueOrigin.ODOO_DATA
            if target_only
            else WorkflowIssueOrigin.INCOMING_RELATIONSHIP
        ),
        owner=(
            "Odoo owner"
            if (target_only and not source_problem) or schema_changed
            else "Data manager"
        ),
        owning_stage="Match data",
        correction_label=correction_label,
        correction_route=correction_route,
        preserved_work=(
            "The saved field providers, value rules, and other relationships "
            "remain unchanged."
        ),
        recheck=recheck,
        evidence_revision=check.check_id,
        affected_record_types=(dataset_name,),
        affected_fields=(field,),
        affected_record_count=(
            _count(card, "blocked_count")
            if severity is WorkflowIssueSeverity.MUST_FIX
            else None
        ),
    )


def _relationship_blocker_cause(
    card: Mapping[str, object],
    *,
    field: str,
) -> str:
    counts = {
        "blank": _count(card, "blank_row_count")
        if bool(card.get("required"))
        else 0,
        "incomplete": _count(card, "incomplete_row_count"),
        "missing": _count(card, "missing_count"),
        "ambiguous": _count(card, "ambiguous_count"),
        "case": _count(card, "case_mismatch_count"),
    }
    active = tuple(name for name, count in counts.items() if count)
    if active == ("blank",):
        return f"{field} is blank on required records"
    if active == ("incomplete",):
        return f"{field} has incomplete compound values"
    if active == ("missing",):
        return f"{field} cannot find a related record"
    if active == ("ambiguous",):
        return f"{field} matches more than one related record"
    if active == ("case",):
        return f"{field} differs only by letter case"
    return f"{field} has unresolved relationship choices"


def _count(card: Mapping[str, object], key: str) -> int:
    value = card.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _strings(card: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = card.get(key, ())
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


__all__ = ["matching_health_workflow_issue_summary"]
