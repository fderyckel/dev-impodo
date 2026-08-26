"""Review Production evidence against the exact qualified Cutover plan."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from impodo.access import Actor
from impodo.domain.coverage import ReferenceBundle
from impodo.domain.cutover.models import (
    PROJECT_SHARED_CONTROL_IDS,
    CutoverPlanRevision,
    CutoverWriteOwnership,
)
from impodo.domain.data_version.models import DataVersionPurpose
from impodo.domain.run.planning import blocking_run_issue, run_requirement_hash
from impodo.domain.serialization import content_hash
from impodo.migration_production import ProductionRunBinding, ProductionRunError
from impodo.workspace_contracts import OdooSchemaCatalog

from .planning_models import IntegratedRunReview


class ProductionRunReviewUseCase:
    """Apply Production-only qualification and target checks to a run review."""

    def __init__(self, *, review) -> None:
        self._review = review

    def review(
        self,
        project_id: str,
        *,
        production_binding: ProductionRunBinding,
        plan: CutoverPlanRevision,
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        test_connection_target_hash: str,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        shared_control_values: Mapping[str, bool],
        actor: Actor,
    ) -> IntegratedRunReview:
        """Review fresh rollout evidence against exact qualified plan meaning."""

        if (
            production_binding.project_id != project_id
            or production_binding.cutover_plan_id != plan.cutover_plan_id
            or production_binding.cutover_plan_revision != plan.version
            or production_binding.plan_content_hash != plan.content_hash
        ):
            raise ProductionRunError(
                "Production setup does not match the selected CutoverPlan"
            )
        review = self._review.review(
            project_id,
            data_version_id=production_binding.data_version_id,
            recipe_revisions=tuple(
                (item.recipe_id, item.recipe_revision)
                for item in plan.selected_revisions
            ),
            dependencies=plan.dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            parameter_values=parameter_values,
            control_values=control_values,
            purpose=DataVersionPurpose.PRODUCTION,
            required_target_workspace_id=production_binding.setup_workspace_id,
            actor=actor,
        )
        issues = list(review.planning_issues)
        if target_schema.connection_target_hash == test_connection_target_hash:
            issues.append(
                blocking_run_issue(
                    "PRODUCTION_TARGET_NOT_INDEPENDENT",
                    "Production uses the same Odoo target as Integrated Test.",
                    "Capture the compatible Odoo 19 Production database instead.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        try:
            odoo_major = int(str(target_schema.odoo_version).split(".", 1)[0])
        except ValueError:
            odoo_major = -1
        if odoo_major != 19 or target_schema.origin.value != "LIVE_API":
            issues.append(
                blocking_run_issue(
                    "PRODUCTION_TARGET_EVIDENCE_UNSUPPORTED",
                    "Production target evidence is not a current live Odoo 19 capture.",
                    "Capture the Production Odoo 19 fields and supporting lists again.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        if run_requirement_hash(review) != plan.requirement_plan_hash:
            issues.append(
                blocking_run_issue(
                    "PRODUCTION_PLAN_MEANING_CHANGED",
                    "The current Recipe requirements no longer match the qualified plan.",
                    "Publish and qualify a new CutoverPlan revision.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        current_ownership = tuple(
            sorted(
                CutoverWriteOwnership(
                    recipe_id=item.selection.recipe_id,
                    model=model,
                    field=field,
                )
                for item in review.applications
                for model, field in item.write_claims
            )
        )
        if current_ownership != plan.write_ownership:
            issues.append(
                blocking_run_issue(
                    "PRODUCTION_WRITE_OWNERSHIP_CHANGED",
                    "Current Recipe write ownership differs from the qualified plan.",
                    "Publish and qualify the corrected CutoverPlan before Production.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        if set(shared_control_values) != set(PROJECT_SHARED_CONTROL_IDS):
            issues.append(
                blocking_run_issue(
                    "PRODUCTION_SHARED_CONTROLS_INCOMPLETE",
                    "The Production run does not contain every Project control.",
                    "Review package completeness and integrated reconciliation controls.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        elif not shared_control_values["control:project.package_completeness"]:
            issues.append(
                blocking_run_issue(
                    "PRODUCTION_PACKAGE_INCOMPLETE",
                    "The latest Production delivery is not confirmed complete.",
                    "Accept the complete Production data version before activation.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        if shared_control_values.get("control:project.integrated_reconciliation"):
            issues.append(
                blocking_run_issue(
                    "PRODUCTION_RECONCILIATION_PREMATURE",
                    "Production reconciliation was marked complete before execution.",
                    "Leave it pending until every application is verified.",
                    tuple(item.recipe_id for item in plan.selected_revisions),
                )
            )
        return replace(
            review,
            planning_issues=tuple(
                sorted(
                    {content_hash(item.to_dict()): item for item in issues}.values(),
                    key=lambda item: (item.code, item.recipe_ids),
                )
            ),
        )
