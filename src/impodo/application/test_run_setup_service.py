"""Create and activate fresh Test evidence for selected Recipe versions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid5

from ..access import Actor, AuthorizationPolicy, Capability
from ..data_version_sources import (
    DataVersionSourcePackage,
    SourcePackageOrigin,
    SourcePackageState,
)
from ..data_versions import DataVersionPurpose, DataVersionState
from ..domain.recipe_parameters import (
    EXPORT_AS_OF_PARAMETER_ID,
    RecipeParameterValueError,
    normalize_recipe_parameter_values,
)
from ..domain.serialization import canonical_json, content_hash
from ..inspection import SourceFileCatalog
from ..migration_foundation import (
    MigrationConflictError,
    MigrationFoundationError,
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
    require_revision,
    require_uuid,
    required_text,
    utc_now,
)
from ..migration_run_planning import RecipeDependency, RecipeRevisionSelection
from ..migration_runs import MigrationRunPurpose
from ..migration_test import (
    RecipeRunParameterValue,
    TestRunParameterValues,
    TestRunSetupBinding,
    TestRunSetupBundle,
    TestRunSetupState,
)
from ..recipe_source_binding import (
    logical_dataset_storage_name,
    normalize_recipe_source_name,
)
from ..recipes import RecipeError
from ..workspace_state import SourceMode, WorkspaceStateNotFoundError


@dataclass(frozen=True, slots=True)
class FreshDataInputRequirement:
    """Describe one logical source table that a Recipe expects."""

    logical_dataset_id: str
    label: str
    columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FreshDataParameterRequirement:
    """Describe one value supplied by this run rather than by source rows."""

    logical_parameter_id: str
    label: str
    value_type: str
    required: bool
    constraints: Mapping[str, object]
    supplied_value: str | None


@dataclass(frozen=True, slots=True)
class FreshDataRecipeRequirement:
    """Present the source contract of one exact selected Recipe revision."""

    recipe_id: str
    recipe_revision: int
    display_name: str
    business_purpose: str
    inputs: tuple[FreshDataInputRequirement, ...]
    parameters: tuple[FreshDataParameterRequirement, ...]


@dataclass(frozen=True, slots=True)
class OdooCheckModelRequirement:
    """Present one Recipe-derived Odoo model and its exact field scope."""

    model_name: str
    field_names: tuple[str, ...]
    recipe_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OdooCheckRelationshipRequirement:
    """Retain one Recipe-owned path to a supporting Odoo record type."""

    parent_model: str
    relationship_field: str
    relationship_type: str


@dataclass(frozen=True, slots=True)
class OdooCheckSupportingRequirement:
    """Present one current Odoo value set required by the selected Recipes."""

    model_name: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...]
    relationships: tuple[OdooCheckRelationshipRequirement, ...]
    recipe_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OdooCheckRequirementPlan:
    """Combine every selected Recipe's target needs before contacting Odoo."""

    models: tuple[OdooCheckModelRequirement, ...]
    supporting_values: tuple[OdooCheckSupportingRequirement, ...]

    @property
    def model_names(self) -> tuple[str, ...]:
        return tuple(item.model_name for item in self.models)


@dataclass(frozen=True, slots=True)
class FreshDataRunValue:
    """Present one shared answer requested by one or more selected Recipes."""

    logical_parameter_id: str
    label: str
    value_type: str
    required: bool
    constraints: Mapping[str, object]
    recipe_ids: tuple[str, ...]
    recipe_names: tuple[str, ...]
    supplied_value: str | None
    automatic: bool
    conflict: str | None = None

    @property
    def input_type(self) -> str:
        return {
            "date": "date",
            "decimal": "number",
            "integer": "number",
        }.get(self.value_type, "text")

    @property
    def input_step(self) -> str | None:
        if self.value_type == "decimal":
            return "any"
        if self.value_type == "integer":
            return "1"
        return None

    @property
    def max_length(self) -> int | None:
        value = self.constraints.get("max_length")
        return int(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class FreshDataRunValuePlan:
    """Hold every Recipe-declared run value and its current confirmation."""

    values: tuple[FreshDataRunValue, ...]
    revision: int | None
    confirmed: bool

    @property
    def editable_values(self) -> tuple[FreshDataRunValue, ...]:
        return tuple(item for item in self.values if not item.automatic)

    @property
    def automatic_values(self) -> tuple[FreshDataRunValue, ...]:
        return tuple(item for item in self.values if item.automatic)

    @property
    def ready_to_continue(self) -> bool:
        if not self.can_confirm:
            return False
        if any(
            item.required and not item.supplied_value for item in self.values
        ):
            return False
        return not self.editable_values or self.confirmed

    @property
    def can_confirm(self) -> bool:
        if any(item.conflict for item in self.values):
            return False
        return not any(
            item.automatic and item.required and not item.supplied_value
            for item in self.values
        )


class FreshDataMatchStatus(StrEnum):
    """Describe whether one Recipe input has a safe physical table choice."""

    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class FreshDataTableCandidate:
    """Present one detected physical table that satisfies an input shape."""

    candidate_id: str
    file_id: str
    file_name: str
    table_key: str
    table_name: str
    table_kind: str
    worksheet_name: str
    row_count: int
    columns: tuple[str, ...]
    warnings: tuple[str, ...]
    name_matches: bool

    @property
    def display_name(self) -> str:
        if self.table_key == "csv":
            return self.file_name
        return f"{self.file_name} / {self.table_name}"


@dataclass(frozen=True, slots=True)
class FreshDataInputMatch:
    """Explain one Recipe logical input and its current physical match."""

    logical_dataset_id: str
    label: str
    dataset_name: str
    columns: tuple[str, ...]
    recipe_names: tuple[str, ...]
    status: FreshDataMatchStatus
    candidates: tuple[FreshDataTableCandidate, ...]
    selected_candidate_id: str | None
    explanation: str

    @property
    def selected_candidate(self) -> FreshDataTableCandidate | None:
        return next(
            (
                item
                for item in self.candidates
                if item.candidate_id == self.selected_candidate_id
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class FreshDataMatchPlan:
    """Hold the complete explainable table match for one fresh delivery."""

    inputs: tuple[FreshDataInputMatch, ...]
    unused_files: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ready_to_accept(self) -> bool:
        return bool(self.inputs) and all(
            item.status is FreshDataMatchStatus.MATCHED for item in self.inputs
        ) and not self.unused_files

    @property
    def can_submit(self) -> bool:
        return bool(self.inputs) and all(
            item.status
            in {FreshDataMatchStatus.MATCHED, FreshDataMatchStatus.AMBIGUOUS}
            for item in self.inputs
        ) and not self.unused_files

    @property
    def needs_choice(self) -> bool:
        return any(
            item.status is FreshDataMatchStatus.AMBIGUOUS for item in self.inputs
        )


@dataclass(slots=True)
class _FreshDataInputDefinition:
    """Merge one logical input declared by one or more selected Recipes."""

    columns: tuple[str, ...]
    dataset_name: str
    label: str
    recipe_names: list[str]
    signature: tuple[str, ...]


class TestRunSetupService:
    """Own the guided Test setup before fresh Recipe work areas exist."""

    def __init__(
        self,
        *,
        projects,
        data_versions,
        runs,
        migration_workspaces,
        source_packages,
        workspace_states,
        recipes,
        test_runs,
        run_planning,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.projects = projects
        self.data_versions = data_versions
        self.runs = runs
        self.migration_workspaces = migration_workspaces
        self.source_packages = source_packages
        self.workspace_states = workspace_states
        self.recipes = recipes
        self.test_runs = test_runs
        self.run_planning = run_planning
        self.authorization = authorization

    def start_setup(
        self,
        project_id: str,
        *,
        expected_workspace_revision: int,
        recipe_revisions: tuple[tuple[str, int], ...],
        dependencies: tuple[RecipeDependency, ...],
        label: str,
        export_as_of: str,
        operation_id: str,
        actor: Actor,
    ) -> TestRunSetupBundle:
        """Create one draft Test delivery and its shared setup workspace."""

        project_id = require_uuid(project_id, "project_id")
        operation_id = require_uuid(operation_id, "operation_id")
        expected_workspace_revision = require_revision(
            expected_workspace_revision,
            "expected_workspace_revision",
        )
        clean_label = required_text(label, "label", maximum=200)
        clean_export_as_of = self._export_date(export_as_of)
        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_CREATE,
            project_id=project_id,
        )
        selections = self._selections(project_id, recipe_revisions, actor=actor)
        replay = self._committed_setup(
            project_id,
            operation_id=operation_id,
            label=clean_label,
            export_as_of=clean_export_as_of,
            selections=selections,
            dependencies=dependencies,
            actor=actor,
        )
        if replay is not None:
            return replay

        authoring_versions = tuple(
            item
            for item in self.data_versions.list(project_id, actor=actor)
            if item.purpose is DataVersionPurpose.AUTHORING
            and item.state is DataVersionState.FROZEN
        )
        if not authoring_versions:
            raise MigrationFoundationError(
                "Save the Recipe from an accepted Authoring data version first"
            )
        parent = max(authoring_versions, key=lambda item: item.version_number)
        parent_package = self.source_packages.repository.get_source_package(
            parent.data_version_id
        )
        if (
            parent_package is None
            or parent_package.origin is not SourcePackageOrigin.FILE
        ):
            raise MigrationFoundationError(
                "Testing with a newer delivery currently requires a file-source Recipe"
            )

        data_version = self._data_version(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            parent_data_version_id=parent.data_version_id,
            label=clean_label,
            export_as_of=clean_export_as_of,
            operation_id=self._child_operation(operation_id, "test-data"),
            actor=actor,
        )
        package = self.source_packages.repository.get_source_package(
            data_version.data_version_id
        )
        if package is None:
            self.source_packages.replace_draft(
                DataVersionSourcePackage(
                    data_version_id=data_version.data_version_id,
                    project_id=project_id,
                    revision=1,
                    origin=SourcePackageOrigin.FILE,
                    state=SourcePackageState.DRAFT,
                    files=(),
                    catalogs=(),
                    configurations=(),
                    datasets=(),
                    updated_at=datetime.now(UTC),
                ),
                actor=actor,
                expected_package_revision=None,
            )
        project = self.projects.get(project_id, actor=actor)
        run = self._run(
            project_id,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            label=clean_label,
            operation_id=self._child_operation(operation_id, "test-run"),
            actor=actor,
        )
        project = self.projects.get(project_id, actor=actor)
        setup_workspace = self._workspace(
            project_id,
            expected_workspace_revision=project.optimistic_revision,
            data_version_id=data_version.data_version_id,
            migration_run_id=run.migration_run_id,
            label=f"{clean_label} data and Odoo target setup",
            operation_id=self._child_operation(operation_id, "test-setup-workspace"),
            actor=actor,
        )
        try:
            self.workspace_states.repository.get(setup_workspace.workspace_id)
        except WorkspaceStateNotFoundError:
            project = self.projects.get(project_id, actor=actor)
            self.workspace_states.provision_migration_workspace(
                setup_workspace.workspace_id,
                actor=actor,
                name=setup_workspace.display_name,
                source_system=project.source_system_identity,
                source_mode=SourceMode.FILE,
                data_classification=project.data_classification.value,
                retention_days=project.retention_days,
            )
        binding = TestRunSetupBinding(
            test_run_setup_id=self._child_operation(operation_id, "test-binding"),
            project_id=project_id,
            migration_run_id=run.migration_run_id,
            data_version_id=data_version.data_version_id,
            setup_workspace_id=setup_workspace.workspace_id,
            selected_revisions=selections,
            dependencies=dependencies,
            state=TestRunSetupState.SETUP,
            target_binding_id=None,
            created_at=utc_now(),
        )
        project = self.projects.get(project_id, actor=actor)
        stored = self.test_runs.bind_setup(
            binding,
            expected_workspace_revision=project.optimistic_revision,
            operation_id=self._child_operation(operation_id, "bind-test-setup"),
            request_hash=content_hash(
                {
                    "binding": binding.to_dict(),
                    "export_as_of": clean_export_as_of,
                    "label": clean_label,
                }
            ),
            actor=actor,
        )
        return self._bundle(stored, actor=actor)

    def activate(
        self,
        project_id: str,
        migration_run_id: str,
        *,
        expected_workspace_revision: int,
        target_schema,
        target_reference_bundle,
        credential_generation: str,
        operation_id: str,
        actor: Actor,
    ):
        binding = self.get(migration_run_id, actor=actor)
        if binding.project_id != require_uuid(project_id, "project_id"):
            raise MigrationFoundationError("Test run does not belong to this Project")
        data_version = self.data_versions.get(binding.data_version_id, actor=actor)
        return self.run_planning.activate_test_run(
            project_id,
            expected_workspace_revision=expected_workspace_revision,
            test_binding=binding,
            target_schema=target_schema,
            target_reference_bundle=target_reference_bundle,
            credential_generation=credential_generation,
            parameter_values=self._run_parameter_values(
                binding,
                data_version.export_as_of,
                actor=actor,
            ),
            operation_id=operation_id,
            actor=actor,
        )

    def get(self, migration_run_id: str, *, actor: Actor) -> TestRunSetupBinding:
        binding = self.test_runs.get(migration_run_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        return binding

    def list(self, project_id: str, *, actor: Actor) -> tuple[TestRunSetupBinding, ...]:
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor, Capability.PROJECT_VIEW, project_id=project_id
        )
        return self.test_runs.list_for_project(project_id)

    def fresh_data_requirements(
        self,
        migration_run_id: str,
        *,
        actor: Actor,
    ) -> tuple[FreshDataRecipeRequirement, ...]:
        """Return Recipe-owned source needs without reading per Recipe registry state."""

        binding = self.get(migration_run_id, actor=actor)
        data_version = self.data_versions.get(binding.data_version_id, actor=actor)
        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in binding.selected_revisions
        )
        revisions = self.recipes.read_revisions(
            binding.project_id,
            selected,
            actor=actor,
        )
        selected_by_id = {
            item.recipe_id: item for item in binding.selected_revisions
        }
        requirements = []
        for recipe_id in self._fresh_recipe_order(binding, revisions):
            selection = selected_by_id[recipe_id]
            key = (selection.recipe_id, selection.recipe_revision)
            revision_read = revisions[key]
            envelope = revision_read.envelope
            if str(envelope.get("semantic_hash", "")) != selection.semantic_hash:
                raise RecipeError("The selected Recipe version has changed")
            recipe = revision_read.recipe
            definition = self._recipe_definition(envelope)
            requirements.append(
                FreshDataRecipeRequirement(
                    recipe_id=recipe.recipe_id,
                    recipe_revision=selection.recipe_revision,
                    display_name=recipe.display_name,
                    business_purpose=recipe.business_purpose,
                    inputs=self._fresh_inputs(definition),
                    parameters=self._fresh_parameters(
                        definition,
                        data_version.export_as_of,
                    ),
                )
            )
        return tuple(requirements)

    def fresh_data_run_value_plan(
        self,
        binding: TestRunSetupBinding,
        requirements: tuple[FreshDataRecipeRequirement, ...],
        *,
        actor: Actor,
    ) -> FreshDataRunValuePlan:
        """Merge identical Recipe requests so the data manager answers once."""

        current_binding = self.get(binding.migration_run_id, actor=actor)
        if current_binding.content_hash != binding.content_hash:
            raise MigrationConflictError("Test setup changed; reload and retry")
        current = self.test_runs.get_parameter_values(binding.migration_run_id)
        self._assert_run_value_ownership(binding, current)
        return self._fresh_data_run_value_plan(requirements, current)

    def replace_fresh_data_run_values(
        self,
        binding: TestRunSetupBinding,
        supplied: Mapping[str, object],
        *,
        expected_revision: int | None,
        actor: Actor,
    ) -> TestRunParameterValues | None:
        """Validate and save only the answers declared by selected Recipes."""

        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_EDIT,
            project_id=binding.project_id,
        )
        current_binding = self.test_runs.get(binding.migration_run_id)
        if current_binding.content_hash != binding.content_hash:
            raise MigrationConflictError("Test setup changed; reload and retry")
        requirements = self.fresh_data_requirements(
            binding.migration_run_id,
            actor=actor,
        )
        current = self.test_runs.get_parameter_values(binding.migration_run_id)
        self._assert_run_value_ownership(binding, current)
        current_revision = current.revision if current is not None else None
        if expected_revision != current_revision:
            raise MigrationConflictError("Run values changed; reload and retry")
        plan = self._fresh_data_run_value_plan(requirements, current)
        conflicts = tuple(item.conflict for item in plan.values if item.conflict)
        if conflicts:
            raise MigrationFoundationError(conflicts[0])
        editable = plan.editable_values
        expected_ids = {item.logical_parameter_id for item in editable}
        unknown = sorted(set(supplied) - expected_ids)
        if unknown:
            raise MigrationFoundationError(
                f"Run value {unknown[0]} is not requested by the selected Recipes"
            )
        if not editable:
            return current

        saved_values: list[RecipeRunParameterValue] = []
        for item in editable:
            definition = {
                "constraints": dict(item.constraints),
                "label": item.label,
                "logical_parameter_id": item.logical_parameter_id,
                "required": item.required,
                "type": item.value_type,
            }
            try:
                normalized = normalize_recipe_parameter_values(
                    (definition,),
                    {
                        item.logical_parameter_id: supplied.get(
                            item.logical_parameter_id,
                            "",
                        )
                    },
                )
            except RecipeParameterValueError as error:
                raise MigrationFoundationError(str(error)) from error
            if item.logical_parameter_id not in normalized:
                continue
            value = normalized[item.logical_parameter_id]
            for recipe_id in item.recipe_ids:
                saved_values.append(
                    RecipeRunParameterValue(
                        recipe_id=recipe_id,
                        logical_parameter_id=item.logical_parameter_id,
                        value=value,
                    )
                )
        ordered = tuple(
            sorted(
                saved_values,
                key=lambda item: (item.recipe_id, item.logical_parameter_id),
            )
        )
        if current is not None and current.values == ordered:
            return current
        data_version = self.data_versions.get(
            binding.data_version_id,
            actor=actor,
        )
        if current is not None and data_version.state is DataVersionState.FROZEN:
            raise MigrationConflictError(
                "Run details were accepted with this fresh data; start a new "
                "Test run to change them"
            )
        replacement = TestRunParameterValues(
            test_run_setup_id=binding.test_run_setup_id,
            project_id=binding.project_id,
            migration_run_id=binding.migration_run_id,
            revision=1 if current is None else current.revision + 1,
            values=ordered,
            updated_by=actor.identity,
            updated_at=utc_now(),
        )
        return self.test_runs.replace_parameter_values(
            replacement,
            expected_revision=expected_revision,
            actor=actor,
        )

    @staticmethod
    def fresh_data_match_plan(
        requirements: tuple[FreshDataRecipeRequirement, ...],
        catalogs: tuple[SourceFileCatalog, ...],
        *,
        overrides: Mapping[str, str] | None = None,
    ) -> FreshDataMatchPlan:
        """Match Recipe logical inputs to bounded detected-table evidence."""

        selected_overrides = dict(overrides or {})
        definitions: dict[str, _FreshDataInputDefinition] = {}
        conflicts: dict[str, str] = {}
        storage_owners: dict[str, str] = {}
        for recipe in requirements:
            for source_input in recipe.inputs:
                logical_id = source_input.logical_dataset_id
                signature = tuple(
                    sorted(
                        normalize_recipe_source_name(column)
                        for column in source_input.columns
                    )
                )
                current = definitions.get(logical_id)
                if current is None:
                    dataset_name = logical_dataset_storage_name(logical_id)
                    owner = storage_owners.get(dataset_name)
                    if owner is not None and owner != logical_id:
                        conflicts[logical_id] = (
                            "Two Recipe inputs resolve to the same accepted table name"
                        )
                        conflicts[owner] = conflicts[logical_id]
                    storage_owners[dataset_name] = logical_id
                    definitions[logical_id] = _FreshDataInputDefinition(
                        columns=tuple(source_input.columns),
                        dataset_name=dataset_name,
                        label=source_input.label,
                        recipe_names=[recipe.display_name],
                        signature=signature,
                    )
                elif current.signature != signature:
                    conflicts[logical_id] = (
                        "Selected Recipes disagree about this logical source input"
                    )
                else:
                    if recipe.display_name not in current.recipe_names:
                        current.recipe_names.append(recipe.display_name)

        matches: list[FreshDataInputMatch] = []
        explicitly_selected: set[str] = set()
        for logical_id, definition in definitions.items():
            label = definition.label
            required_columns = definition.columns
            candidates = _fresh_table_candidates(
                catalogs,
                label=label,
                required_columns=required_columns,
            )
            if logical_id in conflicts:
                matches.append(
                    FreshDataInputMatch(
                        logical_dataset_id=logical_id,
                        label=label,
                        dataset_name=definition.dataset_name,
                        columns=required_columns,
                        recipe_names=tuple(definition.recipe_names),
                        status=FreshDataMatchStatus.CONFLICT,
                        candidates=candidates,
                        selected_candidate_id=None,
                        explanation=conflicts[logical_id],
                    )
                )
                continue

            override = selected_overrides.get(logical_id, "").strip()
            selected: FreshDataTableCandidate | None = None
            status = FreshDataMatchStatus.MISSING
            explanation = (
                "No safe detected table contains every required column."
            )
            if override:
                explicitly_selected.add(logical_id)
                selected = next(
                    (item for item in candidates if item.candidate_id == override),
                    None,
                )
                if selected is None:
                    status = FreshDataMatchStatus.CONFLICT
                    explanation = (
                        "The selected table is no longer a current compatible choice."
                    )
                else:
                    status = FreshDataMatchStatus.MATCHED
                    explanation = (
                        "You selected this table from the compatible choices."
                    )
            elif len(candidates) == 1:
                selected = candidates[0]
                status = FreshDataMatchStatus.MATCHED
                explanation = (
                    "All required columns were found in the only compatible table."
                )
            elif candidates:
                name_matches = tuple(item for item in candidates if item.name_matches)
                if len(name_matches) == 1:
                    selected = name_matches[0]
                    status = FreshDataMatchStatus.MATCHED
                    explanation = (
                        "All required columns were found and the table name matches "
                        "the Recipe input."
                    )
                else:
                    status = FreshDataMatchStatus.AMBIGUOUS
                    explanation = (
                        "More than one detected table contains every required column."
                    )
            matches.append(
                FreshDataInputMatch(
                    logical_dataset_id=logical_id,
                    label=label,
                    dataset_name=definition.dataset_name,
                    columns=required_columns,
                    recipe_names=tuple(definition.recipe_names),
                    status=status,
                    candidates=candidates,
                    selected_candidate_id=(
                        selected.candidate_id if selected is not None else None
                    ),
                    explanation=explanation,
                )
            )

        chosen: dict[str, list[int]] = {}
        for index, match in enumerate(matches):
            if match.selected_candidate_id is not None:
                chosen.setdefault(match.selected_candidate_id, []).append(index)
        for indexes in chosen.values():
            if len(indexes) < 2:
                continue
            for index in indexes:
                match = matches[index]
                can_choose_another = len(match.candidates) > 1
                matches[index] = replace(
                    match,
                    status=(
                        FreshDataMatchStatus.AMBIGUOUS
                        if can_choose_another
                        else FreshDataMatchStatus.CONFLICT
                    ),
                    selected_candidate_id=(
                        match.selected_candidate_id
                        if match.logical_dataset_id in explicitly_selected
                        else None
                    ),
                    explanation=(
                        "One physical table cannot fill two different Recipe inputs. "
                        + (
                            "Choose another compatible table."
                            if can_choose_another
                            else "Add a separate table for one of these inputs."
                        )
                    ),
                )

        overlapping_indexes: set[int] = set()
        for left_index, left in enumerate(matches):
            left_candidate = left.selected_candidate
            if left_candidate is None:
                continue
            for right_index in range(left_index + 1, len(matches)):
                right_candidate = matches[right_index].selected_candidate
                if right_candidate is not None and _fresh_candidates_overlap(
                    left_candidate,
                    right_candidate,
                ):
                    overlapping_indexes.update((left_index, right_index))
        for index in overlapping_indexes:
            match = matches[index]
            can_choose_another = len(match.candidates) > 1
            matches[index] = replace(
                match,
                status=(
                    FreshDataMatchStatus.AMBIGUOUS
                    if can_choose_another
                    else FreshDataMatchStatus.CONFLICT
                ),
                selected_candidate_id=(
                    match.selected_candidate_id
                    if match.logical_dataset_id in explicitly_selected
                    else None
                ),
                explanation=(
                    "A worksheet and one of its Excel tables cover the same "
                    "workbook area. "
                    + (
                        "Choose a non-overlapping table."
                        if can_choose_another
                        else "Supply separate tables for these Recipe inputs."
                    )
                ),
            )

        resolved = all(
            item.status is FreshDataMatchStatus.MATCHED for item in matches
        )
        relevant_file_ids = (
            {
                item.selected_candidate.file_id
                for item in matches
                if item.selected_candidate is not None
            }
            if resolved
            else {
                candidate.file_id
                for item in matches
                for candidate in item.candidates
            }
        )
        unused_files = tuple(
            catalog.display_name
            for catalog in catalogs
            if catalog.file_id not in relevant_file_ids
        )
        warning_values = tuple(
            dict.fromkeys(
                warning
                for item in matches
                for candidate in (
                    (item.selected_candidate,)
                    if item.selected_candidate is not None
                    else item.candidates
                )
                if candidate is not None
                for warning in candidate.warnings
            )
        )
        return FreshDataMatchPlan(
            inputs=tuple(matches),
            unused_files=unused_files,
            warnings=warning_values,
        )

    @staticmethod
    def _fresh_recipe_order(binding, revisions):
        """Order source cards by dependency, then by the Recipe's business name."""

        recipe_ids = {item.recipe_id for item in binding.selected_revisions}
        selections = {
            item.recipe_id: item for item in binding.selected_revisions
        }
        incoming = {recipe_id: 0 for recipe_id in recipe_ids}
        downstream = {recipe_id: set() for recipe_id in recipe_ids}
        for dependency in binding.dependencies:
            if dependency.after_recipe_id not in downstream[dependency.before_recipe_id]:
                downstream[dependency.before_recipe_id].add(
                    dependency.after_recipe_id
                )
                incoming[dependency.after_recipe_id] += 1

        def business_key(recipe_id):
            selection = selections[recipe_id]
            recipe = revisions[
                (selection.recipe_id, selection.recipe_revision)
            ].recipe
            return (recipe.display_name.casefold(), recipe.recipe_id)

        ready = sorted(
            (recipe_id for recipe_id, count in incoming.items() if count == 0),
            key=business_key,
        )
        ordered = []
        while ready:
            recipe_id = ready.pop(0)
            ordered.append(recipe_id)
            for after_id in sorted(downstream[recipe_id], key=business_key):
                incoming[after_id] -= 1
                if incoming[after_id] == 0:
                    ready.append(after_id)
                    ready.sort(key=business_key)
        if len(ordered) != len(recipe_ids):
            ordered.extend(sorted(recipe_ids - set(ordered), key=business_key))
        return tuple(ordered)

    def required_models_for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> tuple[str, ...]:
        """Return the one combined Recipe-derived model scope for a run."""

        plan = self.odoo_check_requirements_for_workspace(
            workspace_id,
            actor=actor,
        )
        return plan.model_names if plan is not None else ()

    def odoo_check_requirements_for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> OdooCheckRequirementPlan | None:
        """Read all selected revisions once and combine their Odoo needs."""

        binding = self.test_runs.for_workspace(workspace_id)
        if binding is None:
            return None
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in binding.selected_revisions
        )
        revisions = self.recipes.read_revisions(
            binding.project_id,
            selected,
            actor=actor,
        )
        model_fields: dict[str, set[str]] = {}
        model_recipes: dict[str, set[str]] = {}
        supporting_recipes: dict[
            tuple[str, tuple[str, ...], tuple[str, ...]], set[str]
        ] = {}
        supporting_paths: dict[
            tuple[str, tuple[str, ...], tuple[str, ...]],
            set[tuple[str, str, str]],
        ] = {}
        for selection in binding.selected_revisions:
            revision = revisions[(selection.recipe_id, selection.recipe_revision)]
            if str(revision.envelope.get("semantic_hash", "")) != selection.semantic_hash:
                raise RecipeError("The selected Recipe version has changed")
            definition = self._recipe_definition(revision.envelope)
            recipe_name = revision.recipe.display_name
            contract = dict(definition.get("odoo_target_contract", {}))
            for model in contract.get("models", ()):  # type: ignore[union-attr]
                model_definition = dict(model)
                model_name = str(model_definition.get("model", "")).strip()
                if model_name:
                    model_fields.setdefault(model_name, set()).update(
                        str(dict(field).get("name", "")).strip()
                        for field in model_definition.get("fields", ())
                        if str(dict(field).get("name", "")).strip()
                    )
                    model_recipes.setdefault(model_name, set()).add(recipe_name)
                for raw_path in model_definition.get("reference_paths", ()):
                    path = dict(raw_path)
                    key_fields = tuple(
                        str(value).strip()
                        for value in path.get("key_fields", ())
                        if str(value).strip()
                    )
                    scope_fields = tuple(
                        str(value).strip()
                        for value in path.get("scope_fields", ())
                        if str(value).strip()
                    )
                    parent_model = str(path.get("parent_model", "")).strip()
                    relationship_field = str(
                        path.get("relationship_field", "")
                    ).strip()
                    relationship_type = str(
                        path.get("relationship_type", "")
                    ).strip()
                    if not (
                        model_name
                        and key_fields
                        and parent_model
                        and relationship_field
                        and relationship_type
                    ):
                        raise RecipeError(
                            "A selected Recipe contains an incomplete Odoo relationship"
                        )
                    identity = (model_name, key_fields, scope_fields)
                    supporting_recipes.setdefault(identity, set()).add(recipe_name)
                    supporting_paths.setdefault(identity, set()).add(
                        (
                            parent_model,
                            relationship_field,
                            relationship_type,
                        )
                    )
        return OdooCheckRequirementPlan(
            models=tuple(
                OdooCheckModelRequirement(
                    model_name=model_name,
                    field_names=tuple(sorted(model_fields[model_name])),
                    recipe_names=tuple(
                        sorted(model_recipes[model_name], key=str.casefold)
                    ),
                )
                for model_name in sorted(model_fields)
            ),
            supporting_values=tuple(
                OdooCheckSupportingRequirement(
                    model_name=identity[0],
                    key_fields=identity[1],
                    scope_fields=identity[2],
                    relationships=tuple(
                        OdooCheckRelationshipRequirement(
                            parent_model=parent_model,
                            relationship_field=relationship_field,
                            relationship_type=relationship_type,
                        )
                        for (
                            parent_model,
                            relationship_field,
                            relationship_type,
                        ) in sorted(supporting_paths[identity])
                    ),
                    recipe_names=tuple(
                        sorted(supporting_recipes[identity], key=str.casefold)
                    ),
                )
                for identity in sorted(supporting_recipes)
            ),
        )

    def setup_binding_for_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
    ) -> TestRunSetupBinding | None:
        """Return the owning setup only when this is its shared workspace."""

        binding = self.test_runs.for_workspace(workspace_id)
        if binding is None or binding.setup_workspace_id != workspace_id:
            return None
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        return binding

    def credential_workspace(self, workspace_id: str, *, actor: Actor):
        """Return the shared Test setup workspace that owns target credentials."""

        workspace_id = self.credential_workspace_id(workspace_id, actor=actor)
        return self.workspace_states.repository.get(workspace_id)

    def credential_workspace_id(self, workspace_id: str, *, actor: Actor) -> str:
        """Return the credential owner without opening another workspace store."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        binding = self.test_runs.for_workspace(workspace_id)
        if binding is None:
            return workspace_id
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=binding.project_id,
        )
        return binding.setup_workspace_id

    def _selections(self, project_id, values, *, actor):
        normalized = tuple(values)
        revisions = self.recipes.read_revisions(
            project_id,
            normalized,
            actor=actor,
        )
        selections = []
        for recipe_id, version in normalized:
            revision_read = revisions[(recipe_id, version)]
            recipe = revision_read.recipe
            if recipe.project_id != project_id:
                raise MigrationFoundationError("Recipe belongs to another Project")
            envelope = revision_read.envelope
            selections.append(
                RecipeRevisionSelection(
                    recipe_id=recipe_id,
                    recipe_revision=version,
                    semantic_hash=str(envelope["semantic_hash"]),
                )
            )
        return tuple(selections)

    def _run_parameter_values(self, binding, export_as_of, *, actor):
        selected = tuple(
            (item.recipe_id, item.recipe_revision)
            for item in binding.selected_revisions
        )
        revisions = self.recipes.read_revisions(
            binding.project_id,
            selected,
            actor=actor,
        )
        stored = self.test_runs.get_parameter_values(binding.migration_run_id)
        self._assert_run_value_ownership(binding, stored)
        stored_by_recipe = stored.by_recipe if stored is not None else {}
        selected_recipe_ids = {
            item.recipe_id for item in binding.selected_revisions
        }
        if set(stored_by_recipe) - selected_recipe_ids:
            raise MigrationConflictError(
                "Saved run values do not match the selected Recipes"
            )
        values = {}
        editable_declared = False
        for selection in binding.selected_revisions:
            definition = self._recipe_definition(
                revisions[(selection.recipe_id, selection.recipe_revision)].envelope
            )
            definitions = self._parameter_definitions(definition)
            declared = {
                str(item.get("logical_parameter_id", "")) for item in definitions
            }
            editable_declared = editable_declared or any(
                str(item.get("logical_parameter_id", ""))
                != EXPORT_AS_OF_PARAMETER_ID
                for item in definitions
            )
            recipe_values = dict(stored_by_recipe.get(selection.recipe_id, {}))
            if EXPORT_AS_OF_PARAMETER_ID in declared:
                recipe_values[EXPORT_AS_OF_PARAMETER_ID] = (
                    self._export_date(export_as_of)
                )
            try:
                values[selection.recipe_id] = normalize_recipe_parameter_values(
                    definitions,
                    recipe_values,
                )
            except RecipeParameterValueError as error:
                raise MigrationFoundationError(str(error)) from error
        if editable_declared and stored is None:
            raise MigrationFoundationError(
                "Confirm the Recipe details for this run on Fresh data"
            )
        return values

    @staticmethod
    def _recipe_definition(envelope) -> Mapping[str, object]:
        definition = envelope.get("recipe")
        if not isinstance(definition, Mapping):
            raise RecipeError("Stored Recipe source requirements are invalid")
        return definition

    @staticmethod
    def _parameter_definitions(definition) -> tuple[Mapping[str, object], ...]:
        payload = definition.get("parameter_definitions", {})
        if not isinstance(payload, Mapping):
            raise RecipeError("Stored Recipe run values are invalid")
        parameters = payload.get("parameters", ())
        if not isinstance(parameters, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in parameters
        ):
            raise RecipeError("Stored Recipe run values are invalid")
        return tuple(parameters)

    @classmethod
    def _fresh_parameters(cls, definition, export_as_of):
        return tuple(
            FreshDataParameterRequirement(
                logical_parameter_id=str(item.get("logical_parameter_id", "")),
                label=str(item.get("label", "Run value")),
                value_type=str(item.get("type", "string")),
                required=bool(item.get("required", False)),
                constraints=dict(item.get("constraints", {})),
                supplied_value=(
                    cls._export_date(export_as_of)
                    if item.get("logical_parameter_id")
                    == "parameter:export_as_of_date"
                    else None
                ),
            )
            for item in cls._parameter_definitions(definition)
        )

    @staticmethod
    def _fresh_data_run_value_plan(
        requirements: tuple[FreshDataRecipeRequirement, ...],
        current: TestRunParameterValues | None,
    ) -> FreshDataRunValuePlan:
        stored = current.by_recipe if current is not None else {}
        grouped: dict[
            str,
            list[tuple[FreshDataRecipeRequirement, FreshDataParameterRequirement]],
        ] = {}
        for recipe in requirements:
            for parameter in recipe.parameters:
                grouped.setdefault(parameter.logical_parameter_id, []).append(
                    (recipe, parameter)
                )

        values: list[FreshDataRunValue] = []
        for logical_id, uses in grouped.items():
            first = uses[0][1]
            signature = canonical_json(
                {
                    "constraints": dict(first.constraints),
                    "required": first.required,
                    "type": first.value_type,
                }
            )
            conflict = None
            if any(
                canonical_json(
                    {
                        "constraints": dict(parameter.constraints),
                        "required": parameter.required,
                        "type": parameter.value_type,
                    }
                )
                != signature
                for _recipe, parameter in uses[1:]
            ):
                conflict = (
                    "Selected Recipes disagree about the meaning of "
                    f"{first.label}. Start a new Test run with compatible "
                    "Recipe versions."
                )

            automatic_flags = {
                parameter.supplied_value is not None
                for _recipe, parameter in uses
            }
            if len(automatic_flags) != 1:
                conflict = (
                    "Selected Recipes disagree about who supplies "
                    f"{first.label}. Start a new Test run with compatible "
                    "Recipe versions."
                )
            automatic = automatic_flags == {True}
            candidate_values: list[object] = []
            for recipe, parameter in uses:
                if automatic:
                    candidate_values.append(parameter.supplied_value)
                elif logical_id in stored.get(recipe.recipe_id, {}):
                    candidate_values.append(stored[recipe.recipe_id][logical_id])
            if candidate_values and (
                len(candidate_values) != len(uses)
                or any(value != candidate_values[0] for value in candidate_values[1:])
            ):
                conflict = f"Saved answers for {first.label} are inconsistent"
            supplied_value = (
                str(candidate_values[0]) if candidate_values else None
            )
            values.append(
                FreshDataRunValue(
                    logical_parameter_id=logical_id,
                    label=first.label,
                    value_type=first.value_type,
                    required=first.required,
                    constraints=dict(first.constraints),
                    recipe_ids=tuple(recipe.recipe_id for recipe, _item in uses),
                    recipe_names=tuple(
                        recipe.display_name for recipe, _item in uses
                    ),
                    supplied_value=supplied_value,
                    automatic=automatic,
                    conflict=conflict,
                )
            )
        return FreshDataRunValuePlan(
            values=tuple(values),
            revision=current.revision if current is not None else None,
            confirmed=current is not None,
        )

    @staticmethod
    def _assert_run_value_ownership(
        binding: TestRunSetupBinding,
        current: TestRunParameterValues | None,
    ) -> None:
        if current is not None and (
            current.test_run_setup_id != binding.test_run_setup_id
            or current.project_id != binding.project_id
            or current.migration_run_id != binding.migration_run_id
        ):
            raise MigrationConflictError(
                "Saved run values do not belong to this Test setup"
            )

    @staticmethod
    def _export_date(export_as_of):
        candidate = str(export_as_of).strip()[:10]
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError as error:
            raise MigrationFoundationError(
                "The Test delivery cutoff must start with a year-month-day date"
            ) from error

    @staticmethod
    def _fresh_inputs(definition) -> tuple[FreshDataInputRequirement, ...]:
        source_shape = definition.get("source_shape", {})
        if not isinstance(source_shape, Mapping):
            raise RecipeError("Stored Recipe source requirements are invalid")
        datasets = source_shape.get("datasets", ())
        if not isinstance(datasets, (list, tuple)) or any(
            not isinstance(item, Mapping) for item in datasets
        ):
            raise RecipeError("Stored Recipe source requirements are invalid")
        result = []
        for dataset in datasets:
            columns = dataset.get("columns", ())
            if not isinstance(columns, (list, tuple)) or any(
                not isinstance(item, Mapping) for item in columns
            ):
                raise RecipeError("Stored Recipe source requirements are invalid")
            result.append(
                FreshDataInputRequirement(
                    logical_dataset_id=str(dataset.get("logical_dataset_id", "")),
                    label=str(dataset.get("logical_name", "Required table")),
                    columns=tuple(
                        str(column.get("source_name", "Required column"))
                        for column in columns
                    ),
                )
            )
        return tuple(sorted(result, key=lambda item: item.logical_dataset_id))

    def _committed_setup(
        self,
        project_id,
        *,
        operation_id,
        label,
        export_as_of,
        selections,
        dependencies,
        actor,
    ):
        bind_operation = self._child_operation(operation_id, "bind-test-setup")
        try:
            intent = self.test_runs.foundation.get_operation_intent(bind_operation)
        except MigrationNotFoundError:
            return None
        if (
            intent.kind is not MigrationOperationKind.TEST_RUN_SETUP
            or intent.project_id != project_id
            or intent.actor.issuer != actor.identity.issuer
            or intent.actor.subject_id != actor.identity.subject_id
        ):
            raise MigrationConflictError(
                "Operation identity was already used with different meaning"
            )
        if intent.state is not MigrationOperationState.COMMITTED:
            return None
        binding = self.test_runs.get(intent.owner_id)
        data_version = self.data_versions.get(binding.data_version_id, actor=actor)
        if (
            binding.selected_revisions != selections
            or binding.dependencies != tuple(sorted(dependencies))
            or data_version.label != label
            or data_version.export_as_of != export_as_of.strip()
        ):
            raise MigrationConflictError(
                "Operation identity was already used for another Test setup"
            )
        return self._bundle(binding, actor=actor)

    def _bundle(self, binding, *, actor):
        return TestRunSetupBundle(
            data_version=self.data_versions.get(binding.data_version_id, actor=actor),
            run=self.runs.get(binding.migration_run_id, actor=actor),
            setup_workspace=self.migration_workspaces.get(
                binding.setup_workspace_id,
                actor=actor,
            ),
            binding=binding,
        )

    def _data_version(
        self,
        project_id,
        *,
        expected_workspace_revision,
        parent_data_version_id,
        label,
        export_as_of,
        operation_id,
        actor,
    ):
        try:
            intent = self.data_versions.repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.data_versions.create(
                project_id,
                actor=actor,
                expected_workspace_revision=expected_workspace_revision,
                purpose=DataVersionPurpose.TEST,
                label=label,
                export_as_of=export_as_of,
                parent_data_version_id=parent_data_version_id,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.data_versions.repository.get_data_version(intent.owner_id)
        return self.data_versions.repository.resume_data_version_creation(
            operation_id,
            actor=actor,
        )

    def _run(
        self,
        project_id,
        *,
        expected_workspace_revision,
        data_version_id,
        label,
        operation_id,
        actor,
    ):
        try:
            intent = self.runs.repository.get_operation_intent(operation_id)
        except MigrationNotFoundError:
            return self.runs.create(
                project_id,
                actor=actor,
                expected_workspace_revision=expected_workspace_revision,
                data_version_id=data_version_id,
                purpose=MigrationRunPurpose.TEST,
                label=label,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.runs.repository.get_migration_run(intent.owner_id)
        return self.runs.repository.resume_migration_run_creation(
            operation_id,
            actor=actor,
        )

    def _workspace(
        self,
        project_id,
        *,
        expected_workspace_revision,
        data_version_id,
        migration_run_id,
        label,
        operation_id,
        actor,
    ):
        try:
            intent = self.migration_workspaces.repository.get_operation_intent(
                operation_id
            )
        except MigrationNotFoundError:
            return self.migration_workspaces.create(
                project_id,
                actor=actor,
                expected_workspace_revision=expected_workspace_revision,
                data_version_id=data_version_id,
                migration_run_id=migration_run_id,
                display_name=label,
                operation_id=operation_id,
            )
        if intent.state is MigrationOperationState.COMMITTED:
            return self.migration_workspaces.repository.get_migration_workspace(
                intent.owner_id
            )
        return self.migration_workspaces.repository.resume_migration_workspace_creation(
            operation_id,
            actor=actor,
        )

    @staticmethod
    def _child_operation(operation_id: str, name: str) -> str:
        return str(uuid5(UUID(operation_id), name))


def _fresh_table_candidates(
    catalogs: tuple[SourceFileCatalog, ...],
    *,
    label: str,
    required_columns: tuple[str, ...],
) -> tuple[FreshDataTableCandidate, ...]:
    """Return safe tables whose detected headers cover one Recipe input."""

    required_tokens = tuple(
        normalize_recipe_source_name(item) for item in required_columns
    )
    if (
        not required_tokens
        or any(not token for token in required_tokens)
        or len(set(required_tokens)) != len(required_tokens)
    ):
        return ()
    candidates = []
    for catalog in catalogs:
        for table in catalog.tables:
            if table.formula_cell_count or table.error_cell_count:
                continue
            columns_by_token: dict[str, list[str]] = {}
            for column in table.columns:
                columns_by_token.setdefault(
                    normalize_recipe_source_name(column.name),
                    [],
                ).append(column.name)
            if any(
                len(columns_by_token.get(token, ())) != 1
                for token in required_tokens
            ):
                continue
            candidate_id = content_hash(
                {
                    "catalog_hash": catalog.content_hash,
                    "file_id": catalog.file_id,
                    "table_key": table.table_key,
                }
            )
            name_token = normalize_recipe_source_name(label)
            candidates.append(
                FreshDataTableCandidate(
                    candidate_id=candidate_id,
                    file_id=catalog.file_id,
                    file_name=catalog.display_name,
                    table_key=table.table_key,
                    table_name=table.name,
                    table_kind=table.kind,
                    worksheet_name=table.worksheet_name,
                    row_count=table.row_count,
                    columns=tuple(column.name for column in table.columns),
                    warnings=tuple(
                        dict.fromkeys((*catalog.warnings, *table.warnings))
                    ),
                    name_matches=(
                        normalize_recipe_source_name(table.name) == name_token
                        or normalize_recipe_source_name(Path(catalog.display_name).stem)
                        == name_token
                    ),
                )
            )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.display_name.casefold(),
                item.candidate_id,
            ),
        )
    )


def _fresh_candidates_overlap(
    left: FreshDataTableCandidate,
    right: FreshDataTableCandidate,
) -> bool:
    """Reject selecting both a worksheet and one of its named Excel tables."""

    if left.file_id != right.file_id:
        return False
    for named, worksheet in ((left, right), (right, left)):
        if (
            named.table_kind == "NAMED_TABLE"
            and worksheet.table_kind == "WORKSHEET"
            and named.worksheet_name == worksheet.table_name
        ):
            return True
    return False
