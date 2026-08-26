"""Confirm and recover reviewed Recipe applications inside one migration run."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.mapping.contracts import ScalarValueSource, TargetFieldHandling
from impodo.domain.recipe_parameters import EXPORT_AS_OF_PARAMETER_ID
from impodo.domain.run.models import MigrationRunPurpose, MigrationRunState
from impodo.domain.serialization import content_hash
from impodo.domain.project.foundation import utc_now
from impodo.domain.run.contracts import (
    MigrationRunPlanIssueLevel,
    MigrationRunPlanningError,
    RecipeApplicationStatus,
    RunRecipeApplication,
)
from impodo.domain.workspace.contracts import OdooSchemaCatalog


class RunApplicationRecoveryUseCase:
    """Own mapping confirmation and required-default recovery for applications."""

    def __init__(
        self,
        *,
        repository,
        authorization: AuthorizationPolicy,
        compiler,
        source_packages,
        data_versions,
        test_run_values,
        recipes,
        package_selection,
    ) -> None:
        self._repository = repository
        self._authorization = authorization
        self._compiler = compiler
        self._source_packages = source_packages
        self._data_versions = data_versions
        self._test_run_values = test_run_values
        self._recipes = recipes
        self._package_selection = package_selection

    def confirm_odoo_defaults(
        self,
        application_id: str,
        *,
        actor: Actor,
    ) -> RunRecipeApplication:
        """Confirm grouped target defaults already checked for one application."""

        application = self._repository.get_application(application_id)
        self._authorization.require(
            actor,
            Capability.RECIPE_APPLY,
            project_id=application.project_id,
        )
        issues = self._repository.list_issues(application.application_id)
        default_reviews = tuple(
            item
            for item in issues
            if item.code == "RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE"
            and item.level is MigrationRunPlanIssueLevel.REVIEW
        )
        other_actionable = tuple(
            item
            for item in issues
            if item not in default_reviews
            and item.level is not MigrationRunPlanIssueLevel.INFORMATION
        )
        if not default_reviews:
            raise MigrationRunPlanningError(
                "No reviewed Odoo defaults are waiting for confirmation"
            )
        if other_actionable:
            raise MigrationRunPlanningError(other_actionable[0].message)
        schema = self._compiler.schemas.get_odoo_schema_catalog(
            application.workspace_id
        )
        revision = self._compiler.mappings.mappings.get_mapping_revision(
            application.workspace_id
        )
        working = self._compiler.mappings.mappings.get_mapping_working_draft(
            application.workspace_id
        )
        if schema is None or revision is None or working is None:
            raise MigrationRunPlanningError(
                "Recheck Odoo and rebuild this Recipe application before "
                "confirming defaults"
            )
        fields_by_model = {
            model.name: {field.name: field for field in model.fields}
            for model in schema.models
        }
        mapped_default_fields = {
            (dataset.target_model, field.target_field)
            for dataset in revision.definition.datasets
            for field in dataset.fields
            if field.value_source is ScalarValueSource.ODOO_DEFAULT
        }
        mapped_default_fields.update(
            (dataset.target_model, disposition.target_field)
            for dataset in revision.definition.datasets
            for disposition in dataset.target_field_dispositions
            if disposition.handling is TargetFieldHandling.ODOO_DEFAULT
        )
        default_fields = tuple(
            sorted(
                (model_name, field_name)
                for model_name, field_name in mapped_default_fields
                if fields_by_model.get(model_name, {}).get(field_name) is not None
                and fields_by_model[model_name][field_name].required
                and fields_by_model[model_name][field_name].create_default_present
            )
        )
        if not default_fields or len(default_fields) != len(default_reviews):
            raise MigrationRunPlanningError(
                "One or more Odoo defaults are no longer verified for this target"
            )
        self._compiler.mappings.submit_current(
            application.workspace_id,
            datasets=revision.definition.datasets,
            expected_version=revision.version,
            expected_working_draft_version=working.version,
            actor=actor,
        )
        remaining = tuple(item for item in issues if item not in default_reviews)
        evidence_hash = content_hash(
            {
                "application_id": application.application_id,
                "confirmed_odoo_defaults": [list(item) for item in default_fields],
                "mapping_content_hash": revision.definition.content_hash,
                "previous_evidence_hash": application.evidence_hash,
                "schema_hash": schema.content_hash,
                "status": RecipeApplicationStatus.READY.value,
            }
        )
        confirmed = self._repository.save_application_materialization(
            application.application_id,
            expected_evidence_hash=application.evidence_hash,
            status=RecipeApplicationStatus.READY,
            issues=remaining,
            mapping_id=revision.mapping_id,
            mapping_content_hash=revision.definition.content_hash,
            evidence_hash=evidence_hash,
            actor=actor,
        )
        self._mark_run_ready_if_complete(confirmed.migration_run_id, actor=actor)
        return confirmed

    def confirm_mapping(
        self,
        application_id: str,
        *,
        actor: Actor,
    ) -> RunRecipeApplication:
        """Record one submitted mapping and clear its stale mapping blockers."""

        application = self._repository.get_application(application_id)
        self._authorization.require(
            actor,
            Capability.RECIPE_APPLY,
            project_id=application.project_id,
        )
        revision = self._compiler.mappings.mappings.get_mapping_revision(
            application.workspace_id
        )
        if revision is None or revision.mapping_id != application.mapping_id:
            raise MigrationRunPlanningError(
                "The confirmed field matches do not belong to this Recipe work area"
            )
        submission = self._compiler.mappings.mappings.get_mapping_submission(
            application.workspace_id,
            revision.version,
        )
        if (
            submission is None
            or submission.mapping_id != revision.mapping_id
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            raise MigrationRunPlanningError(
                "Confirm the current field matches before continuing this Recipe"
            )
        remaining = tuple(
            item
            for item in self._repository.list_issues(application.application_id)
            if not item.code.startswith("MAPPING_")
        )
        default_review_required = any(
            item.code == "RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE"
            and item.level is MigrationRunPlanIssueLevel.REVIEW
            for item in remaining
        )
        status = (
            RecipeApplicationStatus.BLOCKED
            if any(item.blocks for item in remaining) or default_review_required
            else RecipeApplicationStatus.READY
        )
        evidence_hash = content_hash(
            {
                "application_id": application.application_id,
                "mapping_content_hash": revision.definition.content_hash,
                "mapping_submission_hash": content_hash(submission.to_json()),
                "previous_evidence_hash": application.evidence_hash,
                "remaining_issues": [item.to_dict() for item in remaining],
                "status": status.value,
            }
        )
        confirmed = self._repository.save_application_materialization(
            application.application_id,
            expected_evidence_hash=application.evidence_hash,
            status=status,
            issues=remaining,
            mapping_id=revision.mapping_id,
            mapping_content_hash=revision.definition.content_hash,
            evidence_hash=evidence_hash,
            actor=actor,
        )
        self._mark_run_ready_if_complete(confirmed.migration_run_id, actor=actor)
        return confirmed

    def recover_blocked_test_defaults(
        self,
        migration_run_id: str,
        *,
        current_schema: OdooSchemaCatalog,
        actor: Actor,
    ) -> tuple[RunRecipeApplication, ...]:
        """Reassess old required-field blockers with fresh scalar defaults."""

        bundle = self._repository.get_bundle(migration_run_id)
        if bundle.run.purpose is not MigrationRunPurpose.TEST:
            raise MigrationRunPlanningError(
                "Required-field recovery is available here only for Test runs"
            )
        self._authorization.require(
            actor,
            Capability.RECIPE_APPLY,
            project_id=bundle.run.project_id,
        )
        package = self._source_packages.repository.get_source_package(
            bundle.run.data_version_id
        )
        if package is None:
            raise MigrationRunPlanningError("DataVersion source package is missing")
        source_selection = self._package_selection(package)
        run_references = self._repository.get_run_reference_bundle(migration_run_id)
        data_version = self._data_versions.get(
            bundle.run.data_version_id,
            actor=actor,
        )
        saved_run_values = self._test_run_values.get_parameter_values(migration_run_id)
        if saved_run_values is not None and (
            saved_run_values.project_id != bundle.run.project_id
            or saved_run_values.migration_run_id != migration_run_id
        ):
            raise MigrationRunPlanningError(
                "The saved Recipe values do not belong to this Test run"
            )
        saved_values_by_recipe = (
            saved_run_values.by_recipe if saved_run_values is not None else {}
        )
        application_recipe_ids = {
            application.recipe_id for application in bundle.applications
        }
        if set(saved_values_by_recipe) - application_recipe_ids:
            raise MigrationRunPlanningError(
                "The saved Recipe values do not match this Test run"
            )
        current_models = {model.name: model for model in current_schema.models}
        recovered: list[RunRecipeApplication] = []
        refusal_reasons: list[str] = []
        for application in bundle.applications:
            if application.status is not RecipeApplicationStatus.BLOCKED:
                continue
            existing_issues = self._repository.list_issues(application.application_id)
            blockers = tuple(item for item in existing_issues if item.blocks)
            default_reviews = tuple(
                item
                for item in existing_issues
                if item.code == "RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE"
            )
            original_default_blocker = bool(blockers) and all(
                item.code == "RECIPE_TARGET_NEW_REQUIRED_FIELD" for item in blockers
            )
            interrupted_default_recovery = (
                bool(blockers)
                and all(
                    item.code == "RECIPE_MAPPING_MATERIALIZATION_BLOCKED"
                    for item in blockers
                )
                and bool(default_reviews)
                and application.mapping_id is None
            )
            if not (original_default_blocker or interrupted_default_recovery):
                continue
            required_field_count = (
                len(blockers) if original_default_blocker else len(default_reviews)
            )
            frozen_projection = self._repository.get_workspace_target_schema(
                application.workspace_id
            )
            if frozen_projection is None:
                refusal_reasons.append(
                    "The saved Recipe work area no longer has its checked Odoo "
                    "field evidence. Start a new Test run."
                )
                continue
            selected_models = tuple(
                current_models[model.name]
                for model in frozen_projection.models
                if model.name in current_models
            )
            if len(selected_models) != len(frozen_projection.models):
                refusal_reasons.append(
                    "The current Odoo check did not cover every record type used "
                    "by the saved Recipe. Check the run's Odoo requirements again."
                )
                continue
            projection = replace(
                current_schema,
                workspace_id=application.workspace_id,
                models=selected_models,
                content_hash=content_hash(
                    {
                        "application_id": application.application_id,
                        "current_schema_hash": current_schema.content_hash,
                        "frozen_projection_hash": frozen_projection.content_hash,
                        "kind": "RUN_CREATE_DEFAULT_PROJECTION",
                    }
                ),
                pending_refresh=None,
            )
            envelope = self._recipes.read_revision(
                application.recipe_id,
                application.recipe_revision,
                actor=actor,
            )
            definition = dict(envelope["recipe"])
            parameter_values = dict(
                saved_values_by_recipe.get(application.recipe_id, {})
            )
            parameter_contract = definition.get("parameter_definitions", {})
            declared_parameters = (
                parameter_contract.get("parameters", ())
                if isinstance(parameter_contract, Mapping)
                else ()
            )
            if any(
                isinstance(item, Mapping)
                and str(item.get("logical_parameter_id", ""))
                == EXPORT_AS_OF_PARAMETER_ID
                for item in declared_parameters
            ):
                parameter_values[EXPORT_AS_OF_PARAMETER_ID] = data_version.export_as_of
            assessment = self._compiler.assess(
                recipe_id=application.recipe_id,
                definition=definition,
                source_selection=source_selection,
                target_schema=projection,
                reference_bundle=(
                    run_references.for_workspace(application.workspace_id)
                    if run_references is not None
                    else None
                ),
                parameter_values=parameter_values,
                control_values={},
            )
            legacy_binding_hash = content_hash(
                {
                    "control_values": dict(sorted(assessment.control_values.items())),
                    "parameter_values": dict(
                        sorted(assessment.parameter_values.items())
                    ),
                    "source_bindings": dict(sorted(assessment.source_bindings.items())),
                }
            )
            if not assessment.target_default_fields:
                field_word = "field" if required_field_count == 1 else "fields"
                refusal_reasons.append(
                    "Odoo did not return usable create defaults for the "
                    f"{required_field_count} required {field_word} added by installed "
                    "apps. Publish a new Recipe revision that supplies these values."
                )
                continue
            if assessment.blocked:
                blocker = next(
                    (item for item in assessment.issues if item.blocks),
                    None,
                )
                refusal_reasons.append(
                    f"{blocker.message} {blocker.recovery_action}"
                    if blocker is not None
                    else "The checked Odoo defaults did not resolve every "
                    "saved Recipe blocker. Review the run's saved issues."
                )
                continue
            if (
                application.physical_binding_hash
                not in {
                    assessment.physical_binding_hash,
                    legacy_binding_hash,
                }
                or assessment.parameter_values_hash != application.parameter_values_hash
            ):
                refusal_reasons.append(
                    "The saved Recipe values no longer match this reassessment. "
                    "Start a new Test run so Impodo can bind them again safely."
                )
                continue
            save_projection = getattr(
                self._compiler.schemas,
                "save_run_default_projection",
                None,
            )
            if save_projection is None:
                raise MigrationRunPlanningError(
                    "Run default projection storage is unavailable"
                )
            save_projection(application.workspace_id, projection, actor=actor)
            materialized = self._compiler.materialize(
                application.workspace_id,
                application_id=application.application_id,
                recipe_id=application.recipe_id,
                data_version_id=application.data_version_id,
                definition=definition,
                assessment=assessment,
                actor=actor,
            )
            recovered.append(
                self._repository.save_application_materialization(
                    application.application_id,
                    expected_evidence_hash=application.evidence_hash,
                    status=materialized.status,
                    issues=materialized.issues,
                    mapping_id=materialized.mapping_id,
                    mapping_content_hash=materialized.mapping_content_hash,
                    evidence_hash=materialized.evidence_hash,
                    actor=actor,
                )
            )
        if not recovered:
            raise MigrationRunPlanningError(
                refusal_reasons[0]
                if refusal_reasons
                else "This run has no required-field blocker that current Odoo "
                "defaults can recover. Review the run's saved issues."
            )
        return tuple(recovered)

    def _mark_run_ready_if_complete(
        self,
        migration_run_id: str,
        *,
        actor: Actor,
    ) -> None:
        bundle = self._repository.get_bundle(migration_run_id)
        if not bundle.applications or not all(
            item.status is RecipeApplicationStatus.READY for item in bundle.applications
        ):
            return
        current = self._repository.foundation.get_migration_run(migration_run_id)
        if current.state is not MigrationRunState.DRAFT:
            return
        self._repository.foundation.save_migration_run(
            replace(
                current,
                state=MigrationRunState.READY,
                updated_at=utc_now(),
            ),
            expected_revision=current.optimistic_revision,
            event_type="RECIPE_APPLICATIONS_READY",
            actor=actor,
        )
