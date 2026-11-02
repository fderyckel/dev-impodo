"""Retain value-free supporting-plan failures for browser guidance and support.

This domain projection uses only the plan and metadata already checked by its
caller. It never reads source values, opens a database, or contacts Odoo.
"""

from __future__ import annotations

from dataclasses import dataclass

from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.workspace.contracts import OdooSchemaCatalog
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_closure import supporting_mapping_intents
from impodo.domain.workspace.supporting_plan import (
    SupportingDataPlan,
    SupportingPlanGap,
)
from impodo.domain.workspace.supporting_review import SupportingChoice


@dataclass(frozen=True, slots=True)
class SupportingFailureIssue:
    """Explain one failed edge without labels, table identities, or row content."""

    reason: SupportingPlanGap
    source_model: str
    field: str
    related_model: str
    field_type: str
    trigger: str
    required: bool | None
    used_by_mapping: bool
    saved_choice: str
    candidate_count: int


class SupportingPlanIncompleteError(WorkspaceError):
    """Keep the precise cause when an expected validation failure is handled."""

    failure_code = "SUPPORTING_PLAN_INCOMPLETE"

    def __init__(
        self,
        proposal: SupportingDataPlan,
        schema: OdooSchemaCatalog,
        mappings: tuple[DatasetMapping, ...],
        choices: tuple[SupportingChoice, ...],
    ) -> None:
        decision = assess_odoo_operation(schema.odoo_version, OdooOperation.COMPARE)
        self.odoo_major = decision.version.major if decision.allowed else None
        fields = {
            (model.name, field.name): field
            for model in schema.models
            for field in model.fields
        }
        labels = {
            (model.name, field.name): f"{model.label} · {field.label} ({model.name}.{field.name})"
            for model in schema.models
            for field in model.fields
        }
        edges = {(edge.source_model, edge.field_name): edge for edge in proposal.edges}
        saved = {
            (item.source_model, item.field_name): item.action.value for item in choices
        }
        used = {
            (item.source_model, item.field_name)
            for item in supporting_mapping_intents(mappings, schema)
        }
        issues = []
        for issue in proposal.issues:
            key = (issue.source_model, issue.field_name)
            field = fields.get(key)
            edge = edges.get(key)
            issues.append(
                SupportingFailureIssue(
                    reason=issue.reason,
                    source_model=issue.source_model,
                    field=issue.field_name,
                    related_model=issue.related_model,
                    field_type=field.type if field else "UNKNOWN",
                    trigger=edge.reason if edge else "UNREACHED_METADATA",
                    required=field.required if field else None,
                    used_by_mapping=key in used,
                    saved_choice=saved.get(key, "UNSET"),
                    candidate_count=len(edge.candidates) if edge else 0,
                )
            )
        self.issues = tuple(issues)
        reasons = {
            SupportingPlanGap.INCOMING_UNAVAILABLE: "no incoming table is available",
            SupportingPlanGap.AMBIGUOUS_INCOMING: "several incoming tables are possible",
            SupportingPlanGap.INVALID_INTENT: "the selected table or relationship is incompatible",
            SupportingPlanGap.METADATA_UNAVAILABLE: "Odoo details are missing",
        }
        details = "; ".join(
            f"{labels.get((item.source_model, item.field), item.source_model or 'Supporting data')}: "
            f"{reasons.get(item.reason, 'the supporting-plan limit was reached')}"
            for item in self.issues[:5]
        )
        if len(self.issues) > 5:
            details += f"; and {len(self.issues) - 5} more issues"
        super().__init__(
            "Review the incomplete supporting-data plan in Odoo data before checking Match data. "
            + details
        )
