"""Plan and provision one Project-owned Test run for several Recipes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Mapping
from uuid import UUID, uuid4, uuid5

from ..access import Actor, AuthorizationPolicy, Capability
from ..data_version_sources import (
    DataVersionSourcePackage,
    SourcePackageOrigin,
    WorkspaceSourceProjectionService,
)
from ..data_versions import DataVersionPurpose, DataVersionService, DataVersionState
from ..domain.serialization import content_hash
from ..domain.coverage import ReferenceBundle
from ..migration_foundation import (
    FaultInjector,
    MigrationFoundationError,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from ..migration_cutover import CutoverWriteOwnership
from ..migration_projects import MigrationProjectService
from ..migration_run_planning import (
    IntegratedRunBundle,
    MigrationRunPlanIssue,
    MigrationRunPlanIssueLevel,
    MigrationRunPlanningError,
    MigrationRunRequirementPlan,
    OdooModelRequirement,
    PlannedRecipeApplication,
    RecipeApplicationStatus,
    RecipeDependency,
    ReferenceRequirement,
    RecipeRevisionSelection,
    RunRecipeApplication,
    RunTargetBinding,
)
from ..migration_runs import MigrationRun, MigrationRunPurpose, MigrationRunState
from ..migration_workspaces import (
    MigrationWorkspace,
    MigrationWorkspaceState,
)
from ..project_recipes import ProjectRecipe, ProjectRecipeService
from ..projects import ProjectNotFoundError, ProjectService, SourceMode
from ..workspace_contracts import OdooSchemaCatalog, SourceSelection
from .project_recipe_application_compiler import (
    ProjectRecipeApplicationAssessment,
    ProjectRecipeApplicationCompiler,
)


@dataclass(frozen=True, slots=True)
class ReviewedRecipeApplication:
    recipe: ProjectRecipe
    selection: RecipeRevisionSelection
    definition: Mapping[str, object]
    requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    write_claims: tuple[tuple[str, str], ...]
    assessment: ProjectRecipeApplicationAssessment


@dataclass(frozen=True, slots=True)
class IntegratedRunReview:
    """Show planning blockers before any run or workspace is created."""

    project_id: str
    data_version_id: str
    applications: tuple[ReviewedRecipeApplication, ...]
    dependencies: tuple[RecipeDependency, ...]
    model_requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    application_order: tuple[str, ...]
    planning_issues: tuple[MigrationRunPlanIssue, ...]

    @property
    def can_start(self) -> bool:
        return not any(item.blocks for item in self.planning_issues)


class MigrationRunPlanningService:
    """Own M4 validation, isolated provisioning, and compiler orchestration."""

    def __init__(
        self,
        *,
        projects: MigrationProjectService,
        data_versions: DataVersionService,
        recipes: ProjectRecipeService,
        repository,
        source_packages,
        source_projections: WorkspaceSourceProjectionService,
        workspace_states: ProjectService,
        compiler: ProjectRecipeApplicationCompiler,
        cutover_plans,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.projects = projects
        self.data_versions = data_versions
        self.recipes = recipes
        self.repository = repository
        self.source_packages = source_packages
        self.source_projections = source_projections
        self.workspace_states = workspace_states
        self.compiler = compiler
        self.cutover_plans = cutover_plans
        self.authorization = authorization

    def review_test_run(
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
        actor: Actor,
    ) -> IntegratedRunReview:
        """Validate dependency and write ownership before provisioning."""

        project_id = require_uuid(project_id, "project_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        self.authorization.require(
            actor,
            Capability.RECIPE_APPLY,
            project_id=project_id,
        )
        project = self.projects.get(project_id, actor=actor)
        target_workspace = self.repository.foundation.get_migration_workspace(
            require_uuid(target_schema.project_id, "target evidence workspace_id")
        )
        if (
            target_workspace.project_id != project_id
            or target_workspace.recipe_application_id is not None
        ):
            raise MigrationRunPlanningError(
                "Choose reviewed Odoo evidence from this Project's authoring workspace"
            )
        if (
            target_reference_bundle is not None
            and target_reference_bundle.project_id != target_workspace.workspace_id
        ):
            raise MigrationRunPlanningError(
                "The supporting lists do not match the reviewed Odoo workspace"
            )
        data_version = self.data_versions.get(data_version_id, actor=actor)
        if (
            data_version.project_id != project.project_id
            or data_version.purpose is not DataVersionPurpose.TEST
            or data_version.state is not DataVersionState.FROZEN
        ):
            raise MigrationRunPlanningError(
                "Choose one accepted Test DataVersion from this Project"
            )
        package = self.source_packages.repository.get_source_package(data_version_id)
        if package is None or package.content_hash != data_version.source_package_hash:
            raise MigrationRunPlanningError(
                "The Test DataVersion source evidence is missing or inconsistent"
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
            raise MigrationRunPlanningError(
                "Select one revision from each Recipe used by this Test run"
            )
        source_selection = self._package_selection(package)
        supplied_parameters = parameter_values or {}
        supplied_controls = control_values or {}
        applications = []
        for recipe_id, version in normalized:
            recipe = self.recipes.get(recipe_id, actor=actor)
            if recipe.project_id != project_id:
                raise MigrationRunPlanningError(
                    "Every selected Recipe must belong to this Project"
                )
            envelope = self.recipes.read_revision(recipe_id, version, actor=actor)
            semantic_hash = str(envelope["semantic_hash"])
            definition = dict(envelope["recipe"])
            selection = RecipeRevisionSelection(
                recipe_id=recipe_id,
                recipe_revision=version,
                semantic_hash=semantic_hash,
            )
            applications.append(
                ReviewedRecipeApplication(
                    recipe=recipe,
                    selection=selection,
                    definition=definition,
                    requirements=self.compiler.requirements(definition),
                    reference_requirements=(
                        self.compiler.reference_requirements(definition)
                    ),
                    write_claims=self.compiler.write_claims(definition),
                    assessment=self.compiler.assess(
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
        application_order = self._application_order(
            selected_ids,
            dependencies,
            planning_issues,
        )
        self._write_collision_issues(applications, planning_issues)
        requirements = self._union_requirements(applications)
        reference_requirements = self._union_reference_requirements(
            applications,
            planning_issues,
        )
        if target_schema.connection_target_hash.strip() == "":
            planning_issues.append(
                self._block(
                    "RUN_TARGET_IDENTITY_MISSING",
                    "The selected Odoo evidence has no exact target identity.",
                    "Capture current Odoo 19 evidence before starting the Test run.",
                    tuple(selected_ids),
                )
            )
        return IntegratedRunReview(
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

    def start_test_run(
        self,
        project_id: str,
        *,
        expected_project_revision: int,
        data_version_id: str,
        recipe_revisions: tuple[tuple[str, int], ...],
        dependencies: tuple[RecipeDependency, ...],
        target_schema: OdooSchemaCatalog,
        target_reference_bundle: ReferenceBundle | None,
        credential_generation: str,
        label: str,
        operation_id: str,
        actor: Actor,
        parameter_values: Mapping[str, Mapping[str, object]] | None = None,
        control_values: Mapping[str, Mapping[str, str]] | None = None,
        fault: FaultInjector | None = None,
    ) -> IntegratedRunBundle:
        """Provision and materialize one restart-safe multi-Recipe Test run."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_project_revision = require_revision(
            expected_project_revision,
            "expected_project_revision",
        )
        clean_label = required_text(label, "label", maximum=200)
        credential_generation = required_text(
            credential_generation,
            "credential_generation",
            maximum=300,
        )
        if credential_generation != target_schema.read_credential_binding_hash:
            raise MigrationRunPlanningError(
                "The reviewed Odoo evidence belongs to another read credential generation"
            )
        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_CREATE,
            project_id=project_id,
        )
        self.authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_CREATE,
            project_id=project_id,
        )
        review = self.review_test_run(
            project_id,
            data_version_id=data_version_id,
            recipe_revisions=recipe_revisions,
            dependencies=dependencies,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            parameter_values=parameter_values,
            control_values=control_values,
            actor=actor,
        )
        if not review.can_start:
            first = next(item for item in review.planning_issues if item.blocks)
            raise MigrationRunPlanningError(
                f"{first.message} {first.recovery_action}"
            )
        now = utc_now()
        run_id = self._child_operation(operation_id, "migration-run")
        target_binding_id = self._child_operation(operation_id, "target-binding")
        required_reference_names = {
            item.name for item in review.reference_requirements
        }
        captured_reference_datasets = tuple(
            item
            for item in (
                target_reference_bundle.datasets
                if target_reference_bundle is not None
                else ()
            )
            if item.name in required_reference_names
        )
        run_reference_bundle = (
            ReferenceBundle(
                project_id=run_id,
                datasets=captured_reference_datasets,
            )
            if captured_reference_datasets
            else None
        )
        required_models = {item.model for item in review.model_requirements}
        run_target_schema = replace(
            target_schema,
            project_id=run_id,
            models=tuple(
                item for item in target_schema.models if item.name in required_models
            ),
            content_hash=content_hash(
                {
                    "migration_run_id": run_id,
                    "requirements": [
                        item.to_dict() for item in review.model_requirements
                    ],
                    "source_schema_hash": target_schema.content_hash,
                }
            ),
        )
        run = MigrationRun(
            migration_run_id=run_id,
            project_id=project_id,
            data_version_id=data_version_id,
            run_number=self.repository.foundation.next_run_number(project_id),
            purpose=MigrationRunPurpose.TEST,
            label=clean_label,
            state=MigrationRunState.DRAFT,
            target_binding_id=target_binding_id,
            cutover_selection_id=None,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        target = RunTargetBinding(
            target_binding_id=target_binding_id,
            project_id=project_id,
            migration_run_id=run_id,
            environment="TEST",
            connection_target_hash=target_schema.connection_target_hash,
            credential_role="READ",
            credential_generation=credential_generation,
            principal_hash=target_schema.read_principal_hash,
            permission_hash=target_schema.read_permission_hash,
            context_hash=target_schema.read_context_hash,
            schema_dependency_hash=run_target_schema.content_hash,
            reference_snapshot_hashes=tuple(
                item.content_hash for item in captured_reference_datasets
            ),
            created_at=now,
        )
        requirement_plan = MigrationRunRequirementPlan(
            migration_run_id=run_id,
            project_id=project_id,
            data_version_id=data_version_id,
            target_binding_id=target_binding_id,
            selected_revisions=tuple(
                item.selection for item in review.applications
            ),
            dependencies=review.dependencies,
            model_requirements=review.model_requirements,
            reference_requirements=review.reference_requirements,
            application_order=review.application_order,
            created_at=now,
        )
        planned = tuple(
            self._planned_application(
                item,
                run=run,
                target=target,
                now=now,
            )
            for item in review.applications
        )
        request_hash = content_hash(
            {
                "control_values": control_values or {},
                "data_version_id": data_version_id,
                "dependencies": [item.to_dict() for item in review.dependencies],
                "label": clean_label,
                "parameter_values": parameter_values or {},
                "project_id": project_id,
                "selected_revisions": [
                    item.selection.to_dict() for item in review.applications
                ],
                "reference_bundle": (
                    run_reference_bundle.to_portable_dict()
                    if run_reference_bundle is not None
                    else None
                ),
                "target_schema_hash": run_target_schema.content_hash,
            }
        )
        bundle = self.repository.provision_integrated_run(
            run=run,
            target_binding=target,
            requirement_plan=requirement_plan,
            applications=planned,
            target_schema=run_target_schema,
            reference_bundle=run_reference_bundle,
            expected_project_revision=expected_project_revision,
            operation_id=operation_id,
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )
        package = self.source_packages.repository.get_source_package(data_version_id)
        if package is None:
            raise MigrationRunPlanningError("Test DataVersion source package is missing")
        reviewed = {item.selection.recipe_id: item for item in review.applications}
        workspace_by_id = {item.workspace_id: item for item in bundle.workspaces}
        project = self.projects.get(project_id, actor=actor)
        stored_applications = []
        for application in bundle.applications:
            item = reviewed[application.recipe_id]
            workspace = workspace_by_id[application.workspace_id]
            if item.assessment.dataset_ids:
                projection = self.source_projections.repository.get_workspace_source_projection(
                    workspace.workspace_id
                )
                if projection is None:
                    self.source_projections.materialize(
                        workspace.workspace_id,
                        actor=actor,
                        dataset_ids=item.assessment.dataset_ids,
                        expected_workspace_revision=workspace.optimistic_revision,
                        operation_id=self._child_operation(
                            operation_id,
                            f"source:{application.recipe_id}",
                        ),
                    )
            self._provision_engine(
                workspace,
                project=project,
                package=package,
                actor=actor,
            )
            materialized = self.compiler.materialize(
                workspace.workspace_id,
                application_id=application.application_id,
                recipe_id=application.recipe_id,
                data_version_id=application.data_version_id,
                definition=item.definition,
                assessment=item.assessment,
                actor=actor,
            )
            stored_applications.append(
                self.repository.save_application_materialization(
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
        if stored_applications and all(
            item.status is RecipeApplicationStatus.READY
            for item in stored_applications
        ):
            current = self.repository.foundation.get_migration_run(
                bundle.run.migration_run_id
            )
            if current.state is MigrationRunState.DRAFT:
                self.repository.foundation.save_migration_run(
                    replace(current, state=MigrationRunState.READY, updated_at=utc_now()),
                    expected_revision=current.optimistic_revision,
                    event_type="INTEGRATED_TEST_RUN_READY",
                    actor=actor,
                )
        committed = self.repository.commit_provisioning(operation_id)
        self.cutover_plans.ensure_for_run(
            project_id=project_id,
            migration_run_id=committed.run.migration_run_id,
            requirement_plan=committed.requirement_plan,
            write_ownership=tuple(
                sorted(
                    CutoverWriteOwnership(
                        recipe_id=item.selection.recipe_id,
                        model=model,
                        field=field,
                    )
                    for item in review.applications
                    for model, field in item.write_claims
                )
            ),
            operation_id=self._child_operation(operation_id, "cutover-plan"),
            actor=actor,
        )
        return committed

    def target_schema_from_workspace(
        self,
        project_id: str,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> OdooSchemaCatalog:
        """Read one reviewed target snapshot for run-level reuse."""

        return self.target_evidence_from_workspace(
            project_id,
            workspace_id,
            actor=actor,
        )[0]

    def target_evidence_from_workspace(
        self,
        project_id: str,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[OdooSchemaCatalog, ReferenceBundle | None]:
        """Read one reviewed schema and reference package for run-level reuse."""

        project_id = require_uuid(project_id, "project_id")
        workspace_id = require_uuid(workspace_id, "workspace_id")
        workspace = self.repository.foundation.get_migration_workspace(workspace_id)
        if workspace.project_id != project_id:
            raise MigrationRunPlanningError(
                "The selected Odoo evidence belongs to another Project"
            )
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        schema = self.compiler.schemas.get_odoo_schema_catalog(workspace_id)
        if schema is None or schema.origin.value != "LIVE_API":
            raise MigrationRunPlanningError(
                "Capture authenticated Odoo 19 evidence in the authoring workspace first"
            )
        try:
            major = int(str(schema.odoo_version).split(".", 1)[0])
        except ValueError:
            major = -1
        if major != 19:
            raise MigrationRunPlanningError(
                "The selected target evidence is not from Odoo 19"
            )
        references = self.compiler.references.get_reference_bundle(workspace_id)
        return schema, references

    def _planned_application(
        self,
        item: ReviewedRecipeApplication,
        *,
        run: MigrationRun,
        target: RunTargetBinding,
        now: datetime,
    ) -> PlannedRecipeApplication:
        application_id = str(uuid4())
        workspace_id = str(uuid4())
        issues = item.assessment.issues
        status = (
            RecipeApplicationStatus.BLOCKED
            if item.assessment.blocked
            else RecipeApplicationStatus.DRAFT_READINESS
        )
        issue_hash = content_hash([value.to_dict() for value in issues])
        evidence_hash = content_hash(
            {
                "application_id": application_id,
                "issue_hash": issue_hash,
                "physical_binding_hash": item.assessment.physical_binding_hash,
                "recipe": item.selection.to_dict(),
                "status": status.value,
                "target_binding_hash": target.content_hash,
            }
        )
        application = RunRecipeApplication(
            application_id=application_id,
            project_id=run.project_id,
            migration_run_id=run.migration_run_id,
            data_version_id=run.data_version_id,
            workspace_id=workspace_id,
            recipe_id=item.selection.recipe_id,
            recipe_revision=item.selection.recipe_revision,
            recipe_semantic_hash=item.selection.semantic_hash,
            target_binding_id=target.target_binding_id,
            physical_binding_hash=item.assessment.physical_binding_hash,
            parameter_values_hash=item.assessment.parameter_values_hash,
            status=status,
            issue_hash=issue_hash,
            mapping_id=None,
            mapping_content_hash=None,
            evidence_hash=evidence_hash,
            created_at=now,
            updated_at=now,
        )
        workspace = MigrationWorkspace(
            workspace_id=workspace_id,
            project_id=run.project_id,
            data_version_id=run.data_version_id,
            migration_run_id=run.migration_run_id,
            recipe_application_id=application_id,
            display_name=f"{item.recipe.display_name} Test application",
            state=MigrationWorkspaceState.OPEN,
            optimistic_revision=1,
            created_at=now,
            updated_at=now,
        )
        return PlannedRecipeApplication(
            application=application,
            workspace=workspace,
            dataset_ids=item.assessment.dataset_ids,
            requirements=item.requirements,
            reference_requirements=item.reference_requirements,
            issues=issues,
        )

    def _provision_engine(
        self,
        workspace: MigrationWorkspace,
        *,
        project,
        package: DataVersionSourcePackage,
        actor: Actor,
    ) -> None:
        try:
            self.workspace_states.repository.get(workspace.workspace_id)
            return
        except ProjectNotFoundError:
            pass
        source_mode = (
            SourceMode.FILE
            if package.origin is SourcePackageOrigin.FILE
            else SourceMode.ODOO
        )
        self.workspace_states.provision_migration_workspace(
            workspace.workspace_id,
            actor=actor,
            name=workspace.display_name,
            source_system=project.source_system_identity,
            source_mode=source_mode,
            data_classification=project.data_classification.value,
            retention_days=project.retention_days,
        )

    @staticmethod
    def _package_selection(package: DataVersionSourcePackage) -> SourceSelection:
        datasets = tuple(item.to_mapping_dataset() for item in package.datasets)
        return SourceSelection(
            selection_id=str(uuid5(UUID(package.data_version_id), "m4-package-view")),
            version=1,
            project_id=package.data_version_id,
            created_at=package.updated_at,
            created_by="Accepted Test DataVersion",
            datasets=datasets,
            content_hash=content_hash(
                {
                    "data_version_id": package.data_version_id,
                    "datasets": [item.to_dict() for item in datasets],
                    "package_hash": package.content_hash,
                }
            ),
        )

    @staticmethod
    def _union_requirements(
        applications: list[ReviewedRecipeApplication],
    ) -> tuple[OdooModelRequirement, ...]:
        by_model: dict[str, set[str]] = {}
        for item in applications:
            for requirement in item.requirements:
                by_model.setdefault(requirement.model, set()).update(
                    requirement.fields
                )
        return tuple(
            OdooModelRequirement(model=model, fields=tuple(fields))
            for model, fields in sorted(by_model.items())
        )

    @staticmethod
    def _union_reference_requirements(
        applications: list[ReviewedRecipeApplication],
        issues: list[MigrationRunPlanIssue],
    ) -> tuple[ReferenceRequirement, ...]:
        by_name: dict[str, ReferenceRequirement] = {}
        owners: dict[str, str] = {}
        for application in applications:
            for requirement in application.reference_requirements:
                current = by_name.get(requirement.name)
                if current is None:
                    by_name[requirement.name] = requirement
                    owners[requirement.name] = application.selection.recipe_id
                    continue
                if current.content_hash != requirement.content_hash:
                    issues.append(
                        MigrationRunPlanningService._block(
                            "RUN_REFERENCE_REQUIREMENT_COLLISION",
                            (
                                f"Two Recipes require different versions of "
                                f"reference data {requirement.name}."
                            ),
                            (
                                "Publish compatible Recipe revisions or use "
                                "one shared reviewed reference version."
                            ),
                            (
                                owners[requirement.name],
                                application.selection.recipe_id,
                            ),
                        )
                    )
        return tuple(sorted(by_name.values()))

    @staticmethod
    def _write_collision_issues(applications, issues) -> None:
        owners: dict[tuple[str, str], str] = {}
        for item in applications:
            for claim in item.write_claims:
                previous = owners.setdefault(claim, item.selection.recipe_id)
                if previous != item.selection.recipe_id:
                    issues.append(
                        MigrationRunPlanningService._block(
                            "RUN_RECIPE_WRITE_COLLISION",
                            (
                                f"Two Recipes may both write {claim[0]}.{claim[1]}."
                            ),
                            (
                                "Choose one owning Recipe for this Odoo field or "
                                "publish non-overlapping Recipe meaning. Reordering "
                                "does not resolve the collision."
                            ),
                            (previous, item.selection.recipe_id),
                        )
                    )

    @staticmethod
    def _application_order(selected_ids, dependencies, issues) -> tuple[str, ...]:
        following = {recipe_id: set() for recipe_id in selected_ids}
        indegree = {recipe_id: 0 for recipe_id in selected_ids}
        seen = set()
        for edge in dependencies:
            key = (edge.before_recipe_id, edge.after_recipe_id)
            if key in seen:
                issues.append(
                    MigrationRunPlanningService._block(
                        "RUN_DEPENDENCY_DUPLICATED",
                        "The same Recipe dependency was selected more than once.",
                        "Keep one copy of each dependency.",
                        key,
                    )
                )
                continue
            seen.add(key)
            if (
                edge.before_recipe_id not in selected_ids
                or edge.after_recipe_id not in selected_ids
            ):
                issues.append(
                    MigrationRunPlanningService._block(
                        "RUN_DEPENDENCY_RECIPE_MISSING",
                        "A dependency refers to a Recipe outside this Test run.",
                        "Select both Recipes or remove that dependency.",
                        key,
                    )
                )
                continue
            following[edge.before_recipe_id].add(edge.after_recipe_id)
            indegree[edge.after_recipe_id] += 1
        ready = sorted(recipe_id for recipe_id, count in indegree.items() if count == 0)
        order = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for after in sorted(following[current]):
                indegree[after] -= 1
                if indegree[after] == 0:
                    ready.append(after)
                    ready.sort()
        if len(order) != len(selected_ids):
            cycle_ids = tuple(sorted(key for key, value in indegree.items() if value))
            issues.append(
                MigrationRunPlanningService._block(
                    "RUN_RECIPE_DEPENDENCY_CYCLE",
                    "The selected Recipes form a dependency cycle.",
                    "Remove one dependency so the applications have a clear order.",
                    cycle_ids,
                )
            )
            return tuple(sorted(selected_ids))
        return tuple(order)

    @staticmethod
    def _block(code, message, recovery, recipe_ids):
        return MigrationRunPlanIssue(
            code=code,
            level=MigrationRunPlanIssueLevel.BLOCKER,
            message=message,
            recovery_action=recovery,
            recipe_ids=tuple(recipe_ids),
        )

    @staticmethod
    def _child_operation(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), name))
