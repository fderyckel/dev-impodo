"""Project current supporting decisions and correction routes without Odoo I/O."""

from __future__ import annotations

from uuid import uuid4

from impodo.application.supporting_review_service import supporting_review_packet
from impodo.application.workspace.issues import (
    WorkflowIssueOrigin,
    WorkflowIssueProjection,
    WorkflowIssueSeverity,
    WorkflowIssueState,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_models import (
    SupportingModelRole,
    supporting_model_role,
)
from impodo.domain.workspace.supporting_review import (
    SupportingChoiceAction,
    eligible_supporting_fields,
)


def supporting_review_view(context, workspace, selection, schema, governance, *, actor):
    """Reuse the page's evidence and read rules/review once for all fields."""
    service = context.schema_workspace.supporting
    try:
        preparation = context.queries.get_derived_entity_plan(workspace.workspace_id)
        packet = supporting_review_packet(
            workspace, selection, schema, preparation, governance
        )
        review = service.current(workspace.workspace_id, actor=actor)
        fields = eligible_supporting_fields(schema)
        if len(fields) > 1_000:
            raise WorkspaceError(
                "Narrow the selected scope to review at most 1,000 supporting relationships"
            )
    except (ValueError, WorkspaceError) as failure:
        error = str(failure)
        return {
            "rows": (),
            "datasets": (),
            "binding_hash": "",
            "version": 0,
            "operation_id": "",
            "error": error,
            "current": False,
            "has_reuse": False,
            "issues": (_review_issue(workspace.workspace_id, error),),
        }
    error = ""
    try:
        service.require_current(
            packet, actor=actor, review=review, read_if_missing=False
        )
    except (ValueError, WorkspaceError) as failure:
        error = str(failure)
    saved = (
        {(item.source_model, item.field_name): item for item in review.choices}
        if review
        else {}
    )
    labels = {model.name: model.label for model in schema.models}
    rows = []
    descriptor_by_id = {item.dataset_id: item for item in packet.descriptors}
    for (model, name), field in sorted(
        fields.items(), key=lambda item: (not item[1].required, item[0])
    ):
        role = supporting_model_role(field)
        actions = []
        if role is SupportingModelRole.ODOO_MANAGED:
            actions.append(
                (SupportingChoiceAction.ODOO_MANAGED.value, "Handled by Odoo")
            )
        else:
            if field.type != "one2many":
                actions.append(
                    (SupportingChoiceAction.REUSE.value, "Reuse existing Odoo records")
                )
            if field.create_default_present:
                actions.append(
                    (
                        SupportingChoiceAction.CHECKED_DEFAULT.value,
                        "Use the checked Odoo default",
                    )
                )
            if field.relation in workspace.intended_models:
                actions.append(
                    (
                        SupportingChoiceAction.INCOMING.value,
                        "Use an accepted incoming table",
                    )
                )
            if not field.required:
                actions.append(
                    (
                        SupportingChoiceAction.SKIP_OPTIONAL.value,
                        "Leave this optional relationship empty",
                    )
                )
        choice = saved.get((model, name))
        rows.append(
            {
                "model": model,
                "field": name,
                "label": field.label,
                "model_label": labels[model],
                "related_model": field.relation,
                "required": field.required,
                "actions": tuple(actions),
                "chosen": choice.action.value if choice else "",
                "dataset_id": choice.dataset_id if choice else "",
                "dataset_label": descriptor_by_id[choice.dataset_id].label
                if choice and choice.dataset_id in descriptor_by_id
                else "",
                "read_key": ", ".join((*choice.key_fields, *choice.scope_fields))
                if choice
                else "",
                "incoming_available": field.relation in workspace.intended_models,
            }
        )
    issues = ()
    if error:
        issues = (
            _review_issue(
                workspace.workspace_id,
                error,
                evidence_revision=packet.binding_hash,
                affected_record_types=tuple(
                    sorted({item["model_label"] for item in rows if item["required"]})
                ),
            ),
        )
    return {
        "rows": tuple(rows),
        "datasets": packet.descriptors,
        "binding_hash": packet.binding_hash,
        "version": review.version if review else 0,
        "operation_id": str(uuid4()),
        "error": error,
        "current": review is not None and not error,
        "has_reuse": bool(
            review
            and any(
                item.action is SupportingChoiceAction.REUSE for item in review.choices
            )
        ),
        "issues": issues,
    }


def _review_issue(
    workspace_id: str,
    error: str,
    *,
    evidence_revision: str = "",
    affected_record_types: tuple[str, ...] = (),
) -> WorkflowIssueProjection:
    """Keep unavailable or oversized evidence blocked with a correction route."""
    return WorkflowIssueProjection(
        code="SUPPORTING_CHOICES_REVIEW_REQUIRED",
        severity=WorkflowIssueSeverity.MUST_FIX,
        state=WorkflowIssueState.CURRENT,
        cause=error,
        origin=WorkflowIssueOrigin.INCOMING_RELATIONSHIP,
        owner="Data manager",
        owning_stage="Odoo data",
        correction_label="Review supporting-data choices",
        correction_route=f"/workspaces/{workspace_id}/schema#supporting-data-review",
        preserved_work="Accepted source data and saved matching rules remain unchanged.",
        recheck="Resolve the supporting review issue and save current choices, then confirm the matching rules.",
        evidence_revision=evidence_revision,
        affected_record_types=affected_record_types,
    )
