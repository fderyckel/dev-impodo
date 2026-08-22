"""Compile current workspace authoring evidence into portable Recipe meaning."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
from typing import Mapping, Protocol
import unicodedata

from ..access import Actor, AuthorizationPolicy, Capability
from ..derived_entities import DerivedEntityPlan
from ..domain.coverage import ReferenceBundle
from ..domain.mapping.artifacts import MappingRevision, MappingSubmission
from ..domain.mapping.contracts import (
    DatasetMapping,
    MappingTargetMode,
)
from ..domain.schema.governance import BusinessKeyStatus, SchemaGovernance
from ..domain.recipe_parameters import (
    RecipeParameterDefinition,
    RecipeParameterDefinitionError,
    RecipeParameterDefinitions,
    RecipeParameterType,
)
from ..domain.serialization import canonical_json, content_hash, portable
from ..projects import WorkspaceState, ProjectService, SourceMode
from ..quality import QualityRuleSet, QualityRuleSource
from ..reference_keys import (
    REFERENCE_POLICY_HASH,
    GovernedReferenceDecision,
    GovernedReferenceRequest,
    ReferenceEvidenceKind,
    ReferencePolicyDenial,
    ReferenceReadPurpose,
    authorize_governed_reference,
    captured_reference_field_contracts,
)
from ..recipes import (
    DataVersionPurpose,
    Recipe,
    RecipeConflictError,
    RecipeDraft,
    RecipeDraftIssue,
    RecipeDraftRecoveryStep,
    RecipeDraftState,
    RecipeIntent,
    RecipeIntegrityError,
)
from ..workspace_contracts import OdooSchemaCatalog, SourceDataset, SourceSelection
from .recipe_service import FaultInjector, RecipeService


_RECIPE_MAPPING_CONTRACT_VERSIONS = frozenset({11, 12})


class RecipeAuthoringSourceRepository(Protocol):
    def get_source_selection(
        self,
        project_id: str,
    ) -> SourceSelection | None: ...

    def get_mapping_source_selection(
        self,
        project_id: str,
    ) -> SourceSelection | None: ...


class RecipeAuthoringMappingRepository(Protocol):
    def get_mapping_revision(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingRevision | None: ...

    def get_mapping_submission(
        self,
        project_id: str,
        version: int | None = None,
    ) -> MappingSubmission | None: ...


class RecipeAuthoringSchemaRepository(Protocol):
    def get_odoo_schema_catalog(
        self,
        project_id: str,
    ) -> OdooSchemaCatalog | None: ...

    def get_schema_governance(
        self,
        project_id: str,
    ) -> SchemaGovernance | None: ...


class RecipeAuthoringQualityRepository(Protocol):
    def get_current_quality_ruleset(
        self,
        project_id: str,
    ) -> QualityRuleSet | None: ...


class RecipeAuthoringPreparationRepository(Protocol):
    def get_derived_entity_plan(
        self,
        project_id: str,
    ) -> DerivedEntityPlan | None: ...


class RecipeAuthoringReferenceRepository(Protocol):
    def get_reference_bundle(self, project_id: str) -> ReferenceBundle | None: ...


class RecipeAuthoringParameterRepository(Protocol):
    def get_parameter_definitions(
        self,
        project_id: str,
    ) -> RecipeParameterDefinitions: ...

    def save_parameter_definitions(
        self,
        project_id: str,
        definitions: RecipeParameterDefinitions,
        *,
        actor: Actor,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CompiledRecipeDefinition:
    """Portable semantic payload plus exact non-semantic authoring bindings."""

    recipe: Mapping[str, object]
    compatibility_hints: Mapping[str, object]
    source_selection_hash: str
    mapping_id: str
    mapping_version: int
    mapping_content_hash: str
    schema_hash: str
    quality_ruleset_hash: str

    @property
    def semantic_hash(self) -> str:
        return content_hash(self.recipe)


class _RecipeDraftBlocked(Exception):
    """Carry one structured compiler blocker without parsing error text."""

    def __init__(self, issue: RecipeDraftIssue) -> None:
        super().__init__(issue.message)
        self.issue = issue


class RecipeAuthoringService:
    """Create Recipe workspaces and publish exact current authoring evidence."""

    def __init__(
        self,
        recipes: RecipeService,
        projects: ProjectService,
        sources: RecipeAuthoringSourceRepository,
        mappings: RecipeAuthoringMappingRepository,
        schemas: RecipeAuthoringSchemaRepository,
        quality: RecipeAuthoringQualityRepository,
        preparation: RecipeAuthoringPreparationRepository,
        references: RecipeAuthoringReferenceRepository,
        authorization: AuthorizationPolicy,
        parameters: RecipeAuthoringParameterRepository | None = None,
    ) -> None:
        self.recipes = recipes
        self.projects = projects
        self.sources = sources
        self.mappings = mappings
        self.schemas = schemas
        self.quality = quality
        self.preparation = preparation
        self.references = references
        self.authorization = authorization
        self.parameters = parameters

    def create(
        self,
        *,
        name: str,
        source_system: str,
        source_mode: str | SourceMode,
        creation_request_id: str | None = None,
        actor: Actor,
    ) -> tuple[Recipe, WorkspaceState]:
        """Provision Recipe, DataVersion 1, and its contained workspace."""

        self.authorization.require(actor, Capability.RECIPE_CREATE)
        project = self.projects.create_project(
            actor=actor,
            name=name,
            source_system=source_system,
            source_mode=source_mode,
            creation_request_id=creation_request_id,
        )
        workspace = self.recipes.resolve_workspace(project.project_id, actor=actor)
        recipe = self.recipes.get(workspace.recipe_id, actor=actor)
        return recipe, project

    def synchronize_setup(
        self,
        project: WorkspaceState,
        *,
        actor: Actor,
    ) -> Recipe:
        """Synchronize editable authoring setup into the Recipe root."""

        return self.recipes.synchronize_unpublished_setup(project, actor=actor)

    def parameter_definitions(
        self,
        recipe_id: str,
        *,
        actor: Actor,
    ) -> tuple[RecipeParameterDefinition, ...]:
        """Return custom declarations for the current Authoring DataVersion."""

        project_id = self._authoring_workspace(recipe_id, actor=actor)
        if self.parameters is None:
            return ()
        return self.parameters.get_parameter_definitions(project_id).definitions

    def save_parameter_definition(
        self,
        recipe_id: str,
        *,
        name: str,
        label: str,
        value_type: str | RecipeParameterType,
        required: bool,
        actor: Actor,
    ) -> RecipeParameterDefinitions:
        """Add or replace one reusable application parameter declaration."""

        self.authorization.require(actor, Capability.RECIPE_PUBLISH)
        if self.parameters is None:
            raise RecipeConflictError(
                "Recipe parameter authoring is not available in this workspace"
            )
        project_id = self._authoring_workspace(recipe_id, actor=actor)
        definition = RecipeParameterDefinition(
            name=name,
            label=label,
            value_type=RecipeParameterType(value_type),
            required=required,
        )
        current = self.parameters.get_parameter_definitions(project_id)
        updated = RecipeParameterDefinitions(
            definitions=tuple(
                item for item in current.definitions if item.name != definition.name
            )
            + (definition,)
        )
        self.parameters.save_parameter_definitions(
            project_id,
            updated,
            actor=actor,
        )
        return updated

    def remove_parameter_definition(
        self,
        recipe_id: str,
        *,
        name: str,
        actor: Actor,
    ) -> RecipeParameterDefinitions:
        """Remove one custom declaration from the unpublished Recipe meaning."""

        self.authorization.require(actor, Capability.RECIPE_PUBLISH)
        if self.parameters is None:
            raise RecipeConflictError(
                "Recipe parameter authoring is not available in this workspace"
            )
        project_id = self._authoring_workspace(recipe_id, actor=actor)
        current = self.parameters.get_parameter_definitions(project_id)
        updated = RecipeParameterDefinitions(
            definitions=tuple(item for item in current.definitions if item.name != name)
        )
        if updated == current:
            raise RecipeConflictError("Recipe parameter no longer exists")
        self.parameters.save_parameter_definitions(
            project_id,
            updated,
            actor=actor,
        )
        return updated

    def _authoring_workspace(self, recipe_id: str, *, actor: Actor) -> str:
        recipe = self.recipes.get(recipe_id, actor=actor)
        current = next(
            (
                item
                for item in self.recipes.data_versions(recipe_id, actor=actor)
                if item.data_version_id == recipe.current_data_version_id
            ),
            None,
        )
        if current is None or current.purpose is not DataVersionPurpose.AUTHORING:
            raise RecipeConflictError(
                "Application parameters can only be changed in an Authoring data version"
            )
        if current.state.value != "ACTIVE":
            raise RecipeConflictError(
                "Published Recipe parameter definitions cannot be changed"
            )
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=current.workspace_project_id,
        )
        return current.workspace_project_id

    def draft(self, recipe_id: str, *, actor: Actor) -> RecipeDraft:
        """Return publication readiness without creating mutable Recipe state."""

        recipe = self.recipes.get(recipe_id, actor=actor)
        data_versions = self.recipes.data_versions(recipe_id, actor=actor)
        current = next(
            (
                item
                for item in data_versions
                if item.data_version_id == recipe.current_data_version_id
            ),
            None,
        )
        if current is None:
            issue = RecipeDraftIssue(
                "CURRENT_DATA_VERSION_MISSING",
                "This Recipe has no current data version.",
                "Create or resume a data version before publishing.",
                RecipeDraftRecoveryStep.RECIPE_OVERVIEW,
            )
            return self._blocked(recipe, "", "", (issue,))
        if current.purpose is not DataVersionPurpose.AUTHORING:
            issue = RecipeDraftIssue(
                "CURRENT_DATA_VERSION_NOT_AUTHORING",
                "The current data version applies published Recipe meaning.",
                "Open its application flow instead of publishing it as new meaning.",
                RecipeDraftRecoveryStep.RECIPE_APPLICATION,
            )
            return self._blocked(
                recipe,
                current.data_version_id,
                current.workspace_project_id,
                (issue,),
            )
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=current.workspace_project_id,
        )
        compiled, issues = self._compile(current.workspace_project_id)
        if compiled is None:
            return self._blocked(
                recipe,
                current.data_version_id,
                current.workspace_project_id,
                issues,
            )
        return RecipeDraft(
            recipe_id=recipe.recipe_id,
            data_version_id=current.data_version_id,
            workspace_project_id=current.workspace_project_id,
            state=RecipeDraftState.READY,
            expected_recipe_revision=recipe.optimistic_revision,
            next_recipe_revision=(recipe.current_recipe_revision or 0) + 1,
            semantic_hash=compiled.semantic_hash,
            source_selection_hash=compiled.source_selection_hash,
            mapping_content_hash=compiled.mapping_content_hash,
            schema_hash=compiled.schema_hash,
            quality_ruleset_hash=compiled.quality_ruleset_hash,
            issues=(),
        )

    def publish_current(
        self,
        recipe_id: str,
        *,
        expected_recipe_revision: int,
        actor: Actor,
        operation_id: str | None = None,
        fault: FaultInjector | None = None,
    ) -> RecipeIntent:
        """Compile and publish the current workspace as one immutable revision."""

        self.authorization.require(actor, Capability.RECIPE_PUBLISH)
        recipe = self.recipes.get(recipe_id, actor=actor)
        if recipe.optimistic_revision != expected_recipe_revision:
            raise RecipeConflictError("Recipe changed; reload before publishing")
        data_versions = self.recipes.data_versions(recipe_id, actor=actor)
        current = next(
            (
                item
                for item in data_versions
                if item.data_version_id == recipe.current_data_version_id
            ),
            None,
        )
        if current is None:
            raise RecipeConflictError("Create a data version before publishing")
        if current.purpose is not DataVersionPurpose.AUTHORING:
            raise RecipeConflictError(
                "Only an Authoring data version can publish reusable Recipe meaning"
            )
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=current.workspace_project_id,
        )
        compiled, issues = self._compile(current.workspace_project_id)
        if compiled is None:
            first = issues[0]
            raise RecipeConflictError(f"{first.message} {first.recovery_action}")
        next_version = (recipe.current_recipe_revision or 0) + 1
        compiled_at = datetime.now(timezone.utc).isoformat()
        envelope: dict[str, object] = {
            "recipe_contract_version": 2,
            "semantic_hash": compiled.semantic_hash,
            "recipe": compiled.recipe,
            "compatibility_hints": compiled.compatibility_hints,
            "provenance": {
                "compiled_at": compiled_at,
                "mapping_content_hash": compiled.mapping_content_hash,
                "mapping_id": compiled.mapping_id,
                "mapping_version": compiled.mapping_version,
                "origin_data_version_id": current.data_version_id,
                "publisher": {
                    "display_name": actor.identity.display_name,
                    "issuer": actor.identity.issuer,
                    "subject_id": actor.identity.subject_id,
                },
                "quality_ruleset_hash": compiled.quality_ruleset_hash,
                "recipe_id": recipe.recipe_id,
                "recipe_revision": next_version,
                "schema_hash": compiled.schema_hash,
                "source_selection_hash": compiled.source_selection_hash,
                "workspace_project_id": current.workspace_project_id,
            },
        }
        envelope["payload_hash"] = content_hash(envelope)
        return self.recipes.publish_revision(
            recipe.recipe_id,
            expected_recipe_revision=expected_recipe_revision,
            envelope_bytes=canonical_json(envelope).encode("utf-8"),
            actor=actor,
            operation_id=operation_id,
            fault=fault,
        )

    def _compile(
        self,
        project_id: str,
    ) -> tuple[CompiledRecipeDefinition | None, tuple[RecipeDraftIssue, ...]]:
        selection = self.sources.get_mapping_source_selection(project_id)
        if selection is None:
            return None, (
                self._issue(
                    "SOURCE_NOT_FROZEN",
                    "The reusable source shape is not ready.",
                    "Confirm and freeze the source tables.",
                    RecipeDraftRecoveryStep.SOURCE_DATA,
                ),
            )
        base_selection = self.sources.get_source_selection(project_id) or selection
        if any(item.origin.value == "ODOO" for item in base_selection.datasets):
            return None, (
                self._issue(
                    "ODOO_SOURCE_UNSUPPORTED",
                    "Recipe publication currently supports replacement files.",
                    "Use a governed CSV or XLSX source package.",
                    RecipeDraftRecoveryStep.NEW_PROJECT,
                ),
            )
        revision = self.mappings.get_mapping_revision(project_id)
        submission = (
            self.mappings.get_mapping_submission(project_id, revision.version)
            if revision is not None
            else None
        )
        if (
            revision is None
            or submission is None
            or submission.mapping_id != revision.mapping_id
            or submission.mapping_content_hash != revision.definition.content_hash
        ):
            return None, (
                self._issue(
                    "MAPPING_NOT_SUBMITTED",
                    "The current field matches are not published authoring evidence.",
                    "Check and submit the current matching revision.",
                    RecipeDraftRecoveryStep.MATCH_DATA,
                ),
            )
        if (
            revision.definition.contract_version
            not in _RECIPE_MAPPING_CONTRACT_VERSIONS
        ):
            return None, (
                self._issue(
                    "MAPPING_CONTRACT_NOT_PORTABLE",
                    "The submitted field matches use an unsupported contract.",
                    "Review and submit a supported matching revision.",
                    RecipeDraftRecoveryStep.MATCH_DATA,
                ),
        )
        if any(
            item.mode is MappingTargetMode.ODOO_PINNED_UPDATE
            for item in revision.definition.datasets
        ):
            return None, (
                self._issue(
                    "PINNED_UPDATE_NOT_PORTABLE",
                    "Pinned Odoo updates cannot become reusable Recipe meaning.",
                    "Use UPSERT, CREATE, or REFERENCE matching behavior.",
                    RecipeDraftRecoveryStep.MATCH_DATA,
                ),
            )
        schema = self.schemas.get_odoo_schema_catalog(project_id)
        governance = self.schemas.get_schema_governance(project_id)
        if (
            schema is None
            or governance is None
            or governance.catalog_hash != schema.content_hash
            or revision.definition.schema_hash != governance.content_hash
        ):
            return None, (
                self._issue(
                    "TARGET_GOVERNANCE_STALE",
                    "The target requirements no longer match current Odoo evidence.",
                    "Refresh Odoo data and confirm the matching keys again.",
                    RecipeDraftRecoveryStep.ODOO_DATA,
                ),
            )
        if revision.definition.source_selection_hash != selection.content_hash:
            return None, (
                self._issue(
                    "SOURCE_MAPPING_STALE",
                    "The field matches no longer bind the current source shape.",
                    "Review and submit matching again.",
                    RecipeDraftRecoveryStep.MATCH_DATA,
                ),
            )
        ruleset = self.quality.get_current_quality_ruleset(project_id)
        if (
            ruleset is None
            or ruleset.mapping_hash != revision.definition.content_hash
            or ruleset.schema_hash != revision.definition.schema_hash
        ):
            return None, (
                self._issue(
                    "QUALITY_RULES_NOT_READY",
                    "Reusable data checks are not ready for this matching revision.",
                    "Prepare the data once or save the current data-check rules.",
                    RecipeDraftRecoveryStep.PREPARE_DATA,
                ),
            )
        try:
            parameter_definitions = (
                self.parameters.get_parameter_definitions(project_id)
                if self.parameters is not None
                else RecipeParameterDefinitions()
            )
            compiled = self._definition(
                selection=selection,
                base_selection=base_selection,
                revision=revision,
                schema=schema,
                governance=governance,
                ruleset=ruleset,
                preparation=self.preparation.get_derived_entity_plan(project_id),
                references=self.references.get_reference_bundle(project_id),
                parameter_definitions=parameter_definitions,
            )
            self._validate_portability(compiled)
        except _RecipeDraftBlocked as error:
            return None, (error.issue,)
        except (
            RecipeConflictError,
            RecipeIntegrityError,
            RecipeParameterDefinitionError,
        ) as error:
            return None, (
                self._issue(
                    "NONPORTABLE_AUTHORING",
                    "The submitted authoring evidence cannot become reusable Recipe meaning.",
                    "Review the current field matches and correct the affected rule.",
                    RecipeDraftRecoveryStep.MATCH_DATA,
                    support_reference=str(error),
                ),
            )
        return compiled, ()

    @staticmethod
    def _validate_portability(compiled: CompiledRecipeDefinition) -> None:
        """Apply the exact protected publication parser before showing ready."""

        envelope: dict[str, object] = {
            "recipe_contract_version": 2,
            "semantic_hash": compiled.semantic_hash,
            "recipe": compiled.recipe,
            "compatibility_hints": compiled.compatibility_hints,
            "provenance": {},
        }
        envelope["payload_hash"] = content_hash(envelope)
        RecipeService._validated_envelope(canonical_json(envelope).encode("utf-8"))

    def _definition(
        self,
        *,
        selection: SourceSelection,
        base_selection: SourceSelection,
        revision: MappingRevision,
        schema: OdooSchemaCatalog,
        governance: SchemaGovernance,
        ruleset: QualityRuleSet,
        preparation: DerivedEntityPlan | None,
        references: ReferenceBundle | None,
        parameter_definitions: RecipeParameterDefinitions,
    ) -> CompiledRecipeDefinition:
        combined_by_id = {
            item.dataset_id: item
            for item in (*base_selection.datasets, *selection.datasets)
        }
        datasets, dataset_ids, columns = self._source_identity(
            tuple(combined_by_id.values())
        )
        base_ids = {item.dataset_id for item in base_selection.datasets}
        reference_payload, reference_ids = self._references(references)
        mapping_payload = self._mapping(
            revision.definition.datasets,
            dataset_ids,
            columns,
            reference_ids,
        )
        used_columns = self._used_columns(
            revision.definition.datasets,
            dataset_ids,
            columns,
        )
        if preparation is not None:
            for dataset in base_selection.datasets:
                for column in dataset.columns:
                    used_columns.setdefault(
                        (dataset.dataset_id, column.stable_key),
                        set(),
                    ).add("preparation")
        source_shape = {
            "datasets": [
                {
                    "columns": [
                        {
                            "candidate_type_hint": column.candidate_type,
                            "logical_column_id": columns[
                                (dataset.dataset_id, column.stable_key)
                            ],
                            "required_by": sorted(
                                used_columns.get(
                                    (dataset.dataset_id, column.stable_key),
                                    {"mapping"},
                                )
                            ),
                            "source_name": column.source_name,
                        }
                        for column in dataset.columns
                        if (dataset.dataset_id, column.stable_key) in used_columns
                    ],
                    "logical_dataset_id": dataset_ids[dataset.dataset_id],
                    "logical_name": dataset.name,
                    "required": True,
                }
                for dataset in datasets
                if dataset.dataset_id in base_ids
                and any(key[0] == dataset.dataset_id for key in used_columns)
            ]
        }
        quality_payload = self._quality(
            ruleset,
            datasets,
            dataset_ids,
            revision.definition.datasets,
            columns,
            reference_ids,
        )
        target_contract, target_governance = self._target(
            revision.definition.datasets,
            schema,
            governance,
            dataset_ids,
        )
        recipe: dict[str, object] = {
            "contract_versions": {
                "control_definitions": 1,
                "mapping_recipe": 2,
                "odoo_target_contract": 2,
                "quality_recipe": 1,
                "recipe_definition": 2,
                "recipe_parameter_definitions": 1,
                "reference_dependencies": 1,
                "source_preparation_recipe": 1,
                "source_shape_recipe": 1,
                "target_governance_recipe": 1,
            },
            "source_shape": source_shape,
            "parameter_definitions": {
                "parameters": self._parameter_definitions(
                    base_selection,
                    parameter_definitions,
                )
            },
            "source_preparation": self._preparation(
                preparation,
                dataset_ids,
                columns,
                datasets,
            ),
            "mapping": mapping_payload,
            "odoo_target_contract": target_contract,
            "target_governance": target_governance,
            "quality": quality_payload,
            "reference_dependencies": {"references": reference_payload},
            "control_definitions": self._controls(
                revision.definition.datasets,
                dataset_ids,
            ),
        }
        return CompiledRecipeDefinition(
            recipe=recipe,
            compatibility_hints={
                "datasets": [
                    {
                        "logical_dataset_id": dataset_ids[item.dataset_id],
                        "prior_display_name": item.name,
                    }
                    for item in datasets
                    if item.dataset_id in base_ids
                ]
            },
            source_selection_hash=selection.content_hash,
            mapping_id=revision.mapping_id,
            mapping_version=revision.version,
            mapping_content_hash=revision.definition.content_hash,
            schema_hash=revision.definition.schema_hash,
            quality_ruleset_hash=ruleset.content_hash,
        )

    @staticmethod
    def _source_identity(
        source_datasets: tuple[SourceDataset, ...],
    ) -> tuple[
        tuple[SourceDataset, ...],
        dict[str, str],
        dict[tuple[str, str], str],
    ]:
        datasets = tuple(sorted(source_datasets, key=lambda item: item.name.casefold()))
        dataset_ids: dict[str, str] = {}
        logical_ids: set[str] = set()
        columns: dict[tuple[str, str], str] = {}
        for dataset in datasets:
            logical_dataset = f"dataset:{_token(dataset.name)}"
            if logical_dataset in logical_ids:
                raise RecipeConflictError(
                    "Two source tables normalize to the same reusable name."
                )
            logical_ids.add(logical_dataset)
            dataset_ids[dataset.dataset_id] = logical_dataset
            column_ids: set[str] = set()
            for column in dataset.columns:
                logical_column = (
                    f"column:{logical_dataset.removeprefix('dataset:')}."
                    f"{_token(column.source_name)}"
                )
                if logical_column in column_ids:
                    raise RecipeConflictError(
                        f"Table {dataset.name} has ambiguous reusable column names."
                    )
                column_ids.add(logical_column)
                columns[(dataset.dataset_id, column.stable_key)] = logical_column
        return datasets, dataset_ids, columns

    def _mapping(
        self,
        mappings: tuple[DatasetMapping, ...],
        dataset_ids: Mapping[str, str],
        columns: Mapping[tuple[str, str], str],
        reference_ids: Mapping[str, str],
    ) -> dict[str, object]:
        result: list[dict[str, object]] = []
        for dataset in sorted(mappings, key=lambda item: dataset_ids[item.dataset_id]):
            logical_dataset = self._dataset(dataset.dataset_id, dataset_ids)
            fields = []
            for field in sorted(dataset.fields, key=lambda item: item.target_field):
                source_ids = (
                    [self._column(dataset.dataset_id, field.source_column_key, columns)]
                    if field.source_column_key is not None
                    else []
                )
                provider: dict[str, object] = {
                    "kind": field.value_source.value.upper(),
                    "source_column_ids": source_ids,
                }
                if field.literal_value is not None:
                    provider["literal_value"] = field.literal_value
                if field.selection_rules is not None:
                    rule_source_keys = tuple(
                        dict.fromkeys(
                            condition.source_column_key
                            for rule in field.selection_rules.rules
                            for condition in rule.conditions
                        )
                    )
                    provider["source_column_ids"] = [
                        self._column(dataset.dataset_id, key, columns)
                        for key in rule_source_keys
                    ]
                    provider["rules"] = [
                        {
                            "rule_id": rule.rule_id,
                            "join": rule.join.value,
                            "target_value": rule.target_value,
                            "conditions": [
                                {
                                    "condition_id": condition.condition_id,
                                    "source_column_id": self._column(
                                        dataset.dataset_id,
                                        condition.source_column_key,
                                        columns,
                                    ),
                                    "operator": condition.operator.value,
                                    "comparison_value": condition.comparison_value,
                                    "value_type": condition.value_type,
                                }
                                for condition in rule.conditions
                            ],
                        }
                        for rule in field.selection_rules.rules
                    ]
                    provider["otherwise_value"] = (
                        field.selection_rules.otherwise_value
                    )
                if field.reference_lookup is not None:
                    lookup = field.reference_lookup
                    if lookup.reference_id not in reference_ids:
                        raise RecipeConflictError(
                            f"Reference data for {field.target_field} is missing."
                        )
                    provider = {
                        "kind": "REFERENCE_LOOKUP",
                        "logical_reference_id": reference_ids[lookup.reference_id],
                        "source_column_ids": [
                            self._column(dataset.dataset_id, key, columns)
                            for key in lookup.key_source_column_keys
                        ],
                        "value_field": lookup.value_field,
                        "on_blank": lookup.on_blank,
                        "on_unknown": lookup.on_unknown,
                    }
                fields.append(
                    {
                        "categorical_policy": (
                            field.categorical_policy.value
                            if field.categorical_policy is not None
                            else None
                        ),
                        "compare": field.compare,
                        "logical_field_id": (
                            f"field:{logical_dataset.removeprefix('dataset:')}."
                            f"{_token(field.target_field)}"
                        ),
                        "null_policy": field.null_policy,
                        "provider": provider,
                        "required": field.required,
                        "required_on_create": field.required_on_create,
                        "target_field": field.target_field,
                        "transform": portable(asdict(field.transform)),
                        "validation": portable(asdict(field.validation)),
                        "validate_only": field.validate_only,
                        "value_matches": [
                            portable(asdict(item)) for item in field.value_mappings
                        ],
                        "value_type": field.value_type,
                    }
                )
            relationships = []
            for relation in sorted(
                dataset.relationships,
                key=lambda item: item.target_field,
            ):
                resolver = relation.resolver
                relationships.append(
                    {
                        "categorical_policy": (
                            relation.categorical_policy.value
                            if relation.categorical_policy is not None
                            else None
                        ),
                        "compare": relation.compare,
                        "kind": relation.kind,
                        "logical_relationship_id": (
                            "relationship:"
                            f"{logical_dataset.removeprefix('dataset:')}."
                            f"{_token(relation.target_field)}"
                        ),
                        "on_ambiguous": relation.on_ambiguous,
                        "on_missing": relation.on_missing,
                        "operation": relation.operation,
                        "null_policy": relation.null_policy,
                        "required": relation.required,
                        "required_on_create": relation.required_on_create,
                        "separator": relation.separator,
                        "source_column_ids": [
                            self._column(dataset.dataset_id, key, columns)
                            for key in relation.source_column_keys
                        ],
                        "target_field": relation.target_field,
                        "target_model": resolver.model,
                        "target_dataset_id": (
                            self._dataset(resolver.dataset_id, dataset_ids)
                            if resolver.dataset_id is not None
                            else None
                        ),
                        "target_key_fields": [
                            item.target_field for item in resolver.key_mappings
                        ],
                        "target_key_mappings": [
                            {
                                "source_column_id": self._column(
                                    dataset.dataset_id,
                                    item.source_column_key,
                                    columns,
                                ),
                                "target_field": item.target_field,
                            }
                            for item in resolver.key_mappings
                        ],
                        "target_scope_fields": [
                            item.target_field for item in resolver.scope_mappings
                        ],
                        "target_scope_mappings": [
                            {
                                "source_column_id": self._column(
                                    dataset.dataset_id,
                                    item.source_column_key,
                                    columns,
                                ),
                                "target_field": item.target_field,
                            }
                            for item in resolver.scope_mappings
                        ],
                        "validate_only": relation.validate_only,
                        "value_matches": [
                            portable(asdict(item)) for item in resolver.value_mappings
                        ],
                    }
                )
            result.append(
                {
                    "approved_write_fields": list(dataset.approved_write_fields),
                    "comparison_policy": {
                        "missing_source_row": "NO_DELETE_INFERENCE",
                    },
                    "fields": fields,
                    "identity": [
                        self._identity(item, dataset.dataset_id, columns, dataset_ids)
                        for item in dataset.target_identity
                    ],
                    "logical_dataset_id": logical_dataset,
                    "mode": dataset.mode.value.upper(),
                    "on_existing": dataset.on_existing,
                    "relationships": relationships,
                    "source_identity_column_ids": [
                        self._column(dataset.dataset_id, key, columns)
                        for key in dataset.source_identity_column_keys
                    ],
                    "scope": [
                        self._identity(item, dataset.dataset_id, columns, dataset_ids)
                        for item in dataset.target_scope
                    ],
                    "target_field_dispositions": [
                        portable(asdict(item))
                        for item in dataset.target_field_dispositions
                    ],
                    "target_model": dataset.target_model,
                }
            )
        return {"datasets": result}

    def _identity(self, item, dataset_id, columns, dataset_ids):
        resolver = item.resolver
        return {
            "source_column_ids": [
                self._column(dataset_id, key, columns)
                for key in item.source_column_keys
            ],
            "target_fields": list(item.target_fields),
            "value_type": item.value_type,
            "resolver": (
                {
                    "origin": resolver.origin.value,
                    "target_dataset_id": (
                        self._dataset(resolver.dataset_id, dataset_ids)
                        if resolver.dataset_id is not None
                        else None
                    ),
                    "target_model": resolver.model,
                    "target_key_fields": [
                        value.target_field for value in resolver.key_mappings
                    ],
                    "target_key_mappings": [
                        {
                            "source_column_id": self._column(
                                dataset_id,
                                value.source_column_key,
                                columns,
                            ),
                            "target_field": value.target_field,
                        }
                        for value in resolver.key_mappings
                    ],
                    "target_scope_fields": [
                        value.target_field for value in resolver.scope_mappings
                    ],
                    "target_scope_mappings": [
                        {
                            "source_column_id": self._column(
                                dataset_id,
                                value.source_column_key,
                                columns,
                            ),
                            "target_field": value.target_field,
                        }
                        for value in resolver.scope_mappings
                    ],
                    "value_matches": [
                        portable(asdict(value)) for value in resolver.value_mappings
                    ],
                }
                if resolver is not None
                else None
            ),
        }

    def _used_columns(self, mappings, dataset_ids, columns):
        del dataset_ids
        used: dict[tuple[str, str], set[str]] = {}
        for dataset in mappings:
            keys = set(dataset.source_identity_column_keys)
            for identity in (*dataset.target_identity, *dataset.target_scope):
                keys.update(identity.source_column_keys)
            for field in dataset.fields:
                if field.source_column_key is not None:
                    keys.add(field.source_column_key)
                if field.reference_lookup is not None:
                    keys.update(field.reference_lookup.key_source_column_keys)
            for relation in dataset.relationships:
                keys.update(relation.source_column_keys)
                keys.update(
                    item.source_column_key
                    for item in (
                        *relation.resolver.key_mappings,
                        *relation.resolver.scope_mappings,
                    )
                )
            for key in keys:
                self._column(dataset.dataset_id, key, columns)
                used.setdefault((dataset.dataset_id, key), set()).add("mapping")
        return used

    def _quality(
        self,
        ruleset,
        source_datasets,
        dataset_ids,
        mappings,
        columns,
        reference_ids,
    ):
        del columns
        by_name = {item.name: item for item in source_datasets}
        mapping_by_id = {item.dataset_id: item for item in mappings}
        rules = []
        for rule in ruleset.rules:
            if rule.source in {
                QualityRuleSource.MAPPING_DERIVED,
                QualityRuleSource.SCHEMA_DERIVED,
            }:
                continue
            source = by_name.get(rule.dataset)
            if source is None:
                raise RecipeConflictError(
                    f"Data check {rule.name} names an unavailable table."
                )
            mapping = mapping_by_id.get(source.dataset_id)
            if mapping is None:
                raise RecipeConflictError(
                    f"Data check {rule.name} has no reusable field matches."
                )
            known_fields = {item.target_field for item in mapping.fields}
            if any(item not in known_fields for item in rule.input_fields):
                raise RecipeConflictError(
                    f"Data check {rule.name} uses an unsupported field."
                )
            parameters = dict(rule.parameters)
            if "reference_id" in parameters:
                reference_id = parameters["reference_id"]
                if reference_id not in reference_ids:
                    raise RecipeConflictError(
                        f"Reference data for check {rule.name} is missing."
                    )
                parameters["reference_id"] = reference_ids[reference_id]
            logical_dataset = dataset_ids[source.dataset_id]
            rules.append(
                {
                    "dataset_id": logical_dataset,
                    "evidence_display": rule.evidence_display,
                    "explanation": rule.explanation,
                    "field_ids": [
                        f"field:{logical_dataset.removeprefix('dataset:')}."
                        f"{_token(item)}"
                        for item in rule.input_fields
                    ],
                    "kind": rule.family.value,
                    "logical_rule_id": (
                        f"quality:{logical_dataset.removeprefix('dataset:')}."
                        f"{_token(rule.name)}"
                    ),
                    "name": rule.name,
                    "origin": rule.source.value,
                    "owner_role": rule.owner_role.value,
                    "parameters": dict(sorted(parameters.items())),
                    "review_by_days": rule.review_by_days,
                    "severity": rule.outcome.value,
                }
            )
        return {
            "regenerate_mapping_and_schema_rules": True,
            "rules": sorted(rules, key=lambda item: item["logical_rule_id"]),
        }

    def _target(self, mappings, schema, governance, dataset_ids):
        del dataset_ids
        schema_models = {item.name: item for item in schema.models}
        major_match = re.match(r"([0-9]+)", schema.odoo_version)
        if major_match is None:
            raise _RecipeDraftBlocked(
                self._issue(
                    "ODOO_VERSION_EVIDENCE_INVALID",
                    "The current Odoo version evidence cannot be verified.",
                    "Refresh Odoo data before publishing the Recipe.",
                    RecipeDraftRecoveryStep.ODOO_DATA,
                    support_reference=schema.odoo_version,
                )
            )
        odoo_major_version = int(major_match.group(1))
        field_roles: dict[tuple[str, str], set[str]] = {}
        write_fields: dict[str, set[str]] = {}
        selection_codes: dict[tuple[str, str], set[str]] = {}
        reference_decisions: dict[str, list[GovernedReferenceDecision]] = {}
        reference_paths: dict[str, list[dict[str, object]]] = {}
        governed_signatures = {
            (item.model, item.key_fields, item.scope_fields)
            for item in governance.business_keys
            if item.status is BusinessKeyStatus.CONFIRMED
        }

        def register_resolver(
            parent_model: str,
            relationship_field: str,
            relationship_type: str,
            resolver,
        ) -> None:
            if resolver is None or resolver.model is None:
                return
            for key in (*resolver.key_mappings, *resolver.scope_mappings):
                field_roles.setdefault(
                    (resolver.model, key.target_field), set()
                ).add("relationship_key")
            parent = schema_models.get(parent_model)
            relationship = next(
                (
                    field
                    for field in (parent.fields if parent is not None else ())
                    if field.name == relationship_field
                ),
                None,
            )
            key_fields = tuple(
                item.target_field for item in resolver.key_mappings
            )
            scope_fields = tuple(
                item.target_field for item in resolver.scope_mappings
            )
            related = schema_models.get(resolver.model)
            decision = authorize_governed_reference(
                GovernedReferenceRequest(
                    parent_model=parent_model,
                    relationship_field=relationship_field,
                    relationship_type=(
                        relationship.type
                        if relationship is not None
                        else relationship_type
                    ),
                    relationship_model=(
                        relationship.relation if relationship is not None else None
                    ),
                    related_model=resolver.model,
                    key_fields=key_fields,
                    scope_fields=scope_fields,
                    requested_fields=(*key_fields, *scope_fields),
                    purpose=ReferenceReadPurpose.RECIPE_PUBLICATION,
                    odoo_major_version=odoo_major_version,
                    governed_key=(
                        resolver.model,
                        key_fields,
                        scope_fields,
                    )
                    in governed_signatures,
                ),
                captured_fields=(
                    captured_reference_field_contracts(related.fields)
                    if related is not None
                    else None
                ),
            )
            if not decision.accepted:
                captured_metadata_changed = (
                    decision.denial
                    is ReferencePolicyDenial.CAPTURED_METADATA_MISMATCH
                )
                raise _RecipeDraftBlocked(
                    self._issue(
                        (
                            "ODOO_STANDARD_REFERENCE_CHANGED"
                            if captured_metadata_changed
                            else "ODOO_REFERENCE_POLICY_MISMATCH"
                        ),
                        (
                            "A reviewed Odoo reference no longer matches current evidence."
                            if captured_metadata_changed
                            else "A saved relationship no longer matches its governed Odoo reference."
                        ),
                        (
                            "Review the affected record type in Odoo data before publishing."
                            if captured_metadata_changed
                            else "Review the affected field match before publishing the Recipe."
                        ),
                        (
                            RecipeDraftRecoveryStep.ODOO_DATA
                            if captured_metadata_changed
                            else RecipeDraftRecoveryStep.MATCH_DATA
                        ),
                        support_reference=(
                            f"{parent_model}.{relationship_field} -> "
                            f"{resolver.model}"
                        ),
                    )
                )
            reference_decisions.setdefault(resolver.model, []).append(decision)
            reference_paths.setdefault(resolver.model, []).append(
                {
                    "key_fields": list(key_fields),
                    "parent_model": parent_model,
                    "relationship_field": relationship_field,
                    "relationship_type": relationship_type,
                    "scope_fields": list(scope_fields),
                }
            )

        for mapping in mappings:
            write_fields.setdefault(mapping.target_model, set()).update(
                mapping.approved_write_fields
            )
            for identity in (*mapping.target_identity, *mapping.target_scope):
                for field in identity.target_fields:
                    field_roles.setdefault((mapping.target_model, field), set()).add(
                        "business_key"
                    )
                if identity.resolver is not None:
                    register_resolver(
                        mapping.target_model,
                        identity.target_fields[0],
                        "many2one",
                        identity.resolver,
                    )
            for field in mapping.fields:
                field_roles.setdefault(
                    (mapping.target_model, field.target_field), set()
                ).add("mapped_field")
                model = schema_models.get(mapping.target_model)
                metadata = next(
                    (
                        item
                        for item in (model.fields if model is not None else ())
                        if item.name == field.target_field
                    ),
                    None,
                )
                if metadata is not None and metadata.type == "selection":
                    selection_codes.setdefault(
                        (mapping.target_model, field.target_field), set()
                    ).update(item.target_value for item in field.value_mappings)
                    if field.literal_value is not None:
                        selection_codes[(mapping.target_model, field.target_field)].add(
                            field.literal_value
                        )
                    if field.selection_rules is not None:
                        selection_codes[
                            (mapping.target_model, field.target_field)
                        ].update(
                            rule.target_value
                            for rule in field.selection_rules.rules
                        )
                        if field.selection_rules.otherwise_value is not None:
                            selection_codes[
                                (mapping.target_model, field.target_field)
                            ].add(field.selection_rules.otherwise_value)
            for relation in mapping.relationships:
                field_roles.setdefault(
                    (mapping.target_model, relation.target_field), set()
                ).add("relationship")
                register_resolver(
                    mapping.target_model,
                    relation.target_field,
                    relation.kind,
                    relation.resolver,
                )
        business_keys = []
        for key in governance.business_keys:
            if key.status is not BusinessKeyStatus.CONFIRMED:
                continue
            business_keys.append(
                {
                    "model": key.model,
                    "ordered_fields": list(key.key_fields),
                    "scope_fields": list(key.scope_fields),
                }
            )
            for field in (*key.key_fields, *key.scope_fields):
                field_roles.setdefault((key.model, field), set()).add("business_key")
        model_payload = []
        dependencies = []
        for model_name in sorted({item[0] for item in field_roles}):
            model = schema_models.get(model_name)
            required_fields = {
                field_name: field_roles[(model_name, field_name)]
                for candidate_model, field_name in field_roles
                if candidate_model == model_name
            }
            model_reference_decisions = reference_decisions.get(model_name, ())
            reviewed_reference_only = bool(
                model_reference_decisions
                and all(
                    decision.evidence_kind
                    is ReferenceEvidenceKind.REVIEWED_STANDARD
                    for decision in model_reference_decisions
                )
                and not write_fields.get(model_name)
                and all(
                    roles == {"relationship_key"}
                    for field_name, roles in required_fields.items()
                )
            )
            standard = (
                model_reference_decisions[0].contract
                if reviewed_reference_only
                else None
            )
            if model is None and not reviewed_reference_only:
                raise _RecipeDraftBlocked(
                    self._issue(
                        "ODOO_MODEL_EVIDENCE_REQUIRED",
                        "A saved rule requires an Odoo record type that is not in current evidence.",
                        "Review that record type in Odoo data, then confirm the field matches again.",
                        RecipeDraftRecoveryStep.ODOO_DATA,
                        support_reference=model_name,
                    )
                )
            by_field = {item.name: item for item in model.fields} if model else {}
            fields = []
            for field_name, roles in sorted(required_fields.items()):
                field = by_field.get(field_name)
                standard_field = (
                    standard.field_contract(field_name)
                    if reviewed_reference_only and standard is not None
                    else None
                )
                if standard_field is not None:
                    if model is not None and (
                        field is None
                        or field.type != standard_field.field_type
                        or field.required != standard_field.required
                        or field.readonly != standard_field.readonly
                        or field.relation != standard_field.relation_model
                    ):
                        raise _RecipeDraftBlocked(
                            self._issue(
                                "ODOO_STANDARD_REFERENCE_CHANGED",
                                "A reviewed Odoo reference no longer matches current evidence.",
                                "Review the affected record type in Odoo data before publishing.",
                                RecipeDraftRecoveryStep.ODOO_DATA,
                                support_reference=f"{model_name}.{field_name}",
                            )
                        )
                    payload = {
                        "field_type": standard_field.field_type,
                        "name": standard_field.name,
                        "readonly": standard_field.readonly,
                        "required": standard_field.required,
                        "write_use": False,
                    }
                    if standard_field.relation_model is not None:
                        payload["relation_model"] = standard_field.relation_model
                elif field is None:
                    raise _RecipeDraftBlocked(
                        self._issue(
                            "ODOO_FIELD_EVIDENCE_REQUIRED",
                            "A saved rule requires an Odoo field that is not in current evidence.",
                            "Refresh Odoo data, then confirm the field matches again.",
                            RecipeDraftRecoveryStep.ODOO_DATA,
                            support_reference=f"{model_name}.{field_name}",
                        )
                    )
                else:
                    payload = {
                        "field_type": field.type,
                        "name": field.name,
                        "readonly": field.readonly,
                        "required": field.required,
                        "write_use": field.name
                        in write_fields.get(model_name, set()),
                    }
                    if field.relation is not None:
                        payload["relation_model"] = field.relation
                codes = sorted(selection_codes.get((model_name, field_name), set()))
                if codes:
                    payload["required_selection_codes"] = codes
                fields.append(payload)
                dependencies.append(
                    {
                        "field": field_name,
                        "field_type": payload["field_type"],
                        "logical_dependency_id": f"target:{model_name}.{field_name}",
                        "model": model_name,
                        "roles": sorted(roles),
                    }
                )
            model_payload.append(
                {
                    "fields": fields,
                    "model": model_name,
                    "reference_evidence_kind": (
                        ReferenceEvidenceKind.REVIEWED_STANDARD.value
                        if reviewed_reference_only
                        else ReferenceEvidenceKind.CAPTURED_GOVERNED.value
                    ),
                    "reference_paths": sorted(
                        reference_paths.get(model_name, ()),
                        key=lambda item: (
                            item["parent_model"],
                            item["relationship_field"],
                        ),
                    ),
                }
            )
        return (
            {
                "approved_write_fields": {
                    model: sorted(fields)
                    for model, fields in sorted(write_fields.items())
                },
                "business_keys": sorted(
                    business_keys,
                    key=lambda item: (item["model"], item["ordered_fields"]),
                ),
                "models": model_payload,
                "odoo_major_version": odoo_major_version,
                "reference_policy_hash": REFERENCE_POLICY_HASH,
                "required_applications": [],
            },
            {"dependencies": dependencies},
        )

    def _references(self, bundle):
        if bundle is None:
            return [], {}
        payload = []
        identifiers = {}
        used: set[str] = set()
        for item in bundle.datasets:
            logical = f"reference:{_token(item.name)}"
            if logical in used:
                logical = f"{logical}.{item.content_hash[7:15]}"
            used.add(logical)
            identifiers[item.reference_id] = logical
            payload.append(
                {
                    "classification": item.classification,
                    "content_hash": item.content_hash,
                    "contract_version": item.contract_version,
                    "key_fields": list(item.key_fields),
                    "logical_reference_id": logical,
                    "name": item.name,
                    "value_fields": sorted(item.value_kinds),
                }
            )
        return (
            sorted(payload, key=lambda item: item["logical_reference_id"]),
            identifiers,
        )

    def _preparation(self, plan, dataset_ids, columns, datasets):
        if plan is None:
            return {"rules": []}
        by_name = {item.name: item.dataset_id for item in datasets}
        rules = []
        for raw in plan.to_dict(include_hash=False)["rules"]:
            value = dict(raw)
            value.pop("rule_id", None)
            output_name = str(value.get("output_dataset_name", "prepared"))
            value["logical_rule_id"] = (
                f"preparation:{_token(str(value.get('kind', 'rule')))}."
                f"{_token(output_name)}"
            )
            self._replace_preparation_ids(
                value,
                dataset_ids,
                columns,
                output_dataset_id=by_name.get(output_name),
            )
            rules.append(portable(value))
        return {"rules": sorted(rules, key=lambda item: item["logical_rule_id"])}

    def _replace_preparation_ids(
        self,
        value,
        dataset_ids,
        columns,
        *,
        context_dataset_id=None,
        output_dataset_id=None,
    ):
        if isinstance(value, list):
            for item in value:
                self._replace_preparation_ids(
                    item,
                    dataset_ids,
                    columns,
                    context_dataset_id=context_dataset_id,
                    output_dataset_id=output_dataset_id,
                )
            return
        if not isinstance(value, dict):
            return
        local_dataset = context_dataset_id
        raw_left_dataset = value.get("left_dataset_id")
        raw_right_dataset = value.get("right_dataset_id")
        if isinstance(raw_left_dataset, str) and isinstance(
            raw_right_dataset,
            str,
        ):
            for key_pair in value.get("keys", []):
                if not isinstance(key_pair, dict):
                    continue
                key_pair["left_column_key"] = self._column(
                    raw_left_dataset,
                    key_pair.get("left_column_key"),
                    columns,
                )
                key_pair["right_column_key"] = self._column(
                    raw_right_dataset,
                    key_pair.get("right_column_key"),
                    columns,
                )
        for key in (
            "source_dataset_id",
            "dataset_id",
            "left_dataset_id",
            "right_dataset_id",
        ):
            raw = value.get(key)
            if isinstance(raw, str) and raw in dataset_ids:
                if key in {"source_dataset_id", "dataset_id"}:
                    local_dataset = raw
                value[key] = dataset_ids[raw]
        for key, raw in list(value.items()):
            if not isinstance(raw, str):
                continue
            if (
                key
                in {
                    "source_column_key",
                    "parent_key_column_key",
                    "child_key_column_key",
                    "scope_column_key",
                }
                and local_dataset is not None
            ):
                value[key] = self._column(local_dataset, raw, columns)
            elif key == "output_column_key" and output_dataset_id is not None:
                value[key] = self._column(output_dataset_id, raw, columns)
            elif key == "column_key" and output_dataset_id is not None:
                value[key] = self._column(output_dataset_id, raw, columns)
            elif key == "left_column_key" and isinstance(raw_left_dataset, str):
                value[key] = self._column(raw_left_dataset, raw, columns)
            elif key == "right_column_key" and isinstance(raw_right_dataset, str):
                value[key] = self._column(raw_right_dataset, raw, columns)
        for item in value.values():
            self._replace_preparation_ids(
                item,
                dataset_ids,
                columns,
                context_dataset_id=local_dataset,
                output_dataset_id=output_dataset_id,
            )

    @staticmethod
    def _parameter_definitions(selection, custom):
        definitions = []
        if any(item.origin.value == "FILE" for item in selection.datasets):
            definitions.append(
                {
                    "allowed_use_sites": ["controls", "provenance"],
                    "constraints": {"not_after_application_date": True},
                    "label": "Export as-of date",
                    "logical_parameter_id": "parameter:export_as_of_date",
                    "required": True,
                    "type": "date",
                }
            )
        definitions.extend(item.to_recipe_dict() for item in custom.definitions)
        return definitions

    @staticmethod
    def _controls(mappings, dataset_ids):
        controls = []
        for dataset in mappings:
            logical_dataset = dataset_ids[dataset.dataset_id]
            expectations = {
                item.control_id: item.expected_total
                for item in dataset.control_expectations
            }
            for item in dataset.control_definitions:
                payload = {
                    "calculation": item.calculation,
                    "dataset_id": logical_dataset,
                    "invariant_expectation": item.invariant_expectation,
                    "logical_control_id": (
                        f"control:{logical_dataset.removeprefix('dataset:')}."
                        f"{_token(item.target_field)}.{_token(item.name)}"
                    ),
                    "name": item.name,
                    "target_field": item.target_field,
                    "tolerance": item.tolerance,
                    "unit": item.unit,
                }
                if item.invariant_expectation:
                    if item.control_id not in expectations:
                        raise RecipeConflictError(
                            f"Invariant control {item.name} has no expected value."
                        )
                    payload["invariant_expected_total"] = expectations[item.control_id]
                controls.append(payload)
        return {
            "controls": sorted(
                controls,
                key=lambda item: item["logical_control_id"],
            )
        }

    @staticmethod
    def _dataset(value, dataset_ids):
        if value is None or value not in dataset_ids:
            raise RecipeConflictError("A reusable mapping names an unknown table.")
        return dataset_ids[value]

    @staticmethod
    def _column(dataset_id, value, columns):
        if value is None or (dataset_id, value) not in columns:
            raise RecipeConflictError("A reusable mapping names an unknown column.")
        return columns[(dataset_id, value)]

    @staticmethod
    def _issue(
        code,
        message,
        recovery_action,
        recovery_step,
        *,
        support_reference="",
    ):
        return RecipeDraftIssue(
            code,
            message,
            recovery_action,
            recovery_step,
            support_reference,
        )

    @staticmethod
    def _blocked(recipe, data_version_id, workspace_project_id, issues):
        return RecipeDraft(
            recipe_id=recipe.recipe_id,
            data_version_id=data_version_id,
            workspace_project_id=workspace_project_id,
            state=RecipeDraftState.BLOCKED,
            expected_recipe_revision=recipe.optimistic_revision,
            next_recipe_revision=(recipe.current_recipe_revision or 0) + 1,
            semantic_hash=None,
            source_selection_hash=None,
            mapping_content_hash=None,
            schema_hash=None,
            quality_ruleset_hash=None,
            issues=issues,
        )


def _token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    token = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if not token:
        raise RecipeConflictError("Reusable names must contain a letter or number.")
    return token[:120]
