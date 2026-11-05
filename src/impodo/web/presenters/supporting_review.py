"""Project current supporting decisions and correction routes without Odoo I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import uuid4

from impodo.application.supporting_review_service import supporting_review_packet
from impodo.application.workspace.issues import (
    WorkflowIssueOrigin,
    WorkflowIssueProjection,
    WorkflowIssueSeverity,
    WorkflowIssueState,
)
from impodo.domain.workspace.contracts import SchemaOrigin
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_models import (
    SupportingModelRole,
    supporting_model_role,
)
from impodo.domain.workspace.supporting_review import (
    SupportingChoiceAction,
    SupportingChoiceValidationError,
    SupportingReviewProblem,
    SupportingReviewRequiredError,
    eligible_supporting_fields,
)
from impodo.domain.workspace.workbench import SourceMode

_SOURCE_LABELS: dict[str, str] = {
    SupportingChoiceAction.ODOO_MANAGED: "Handled by Odoo",
    SupportingChoiceAction.REUSE: "Reuse existing Odoo records",
    SupportingChoiceAction.CHECKED_DEFAULT: "Use the checked Odoo default",
    SupportingChoiceAction.INCOMING: "Use an accepted incoming table",
    SupportingChoiceAction.SKIP_OPTIONAL: "Leave this optional relationship empty",
}


def supporting_review_view(
    context, workspace, selection, schema, governance, *, actor, packet=None,
    save_error: ValueError | WorkspaceError | None = None,
    drafts: Mapping[str, tuple[str, str]] | None = None,
):
    """Reuse the page's evidence and read rules/review once for all fields."""
    service = context.schema_workspace.supporting
    try:
        if packet is None:
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
            "save_error": str(save_error) if save_error is not None else "",
            "current": False,
            "has_reuse": False,
            "issues": (_review_issue(workspace.workspace_id, error),),
        }
    error = ""
    problem = ""
    try:
        service.require_current(
            packet, actor=actor, review=review, read_if_missing=False
        )
    except (ValueError, WorkspaceError) as failure:
        error = str(failure)
        if isinstance(failure, SupportingReviewRequiredError):
            problem = failure.problem.value
    saved = (
        {(item.source_model, item.field_name): item for item in review.choices}
        if review
        else {}
    )
    labels = {model.name: model.label for model in schema.models}
    rows = []
    field_errors = (
        {edge: save_error.field_message for edge in save_error.relationships if edge in fields}
        if isinstance(save_error, SupportingChoiceValidationError)
        else {}
    )
    descriptor_by_id = {item.dataset_id: item for item in packet.descriptors}
    for (model, name), field in sorted(
        fields.items(), key=lambda item: (not item[1].required, item[0])
    ):
        role = supporting_model_role(
            field, source_model=model, odoo_version=schema.odoo_version
        )
        actions: list[SupportingChoiceAction] = []
        if role is SupportingModelRole.ODOO_MANAGED:
            actions.append(SupportingChoiceAction.ODOO_MANAGED)
        if (
            role is not SupportingModelRole.ODOO_MANAGED
            or supporting_model_role(field) is not SupportingModelRole.ODOO_MANAGED
        ):
            if field.type != "one2many":
                actions.append(SupportingChoiceAction.REUSE)
            if field.create_default_present:
                actions.append(SupportingChoiceAction.CHECKED_DEFAULT)
            if field.relation in workspace.intended_models:
                actions.append(SupportingChoiceAction.INCOMING)
            if not field.required:
                actions.append(SupportingChoiceAction.SKIP_OPTIONAL)
        choice = saved.get((model, name))
        draft = drafts.get(f"{model}|{name}") if drafts is not None else None
        chosen = choice.action.value if choice else ""
        dataset_label = (
            descriptor_by_id[choice.dataset_id].label
            if choice and choice.dataset_id in descriptor_by_id
            else ""
        )
        if draft is not None:
            chosen, dataset_label = draft
        rows.append(
            {
                "model": model,
                "field": name,
                "label": field.label,
                "model_label": labels[model],
                "related_model": field.relation,
                "required": field.required,
                "managed": role is SupportingModelRole.ODOO_MANAGED and len(actions) == 1,
                "actions": tuple((action.value, _SOURCE_LABELS[action]) for action in actions),
                "chosen": chosen,
                "unavailable_draft": (
                    _SOURCE_LABELS.get(chosen, "Unknown source")
                    if draft is not None and chosen and chosen not in actions
                    else ""
                ),
                "save_error": field_errors.get((model, name), ""),
                "dataset_id": choice.dataset_id if choice else "",
                "dataset_label": dataset_label,
                "read_key": ", ".join((*choice.key_fields, *choice.scope_fields))
                if choice and draft is None
                else "",
                "incoming_available": field.relation in workspace.intended_models,
                "needs_change": bool(
                    choice and choice.action not in actions
                    or field.required and choice is None
                ),
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
        "saved_binding_hash": review.binding_hash if review else "",
        "version": review.version if review else 0,
        "operation_id": str(uuid4()),
        "error": error,
        "save_error": str(save_error) if save_error is not None else "",
        "has_field_errors": bool(field_errors),
        "problem": problem,
        "current": review is not None and not error,
        "has_reuse": bool(
            review
            and any(
                item.action is SupportingChoiceAction.REUSE for item in review.choices
            )
        ),
        "issues": issues,
    }


def mapping_supporting_review_view(
    request, context, workspace, selection, schema, governance, *, actor, preparation
):
    """Project inline recovery from already loaded page evidence, without Odoo I/O."""
    if (
        workspace.source_mode is not SourceMode.FILE
        or selection is None
        or schema is None
        or schema.origin is not SchemaOrigin.LIVE_API
    ):
        return None
    owner = context.migration_workspaces.get(workspace.workspace_id, actor=actor)
    if owner.recipe_application_id:
        return None
    packet = supporting_review_packet(
        workspace, selection, schema, preparation, governance
    )
    view = supporting_review_view(
        context, workspace, selection, schema, governance, actor=actor, packet=packet
    )
    marker = request.session.get("supporting_review_refresh")
    refreshed = bool(
        isinstance(marker, dict)
        and marker.get("workspace_id") == workspace.workspace_id
        and marker.get("schema_hash") == schema.content_hash
        and marker.get("context_hash") == schema.read_context_hash
        and marker.get("credential_hash") == schema.read_credential_binding_hash
        and workspace.odoo_company is not None
        and workspace.odoo_company.context_hash == schema.read_context_hash
        and isinstance(marker.get("previous_context_hash"), str)
        and isinstance(marker.get("previous_credential_hash"), str)
        and view.get("problem") == SupportingReviewProblem.STALE
    )
    if refreshed:
        # Prove that only the access evidence changed. A later source or
        # preparation edit must not inherit an old company-refresh explanation.
        previous = supporting_review_packet(
            workspace, selection,
            replace(
                schema,
                read_context_hash=marker["previous_context_hash"],
                read_credential_binding_hash=marker["previous_credential_hash"],
            ),
            preparation, governance,
        )
        refreshed = previous.binding_hash == view.get("saved_binding_hash")
    mapping_conflict = request.query_params.get("problem") == SupportingReviewProblem.MAPPING_CONFLICT
    if mapping_conflict:
        message = "A related-data choice does not match its linked-field rule. Review the linked fields, then check matches again."
    elif refreshed and workspace.odoo_company is not None:
        message = f"Odoo access changed for {workspace.odoo_company.name}. Keep these choices if they are still correct."
    elif view.get("problem") == SupportingReviewProblem.STALE:
        message = "Your source or Odoo details changed. Keep these choices if they are still correct."
    elif not view.get("error"):
        message = "Your related-data choices are saved. Check matches to continue."
    else:
        message = "Review where related values come from before checking matches."
    return {
        **view, "message": message, "mapping_conflict": mapping_conflict,
        "account_companies": any(row["model"] == "account.account" and row["field"] == "company_ids" for row in view["rows"]),
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
