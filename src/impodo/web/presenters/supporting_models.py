"""Present Stage 2 supporting-model defaults and actionable issues."""

from __future__ import annotations

from enum import StrEnum
from itertools import groupby
from typing import Iterable

from impodo.application.workspace.issues import (
    WorkflowIssueOrigin,
    WorkflowIssueProjection,
    WorkflowIssueSeverity,
    WorkflowIssueState,
    summarize_workflow_issues,
)
from impodo.domain.odoo_relationship_profiles import (
    DEFAULT_ODOO_RELATIONSHIP_PROFILES,
)
from impodo.domain.workspace.contracts import (
    OdooModelCatalog,
    OdooSchemaCatalog,
    SchemaOrigin,
)
from impodo.domain.workspace.supporting_models import (
    SupportingModelDependency,
    SupportingModelRole,
    derive_supporting_model_dependencies,
)


class _SupportingPreviewOutcome(StrEnum):
    """One summary outcome shown before detailed relationship review."""

    KEEP_LINKED = "KEEP_LINKED"
    USE_EXISTING_DESTINATION = "USE_EXISTING_DESTINATION"
    ODOO_MANAGED = "ODOO_MANAGED"


_OUTCOME_COPY = {
    _SupportingPreviewOutcome.KEEP_LINKED: (
        "Keep linked value",
        "For each populated link, Impodo first reuses a matching destination "
        "record. If no match exists, it prepares only the identity and required "
        "values needed to create that record. It does not add the whole related "
        "record type to the top-level transfer.",
        "Reuse or create identity and required values only",
    ),
    _SupportingPreviewOutcome.USE_EXISTING_DESTINATION: (
        "Use existing destination records only",
        "Destination-owned setup stays protected. Impodo must match an existing "
        "destination record; a missing match stops the load instead of creating "
        "or updating setup here.",
        "Protected destination setup",
    ),
    _SupportingPreviewOutcome.ODOO_MANAGED: (
        "Handled by Odoo",
        "Odoo computes these relationships or owns them through the related "
        "record. Impodo will not ask you to migrate the related record type here.",
        "No incoming data required",
    ),
}


def supporting_model_plan_view(
    workspace_id: str,
    schema: OdooSchemaCatalog | None,
    model_catalog: OdooModelCatalog | None,
) -> dict[str, object]:
    """Preview direct relationship defaults without widening write scope."""

    if schema is None or schema.origin is not SchemaOrigin.LIVE_API:
        return {
            "groups": (),
            "dependency_count": 0,
            "issue_summary": summarize_workflow_issues(()),
            "suggestable_model_names": (),
        }
    dependencies = derive_supporting_model_dependencies(schema.models)
    catalog_models = {
        model.name: model
        for model in (model_catalog.models if model_catalog is not None else ())
    }
    grouped_dependencies = {
        relation_model: tuple(items)
        for relation_model, items in groupby(
            sorted(dependencies, key=lambda item: item.relation_model),
            key=lambda item: item.relation_model,
        )
    }
    models_by_outcome: dict[
        _SupportingPreviewOutcome,
        list[dict[str, object]],
    ] = {outcome: [] for outcome in _SupportingPreviewOutcome}
    issues = []
    for relation_model, related in grouped_dependencies.items():
        outcome = _preview_outcome(relation_model, related)
        catalog_model = catalog_models.get(relation_model)
        relation_label = (
            catalog_model.label
            if catalog_model is not None
            else _relationship_label(related, relation_model)
        )
        available = catalog_model is not None
        availability_relevant = outcome is not _SupportingPreviewOutcome.ODOO_MANAGED
        required = any(item.required for item in related)
        fields = tuple(
            {
                "source_label": dependency.source_label,
                "field_label": dependency.field_label,
                "required": dependency.required,
                "technical_name": (
                    f"{dependency.source_model}.{dependency.field_name} -> "
                    f"{dependency.relation_model}"
                ),
            }
            for dependency in related
        )
        models_by_outcome[outcome].append(
            {
                "model_name": relation_model,
                "label": relation_label,
                "available": available,
                "availability_relevant": availability_relevant,
                "required": required,
                "fields": fields,
            }
        )
        if not available and availability_relevant:
            issues.append(
                WorkflowIssueProjection(
                    code=f"ODOO_SUPPORTING_MODEL_UNAVAILABLE:{relation_model}",
                    severity=(
                        WorkflowIssueSeverity.MUST_FIX
                        if required
                        else WorkflowIssueSeverity.REVIEW
                    ),
                    state=WorkflowIssueState.CURRENT,
                    cause=(
                        f"Impodo cannot find {relation_label} in the "
                        "current Odoo record-type list."
                    ),
                    origin=WorkflowIssueOrigin.ODOO_SCHEMA,
                    owner="Data manager or Odoo administrator",
                    owning_stage="Odoo data",
                    correction_label="Review available Odoo data",
                    correction_route=(
                        f"/workspaces/{workspace_id}/schema#odoo-data-choices"
                    ),
                    preserved_work=(
                        "The accepted source data and selected Odoo "
                        "business records remain unchanged."
                    ),
                    recheck=(
                        "Update the available choices, then load the "
                        "selected Odoo details again."
                    ),
                    evidence_revision=schema.content_hash,
                    affected_record_types=(relation_label,),
                    affected_fields=tuple(
                        f"{item.source_label}: {item.field_label}"
                        for item in related
                    ),
                )
            )
    groups = []
    for outcome in _SupportingPreviewOutcome:
        models = models_by_outcome[outcome]
        if not models:
            continue
        title, description, status_label = _OUTCOME_COPY[outcome]
        groups.append(
            {
                "outcome": outcome,
                "title": title,
                "description": description,
                "status_label": status_label,
                "models": tuple(
                    sorted(
                        models,
                        key=lambda model: (
                            str(model["label"]).casefold(),
                            str(model["model_name"]),
                        ),
                    )
                ),
            }
        )
    return {
        "groups": tuple(groups),
        "dependency_count": len(dependencies),
        "issue_summary": summarize_workflow_issues(issues),
        "suggestable_model_names": (),
    }


def _preview_outcome(
    relation_model: str,
    dependencies: tuple[SupportingModelDependency, ...],
) -> _SupportingPreviewOutcome:
    if any(
        relation_model in profile.destination_configuration_models
        for profile in DEFAULT_ODOO_RELATIONSHIP_PROFILES
    ):
        return _SupportingPreviewOutcome.USE_EXISTING_DESTINATION
    if all(
        dependency.role is SupportingModelRole.ODOO_MANAGED
        for dependency in dependencies
    ):
        return _SupportingPreviewOutcome.ODOO_MANAGED
    return _SupportingPreviewOutcome.KEEP_LINKED


def _fallback_model_label(model_name: str) -> str:
    return model_name.rsplit(".", 1)[-1].replace("_", " ").title()


def _relationship_label(
    dependencies: Iterable[SupportingModelDependency],
    model_name: str,
) -> str:
    labels = {
        dependency.field_label.strip()
        for dependency in dependencies
        if dependency.field_label.strip()
    }
    if len(labels) == 1:
        return next(iter(labels))
    return _fallback_model_label(model_name)


__all__ = ["supporting_model_plan_view"]
