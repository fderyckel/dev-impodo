"""Adapt the Recipe compiler to current application workspaces."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

from ..access import Actor
from ..domain.mapping.contracts import MappingDefinition
from ..domain.coverage import ReferenceBundle
from ..domain.recipe_applications import (
    RecipeApplicationError,
    RecipeApplicationIssue,
    RecipeApplicationIssueLevel,
    RecipeControlValues,
)
from ..domain.serialization import content_hash
from ..migration_run_planning import (
    MigrationRunPlanIssue,
    MigrationRunPlanIssueLevel,
    OdooModelRequirement,
    ReferenceRequirement,
    RecipeApplicationStatus,
)
from ..workspace_contracts import OdooSchemaCatalog, SourceDatasetSet
from ..workspace_errors import WorkspaceError
from .recipe_application_compilation import RecipeApplicationCompiler


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
        schema_workspace,
        references,
        preparation,
        mappings,
        categorical,
        application_state,
    ) -> None:
        self.sources = sources
        self.schemas = schemas
        self.schema_workspace = schema_workspace
        self.references = references
        self.preparation = preparation
        self.mappings = mappings
        self.categorical = categorical
        self.application_state = application_state

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
            normalized_parameters = self._parameter_values(
                tuple(
                    dict(definition.get("parameter_definitions", {})).get(
                        "parameters",
                        (),
                    )
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
            normalized_controls = self._control_values(
                tuple(
                    dict(definition.get("control_definitions", {})).get(
                        "controls",
                        (),
                    )
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
                    confirmed_at=datetime.now(timezone.utc),
                )
                datasets = self._mapping_datasets(
                    application_definition,
                    effective_bindings,
                    selection,
                    controls,
                    self.references.get_reference_bundle(workspace_id),
                    assessment.target_default_fields,
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
                    revision, _validation = self.mappings.check_definition(
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
                    checked_draft = (
                        self.mappings.mappings.get_mapping_working_draft(
                            workspace_id
                        )
                    )
                    if not default_review_required:
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
            None,
            None,
            issues,
            application_id=application_id,
            assessment=assessment,
        )

    @staticmethod
    def requirements(definition: Mapping[str, object]) -> tuple[OdooModelRequirement, ...]:
        """Return one bounded per-Recipe target requirement projection."""

        contract = dict(definition.get("odoo_target_contract", {}))
        requirements = []
        for model in contract.get("models", ()):
            fields = tuple(sorted(str(item["name"]) for item in model.get("fields", ())))
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

        contract = dict(definition.get("odoo_target_contract", {}))
        claims = {
            (str(model), str(field))
            for model, fields in dict(
                contract.get("approved_write_fields", {})
            ).items()
            for field in fields
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
        preparation = dict(result.get("source_preparation", {}))
        rules = list(preparation.get("rules", ()))
        remaining = []
        for raw_rule in rules:
            rule = dict(raw_rule)
            if str(rule.get("operation", "")) != "NORMALIZE_TEXT":
                remaining.append(raw_rule)
                continue
            inputs = tuple(str(item) for item in rule.get("input_column_ids", ()))
            output = str(rule.get("output_logical_column_id", ""))
            if len(inputs) != 1 or not output:
                raise RecipeApplicationError(
                    "Reusable text preparation needs one input and one output"
                )
            parameters = dict(rule.get("parameters", {}))
            allowed = {"trim", "collapse_whitespace", "empty_as_null", "case_mode"}
            if not set(parameters).issubset(allowed):
                raise RecipeApplicationError(
                    "Reusable text preparation contains an unsupported option"
                )
            uses = 0
            for dataset in dict(result.get("mapping", {})).get("datasets", ()):
                for field in dataset.get("fields", ()):
                    provider = dict(field.get("provider", {}))
                    source_ids = tuple(
                        str(item) for item in provider.get("source_column_ids", ())
                    )
                    if output not in source_ids:
                        continue
                    if provider.get("kind") != "SOURCE" or source_ids != (output,):
                        raise RecipeApplicationError(
                            "Reusable text preparation is used outside one source field"
                        )
                    provider["source_column_ids"] = [inputs[0]]
                    field["provider"] = provider
                    transform = dict(field.get("transform", {}))
                    for key, value in parameters.items():
                        if key in transform and transform[key] != value:
                            raise RecipeApplicationError(
                                "Reusable text preparation conflicts with a mapping rule"
                            )
                        transform[key] = value
                    field["transform"] = transform
                    uses += 1
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

        contract = dict(definition.get("reference_dependencies", {}))
        return tuple(
            sorted(
                ReferenceRequirement(
                    name=str(item["name"]),
                    content_hash=str(item["content_hash"]),
                )
                for item in contract.get("references", ())
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
        )
