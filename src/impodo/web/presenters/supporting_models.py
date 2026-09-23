"""Present Stage 2 supporting-model decisions and actionable issues."""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlencode

from impodo.application.workspace.issues import (
    WorkflowIssueOrigin,
    WorkflowIssueProjection,
    WorkflowIssueSeverity,
    WorkflowIssueState,
    summarize_workflow_issues,
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


_SUPPORTING_ROLE_COPY = {
    SupportingModelRole.REUSE_EXISTING: (
        "Reuse existing Odoo records",
        "These relationships can point to existing Odoo records. They stay "
        "outside the migration write scope; Match data must still bind an "
        "approved portable identity before Impodo reads their values.",
        "Review in Match data",
    ),
    SupportingModelRole.CHECKED_DEFAULT: (
        "Use checked Odoo defaults",
        "The captured Odoo details contain a usable create default for these "
        "required relationships. Match data will still show the decision.",
        "Checked default available",
    ),
    SupportingModelRole.ODOO_MANAGED: (
        "Handled by Odoo",
        "Odoo computes these relationships or owns them through the related "
        "record. Impodo will not ask you to migrate the related model here.",
        "No incoming table required",
    ),
    SupportingModelRole.REVIEW_INCOMING: (
        "Related records you may migrate separately",
        "Include these records only when they are present in the accepted "
        "source and belong to the approved migration scope.",
        "Include only when it is business data",
    ),
}


def supporting_model_plan_view(
    workspace_id: str,
    schema: OdooSchemaCatalog | None,
    model_catalog: OdooModelCatalog | None,
) -> dict[str, object]:
    """Project direct schema dependencies without widening write scope."""

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
    groups = []
    issues = []
    for role in SupportingModelRole:
        role_dependencies = tuple(
            dependency
            for dependency in dependencies
            if dependency.role is role
        )
        if not role_dependencies:
            continue
        models = []
        for relation_model in sorted(
            {dependency.relation_model for dependency in role_dependencies},
            key=lambda name: (
                catalog_models.get(name).label.casefold()
                if catalog_models.get(name) is not None
                else _fallback_model_label(name).casefold(),
                name,
            ),
        ):
            related = tuple(
                dependency
                for dependency in role_dependencies
                if dependency.relation_model == relation_model
            )
            catalog_model = catalog_models.get(relation_model)
            relation_label = (
                catalog_model.label
                if catalog_model is not None
                else _relationship_label(related, relation_model)
            )
            available = catalog_model is not None
            availability_relevant = role in {
                SupportingModelRole.REUSE_EXISTING,
                SupportingModelRole.REVIEW_INCOMING,
            }
            can_include_incoming = available and availability_relevant
            include_url = None
            if can_include_incoming:
                include_url = (
                    f"/workspaces/{workspace_id}/schema?"
                    f"{urlencode({'suggested_model': relation_model})}"
                    "#odoo-data-choices"
                )
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
            models.append(
                {
                    "model_name": relation_model,
                    "label": relation_label,
                    "available": available,
                    "availability_relevant": availability_relevant,
                    "required": any(item.required for item in related),
                    "fields": fields,
                    "include_url": include_url,
                    "include_label": (
                        "Include incoming data"
                        if role is SupportingModelRole.REUSE_EXISTING
                        else "Include this related business data"
                    ),
                }
            )
            if not available and availability_relevant:
                required = any(item.required for item in related)
                issues.append(
                    WorkflowIssueProjection(
                        code=(
                            "ODOO_SUPPORTING_MODEL_UNAVAILABLE:"
                            f"{relation_model}"
                        ),
                        severity=(
                            WorkflowIssueSeverity.MUST_FIX
                            if required
                            and role is SupportingModelRole.REUSE_EXISTING
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
                            f"/workspaces/{workspace_id}/schema"
                            "#odoo-data-choices"
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
        title, description, status_label = _SUPPORTING_ROLE_COPY[role]
        groups.append(
            {
                "role": role,
                "title": title,
                "description": description,
                "status_label": status_label,
                "models": tuple(models),
            }
        )
    return {
        "groups": tuple(groups),
        "dependency_count": len(dependencies),
        "issue_summary": summarize_workflow_issues(issues),
        "suggestable_model_names": tuple(
            model["model_name"]
            for group in groups
            for model in group["models"]
            if model["include_url"] is not None
        ),
    }


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
