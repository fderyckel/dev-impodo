"""Validate a proposed integrated run before any workspace is created."""

from __future__ import annotations

from collections.abc import Mapping

from impodo.domain.shared.access import Actor, Capability
from impodo.domain.data_version.models import DataVersionPurpose, DataVersionState
from impodo.domain.coverage import ReferenceBundle
from impodo.domain.run.planning import (
    blocking_run_issue,
    collect_write_collision_issues,
    order_recipe_applications,
    union_model_requirements,
    union_reference_requirements,
)
from impodo.domain.project.foundation import require_revision, require_uuid
from impodo.domain.run.contracts import RecipeDependency, RecipeRevisionSelection
from impodo.domain.workspace.contracts import OdooSchemaCatalog


class RunReviewUseCase:
    """Own selection, compatibility, and write-ownership review for a run."""

    def __init__(
        self,
        *,
        projects,
        data_versions,
        recipes,
        repository,
        source_packages,
        compiler,
        authorization,
        planning_error: type[Exception],
        reviewed_application_type,
        review_type,
        package_selection,
    ) -> None:
        self._projects = projects
        self._data_versions = data_versions
        self._recipes = recipes
        self._repository = repository
        self._source_packages = source_packages
        self._compiler = compiler
        self._authorization = authorization
        self._planning_error = planning_error
        self._reviewed_application_type = reviewed_application_type
        self._review_type = review_type
        self._package_selection = package_selection

    def review(
        self,
        project_id: str,
        *,
        data_version_id: str,
        recipe_revisions: tuple[tuple[str, int], ...],
        dependencies: tuple[RecipeDependency, ...],
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        parameter_values: Mapping[str, Mapping[str, object]] | None,
        control_values: Mapping[str, Mapping[str, str]] | None,
        purpose: DataVersionPurpose,
        required_target_workspace_id: str | None,
        actor: Actor,
    ):
        """Validate one Test or Production plan without creating workspaces."""

        project_id = require_uuid(project_id, "project_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        self._authorization.require(
            actor,
            Capability.RECIPE_APPLY,
            project_id=project_id,
        )
        project = self._projects.get(project_id, actor=actor)
        target_workspace = self._repository.foundation.get_migration_workspace(
            require_uuid(target_schema.workspace_id, "target evidence workspace_id")
        )
        target_data_version = self._data_versions.repository.get_data_version(
            target_workspace.data_version_id
        )
        if (
            target_workspace.project_id != project_id
            or target_workspace.recipe_application_id is not None
            or (
                purpose is DataVersionPurpose.TEST
                and target_data_version.purpose is DataVersionPurpose.PRODUCTION
            )
            or (
                required_target_workspace_id is not None
                and target_workspace.workspace_id != required_target_workspace_id
            )
        ):
            raise self._planning_error(
                "Choose reviewed Odoo evidence from this run's setup workspace"
            )
        if (
            target_reference_bundle is not None
            and target_reference_bundle.workspace_id != target_workspace.workspace_id
        ):
            raise self._planning_error(
                "The supporting lists do not match the reviewed Odoo workspace"
            )
        data_version = self._data_versions.get(data_version_id, actor=actor)
        if (
            data_version.project_id != project.project_id
            or data_version.purpose is not purpose
            or data_version.state is not DataVersionState.FROZEN
        ):
            raise self._planning_error(
                f"Choose one accepted {purpose.value.title()} DataVersion from this Project"
            )
        package = self._source_packages.repository.get_source_package(data_version_id)
        if package is None or package.content_hash != data_version.source_package_hash:
            raise self._planning_error(
                f"The {purpose.value.title()} DataVersion source evidence is "
                "missing or inconsistent"
            )
        normalized = tuple(
            sorted(
                (
                    require_uuid(recipe_id, "recipe_id"),
                    require_revision(version, "recipe_revision"),
                )
                for recipe_id, version in recipe_revisions
            )
        )
        if not normalized or len({item[0] for item in normalized}) != len(normalized):
            raise self._planning_error(
                f"Select one revision from each Recipe used by this {purpose.value.title()} run"
            )
        source_selection = self._package_selection(package)
        supplied_parameters = parameter_values or {}
        supplied_controls = control_values or {}
        applications = []
        for recipe_id, version in normalized:
            recipe = self._recipes.get(recipe_id, actor=actor)
            if recipe.project_id != project_id:
                raise self._planning_error(
                    "Every selected Recipe must belong to this Project"
                )
            envelope = self._recipes.read_revision(recipe_id, version, actor=actor)
            semantic_hash = str(envelope["semantic_hash"])
            definition = dict(envelope["recipe"])
            selection = RecipeRevisionSelection(
                recipe_id=recipe_id,
                recipe_revision=version,
                semantic_hash=semantic_hash,
            )
            applications.append(
                self._reviewed_application_type(
                    recipe=recipe,
                    selection=selection,
                    definition=definition,
                    requirements=self._compiler.requirements(definition),
                    reference_requirements=(
                        self._compiler.reference_requirements(definition)
                    ),
                    write_claims=self._compiler.write_claims(definition),
                    assessment=self._compiler.assess(
                        recipe_id=recipe_id,
                        definition=definition,
                        source_selection=source_selection,
                        target_schema=target_schema,
                        reference_bundle=target_reference_bundle,
                        parameter_values=supplied_parameters.get(recipe_id, {}),
                        control_values=supplied_controls.get(recipe_id, {}),
                    ),
                )
            )
        selected_ids = {item.selection.recipe_id for item in applications}
        planning_issues = []
        application_order, dependency_issues = order_recipe_applications(
            selected_ids,
            dependencies,
        )
        planning_issues.extend(dependency_issues)
        planning_issues.extend(collect_write_collision_issues(applications))
        requirements = union_model_requirements(applications)
        reference_requirements, reference_issues = union_reference_requirements(
            applications
        )
        planning_issues.extend(reference_issues)
        if target_schema.connection_target_hash.strip() == "":
            planning_issues.append(
                blocking_run_issue(
                    "RUN_TARGET_IDENTITY_MISSING",
                    "The selected Odoo evidence has no exact target identity.",
                    "Capture current Odoo 19 evidence before starting the "
                    f"{purpose.value.title()} run.",
                    tuple(selected_ids),
                )
            )
        return self._review_type(
            project_id=project_id,
            data_version_id=data_version_id,
            applications=tuple(applications),
            dependencies=tuple(
                sorted(
                    dependencies,
                    key=lambda item: (
                        item.before_recipe_id,
                        item.after_recipe_id,
                    ),
                )
            ),
            model_requirements=requirements,
            reference_requirements=reference_requirements,
            application_order=application_order,
            planning_issues=tuple(
                sorted(
                    planning_issues,
                    key=lambda item: (item.code, item.recipe_ids),
                )
            ),
        )
