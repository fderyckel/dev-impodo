"""Compile reusable Recipe meaning into one fresh application workspace."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    RelatedDatasetRule,
)
from ..domain.mapping.contracts import (
    BusinessControlDefinition,
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    MappingControlExpectation,
    MappingTargetMode,
    ReferenceKeyMapping,
    ReferenceLookupMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRule,
    SelectionRuleJoin,
    SelectionRuleSet,
    TargetFieldDisposition,
    TargetFieldHandling,
    ValueMapping,
)
from ..domain.mapping.create_field_policy import (
    CreateFieldCoverage,
    evaluate_create_field,
)
from ..domain.recipe_applications import (
    RecipeApplicationError,
    RecipeApplicationIssue,
    RecipeApplicationIssueLevel,
)
from ..domain.recipe_parameters import (
    RecipeParameterValueError,
    normalize_recipe_parameter_values,
)
from ..domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
)
from ..domain.serialization import content_hash
from ..domain.structural import (
    AggregateSpec,
    ExactJoinRule,
    GroupAggregateRule,
    GroupKey,
    JoinKey,
    JoinKind,
    StructuralOutputColumn,
    StructuralProjection,
    UnionAllRule,
    UnionBranch,
)
from impodo.domain.preparation.quality import (
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityRule,
    QualityRuleFamily,
    QualityRuleSource,
)
from impodo.domain.recipe.source_binding import (
    logical_dataset_storage_name,
    normalize_recipe_source_name,
)
from impodo.domain.workspace.reference_keys import (
    REFERENCE_POLICY_HASH,
    GovernedReferenceRequest,
    ReferenceEvidenceKind,
    ReferenceReadPurpose,
    authorize_governed_reference,
    captured_reference_field_contracts,
)
from impodo.domain.recipe.value_rules import (
    ScalarTransformPolicy,
    ScalarValidationPolicy,
    TextTransformStep,
)


class RecipeApplicationCompiler:
    """Provide deterministic, side-effect-bounded Recipe compiler helpers."""

    @staticmethod
    def _parameter_values(definitions, supplied):
        try:
            return normalize_recipe_parameter_values(definitions, supplied)
        except RecipeParameterValueError as error:
            raise RecipeApplicationError(str(error)) from error

    @staticmethod
    def _control_values(definitions, supplied):
        expected = {str(item["logical_control_id"]): dict(item) for item in definitions}
        unknown = sorted(set(supplied) - set(expected))
        if unknown:
            raise RecipeApplicationError(f"Control {unknown[0]} is not declared by this Recipe")
        values: dict[str, str] = {}
        for logical_id, definition in expected.items():
            if bool(definition.get("invariant_expectation")):
                values[logical_id] = str(definition["invariant_expected_total"])
                continue
            raw = str(supplied.get(logical_id, "")).strip()
            if not raw:
                continue
            try:
                values[logical_id] = format(Decimal(raw), "f")
            except InvalidOperation as error:
                raise RecipeApplicationError(f"Control {definition.get('name', logical_id)} must be a number") from error
        return values

    def _source_assessment(self, definition, selection, overrides):
        issues = []
        bindings: dict[str, str] = {}
        candidates: dict[str, tuple[tuple[str, str], ...]] = {}
        used_datasets = set()
        used_columns: dict[str, set[str]] = {}
        for required in dict(definition["source_shape"]).get("datasets", ()):
            logical_dataset = str(required["logical_dataset_id"])
            logical_name = str(required["logical_name"])
            accepted_name = logical_dataset_storage_name(logical_dataset)
            matches = [
                dataset
                for dataset in selection.datasets
                if dataset.name in {logical_name, accepted_name}
            ]
            if len(matches) != 1:
                issues.append(self._block("RECIPE_SOURCE_DATASET_MISSING", f"Required source table {required['logical_name']} is missing or ambiguous.", "Confirm one table with the exact reusable name.", logical_dataset))
                continue
            dataset = matches[0]
            bindings[logical_dataset] = dataset.dataset_id
            used_datasets.add(dataset.dataset_id)
            by_column: dict[str, list] = {}
            for column in dataset.columns:
                by_column.setdefault(
                    normalize_recipe_source_name(column.source_name),
                    [],
                ).append(column)
            used_columns[dataset.dataset_id] = set()
            for required_column in required.get("columns", ()):
                logical_column = str(required_column["logical_column_id"])
                matches = by_column.get(
                    normalize_recipe_source_name(
                        str(required_column["source_name"])
                    ),
                    [],
                )
                selected = matches[0] if len(matches) == 1 else None
                override = overrides.get(logical_column)
                if override:
                    selected = next((column for column in dataset.columns if column.stable_key == override), None)
                    if selected is None:
                        issues.append(self._block("RECIPE_SOURCE_OVERRIDE_STALE", f"The confirmed replacement for {required_column['source_name']} is no longer present.", "Choose the current exact replacement column.", logical_column))
                if selected is None:
                    candidates[logical_column] = tuple((column.stable_key, column.source_name) for column in dataset.columns)
                    issues.append(self._block("RECIPE_SOURCE_COLUMN_MISSING", f"Required source column {required_column['source_name']} is missing.", "Confirm the exact replacement column.", logical_column))
                    continue
                bindings[logical_column] = selected.stable_key
                used_columns[dataset.dataset_id].add(selected.stable_key)
        for dataset in selection.datasets:
            if dataset.dataset_id not in used_datasets:
                issues.append(self._info("RECIPE_SOURCE_DATASET_UNUSED", f"New source table {dataset.name} is not used by this Recipe.", "No action is required unless the table should become reusable Recipe meaning."))
                continue
            for column in dataset.columns:
                if column.stable_key not in used_columns.get(dataset.dataset_id, set()):
                    issues.append(self._info("RECIPE_SOURCE_COLUMN_UNUSED", f"New source column {column.source_name} is not used by this Recipe.", "No action is required unless the column should become reusable Recipe meaning."))
        return bindings, candidates, issues

    def _target_assessment(self, definition, schema):
        issues = []
        contract = dict(definition["odoo_target_contract"])
        target_contract_version = int(
            dict(definition["contract_versions"])["odoo_target_contract"]
        )
        if target_contract_version != 2:
            return "", [
                self._block(
                    "RECIPE_TARGET_CONTRACT_RETIRED",
                    "The Recipe target contract is not current.",
                    "Publish a new Recipe revision with the current editor.",
                )
            ]
        try:
            actual_major = int(str(schema.odoo_version).split(".", 1)[0])
        except ValueError:
            actual_major = -1
        if actual_major != int(contract["odoo_major_version"]):
            issues.append(self._block("RECIPE_TARGET_VERSION_INCOMPATIBLE", "The connected Odoo major version does not match this Recipe.", "Choose a compatible Odoo server or publish and retest a new Recipe revision."))
        actual_models = {item.name: item for item in schema.models}
        dependency_projection = []
        if contract.get("reference_policy_hash") != REFERENCE_POLICY_HASH:
            issues.append(
                self._block(
                    "RECIPE_REFERENCE_POLICY_CHANGED",
                    "The reviewed Odoo reference policy changed after this Recipe was published.",
                    "Review and publish a new Recipe revision under the current policy.",
                    str(contract.get("reference_policy_hash", "missing")),
                )
            )
        provider_fields = {
            (str(dataset["target_model"]), str(field["target_field"]))
            for dataset in dict(definition["mapping"]).get("datasets", ())
            for field in dataset.get("fields", ())
            if str(dict(field.get("provider", {})).get("kind")) != "ODOO_DEFAULT"
        }
        provider_fields.update(
            (str(dataset["target_model"]), str(field["target_field"]))
            for dataset in dict(definition["mapping"]).get("datasets", ())
            for field in dataset.get("relationships", ())
        )
        dispositions = {
            (str(dataset["target_model"]), str(item["target_field"])): (
                TargetFieldHandling(str(item["handling"]))
            )
            for dataset in dict(definition["mapping"]).get("datasets", ())
            for item in dataset.get("target_field_dispositions", ())
        }
        available_defaults: list[tuple[str, str]] = []
        for required_model in contract.get("models", ()):
            model_name = str(required_model["model"])
            model = actual_models.get(model_name)
            evidence_kind = str(
                required_model.get(
                    "reference_evidence_kind",
                    ReferenceEvidenceKind.CAPTURED_GOVERNED.value,
                )
            )
            reviewed_standard = (
                evidence_kind == ReferenceEvidenceKind.REVIEWED_STANDARD.value
            )
            reference_decisions = []
            if reviewed_standard:
                for path in required_model.get("reference_paths", ()):
                    parent_model = str(path["parent_model"])
                    relationship_field = str(path["relationship_field"])
                    parent = actual_models.get(parent_model)
                    relationship = next(
                        (
                            field
                            for field in (
                                parent.fields if parent is not None else ()
                            )
                            if field.name == relationship_field
                        ),
                        None,
                    )
                    key_fields = tuple(
                        str(item) for item in path["key_fields"]
                    )
                    scope_fields = tuple(
                        str(item) for item in path.get("scope_fields", ())
                    )
                    reference_decisions.append(
                        authorize_governed_reference(
                            GovernedReferenceRequest(
                                parent_model=parent_model,
                                relationship_field=relationship_field,
                                relationship_type=(
                                    relationship.type
                                    if relationship is not None
                                    else str(
                                        path.get("relationship_type", "")
                                    )
                                ),
                                relationship_model=(
                                    relationship.relation
                                    if relationship is not None
                                    else None
                                ),
                                related_model=model_name,
                                key_fields=key_fields,
                                scope_fields=scope_fields,
                                requested_fields=(
                                    *key_fields,
                                    *scope_fields,
                                ),
                                purpose=(
                                    ReferenceReadPurpose.RECIPE_APPLICATION
                                ),
                                odoo_major_version=actual_major,
                            ),
                            captured_fields=(
                                captured_reference_field_contracts(model.fields)
                                if model is not None
                                else None
                            ),
                        )
                    )
                if not reference_decisions or any(
                    not decision.accepted
                    for decision in reference_decisions
                ):
                    issues.append(
                        self._block(
                            "RECIPE_REFERENCE_POLICY_MISMATCH",
                            (
                                f"Reviewed Odoo reference {model_name} no "
                                "longer matches this Recipe."
                            ),
                            (
                                "Review the relationship and publish a new "
                                "Recipe revision."
                            ),
                            model_name,
                        )
                    )
                    continue
            if model is None and not reviewed_standard:
                issues.append(
                    self._block(
                        "RECIPE_TARGET_MODEL_MISSING",
                        f"Required Odoo model {model_name} is missing.",
                        (
                            "Install or expose the required Odoo application, "
                            "then refresh Odoo data."
                        ),
                        model_name,
                    )
                )
                continue
            fields = (
                {item.name: item for item in model.fields}
                if model is not None
                else {}
            )
            for required_field in required_model.get("fields", ()):
                field_name = str(required_field["name"])
                field = fields.get(field_name)
                logical = f"{model_name}.{field_name}"
                if field is None and reviewed_standard:
                    standard = reference_decisions[0].contract
                    expected = (
                        standard.field_contract(field_name)
                        if standard is not None
                        else None
                    )
                    if expected is None or any(
                        (
                            str(required_field["field_type"])
                            != expected.field_type,
                            bool(required_field["required"])
                            != expected.required,
                            bool(required_field["readonly"])
                            != expected.readonly,
                            required_field.get("relation_model")
                            != expected.relation_model,
                            bool(required_field.get("write_use")),
                        )
                    ):
                        issues.append(
                            self._block(
                                "RECIPE_REFERENCE_CONTRACT_INVALID",
                                (
                                    f"Reviewed Odoo reference field {logical} "
                                    "is not valid under the current policy."
                                ),
                                "Review and publish a new Recipe revision.",
                                logical,
                            )
                        )
                        continue
                    dependency_projection.append(
                        {
                            "model": model_name,
                            "field": field_name,
                            "type": expected.field_type,
                            "relation": expected.relation_model,
                            "required": expected.required,
                            "readonly": expected.readonly,
                            "selection": [],
                        }
                    )
                    continue
                if field is None:
                    issues.append(self._block("RECIPE_TARGET_FIELD_MISSING", f"Required Odoo field {logical} is missing.", "Add or expose the field, then refresh Odoo data.", logical))
                    continue
                dependency_projection.append({"model": model_name, "field": field_name, "type": field.type, "relation": field.relation, "required": field.required, "readonly": field.readonly, "selection": list(field.selection)})
                if field.type != str(required_field["field_type"]):
                    issues.append(self._block("RECIPE_TARGET_FIELD_TYPE_CHANGED", f"Odoo field {logical} changed type.", "Restore the compatible field type or publish and retest a new Recipe revision.", logical))
                if required_field.get("relation_model") != field.relation:
                    issues.append(self._block("RECIPE_TARGET_RELATION_CHANGED", f"Odoo relationship {logical} points to a different model.", "Restore the compatible relationship or publish and retest a new Recipe revision.", logical))
                if bool(required_field.get("write_use")) and field.readonly:
                    issues.append(self._block("RECIPE_TARGET_FIELD_READONLY", f"Odoo field {logical} is now read-only.", "Restore write access or publish and retest a Recipe that does not write this field.", logical))
                actual_codes = {str(item[0]) for item in field.selection}
                missing_codes = sorted(set(required_field.get("required_selection_codes", ())) - actual_codes)
                if missing_codes:
                    issues.append(self._block("RECIPE_TARGET_SELECTION_MISSING", f"Odoo field {logical} no longer offers required value {missing_codes[0]}.", "Restore the choice or update and retest the Recipe value mapping.", logical))
            for field in (model.fields if model is not None else ()):
                if (
                    model_name in contract.get("approved_write_fields", {})
                    and field.required
                    and not field.readonly
                    and (model_name, field.name) not in provider_fields
                ):
                    handling = dispositions.get((model_name, field.name))
                    assessment = evaluate_create_field(
                        field,
                        provided=False,
                        handling=handling,
                    )
                    if assessment.coverage in {
                        CreateFieldCoverage.DEFAULT_CONFIRMED,
                        CreateFieldCoverage.ODOO_MANAGED_CONFIRMED,
                    }:
                        continue
                    if assessment.coverage is CreateFieldCoverage.DEFAULT_AVAILABLE:
                        available_defaults.append((model_name, field.name))
                        issues.append(
                            self._review(
                                "RECIPE_TARGET_ODOO_DEFAULT_AVAILABLE",
                                (
                                    f"Odoo can provide {model_name}.{field.name} "
                                    "when it creates the record."
                                ),
                                (
                                    "Review the current Odoo value and confirm "
                                    "it for this run."
                                ),
                                f"{model_name}.{field.name}",
                            )
                        )
                        continue
                    issues.append(
                        self._block(
                            "RECIPE_TARGET_NEW_REQUIRED_FIELD",
                            (
                                f"Odoo now requires {model_name}.{field.name}, "
                                "but this Recipe provides no value."
                            ),
                            (
                                "Provide a value in a new Recipe revision; Impodo "
                                "could not verify an Odoo create default."
                            ),
                            f"{model_name}.{field.name}",
                        )
                    )
        assessment_hash = content_hash({"contract": contract, "current_dependencies": dependency_projection})
        return assessment_hash, issues, tuple(sorted(available_defaults))


    def _reference_issues(self, definition, project_id):
        required = tuple(dict(definition["reference_dependencies"]).get("references", ()))
        if not required:
            return []
        bundle = self.references.get_reference_bundle(project_id)
        current = {item.name: item for item in bundle.datasets} if bundle else {}
        issues = []
        for item in required:
            found = current.get(str(item["name"]))
            if found is None or found.content_hash != str(item["content_hash"]):
                issues.append(self._block("RECIPE_REFERENCE_DEPENDENCY_MISSING", f"Reference data {item['name']} is missing or changed.", "Load and confirm the exact current reference package.", str(item["logical_reference_id"])))
        return issues

    def _quality_issues(self, definition):
        """Keep target-snapshot-dependent advanced checks out of silent reuse."""

        issues = []
        for rule in dict(definition["quality"]).get("rules", ()):
            if str(rule.get("origin")) != QualityRuleSource.MANAGER_AUTHORED.value:
                issues.append(
                    self._block(
                        "RECIPE_QUALITY_SCOPE_REVIEW_REQUIRED",
                        f"Data check {rule['name']} depends on a prior approved scope.",
                        "Re-establish the current reference scope and publish a new Recipe revision before reuse.",
                        str(rule["logical_rule_id"]),
                    )
                )
        return issues

    @staticmethod
    def _quality_rules(definition, bindings, selection):
        """Compile manager-authored Recipe checks into fresh physical rules."""

        datasets = {item.dataset_id: item for item in selection.datasets}
        mapping_datasets = {
            str(item["logical_dataset_id"]): item
            for item in dict(definition["mapping"]).get("datasets", ())
        }
        field_names = {
            str(field["logical_field_id"]): str(field["target_field"])
            for dataset in mapping_datasets.values()
            for field in dataset.get("fields", ())
        }
        rules = []
        for item in dict(definition["quality"]).get("rules", ()):
            if str(item.get("origin")) != QualityRuleSource.MANAGER_AUTHORED.value:
                continue
            logical_dataset = str(item["dataset_id"])
            physical_dataset = str(bindings[logical_dataset])
            source_dataset = datasets[physical_dataset]
            rule_id = content_hash(
                {
                    "logical_rule_id": str(item["logical_rule_id"]),
                    "physical_dataset": physical_dataset,
                    "data_version_id": selection.data_version_id,
                }
            )
            rules.append(
                QualityRule(
                    rule_id=rule_id,
                    dataset=source_dataset.name,
                    family=QualityRuleFamily(str(item["kind"])),
                    name=str(item["name"]),
                    explanation=str(item["explanation"]),
                    input_fields=tuple(
                        field_names[str(value)]
                        for value in item.get("field_ids", ())
                    ),
                    parameters={
                        str(key): str(value)
                        for key, value in dict(item.get("parameters", {})).items()
                    },
                    outcome=QualityOutcomePolicy(str(item["severity"])),
                    owner_role=QualityOwnerRole(str(item["owner_role"])),
                    source=QualityRuleSource.MANAGER_AUTHORED,
                    review_by_days=(
                        int(item["review_by_days"])
                        if item.get("review_by_days") is not None
                        else None
                    ),
                    evidence_display=str(item.get("evidence_display", "masked")),
                )
            )
        return tuple(sorted(rules, key=lambda item: item.rule_id))

    def _materialize_governance(self, project_id, definition, *, actor):
        keys = tuple(
            BusinessKeyDefinition(
                key_id=f"recipe:{item['model']}:{':'.join(item['ordered_fields'])}:{':'.join(item.get('scope_fields', ())) or 'global'}",
                model=str(item["model"]),
                key_fields=tuple(str(value) for value in item["ordered_fields"]),
                scope_fields=tuple(str(value) for value in item.get("scope_fields", ())),
                description="Reused from the published Recipe target contract",
                status=BusinessKeyStatus.CONFIRMED,
            )
            for item in dict(definition["odoo_target_contract"]).get("business_keys", ())
        )
        current = self.schemas.get_schema_governance(project_id)
        current_shapes = (
            {(item.model, item.key_fields, item.scope_fields) for item in current.business_keys}
            if current else set()
        )
        desired_shapes = {(item.model, item.key_fields, item.scope_fields) for item in keys}
        if current is not None and current.catalog_hash == self.schemas.get_odoo_schema_catalog(project_id).content_hash and current_shapes == desired_shapes:
            return current
        return self.schema_workspace.govern(project_id, business_keys=keys, actor=actor)

    def _materialize_preparation(
        self,
        project_id,
        definition,
        bindings,
        *,
        actor,
    ):
        semantic_rules = tuple(
            dict(definition["source_preparation"]).get("rules", ())
        )
        if not semantic_rules:
            return None
        source = self.sources.get_source_selection(project_id)
        if source is None:
            raise RecipeApplicationError(
                "Freeze source data before materializing Recipe preparation"
            )
        current = self.preparation.get_derived_entity_plan(project_id)
        if current is not None:
            if current.source_selection_hash != source.content_hash:
                raise RecipeApplicationError(
                    "Current source preparation is stale for this data version"
                )
            return current
        rules = tuple(
            self._preparation_rule(item, bindings)
            for item in semantic_rules
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id=project_id,
            source_selection_hash=source.content_hash,
            rules=rules,
            updated_at=datetime.now(UTC),
            updated_by=actor.identity.display_name,
        )
        self.preparation.save_derived_entity_plan(
            project_id,
            plan,
            expected_parent_version=None,
            actor=actor,
        )
        return plan

    def _preparation_rule(self, raw, bindings):
        value = dict(raw)
        value.pop("logical_rule_id", None)
        kind = str(value.pop("kind", ""))
        rule_id = str(uuid4())
        if kind == "lookup":
            return DerivedEntityRule(
                rule_id=rule_id,
                output_dataset_name=str(value["output_dataset_name"]),
                source_dataset_id=bindings[str(value["source_dataset_id"])],
                source_column_key=bindings[str(value["source_column_key"])],
                target_model=str(value["target_model"]),
                target_name_field=str(value["target_name_field"]),
                external_id_namespace=str(value["external_id_namespace"]),
                parent_separator=(
                    str(value["parent_separator"])
                    if value.get("parent_separator") is not None
                    else None
                ),
                blank_policy=str(value.get("blank_policy", "block")),
            )
        if kind == "parent_child":
            return RelatedDatasetRule(
                rule_id=rule_id,
                source_dataset_id=bindings[str(value["source_dataset_id"])],
                parent_dataset_name=str(value["parent_dataset_name"]),
                child_dataset_name=str(value["child_dataset_name"]),
                parent_key_column_key=bindings[
                    str(value["parent_key_column_key"])
                ],
                child_key_column_key=bindings[str(value["child_key_column_key"])],
                scope_column_key=(
                    bindings[str(value["scope_column_key"])]
                    if value.get("scope_column_key") is not None
                    else None
                ),
                blank_policy=str(value.get("blank_policy", "block")),
            )
        if kind in {"exact_join", "LEFT", "INNER"} or {
            "left_dataset_id",
            "right_dataset_id",
            "keys",
        }.issubset(value):
            output_keys = {
                str(item["column_key"]): self._application_column_key(
                    str(item["column_key"])
                )
                for item in value.get("output_columns", ())
            }
            return ExactJoinRule(
                rule_id=rule_id,
                output_dataset_name=str(value["output_dataset_name"]),
                left_dataset_id=bindings[str(value["left_dataset_id"])],
                right_dataset_id=bindings[str(value["right_dataset_id"])],
                keys=tuple(
                    JoinKey(
                        left_column_key=bindings[str(item["left_column_key"])],
                        right_column_key=bindings[str(item["right_column_key"])],
                        value_type=str(item.get("value_type", "string")),
                    )
                    for item in value.get("keys", ())
                ),
                output_columns=tuple(
                    StructuralOutputColumn(
                        column_key=output_keys[str(item["column_key"])],
                        source_name=str(item["source_name"]),
                        candidate_type=str(item["candidate_type"]),
                    )
                    for item in value.get("output_columns", ())
                ),
                projections=tuple(
                    StructuralProjection(
                        output_column_key=output_keys[
                            str(item["output_column_key"])
                        ],
                        source_dataset_id=bindings[
                            str(item["source_dataset_id"])
                        ],
                        source_column_key=bindings[
                            str(item["source_column_key"])
                        ],
                    )
                    for item in value.get("projections", ())
                ),
                kind=JoinKind(kind if kind in {"LEFT", "INNER"} else value.get("join_kind", "LEFT")),
                require_all_right_rows=bool(
                    value.get("require_all_right_rows", True)
                ),
            )
        if kind == "union_all" or "branches" in value:
            output_keys = {
                str(item["column_key"]): self._application_column_key(
                    str(item["column_key"])
                )
                for item in value.get("output_columns", ())
            }
            return UnionAllRule(
                rule_id=rule_id,
                output_dataset_name=str(value["output_dataset_name"]),
                output_columns=tuple(
                    StructuralOutputColumn(
                        column_key=output_keys[str(item["column_key"])],
                        source_name=str(item["source_name"]),
                        candidate_type=str(item["candidate_type"]),

                    )
                    for item in value.get("output_columns", ())
                ),
                branches=tuple(
                    UnionBranch(
                        source_dataset_id=bindings[
                            str(branch["source_dataset_id"])
                        ],
                        projections=tuple(
                            StructuralProjection(
                                output_column_key=output_keys[
                                    str(item["output_column_key"])
                                ],
                                source_dataset_id=bindings[
                                    str(item["source_dataset_id"])
                                ],
                                source_column_key=bindings[
                                    str(item["source_column_key"])
                                ],
                            )
                            for item in branch.get("projections", ())
                        ),
                    )
                    for branch in value.get("branches", ())
                ),
            )
        if kind == "group_aggregate" or "aggregates" in value:
            output_keys = {
                str(item["column_key"]): self._application_column_key(
                    str(item["column_key"])
                )
                for item in value.get("output_columns", ())
            }
            return GroupAggregateRule(
                rule_id=rule_id,
                output_dataset_name=str(value["output_dataset_name"]),
                source_dataset_id=bindings[str(value["source_dataset_id"])],
                output_columns=tuple(
                    StructuralOutputColumn(
                        column_key=output_keys[str(item["column_key"])],
                        source_name=str(item["source_name"]),
                        candidate_type=str(item["candidate_type"]),
                    )
                    for item in value.get("output_columns", ())
                ),
                group_keys=tuple(
                    GroupKey(
                        output_column_key=output_keys[
                            str(item["output_column_key"])
                        ],
                        source_column_key=bindings[
                            str(item["source_column_key"])
                        ],
                        value_type=str(item.get("value_type", "string")),
                    )
                    for item in value.get("group_keys", ())
                ),
                aggregates=tuple(
                    AggregateSpec(
                        output_column_key=output_keys[
                            str(item["output_column_key"])
                        ],
                        operation=str(item["operation"]),
                        source_column_key=(
                            bindings[str(item["source_column_key"])]
                            if item.get("source_column_key") is not None
                            else None
                        ),
                        unit=str(item.get("unit", "")),
                    )
                    for item in value.get("aggregates", ())
                ),
            )
        raise RecipeApplicationError(
            "Recipe source preparation contains an unsupported rule"
        )

    @staticmethod
    def _application_column_key(logical_id: str) -> str:
        return f"recipe:{content_hash(logical_id)[7:39]}"

    @staticmethod
    def _effective_bindings(selection, base_bindings):
        bindings = dict(base_bindings)
        used_dataset_ids = {
            value
            for logical, value in bindings.items()
            if logical.startswith("dataset:")
        }
        for dataset in selection.datasets:
            if dataset.dataset_id in used_dataset_ids:
                continue
            logical_dataset = f"dataset:{_recipe_token(dataset.name)}"
            if logical_dataset in bindings and bindings[logical_dataset] != dataset.dataset_id:
                raise RecipeApplicationError(
                    "Prepared datasets have ambiguous reusable names"
                )
            bindings[logical_dataset] = dataset.dataset_id
            for column in dataset.columns:
                logical_column = (
                    f"column:{_recipe_token(dataset.name)}."
                    f"{_recipe_token(column.source_name)}"
                )
                if logical_column in bindings and bindings[logical_column] != column.stable_key:
                    raise RecipeApplicationError(
                        "Prepared columns have ambiguous reusable names"
                    )
                bindings[logical_column] = column.stable_key
        return bindings

    def _mapping_datasets(
        self,
        definition,
        bindings,
        selection,
        controls,
        references,
        target_default_fields=(),
    ):
        reference_by_logical = {}
        if references:
            by_name = {item.name: item for item in references.datasets}
            for item in dict(definition["reference_dependencies"]).get("references", ()):
                reference_by_logical[str(item["logical_reference_id"])] = by_name[str(item["name"])]
        controls_by_dataset: dict[str, list] = {}
        for item in dict(definition["control_definitions"]).get("controls", ()):
            controls_by_dataset.setdefault(str(item["dataset_id"]), []).append(item)
        result = []
        for dataset in dict(definition["mapping"]).get("datasets", ()):
            logical_dataset = str(dataset["logical_dataset_id"])
            physical_dataset = bindings[logical_dataset]
            mode = MappingTargetMode(str(dataset["mode"]).casefold())
            fields = tuple(self._field(item, bindings, reference_by_logical) for item in dataset.get("fields", ()))
            relationships = tuple(self._relationship(item, bindings) for item in dataset.get("relationships", ()))
            definitions = tuple(
                BusinessControlDefinition(
                    control_id=str(item["logical_control_id"]),
                    name=str(item["name"]),
                    target_field=str(item["target_field"]),
                    unit=str(item.get("unit", "")),
                    tolerance=str(item.get("tolerance", "0")),
                    calculation=str(item.get("calculation", "SUM")),
                    invariant_expectation=bool(item.get("invariant_expectation")),
                )
                for item in controls_by_dataset.get(logical_dataset, ())
            )
            expectations = tuple(
                MappingControlExpectation(control_id=item.control_id, expected_total=str(controls.values[item.control_id]))
                for item in definitions
                if controls is not None and item.control_id in controls.values
            )
            dispositions = {
                str(item["target_field"]): TargetFieldDisposition(
                    target_field=str(item["target_field"]),
                    handling=TargetFieldHandling(str(item["handling"])),
                )
                for item in dataset.get("target_field_dispositions", ())
            }
            scalar_defaults = {
                item.target_field
                for item in fields
                if item.value_source is ScalarValueSource.ODOO_DEFAULT
            }
            for model_name, field_name in target_default_fields:
                if (
                    model_name == str(dataset["target_model"])
                    and field_name not in scalar_defaults
                ):
                    dispositions[field_name] = TargetFieldDisposition(
                        target_field=field_name,
                        handling=TargetFieldHandling.ODOO_DEFAULT,
                    )
            result.append(DatasetMapping(
                dataset_id=physical_dataset,
                target_model=str(dataset["target_model"]),
                mode=mode,
                on_existing=(str(dataset["on_existing"]) if dataset.get("on_existing") is not None else None),
                source_identity_column_keys=tuple(bindings[str(value)] for value in dataset.get("source_identity_column_ids", ())),
                target_identity=tuple(self._identity(item, bindings) for item in dataset.get("identity", ())),
                target_scope=tuple(self._identity(item, bindings) for item in dataset.get("scope", ())),
                fields=fields,
                relationships=relationships,
                target_field_dispositions=tuple(
                    dispositions[name] for name in sorted(dispositions)
                ),
                approved_write_fields=(
                    tuple(
                        str(item)
                        for item in dataset.get("approved_write_fields", ())
                    )
                    if mode is MappingTargetMode.ODOO_PINNED_UPDATE
                    else ()
                ),
                control_definitions=definitions,
                control_expectations=expectations,
            ))
        return tuple(result)

    def _field(self, item, bindings, references):
        provider = dict(item["provider"])
        source_ids = tuple(bindings[str(value)] for value in provider.get("source_column_ids", ()))
        reference = None
        kind = str(provider["kind"])
        value_source = ScalarValueSource(kind.casefold()) if kind != "REFERENCE_LOOKUP" else ScalarValueSource.SOURCE
        if kind == "REFERENCE_LOOKUP":
            current = references[str(provider["logical_reference_id"])]
            reference = ReferenceLookupMapping(
                reference_id=current.reference_id,
                reference_content_hash=current.content_hash,
                key_source_column_keys=source_ids,
                value_field=str(provider["value_field"]),
                on_blank=str(provider["on_blank"]),
                on_unknown=str(provider["on_unknown"]),
            )
        selection_rules = None
        if kind == "CONDITIONAL_RULES":
            selection_rules = SelectionRuleSet(
                rules=tuple(
                    SelectionRule(
                        rule_id=str(rule["rule_id"]),
                        join=SelectionRuleJoin(str(rule["join"])),
                        target_value=str(rule["target_value"]),
                        conditions=tuple(
                            SelectionCondition(
                                condition_id=str(condition["condition_id"]),
                                source_column_key=bindings[
                                    str(condition["source_column_id"])
                                ],
                                operator=SelectionConditionOperator(
                                    str(condition["operator"])
                                ),
                                comparison_value=(
                                    str(condition["comparison_value"])
                                    if condition.get("comparison_value") is not None
                                    else None
                                ),
                                value_type=str(condition["value_type"]),
                            )
                            for condition in rule["conditions"]
                        ),
                    )
                    for rule in provider.get("rules", ())
                ),
                otherwise_value=(
                    str(provider["otherwise_value"])
                    if provider.get("otherwise_value") is not None
                    else None
                ),
            )
        transform = dict(item.get("transform", {}))
        validation = dict(item.get("validation", {}))
        return ScalarFieldMapping(
            target_field=str(item["target_field"]),
            source_column_key=(
                source_ids[0]
                if source_ids and kind != "CONDITIONAL_RULES"
                else None
            ),
            value_source=value_source,
            literal_value=(str(provider["literal_value"]) if provider.get("literal_value") is not None else None),
            transform=ScalarTransformPolicy(
                **{key: value for key, value in transform.items() if key != "text_steps"},
                text_steps=tuple(TextTransformStep(**dict(value)) for value in transform.get("text_steps", ())),
            ),
            validation=ScalarValidationPolicy(**validation),
            value_mappings=tuple(ValueMapping(source_value=str(value["source_value"]), target_value=str(value["target_value"])) for value in item.get("value_matches", ())),
            value_type=str(item.get("value_type", "string")),
            required=bool(item.get("required")),
            required_on_create=bool(item.get("required_on_create")),
            compare=bool(item.get("compare", True)),
            validate_only=bool(item.get("validate_only")),
            null_policy=str(item.get("null_policy", "distinct")),
            reference_lookup=reference,
            categorical_policy=(CategoricalCoveragePolicy(str(item["categorical_policy"])) if item.get("categorical_policy") else None),
            selection_rules=selection_rules,
        )

    def _identity(self, item, bindings):
        resolver = dict(item["resolver"]) if item.get("resolver") else None
        return IdentityComponentMapping(
            source_column_keys=tuple(bindings[str(value)] for value in item.get("source_column_ids", ())),
            target_fields=tuple(str(value) for value in item.get("target_fields", ())),
            value_type=str(item.get("value_type", "string")),
            resolver=(self._resolver(resolver, bindings) if resolver else None),
        )

    def _relationship(self, item, bindings):
        resolver_payload = {
            "origin": (
                item.get("origin")
                or (
                    "dataset"
                    if item.get("target_dataset_id")
                    else "target_catalog"
                )
            ),
            "target_dataset_id": item.get("target_dataset_id"),
            "target_model": item.get("target_model"),
            "target_key_mappings": item.get("target_key_mappings", ()),
            "target_scope_mappings": item.get("target_scope_mappings", ()),
            "value_matches": item.get("value_matches", ()),
        }
        return RelationshipMapping(
            target_field=str(item["target_field"]),
            kind=str(item["kind"]),
            source_column_keys=tuple(bindings[str(value)] for value in item.get("source_column_ids", ())),
            resolver=self._resolver(resolver_payload, bindings),
            compare=bool(item.get("compare", True)),
            validate_only=bool(item.get("validate_only")),
            required=bool(item.get("required")),
            required_on_create=bool(item.get("required_on_create")),
            on_missing=str(item.get("on_missing", "error")),
            on_ambiguous=str(item.get("on_ambiguous", "error")),
            operation=str(item.get("operation", "replace")),
            separator=str(item.get("separator", ";")),
            null_policy=str(item.get("null_policy", "distinct")),
            categorical_policy=(CategoricalCoveragePolicy(str(item["categorical_policy"])) if item.get("categorical_policy") else None),
        )

    def _resolver(self, payload, bindings):
        return RelationshipResolver(
            origin=ResolverOrigin(str(payload["origin"])),
            dataset_id=(bindings[str(payload["target_dataset_id"])] if payload.get("target_dataset_id") else None),
            model=(str(payload["target_model"]) if payload.get("target_model") else None),
            key_mappings=tuple(ReferenceKeyMapping(source_column_key=bindings[str(item["source_column_id"])], target_field=str(item["target_field"])) for item in payload.get("target_key_mappings", ())),
            scope_mappings=tuple(ReferenceKeyMapping(source_column_key=bindings[str(item["source_column_id"])], target_field=str(item["target_field"])) for item in payload.get("target_scope_mappings", ())),
            value_mappings=tuple(ValueMapping(source_value=str(item["source_value"]), target_value=str(item["target_value"])) for item in payload.get("value_matches", ())),
        )

    @staticmethod
    def _block(code, message, recovery, logical_id=""):
        return RecipeApplicationIssue(code, RecipeApplicationIssueLevel.BLOCKER, message, recovery, logical_id)

    @staticmethod
    def _info(code, message, recovery, logical_id=""):
        return RecipeApplicationIssue(code, RecipeApplicationIssueLevel.INFORMATION, message, recovery, logical_id)

    @staticmethod
    def _review(code, message, recovery, logical_id=""):
        return RecipeApplicationIssue(
            code,
            RecipeApplicationIssueLevel.REVIEW,
            message,
            recovery,
            logical_id,
        )


def _recipe_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    token = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    if not token:
        raise RecipeApplicationError(
            "Reusable source names must contain a letter or number"
        )
    return token[:120]
