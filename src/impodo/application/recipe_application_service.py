"""Adapt the Recipe compiler to current application workspaces."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from impodo.domain.coverage import validate_odoo_selection_reference_outputs
from impodo.domain.mapping.mutations import MappingMutationAction, MappingMutationState
from impodo.domain.preparation.quality import QualityRuleSource
from impodo.domain.recipe_parameters import (
    EXPORT_AS_OF_PARAMETER_ID,
    RecipeParameterDefinition,
    RecipeParameterDefinitions,
    RecipeParameterType,
)
from impodo.domain.run.contracts import (
    MigrationRunPlanIssue,
    MigrationRunPlanIssueLevel,
    OdooModelRequirement,
    RecipeApplicationStatus,
    ReferenceRequirement,
)
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SourceDatasetSet,
    SourceSelection,
    canonical_mapping_source_selection,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.supporting_closure import (
    close_supporting_plan,
    recipe_supporting_choices,
    supporting_mapping_conflicts,
)
from impodo.domain.workspace.supporting_plan import SupportingSourceDescriptor
from impodo.domain.workspace.workbench import WorkspaceState

from ..domain.coverage import CoverageScopeRevision, ReferenceBundle
from ..domain.mapping.contracts import (
    MappingDefinition,
    RowInclusionMode,
)
from ..domain.mapping.create_field_policy import (
    CREATE_DEFAULT_DECISION_POLICY_VERSION,
)
from ..domain.recipe.models import RecipeError
from ..domain.recipe_applications import (
    RecipeApplicationError,
    RecipeApplicationIssue,
    RecipeApplicationIssueLevel,
    RecipeControlValues,
)
from ..domain.serialization import content_hash
from .recipe_application_compilation import RecipeApplicationCompiler
from .recipe_compilation_service import RecipeAuthoringParameterRepository
from .schema_workspace_service import SchemaWorkspaceService
from .supporting_review_service import supporting_review_packet


@dataclass(frozen=True, slots=True)
class RecipeApplicationAssessment:
    """Hold exact physical bindings and focused drift before provisioning."""

    dataset_ids: tuple[str, ...]
    source_bindings: Mapping[str, str]
    parameter_values: Mapping[str, object]
    control_values: Mapping[str, str]
    physical_binding_hash: str
    parameter_values_hash: str
    target_default_fields: tuple[tuple[str, str], ...]
    issues: tuple[MigrationRunPlanIssue, ...]

    @property
    def blocked(self) -> bool:
        return any(item.blocks for item in self.issues)


@dataclass(frozen=True, slots=True)
class RecipeMaterialization:
    """Return the isolated mapping draft and final application issues."""

    status: RecipeApplicationStatus
    mapping_id: str | None
    mapping_content_hash: str | None
    issues: tuple[MigrationRunPlanIssue, ...]
    evidence_hash: str
    completed: bool = True


def _portable_mapping(value: object, label: str) -> dict[str, object]:
    """Return one validated JSON object from a Recipe definition."""

    if not isinstance(value, Mapping):
        raise RecipeApplicationError(f"{label} must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RecipeApplicationError(f"{label} keys must be text")
        result[key] = item
    return result


def _portable_sequence(value: object, label: str) -> tuple[object, ...]:
    """Return one validated JSON array from a Recipe definition."""

    if not isinstance(value, (list, tuple)):
        raise RecipeApplicationError(f"{label} must be a sequence")
    return tuple(value)


class RecipeApplicationService(RecipeApplicationCompiler):
    """Assess and materialize one exact Project-scoped Recipe application.

    The caller supplies one already-provisioned workspace, one immutable
    Recipe envelope, one DataVersion source projection, and one run-level
    target projection. This service does not create DataVersions or call Odoo.
    """

    def __init__(
        self,
        *,
        sources,
        schemas,
        schema_workspace: SchemaWorkspaceService,
        references,
        preparation,
        mappings,
        categorical,
        application_state,
        row_inclusion_reviews=None,
        parameters: RecipeAuthoringParameterRepository | None = None,
    ) -> None:
        self.sources = sources
        self.schemas = schemas
        self._schema_workspace = schema_workspace
        self.references = references
        self.preparation = preparation
        self.mappings = mappings
        self.categorical = categorical
        self.application_state = application_state
        self.row_inclusion_reviews = row_inclusion_reviews
        self.parameters = parameters

    @property
    def schema_workspace(self) -> SchemaWorkspaceService:
        return self._schema_workspace

    def authoring_source_bindings(
        self, definition: Mapping[str, object], selection: SourceSelection,
    ) -> dict[str, str]:
        """Rebind saved file rules without requiring delivery-specific run values."""

        bindings, _, issues = self._source_assessment(definition, selection, {})
        for issue in issues:
            if issue.blocks:
                raise RecipeError(issue.message)
        return bindings

    @staticmethod
    def _authoring_parameters(definition: Mapping[str, object]) -> RecipeParameterDefinitions:
        """Reconstruct declarations without copying any per-delivery values."""
        parameters = _portable_mapping(definition.get("parameter_definitions"), "Recipe inputs")
        custom = []
        for raw in _portable_sequence(parameters.get("parameters", ()), "Recipe inputs"):
            item = _portable_mapping(raw, "Recipe input")
            logical_id = str(item["logical_parameter_id"])
            if logical_id == EXPORT_AS_OF_PARAMETER_ID:
                continue
            declaration = RecipeParameterDefinition(
                name=logical_id.removeprefix("parameter:"),
                label=str(item["label"]),
                value_type=RecipeParameterType(str(item["type"])),
                required=bool(item["required"]),
            )
            if content_hash(declaration.to_recipe_dict()) != content_hash(item):
                raise RecipeError("A saved Recipe input cannot be restored.")
            custom.append(declaration)
        return RecipeParameterDefinitions(tuple(custom))

    @staticmethod
    def advanced_checks(definition: Mapping[str, object]) -> tuple[dict[str, object], ...]:
        """Read the exact advanced rules requiring fresh Authoring approval."""
        quality = _portable_mapping(definition["quality"], "Recipe checks")
        return tuple(
            rule for item in _portable_sequence(quality.get("rules", ()), "Recipe checks")
            for rule in (_portable_mapping(item, "Recipe check"),)
            if rule.get("origin") == QualityRuleSource.SCOPE_APPROVED.value
        )

    def _authoring_scope(
        self, workspace_id: str, origin: CoverageScopeRevision, selection: SourceSelection,
        effective: SourceSelection, *, actor: Actor,
    ) -> tuple[CoverageScopeRevision, bool]:
        self.schema_workspace.authorization.require(
            actor, Capability.COVERAGE_SCOPE, workspace_id=workspace_id,
        )
        names = {dataset.name for source in (selection, effective) for dataset in source.datasets}
        if any(set(declaration.datasets) - names for declaration in origin.declarations):
            raise RecipeError("A table in the saved coverage scope is unavailable in this draft.")
        scope_id = str(uuid5(UUID(workspace_id), "recipe-coverage"))
        current = self.references.get_coverage_scope(workspace_id)
        if current is not None:
            if (
                current.source_selection_hash != selection.content_hash
                or current.declarations != origin.declarations
                or current.scope_id != scope_id
            ):
                raise RecipeError("This draft already has a different approved coverage scope.")
            return current, False
        scope = CoverageScopeRevision(
            scope_id=scope_id,
            workspace_id=workspace_id, version=1, parent_version=None,
            source_selection_hash=selection.content_hash, declarations=origin.declarations,
            approved_by=actor.identity, approved_at=datetime.now(UTC),
        )
        return scope, True

    def _authoring_references(
        self, workspace_id: str, origin_workspace_id: str, definition: Mapping[str, object],
        *, actor: Actor,
    ) -> ReferenceBundle | None:
        required = self.reference_requirements(definition)
        if not required:
            return None
        authorization = self.schema_workspace.authorization
        authorization.require(actor, Capability.COVERAGE_SCOPE, workspace_id=workspace_id)
        authorization.require(actor, Capability.PROJECT_VIEW, workspace_id=origin_workspace_id)
        saved = self.references.get_saved_reference_bundle(
            origin_workspace_id, tuple(item.content_hash for item in required),
        )
        if saved is None:
            raise RecipeError("This Recipe version’s reference lists are unavailable.")
        return replace(saved, workspace_id=workspace_id)

    def has_authoring_seed(self, workspace_id: str, operation_id: str) -> bool:
        """Read the receipt committed with this draft's original mapping write."""

        receipt = self.mappings.mappings.get_mapping_mutation_receipt(workspace_id, operation_id)
        return receipt is not None and receipt.state is MappingMutationState.COMMITTED

    def seed_authoring_mapping(
        self, workspace_id: str, *, definition: Mapping[str, object], schema: OdooSchemaCatalog,
        state: WorkspaceState, operation_id: str, recipe_hash: str, origin_workspace_id: str,
        coverage_scope: CoverageScopeRevision | None = None, actor: Actor,
    ) -> None:
        """Materialize editable rules after a fresh capture, leaving checks to Authoring."""

        working = self.mappings.mappings.get_mapping_working_draft(workspace_id)
        if self.has_authoring_seed(workspace_id, operation_id):
            if working is not None:
                return
            raise RecipeError("The saved field matches for this draft are unavailable.")
        if working is not None:
            raise RecipeError("This workspace already has field matches.")
        if coverage_scope is None and self.advanced_checks(definition):
            raise RecipeError("Review and approve the saved advanced checks.")
        parameters = self._authoring_parameters(definition)
        parameter_store = self.parameters
        if parameters.definitions and parameter_store is None:
            raise RecipeError("Recipe inputs are unavailable.")
        preparation = _portable_mapping(definition.get("source_preparation"), "Recipe source preparation")
        has_preparation = bool(preparation.get("rules") or preparation.get("dataset_uses"))
        needs_physical_selection = has_preparation or coverage_scope is not None
        selection = (
            self.sources.get_source_selection(workspace_id) if needs_physical_selection
            else self.sources.get_mapping_source_selection(workspace_id)
        )
        if selection is None:
            raise RecipeError("The accepted source tables are unavailable.")
        bindings = self.authoring_source_bindings(definition, selection)
        _, target_issues, _ = self._target_assessment(definition, schema)
        for issue in target_issues:
            if issue.blocks and issue.code not in {"RECIPE_TARGET_NEW_REQUIRED_FIELD", "RECIPE_CREATE_HOOK_INPUT_MISSING"}:
                raise RecipeError(issue.message)
        references = self._authoring_references(
            workspace_id, origin_workspace_id, definition, actor=actor,
        )
        application_definition = self._application_definition(definition)
        plan = None
        if has_preparation:
            plan = self._materialize_preparation(
                workspace_id, application_definition, bindings, actor=actor, source=selection,
            )
        governance = self._materialize_governance(
            workspace_id, application_definition, actor=actor, schema=schema,
        )
        effective = selection
        if has_preparation:
            effective = self.sources.get_mapping_source_selection(workspace_id)
        elif coverage_scope is not None:
            if self.preparation.get_derived_entity_plan(workspace_id) is not None:
                raise RecipeError("This draft already has source preparation.")
            effective = canonical_mapping_source_selection(selection)
        if effective is None:
            raise RecipeError("The accepted source tables are unavailable.")
        controls = _portable_mapping(definition.get("control_definitions"), "Recipe controls")
        invariants = {
            str(item["logical_control_id"]): str(item["invariant_expected_total"])
            for raw in _portable_sequence(controls.get("controls", ()), "Recipe controls")
            for item in (_portable_mapping(raw, "Recipe control"),)
            if item.get("invariant_expectation")
        }
        effective_bindings = self._effective_bindings(effective, bindings)
        datasets = self._mapping_datasets(
            application_definition, effective_bindings, effective,
            RecipeControlValues(selection.data_version_id, invariants, actor.identity, datetime.now(UTC)),
            references,
        )
        candidate = MappingDefinition(
            mapping_id=operation_id, source_selection_hash=effective.content_hash,
            schema_hash=governance.content_hash, datasets=datasets,
        )
        quality_rules = self._quality_rules(
            application_definition, effective_bindings, effective,
            approved_scope=coverage_scope is not None, references=references,
        )
        choices = recipe_supporting_choices(
            _portable_mapping(application_definition["odoo_target_contract"], "Recipe Odoo target contract"),
            effective_bindings,
        )
        if references is not None:
            validate_odoo_selection_reference_outputs(
                references, candidate, schema, governance=governance,
            )
        if choices:
            packet = supporting_review_packet(state, selection, schema, plan, governance)
            supporting = self.schema_workspace.supporting
            conflicts = supporting_mapping_conflicts(choices, datasets, schema)
            if conflicts:
                raise RecipeError("Review the saved supporting choices in Odoo data.")
            review_id = str(uuid5(UUID(operation_id), "supporting-choices"))
            current = supporting.current(workspace_id, actor=actor)
            if current is None:
                supporting.save(
                    packet, expected_binding_hash=packet.binding_hash, expected_version=0,
                    operation_id=review_id, choices=choices, actor=actor,
                )
            else:
                supporting.require_current(packet, actor=actor, review=current)
                if current.choices != supporting.normalize(packet, choices):
                    raise RecipeError("Supporting choices changed. Review them in Odoo data.")
        self.mappings.begin_mutation(
            workspace_id, operation_id=operation_id, action=MappingMutationAction.SAVE_PROGRESS,
            request_hash=content_hash((recipe_hash, selection.content_hash)).removeprefix("sha256:"),
            submitted_working_draft_version=None, submitted_mapping_revision_version=None, actor=actor,
        )
        if references is not None:
            self.references.save_reference_bundle(workspace_id, references, actor=actor, restore=True)
        if parameters.definitions and parameter_store is not None:
            parameter_store.save_parameter_definitions(workspace_id, parameters, actor=actor, restore=True)
        approved_scope, needs_approval_write = (
            self._authoring_scope(
                workspace_id, coverage_scope, selection, effective, actor=actor,
            )
            if coverage_scope is not None else (None, False)
        )
        if quality_rules:
            self.application_state.save_authoring_quality_seed(
                workspace_id, operation_id=operation_id, definition=candidate,
                rules=quality_rules, actor=actor,
                coverage_scope_hash=approved_scope.content_hash if approved_scope else None,
                reference_bundle_hash=references.content_hash if approved_scope and references else None,
            )
        if needs_approval_write and approved_scope is not None:
            # A partial attempt with a staged rule still fails closed until the scope exists.
            self.references.save_coverage_scope(
                workspace_id, approved_scope, expected_parent_version=None, actor=actor,
            )
        self.mappings.save_working_draft(
            workspace_id, datasets=datasets, expected_version=None, actor=actor, operation_id=operation_id,
        )

    def assess(
        self,
        *,
        recipe_id: str,
        definition: Mapping[str, object],
        source_selection: SourceDatasetSet,
        target_schema: OdooSchemaCatalog,
        reference_bundle: ReferenceBundle | None,
        parameter_values: Mapping[str, object],
        control_values: Mapping[str, str],
    ) -> RecipeApplicationAssessment:
        """Assess one Recipe without writing a workspace or calling Odoo."""

        application_issues: list[RecipeApplicationIssue] = []
        bindings, _candidates, source_issues = self._source_assessment(
            definition,
            source_selection,
            {},
        )
        application_issues.extend(source_issues)
        _target_hash, target_issues, target_default_fields = self._target_assessment(
            definition,
            target_schema,
        )
        application_issues.extend(target_issues)
        application_issues.extend(
            self._reference_assessment(definition, reference_bundle)
        )
        normalized_parameters: Mapping[str, object] = {}
        normalized_controls: Mapping[str, str] = {}
        try:
            parameter_definitions = _portable_mapping(
                definition.get("parameter_definitions", {}),
                "Recipe parameter definitions",
            )
            normalized_parameters = self._parameter_values(
                _portable_sequence(
                    parameter_definitions.get("parameters", ()),
                    "Recipe parameters",
                ),
                parameter_values,
            )
        except (KeyError, TypeError, ValueError, RecipeApplicationError) as error:
            application_issues.append(
                self._block(
                    "RECIPE_PARAMETER_REVIEW_REQUIRED",
                    str(error),
                    "Confirm the required values for this Test run.",
                    recipe_id,
                )
            )
        try:
            control_definitions = _portable_mapping(
                definition.get("control_definitions", {}),
                "Recipe control definitions",
            )
            normalized_controls = self._control_values(
                _portable_sequence(
                    control_definitions.get("controls", ()),
                    "Recipe controls",
                ),
                control_values,
            )
        except (KeyError, TypeError, ValueError, RecipeApplicationError) as error:
            application_issues.append(
                self._block(
                    "RECIPE_CONTROL_REVIEW_REQUIRED",
                    str(error),
                    "Confirm the required control totals for this Test run.",
                    recipe_id,
                )
            )
        dataset_ids = tuple(
            sorted(
                value
                for logical, value in bindings.items()
                if logical.startswith("dataset:")
            )
        )
        issues = self._issues(recipe_id, tuple(application_issues))
        binding_hash = content_hash(
            {
                "control_values": dict(sorted(normalized_controls.items())),
                "parameter_values": dict(sorted(normalized_parameters.items())),
                "source_bindings": dict(sorted(bindings.items())),
                "target_default_policy_version": (
                    CREATE_DEFAULT_DECISION_POLICY_VERSION
                ),
                "target_default_fields": [
                    list(item) for item in target_default_fields
                ],
            }
        )
        return RecipeApplicationAssessment(
            dataset_ids=dataset_ids,
            source_bindings=bindings,
            parameter_values=normalized_parameters,
            control_values=normalized_controls,
            physical_binding_hash=binding_hash,
            parameter_values_hash=content_hash(
                dict(sorted(normalized_parameters.items()))
            ),
            target_default_fields=target_default_fields,
            issues=issues,
        )

    def materialize(
        self,
        workspace_id: str,
        *,
        application_id: str,
        recipe_id: str,
        data_version_id: str,
        definition: Mapping[str, object],
        assessment: RecipeApplicationAssessment,
        actor: Actor,
    ) -> RecipeMaterialization:
        """Create one fresh mapping draft using only this workspace projection."""

        issues = list(assessment.issues)
        mapping_id = None
        mapping_hash = None
        completed = True
        if not any(item.blocks for item in issues):
            try:
                application_definition = self._application_definition(definition)
                self._materialize_preparation(
                    workspace_id,
                    application_definition,
                    assessment.source_bindings,
                    actor=actor,
                )
                governance = self._materialize_governance(
                    workspace_id,
                    application_definition,
                    actor=actor,
                )
                selection = self.sources.get_mapping_source_selection(workspace_id)
                schema = self.schemas.get_odoo_schema_catalog(workspace_id)
                if selection is None or schema is None:
                    raise RecipeApplicationError(
                        "Application source or target projection is missing"
                    )
                reference_issues = self._reference_issues(
                    application_definition,
                    workspace_id,
                )
                quality_issues = self._quality_issues(application_definition)
                issues.extend(self._issues(recipe_id, tuple(reference_issues)))
                issues.extend(self._issues(recipe_id, tuple(quality_issues)))
                if reference_issues:
                    return self._result(
                        RecipeApplicationStatus.BLOCKED,
                        None,
                        None,
                        issues,
                        application_id=application_id,
                        assessment=assessment,
                    )
                effective_bindings = self._effective_bindings(
                    selection,
                    assessment.source_bindings,
                )
                controls = RecipeControlValues(
                    data_version_id=data_version_id,
                    values=assessment.control_values,
                    actor=actor.identity,
                    confirmed_at=datetime.now(UTC),
                )
                datasets = self._mapping_datasets(
                    application_definition,
                    effective_bindings,
                    selection,
                    controls,
                    self.references.get_reference_bundle(workspace_id),
                    assessment.target_default_fields,
                )
                supporting_choices = recipe_supporting_choices(
                    _portable_mapping(application_definition.get("odoo_target_contract", {}), "Recipe Odoo target contract"),
                    effective_bindings,
                )
                conflicts = supporting_mapping_conflicts(supporting_choices, datasets, schema)
                if supporting_choices:
                    closure = close_supporting_plan(
                        schema=schema,
                        datasets=tuple(SupportingSourceDescriptor(
                            item.dataset_id, item.name, item.origin.value, item.source_evidence_hash
                        ) for item in selection.datasets),
                        mappings=datasets, choices=supporting_choices,
                        source_hash=selection.content_hash, preparation_hash=None,
                        recipe_hash=content_hash(application_definition),
                    )
                    if not closure.proposal.complete:
                        raise RecipeApplicationError("The Recipe supporting-data plan is incomplete for the current source and Odoo details")
                if conflicts:
                    raise RecipeApplicationError(
                        "The Recipe's reviewed supporting choices conflict with its current mapping: "
                        + "; ".join(conflicts)
                    )
                candidate = MappingDefinition(
                    mapping_id=str(uuid4()),
                    source_selection_hash=selection.content_hash,
                    schema_hash=governance.content_hash,
                    datasets=datasets,
                )
                coverage = self.categorical.collect(
                    workspace_id,
                    candidate,
                    selection,
                    schema,
                )
                issues.extend(
                    MigrationRunPlanIssue(
                        code=item.code,
                        level=MigrationRunPlanIssueLevel.BLOCKER,
                        message=item.message,
                        recovery_action=item.remediation,
                        recipe_ids=(recipe_id,),
                    )
                    for item in coverage.issues
                )
                current = self.mappings.mappings.get_mapping_working_draft(
                    workspace_id
                )
                if (
                    current is not None
                    and current.definition.datasets == datasets
                    and current.definition.source_selection_hash
                    == selection.content_hash
                    and current.definition.schema_hash == governance.content_hash
                ):
                    draft = current
                else:
                    draft = self.mappings.save_working_draft(
                        workspace_id,
                        datasets=datasets,
                        expected_version=(current.version if current else None),
                        actor=actor,
                    )
                mapping_id = draft.mapping_id
                mapping_hash = draft.definition.content_hash
                self.application_state.save_quality_seed(
                    workspace_id,
                    application_id=application_id,
                    mapping_content_hash=mapping_hash,
                    mapping_definition=draft.definition,
                    rules=self._quality_rules(
                        application_definition,
                        effective_bindings,
                        selection,
                    ),
                    actor=actor,
                )
                default_review_required = any(
                    item.code == "RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE"
                    and item.level is MigrationRunPlanIssueLevel.REVIEW
                    for item in issues
                )
                if not any(item.blocks for item in issues):
                    current_revision = (
                        self.mappings.mappings.get_mapping_revision(workspace_id)
                    )
                    revision, validation = self.mappings.check_definition(
                        workspace_id,
                        datasets=draft.definition.datasets,
                        expected_parent_version=(
                            current_revision.version
                            if current_revision is not None
                            else None
                        ),
                        expected_working_draft_version=draft.version,
                        actor=actor,
                    )
                    issues.extend(
                        MigrationRunPlanIssue(
                            code=item.code,
                            level=MigrationRunPlanIssueLevel.BLOCKER,
                            message=item.message,
                            recovery_action=item.remediation,
                            recipe_ids=(recipe_id,),
                        )
                        for item in validation.issues
                        if item.severity in {"error", "warning"}
                    )
                    if not any(item.blocks for item in issues) and any(
                        item.row_inclusion.mode
                        is RowInclusionMode.MATCHING_ROWS
                        for item in revision.definition.datasets
                    ):
                        if self.row_inclusion_reviews is None:
                            raise RecipeApplicationError(
                                "Rows-to-use review is unavailable for this Recipe application"
                            )
                        row_review = self.row_inclusion_reviews.check_current(
                            workspace_id,
                            actor=actor,
                        )
                        issues.extend(
                            self._row_inclusion_issues(
                                row_review,
                                recipe_id=recipe_id,
                            )
                        )
                    checked_draft = (
                        self.mappings.mappings.get_mapping_working_draft(
                            workspace_id
                        )
                    )
                    if not default_review_required and not any(
                        item.blocks for item in issues
                    ):
                        self.mappings.submit_current(
                            workspace_id,
                            datasets=revision.definition.datasets,
                            expected_version=revision.version,
                            expected_working_draft_version=(
                                checked_draft.version
                                if checked_draft is not None
                                else None
                            ),
                            actor=actor,
                        )
                    mapping_id = revision.mapping_id
                    mapping_hash = revision.definition.content_hash
                return self._result(
                    (
                        RecipeApplicationStatus.BLOCKED
                        if any(item.blocks for item in issues)
                        or default_review_required
                        else RecipeApplicationStatus.READY
                    ),
                    mapping_id,
                    mapping_hash,
                    issues,
                    application_id=application_id,
                    assessment=assessment,
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                RecipeApplicationError,
                WorkspaceError,
            ) as error:
                completed = False
                issues.append(
                    MigrationRunPlanIssue(
                        code="RECIPE_MAPPING_MATERIALIZATION_BLOCKED",
                        level=MigrationRunPlanIssueLevel.BLOCKER,
                        message=str(error),
                        recovery_action=(
                            "Review the focused source, target, reference, or "
                            "parameter issue before preparing this Recipe."
                        ),
                        recipe_ids=(recipe_id,),
                    )
                )
        return self._result(
            RecipeApplicationStatus.BLOCKED,
            mapping_id,
            mapping_hash,
            issues,
            application_id=application_id,
            assessment=assessment,
            completed=completed,
        )

    @staticmethod
    def _row_inclusion_issues(snapshot, *, recipe_id: str):
        """Project exact fresh-data counts and any required row decision."""

        counts = MigrationRunPlanIssue(
            code="RECIPE_ROW_INCLUSION_CURRENT_COUNTS",
            level=MigrationRunPlanIssueLevel.INFORMATION,
            message=(
                "Current Data version row result: "
                f"{snapshot.source_row_count} source, "
                f"{snapshot.included_count} included, "
                f"{snapshot.excluded_count} excluded, and "
                f"{snapshot.cannot_evaluate_count} could not be checked."
            ),
            recovery_action=(
                "These counts belong to this Data version. The reusable "
                "Recipe rule has not changed."
            ),
            recipe_ids=(recipe_id,),
        )
        blockers = []
        if snapshot.cannot_evaluate_count:
            blockers.append(
                MigrationRunPlanIssue(
                    code="MAPPING_ROW_INCLUSION_CANNOT_EVALUATE",
                    level=MigrationRunPlanIssueLevel.BLOCKER,
                    message=(
                        f"{snapshot.cannot_evaluate_count} source row"
                        f"{'s' if snapshot.cannot_evaluate_count != 1 else ''} "
                        "could not be checked safely."
                    ),
                    recovery_action=(
                        "Review Rows to use and correct the comparison type, "
                        "source value, or rule before continuing."
                    ),
                    recipe_ids=(recipe_id,),
                )
            )
        if snapshot.included_count == 0:
            blockers.append(
                MigrationRunPlanIssue(
                    code="MAPPING_ROW_INCLUSION_ZERO_INCLUDED",
                    level=MigrationRunPlanIssueLevel.BLOCKER,
                    message="The reusable row rule includes no rows in this Data version.",
                    recovery_action=(
                        "Review Rows to use and the current source values before "
                        "continuing."
                    ),
                    recipe_ids=(recipe_id,),
                )
            )
        if snapshot.confirmable and snapshot.excluded_count:
            blockers.append(
                MigrationRunPlanIssue(
                    code="MAPPING_ROW_INCLUSION_CONFIRMATION_REQUIRED",
                    level=MigrationRunPlanIssueLevel.BLOCKER,
                    message=(
                        f"Confirm {snapshot.included_count} included row"
                        f"{'s' if snapshot.included_count != 1 else ''} before "
                        f"excluding {snapshot.excluded_count} other row"
                        f"{'s' if snapshot.excluded_count != 1 else ''}."
                    ),
                    recovery_action=(
                        "Open Rows to use, review the current counts, and confirm "
                        "the rows for this Data version."
                    ),
                    recipe_ids=(recipe_id,),
                )
            )
        return (counts, *blockers)

    @staticmethod
    def requirements(definition: Mapping[str, object]) -> tuple[OdooModelRequirement, ...]:
        """Return one bounded per-Recipe target requirement projection."""

        contract = _portable_mapping(
            definition.get("odoo_target_contract", {}),
            "Recipe Odoo target contract",
        )
        requirements = []
        for raw_model in _portable_sequence(
            contract.get("models", ()),
            "Recipe target models",
        ):
            model = _portable_mapping(raw_model, "Recipe target model")
            fields = tuple(
                sorted(
                    str(_portable_mapping(item, "Recipe target field")["name"])
                    for item in _portable_sequence(
                        model.get("fields", ()),
                        "Recipe target fields",
                    )
                )
            )
            if fields:
                requirements.append(
                    OdooModelRequirement(model=str(model["model"]), fields=fields)
                )
        return tuple(sorted(requirements))

    @staticmethod
    def write_claims(
        definition: Mapping[str, object],
    ) -> tuple[tuple[str, str], ...]:
        """Return conservative first-release model and field ownership claims."""

        contract = _portable_mapping(
            definition.get("odoo_target_contract", {}),
            "Recipe Odoo target contract",
        )
        approved_fields = _portable_mapping(
            contract.get("approved_write_fields", {}),
            "Recipe approved write fields",
        )
        claims = {
            (str(model), str(field))
            for model, fields in approved_fields.items()
            for field in _portable_sequence(
                fields,
                f"Recipe approved fields for {model}",
            )
        }
        return tuple(sorted(claims))

    @staticmethod
    def _application_definition(
        definition: Mapping[str, object],
    ) -> dict[str, object]:
        """Inline portable scalar preparation into fresh mapping rules.

        Structural preparation remains a DerivedEntityPlan. A single-column
        ``NORMALIZE_TEXT`` rule is a scalar value transformation, so carrying
        it into the mapping avoids inventing a copied source table or column.
        """

        result = deepcopy(dict(definition))
        preparation = _portable_mapping(
            result.get("source_preparation", {}),
            "Recipe source preparation",
        )
        rules = _portable_sequence(
            preparation.get("rules", ()),
            "Recipe source-preparation rules",
        )
        remaining = []
        for raw_rule in rules:
            rule = _portable_mapping(raw_rule, "Recipe source-preparation rule")
            if str(rule.get("operation", "")) != "NORMALIZE_TEXT":
                remaining.append(raw_rule)
                continue
            inputs = tuple(
                str(item)
                for item in _portable_sequence(
                    rule.get("input_column_ids", ()),
                    "Recipe preparation inputs",
                )
            )
            output = str(rule.get("output_logical_column_id", ""))
            if len(inputs) != 1 or not output:
                raise RecipeApplicationError(
                    "Reusable text preparation needs one input and one output"
                )
            parameters = _portable_mapping(
                rule.get("parameters", {}),
                "Recipe preparation parameters",
            )
            allowed = {"trim", "collapse_whitespace", "empty_as_null", "case_mode"}
            if not set(parameters).issubset(allowed):
                raise RecipeApplicationError(
                    "Reusable text preparation contains an unsupported option"
                )
            uses = 0
            mapping = _portable_mapping(
                result.get("mapping", {}),
                "Recipe mapping",
            )
            datasets = []
            for raw_dataset in _portable_sequence(
                mapping.get("datasets", ()),
                "Recipe mapping datasets",
            ):
                dataset = _portable_mapping(raw_dataset, "Recipe mapping dataset")
                fields = []
                for raw_field in _portable_sequence(
                    dataset.get("fields", ()),
                    "Recipe mapping fields",
                ):
                    field = _portable_mapping(raw_field, "Recipe mapping field")
                    provider = _portable_mapping(
                        field.get("provider", {}),
                        "Recipe field provider",
                    )
                    source_ids = tuple(
                        str(item)
                        for item in _portable_sequence(
                            provider.get("source_column_ids", ()),
                            "Recipe provider source columns",
                        )
                    )
                    if output not in source_ids:
                        fields.append(field)
                        continue
                    if provider.get("kind") != "SOURCE" or source_ids != (output,):
                        raise RecipeApplicationError(
                            "Reusable text preparation is used outside one source field"
                        )
                    provider["source_column_ids"] = [inputs[0]]
                    field["provider"] = provider
                    transform = _portable_mapping(
                        field.get("transform", {}),
                        "Recipe field transformation",
                    )
                    for key, value in parameters.items():
                        if key in transform and transform[key] != value:
                            raise RecipeApplicationError(
                                "Reusable text preparation conflicts with a mapping rule"
                            )
                        transform[key] = value
                    field["transform"] = transform
                    uses += 1
                    fields.append(field)
                dataset["fields"] = fields
                datasets.append(dataset)
            mapping["datasets"] = datasets
            result["mapping"] = mapping
            if uses == 0:
                raise RecipeApplicationError(
                    "Reusable text preparation output is not used by the mapping"
                )
        preparation["rules"] = remaining
        result["source_preparation"] = preparation
        return result

    @staticmethod
    def reference_requirements(
        definition: Mapping[str, object],
    ) -> tuple[ReferenceRequirement, ...]:
        """Return exact reference datasets required by one Recipe."""

        contract = _portable_mapping(
            definition.get("reference_dependencies", {}),
            "Recipe reference dependencies",
        )
        return tuple(
            sorted(
                ReferenceRequirement(
                    name=str(reference["name"]),
                    content_hash=str(reference["content_hash"]),
                )
                for item in _portable_sequence(
                    contract.get("references", ()),
                    "Recipe references",
                )
                for reference in (
                    _portable_mapping(item, "Recipe reference"),
                )
            )
        )

    def _reference_assessment(
        self,
        definition: Mapping[str, object],
        bundle: ReferenceBundle | None,
    ) -> list[RecipeApplicationIssue]:
        required = self.reference_requirements(definition)
        current = {item.name: item for item in bundle.datasets} if bundle else {}
        return [
            self._block(
                "RECIPE_REFERENCE_DEPENDENCY_MISSING",
                f"Reference data {item.name} is missing or changed.",
                "Load and confirm the exact current reference package.",
                item.name,
            )
            for item in required
            if item.name not in current
            or current[item.name].content_hash != item.content_hash
        ]

    @staticmethod
    def _issues(
        recipe_id: str,
        issues: tuple[RecipeApplicationIssue, ...],
    ) -> tuple[MigrationRunPlanIssue, ...]:
        level = {
            RecipeApplicationIssueLevel.BLOCKER: MigrationRunPlanIssueLevel.BLOCKER,
            RecipeApplicationIssueLevel.REVIEW: MigrationRunPlanIssueLevel.REVIEW,
            RecipeApplicationIssueLevel.INFORMATION: (
                MigrationRunPlanIssueLevel.INFORMATION
            ),
        }
        return tuple(
            MigrationRunPlanIssue(
                code=item.code,
                level=level[item.level],
                message=item.message,
                recovery_action=item.recovery_action,
                recipe_ids=(recipe_id,),
            )
            for item in issues
        )

    @staticmethod
    def _result(
        status,
        mapping_id,
        mapping_hash,
        issues,
        *,
        application_id,
        assessment,
        completed=True,
    ) -> RecipeMaterialization:
        ordered = tuple(
            sorted(
                {content_hash(item.to_dict()): item for item in issues}.values(),
                key=lambda item: (item.level.value, item.code, item.recipe_ids),
            )
        )
        evidence_hash = content_hash(
            {
                "application_id": application_id,
                "issues": [item.to_dict() for item in ordered],
                "mapping_content_hash": mapping_hash,
                "mapping_id": mapping_id,
                "physical_binding_hash": assessment.physical_binding_hash,
                "status": RecipeApplicationStatus(status).value,
            }
        )
        return RecipeMaterialization(
            status=RecipeApplicationStatus(status),
            mapping_id=mapping_id,
            mapping_content_hash=mapping_hash,
            issues=ordered,
            evidence_hash=evidence_hash,
            completed=completed,
        )
