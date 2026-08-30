from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.mapping.contracts import (
    BusinessControlDefinition,
    BusinessControlTotal,
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    MappingControlExpectation,
    MappingTargetMode,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
    TargetFieldDisposition,
    TargetFieldHandling,
    ValueMapping,
)
from impodo.domain.mapping.scalar_values import (
    ScalarValueError,
    ScalarValueRuleError,
    canonicalize_scalar_value,
    evaluate_scalar_mapping_value,
)
from impodo.domain.mapping.canonicalization import canonicalize_mapping_definition
from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.relationship_dependencies import DependencyStrength
from impodo.domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
)
from impodo.domain.mapping.validation.validator import MappingSemanticValidator
from impodo.domain.source_binding import FileSourceBinding, OdooSourceBinding
from impodo.domain.staging.transformation_impact import (
    TransformationRuleImpact,
    transformation_rule_impact_definitions,
)
from impodo.domain.recipe.value_rules import (
    ScalarTransformPolicy,
    ScalarValidationPolicy,
    TextTransformStep,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


class MappingSemanticValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = _source_selection()
        self.schema = _schema_catalog()
        self.governance = _schema_governance(self.schema)
        self.validator = MappingSemanticValidator()

    def test_find_rule_counts_distinguish_plain_text_from_advanced_pattern(
        self,
    ) -> None:
        def counts(search_mode: str) -> tuple[int, int, int]:
            observed: list[tuple[bool, bool]] = []
            mapping = ScalarFieldMapping(
                target_field="phone",
                source_column_key="column:phone",
                transform=ScalarTransformPolicy(
                    text_steps=(
                        TextTransformStep(
                            search_value="^00",
                            replacement_value="+",
                            search_mode=search_mode,
                        ),
                    ),
                ),
            )
            for value in ("00352 1", "33123", "0044 2", "", None):
                canonicalize_scalar_value(
                    mapping,
                    value,
                    text_step_observer=(
                        lambda _index, matched, changed: observed.append(
                            (matched, changed)
                        )
                    ),
                )
            return (
                len(observed),
                sum(int(matched) for matched, _changed in observed),
                sum(int(changed) for _matched, changed in observed),
            )

        self.assertEqual(counts("literal"), (3, 0, 0))
        self.assertEqual(counts("pattern"), (3, 2, 2))

    def test_ordered_cleanup_steps_normalize_phone_values(self) -> None:
        observations: list[tuple[int, bool, bool]] = []
        mapping = ScalarFieldMapping(
            target_field="phone",
            source_column_key="column:phone",
            transform=ScalarTransformPolicy(
                text_steps=(
                    TextTransformStep(
                        search_value="00",
                        replacement_value="+",
                        search_mode="starts_with",
                        replace_all=False,
                    ),
                    TextTransformStep(
                        kind="remove_separators_between_digits",
                        characters=" .-/",
                    ),
                )
            ),
        )

        self.assertEqual(
            canonicalize_scalar_value(
                mapping,
                "00352-621.23.45",
                text_step_observer=(
                    lambda index, matched, changed: observations.append(
                        (index, matched, changed)
                    )
                ),
            ),
            "+3526212345",
        )
        self.assertEqual(observations, [(0, True, True), (1, True, True)])
        self.assertEqual(
            canonicalize_scalar_value(mapping, "067-77-37-67"),
            "067773767",
        )
        self.assertEqual(
            canonicalize_scalar_value(mapping, "067.37.67.77"),
            "067376777",
        )
        self.assertEqual(
            canonicalize_scalar_value(mapping, "067/37/67/77"),
            "067376777",
        )
        self.assertEqual(canonicalize_scalar_value(mapping, "120034"), "120034")

    def test_ordered_cleanup_sequence_is_portable_and_order_sensitive(self) -> None:
        first = TextTransformStep(search_value="ab", replacement_value="x")
        second = TextTransformStep(search_value="x", replacement_value="y")
        forward = ScalarFieldMapping(
            target_field="name",
            source_column_key="column:name",
            transform=ScalarTransformPolicy(text_steps=(first, second)),
        )
        reverse = replace(
            forward,
            transform=ScalarTransformPolicy(text_steps=(second, first)),
        )

        self.assertEqual(canonicalize_scalar_value(forward, "ab"), "y")
        self.assertEqual(canonicalize_scalar_value(reverse, "ab"), "x")

        definition = replace(
            _valid_definition(self.selection, self.governance),
            datasets=(
                replace(
                    _valid_definition(self.selection, self.governance).datasets[0],
                    fields=(forward,),
                ),
            ),
        )
        payload = definition.to_dict()
        transform = payload["datasets"][0]["fields"][0]["transform"]
        self.assertNotIn("search_value", transform)
        self.assertEqual(len(transform["text_steps"]), 2)
        self.assertEqual(MappingDefinition.from_dict(payload), definition)
        for noncurrent_version in (2, 7, 11):
            with self.subTest(noncurrent_version=noncurrent_version):
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    replace(definition, contract_version=noncurrent_version)

        noncurrent_payload = definition.to_dict()
        noncurrent_payload["datasets"][0]["fields"][0]["transform"][
            "retired_field"
        ] = "value"
        with self.assertRaisesRegex(ValueError, "current contract"):
            MappingDefinition.from_dict(noncurrent_payload)

    def test_ordered_cleanup_steps_have_distinct_review_evidence(self) -> None:
        field = ScalarFieldMapping(
            target_field="phone",
            source_column_key="column:phone",
            transform=ScalarTransformPolicy(
                text_steps=(
                    TextTransformStep(
                        search_value="00",
                        replacement_value="+",
                        search_mode="starts_with",
                    ),
                    TextTransformStep(
                        kind="remove_separators_between_digits",
                        characters=" .-",
                    ),
                )
            ),
        )

        definitions = transformation_rule_impact_definitions(
            "dataset:phone",
            field,
        )

        self.assertEqual(
            [item.rule_kind for item in definitions],
            ["find_replace_starts_with", "remove_separators_between_digits"],
        )
        self.assertEqual(
            len({item.rule_fingerprint for item in definitions}),
            2,
        )
        matched_but_unchanged = replace(
            definitions[0],
            evaluated_value_count=3,
            matched_value_count=2,
            changed_value_count=0,
        )
        self.assertTrue(matched_but_unchanged.requires_acknowledgement)
        self.assertFalse(
            TransformationRuleImpact(
                dataset_id="dataset:phone",
                target_field="phone",
                rule_kind="find_replace_literal",
                rule_fingerprint="sha256:" + "a" * 64,
                evaluated_value_count=3,
                matched_value_count=1,
                changed_value_count=1,
            ).requires_acknowledgement
        )

    def test_incomplete_cleanup_step_is_kept_visible_and_invalid(self) -> None:
        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        name_field = replace(
            partner.fields[0],
            transform=ScalarTransformPolicy(
                text_steps=(TextTransformStep(),),
            ),
        )

        result = self.validator.validate(
            replace(
                definition,
                datasets=(company, replace(partner, fields=(name_field,))),
            ),
            self.selection,
            self.schema,
            self.governance,
        )

        cleanup_issues = [
            item
            for item in result.issues
            if "/transform/text_steps/0" in item.path
        ]
        self.assertEqual(result.status, MappingValidationStatus.INVALID)
        self.assertEqual(len(cleanup_issues), 1)
        self.assertIn("no text to find", cleanup_issues[0].message)

    def test_valid_relationship_mapping_is_deterministic_and_portable(
        self,
    ) -> None:
        definition = _valid_definition(self.selection, self.governance)

        first = self.validator.validate(
            definition,
            self.selection,
            self.schema,
            self.governance,
        )
        second = self.validator.validate(
            definition,
            self.selection,
            self.schema,
            self.governance,
        )

        self.assertEqual(first.status, MappingValidationStatus.VALID)
        self.assertEqual(first.issues, ())
        self.assertEqual(first.validation_hash, second.validation_hash)
        self.assertEqual(
            definition.content_hash,
            "sha256:9f11acde136655839fbc36fe4ee46be98f75bf817219f61ae0fa4baa95f515d7",
        )
        self.assertEqual(
            first.validation_hash,
            "sha256:76a59233ea90502ade746d030a69234dca3435ad0c07cb46960aaf06029f3e49",
        )
        reversed_definition = replace(
            definition,
            datasets=tuple(reversed(definition.datasets)),
        )
        self.assertEqual(
            definition.content_hash,
            reversed_definition.content_hash,
        )
        self.assertEqual(
            canonicalize_mapping_definition(reversed_definition).datasets,
            definition.datasets,
        )
        self.assertEqual(
            MappingDefinition.from_json(definition.to_json()),
            definition,
        )
        self.assertEqual(
            MappingValidationResult.from_json(first.to_json()),
            first,
        )
        self.assertEqual(
            {item.code for item in first.deferred_runtime_checks},
            {
                "REFERENCE_RESOLUTION",
                "REQUIRED_ROW_VALUES",
                "SOURCE_IDENTITY_UNIQUENESS",
                "TARGET_REFERENCE_COVERAGE_DEFERRED",
                "TARGET_IDENTITY_UNIQUENESS",
            },
        )

    def test_pinned_odoo_update_needs_no_portable_business_key(self) -> None:
        selection, schema = _pinned_odoo_inputs()
        definition = MappingDefinition(
            mapping_id="mapping:odoo-pinned",
            source_selection_hash=selection.content_hash,
            schema_hash=schema.content_hash,
            datasets=(
                DatasetMapping(
                    dataset_id=selection.datasets[0].dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.ODOO_PINNED_UPDATE,
                    fields=(
                        ScalarFieldMapping(
                            target_field="name",
                            source_column_key="odoo:name",
                        ),
                    ),
                    approved_write_fields=("name",),
                ),
            ),
        )

        result = self.validator.validate(
            definition,
            selection,
            schema,
            None,
        )

        self.assertEqual(result.status, MappingValidationStatus.VALID)
        self.assertEqual(result.issues, ())
        self.assertEqual(
            {item.code for item in result.deferred_runtime_checks},
            {"REQUIRED_ROW_VALUES"},
        )
        restored = MappingDefinition.from_json(definition.to_json())
        self.assertEqual(restored, definition)
        self.assertEqual(restored.datasets[0].approved_write_fields, ("name",))
        self.assertNotIn('"id"', definition.to_json())

    def test_pinned_odoo_update_fails_closed_on_write_policy(self) -> None:
        selection, schema = _pinned_odoo_inputs()
        definition = MappingDefinition(
            mapping_id="mapping:odoo-pinned-invalid",
            source_selection_hash=selection.content_hash,
            schema_hash=schema.content_hash,
            datasets=(
                DatasetMapping(
                    dataset_id=selection.datasets[0].dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.ODOO_PINNED_UPDATE,
                    fields=(
                        ScalarFieldMapping(
                            target_field="email",
                            source_column_key="odoo:name",
                        ),
                        ScalarFieldMapping(
                            target_field="display_name",
                            source_column_key="odoo:name",
                        ),
                    ),
                    approved_write_fields=("display_name",),
                ),
            ),
        )

        result = self.validator.validate(
            definition,
            selection,
            schema,
            None,
        )

        self.assertEqual(result.status, MappingValidationStatus.INVALID)
        self.assertTrue(
            {
                "MAPPING_ODOO_WRITE_FIELD_UNAPPROVED",
                "MAPPING_ODOO_WRITE_BASELINE_MISSING",
                "MAPPING_ODOO_WRITE_FIELD_INELIGIBLE",
            }.issubset({item.code for item in result.issues})
        )

    def test_pinned_odoo_update_keeps_unknown_compute_metadata_ineligible(
        self,
    ) -> None:
        selection, schema = _pinned_odoo_inputs()
        model = schema.models[0]
        schema = replace(
            schema,
            models=(
                replace(
                    model,
                    fields=tuple(
                        replace(field, computed=None)
                        if field.name == "name"
                        else field
                        for field in model.fields
                    ),
                ),
            ),
        )
        definition = MappingDefinition(
            mapping_id="mapping:odoo-pinned-unknown-compute",
            source_selection_hash=selection.content_hash,
            schema_hash=schema.content_hash,
            datasets=(
                DatasetMapping(
                    dataset_id=selection.datasets[0].dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.ODOO_PINNED_UPDATE,
                    fields=(
                        ScalarFieldMapping(
                            target_field="name",
                            source_column_key="odoo:name",
                        ),
                    ),
                    approved_write_fields=("name",),
                ),
            ),
        )

        result = self.validator.validate(definition, selection, schema, None)

        self.assertEqual(result.status, MappingValidationStatus.INVALID)
        self.assertIn(
            "MAPPING_ODOO_WRITE_FIELD_INELIGIBLE",
            {item.code for item in result.issues},
        )

    def test_reviewed_country_code_resolves_without_capturing_country_as_target(
        self,
    ) -> None:
        partner_source = self.selection.datasets[1]
        selection = replace(
            self.selection,
            datasets=(
                self.selection.datasets[0],
                replace(
                    partner_source,
                    columns=(
                        *partner_source.columns,
                        SourceDatasetColumn(
                            7,
                            "country",
                            "partner.country",
                            "string",
                        ),
                    ),
                ),
            ),
        )
        partner_model = next(
            item for item in self.schema.models if item.name == "res.partner"
        )
        schema = replace(
            self.schema,
            models=tuple(
                replace(
                    item,
                    fields=(
                        *item.fields,
                        _field(
                            "country_id",
                            "many2one",
                            relation="res.country",
                        ),
                    ),
                )
                if item is partner_model
                else item
                for item in self.schema.models
            ),
        )
        definition = _valid_definition(selection, self.governance)
        company, partner = definition.datasets
        country = RelationshipMapping(
            target_field="country_id",
            kind="many2one",
            source_column_keys=("partner.country",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="res.country",
                key_mappings=(
                    ReferenceKeyMapping("partner.country", "code"),
                ),
                value_mappings=(ValueMapping("FRA", "FR"),),
            ),
        )

        valid = self.validator.validate(
            replace(
                definition,
                datasets=(
                    company,
                    replace(
                        partner,
                        relationships=(*partner.relationships, country),
                    ),
                ),
            ),
            selection,
            schema,
            self.governance,
        )
        wrong_key = self.validator.validate(
            replace(
                definition,
                datasets=(
                    company,
                    replace(
                        partner,
                        relationships=(
                            *partner.relationships,
                            replace(
                                country,
                                resolver=replace(
                                    country.resolver,
                                    key_mappings=(
                                        ReferenceKeyMapping(
                                            "partner.country",
                                            "name",
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            selection,
            schema,
            self.governance,
        )

        self.assertNotIn(
            "MAPPING_TARGET_MODEL_UNKNOWN",
            {item.code for item in valid.issues},
        )
        self.assertNotIn(
            "MAPPING_BUSINESS_KEY_NOT_GOVERNED",
            {item.code for item in valid.issues},
        )
        self.assertIn(
            "MAPPING_BUSINESS_KEY_NOT_GOVERNED",
            {item.code for item in wrong_key.issues},
        )

    def test_unsafe_relationships_are_blocking_but_self_edges_are_retained(
        self,
    ) -> None:
        valid = _valid_definition(self.selection, self.governance)
        company, partner = valid.datasets
        invalid_scope = replace(
            partner.target_scope[0],
            resolver=RelationshipResolver(
                origin=ResolverOrigin.DATASET,
                dataset_id=partner.dataset_id,
            ),
        )
        one2many = RelationshipMapping(
            target_field="child_ids",
            kind="one2many",
            source_column_keys=("partner.parent_ref",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="res.partner",
                key_mappings=(
                    ReferenceKeyMapping("partner.parent_ref", "ref"),
                ),
            ),
            on_missing="warning",
            on_ambiguous="warning",
        )
        definition = replace(
            valid,
            datasets=(
                company,
                replace(
                    partner,
                    target_scope=(invalid_scope,),
                    relationships=(*partner.relationships, one2many),
                ),
            ),
        )

        result = self.validator.validate(
            definition,
            self.selection,
            self.schema,
            self.governance,
        )
        codes = {item.code for item in result.issues}

        self.assertEqual(result.status, MappingValidationStatus.INVALID)
        self.assertNotIn("MAPPING_DEPENDENCY_CYCLE", codes)
        self.assertIn("MAPPING_ONE2MANY_OWNER_INVALID", codes)
        self.assertIn("MAPPING_RELATED_MODEL_INCORRECT", codes)
        self.assertIn("MAPPING_RELATION_POLICY_UNSAFE", codes)
        self.assertIn("MAPPING_BUSINESS_KEY_NOT_GOVERNED", codes)

    def test_self_relationships_are_left_for_row_level_cycle_analysis(
        self,
    ) -> None:
        valid = _valid_definition(self.selection, self.governance)
        company, partner = valid.datasets
        parent = RelationshipMapping(
            target_field="parent_id",
            kind="many2one",
            source_column_keys=("partner.parent_ref",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.DATASET,
                dataset_id=partner.dataset_id,
            ),
        )
        optional = replace(
            valid,
            datasets=(
                company,
                replace(partner, relationships=(*partner.relationships, parent)),
            ),
        )
        required = replace(
            optional,
            datasets=(
                company,
                replace(
                    optional.datasets[1],
                    relationships=(
                        *optional.datasets[1].relationships[:-1],
                        replace(parent, required_on_create=True),
                    ),
                ),
            ),
        )

        optional_result = self.validator.validate(
            optional,
            self.selection,
            self.schema,
            self.governance,
        )
        required_result = self.validator.validate(
            required,
            self.selection,
            self.schema,
            self.governance,
        )
        required_schema = replace(
            self.schema,
            models=tuple(
                replace(
                    item,
                    fields=tuple(
                        replace(field, required=True)
                        if field.name == "parent_id"
                        else field
                        for field in item.fields
                    ),
                )
                if item.name == "res.partner"
                else item
                for item in self.schema.models
            ),
            content_hash="sha256:required-parent-schema",
        )
        required_governance = _schema_governance(required_schema)
        schema_required_result = self.validator.validate(
            replace(optional, schema_hash=required_governance.content_hash),
            self.selection,
            required_schema,
            required_governance,
        )

        self.assertNotIn(
            "MAPPING_DEPENDENCY_CYCLE",
            {item.code for item in optional_result.issues},
        )
        self.assertNotIn(
            "MAPPING_DEPENDENCY_CYCLE",
            {item.code for item in required_result.issues},
        )
        self.assertNotIn(
            "MAPPING_DEPENDENCY_CYCLE",
            {item.code for item in schema_required_result.issues},
        )

    def test_required_cross_dataset_cycle_still_blocks(self) -> None:
        valid = _valid_definition(self.selection, self.governance)
        company, partner = valid.datasets
        relationship = RelationshipMapping(
            target_field="x_partner_id",
            kind="many2one",
            source_column_keys=("company.code",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.DATASET,
                dataset_id=partner.dataset_id,
            ),
            required_on_create=True,
            categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
        )
        definition = replace(
            valid,
            datasets=(
                replace(company, relationships=(relationship,)),
                partner,
            ),
        )
        schema = replace(
            self.schema,
            models=tuple(
                replace(
                    model,
                    fields=(
                        *model.fields,
                        _field(
                            "x_partner_id",
                            "many2one",
                            relation="res.partner",
                        ),
                    ),
                )
                if model.name == "res.company"
                else model
                for model in self.schema.models
            ),
        )

        result = self.validator.validate(
            definition,
            self.selection,
            schema,
            self.governance,
        )

        self.assertIn(
            "MAPPING_DEPENDENCY_CYCLE",
            {item.code for item in result.issues},
        )

    def test_browser_compiler_normalizes_captured_required_relationships(
        self,
    ) -> None:
        selection = replace(
            self.selection,
            content_hash="sha256:" + "1" * 64,
        )
        valid = _valid_definition(selection, self.governance)
        company, partner = valid.datasets
        parent = RelationshipMapping(
            target_field="parent_id",
            kind="many2one",
            source_column_keys=("partner.parent_ref",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.DATASET,
                dataset_id=partner.dataset_id,
            ),
            categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
        )
        definition = replace(
            valid,
            datasets=(
                company,
                replace(partner, relationships=(*partner.relationships, parent)),
            ),
        )

        compiled = compile_browser_mapping(
            definition,
            selection,
            required_relationship_fields={"res.partner": {"parent_id"}},
        )
        compiled_partner = next(
            dataset for dataset in compiled.datasets if dataset.name == "partners"
        )
        edge = next(
            item
            for item in compiled.dependency_edges
            if item.owner_dataset == "partners" and item.target_field == "parent_id"
        )

        self.assertTrue(compiled_partner.relations["parent_id"].required_on_create)
        self.assertEqual(edge.strength, DependencyStrength.HARD)
        self.assertTrue(edge.is_self_reference)

    def test_generated_target_projection_is_reviewed_and_compiled_generically(
        self,
    ) -> None:
        selection = SourceSelection(
            selection_id="selection:generated-target",
            version=1,
            data_version_id="project:test",
            created_at=NOW,
            created_by="Test operator",
            datasets=(
                SourceDataset(
                    dataset_id="dataset:products",
                    name="products",
                    source=FileSourceBinding(
                        file_id="file:products",
                        table_key="csv",
                        source_sha256="a" * 64,
                        catalog_hash="sha256:" + "1" * 64,
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
                    row_count=2,
                    columns=(
                        SourceDatasetColumn(
                            1,
                            "Product code",
                            "product.code",
                            "string",
                        ),
                        SourceDatasetColumn(
                            2,
                            "Product name",
                            "product.name",
                            "string",
                        ),
                    ),
                ),
                SourceDataset(
                    dataset_id="dataset:bom-lines",
                    name="bom_lines",
                    source=FileSourceBinding(
                        file_id="file:bom-lines",
                        table_key="csv",
                        source_sha256="b" * 64,
                        catalog_hash="sha256:" + "2" * 64,
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
                    row_count=2,
                    columns=(
                        SourceDatasetColumn(
                            1,
                            "Line code",
                            "line.code",
                            "string",
                        ),
                        SourceDatasetColumn(
                            2,
                            "Product code",
                            "line.product_code",
                            "string",
                        ),
                    ),
                ),
            ),
            content_hash="sha256:" + "3" * 64,
        )
        schema = replace(
            self.schema,
            models=(
                SchemaModel(
                    "mrp.bom.line",
                    "BOM Component",
                    (
                        _field("x_legacy_key", required=True),
                        _field(
                            "product_id",
                            "many2one",
                            required=True,
                            relation="product.product",
                        ),
                    ),
                ),
                SchemaModel(
                    "product.product",
                    "Product Variant",
                    (_field("default_code", required=True),),
                ),
                SchemaModel(
                    "product.template",
                    "Product",
                    (
                        _field("default_code", required=True),
                        _field("name", required=True),
                        _field(
                            "product_variant_id",
                            "many2one",
                            readonly=True,
                            relation="product.product",
                        ),
                    ),
                ),
            ),
            content_hash="sha256:" + "4" * 64,
        )
        governance = SchemaGovernance(
            governance_id="governance:generated-target",
            version=1,
            workspace_id=schema.workspace_id,
            catalog_hash=schema.content_hash,
            permitted_models=tuple(model.name for model in schema.models),
            business_keys=(
                BusinessKeyDefinition(
                    key_id="bom-line-key",
                    model="mrp.bom.line",
                    key_fields=("x_legacy_key",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    key_id="variant-code",
                    model="product.product",
                    key_fields=("default_code",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    key_id="template-code",
                    model="product.template",
                    key_fields=("default_code",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
            recorded_at=NOW,
            recorded_by="Test operator",
        )
        products = DatasetMapping(
            dataset_id="dataset:products",
            target_model="product.template",
            source_identity_column_keys=("product.code",),
            target_identity=(
                IdentityComponentMapping(
                    source_column_keys=("product.code",),
                    target_fields=("default_code",),
                ),
            ),
            fields=(
                ScalarFieldMapping(
                    target_field="name",
                    source_column_key="product.name",
                ),
            ),
        )
        relation = RelationshipMapping(
            target_field="product_id",
            kind="many2one",
            source_column_keys=("line.product_code",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_THEN_DATASET,
                dataset_id=products.dataset_id,
                model="product.product",
                key_mappings=(
                    ReferenceKeyMapping(
                        "line.product_code",
                        "default_code",
                    ),
                ),
                dataset_projection_field="product_variant_id",
            ),
            required=True,
            required_on_create=True,
            categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
        )
        definition = MappingDefinition(
            mapping_id="mapping:generated-target",
            source_selection_hash=selection.content_hash,
            schema_hash=governance.content_hash,
            datasets=(
                products,
                DatasetMapping(
                    dataset_id="dataset:bom-lines",
                    target_model="mrp.bom.line",
                    source_identity_column_keys=("line.code",),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=("line.code",),
                            target_fields=("x_legacy_key",),
                        ),
                    ),
                    relationships=(relation,),
                ),
            ),
        )

        validation = self.validator.validate(
            definition,
            selection,
            schema,
            governance,
        )
        compiled = compile_browser_mapping(definition, selection)

        self.assertEqual(validation.status, MappingValidationStatus.VALID)
        self.assertEqual(validation.issues, ())
        self.assertEqual(
            compiled.dataset("bom_lines")
            .relations["product_id"]
            .resolve.incoming_projection_field,
            "product_variant_id",
        )
        invalid = replace(
            definition,
            datasets=(
                products,
                replace(
                    definition.datasets[1],
                    relationships=(
                        replace(
                            relation,
                            resolver=replace(
                                relation.resolver,
                                dataset_projection_field="name",
                            ),
                        ),
                    ),
                ),
            ),
        )
        self.assertIn(
            "MAPPING_GENERATED_TARGET_INVALID",
            {
                item.code
                for item in self.validator.validate(
                    invalid,
                    selection,
                    schema,
                    governance,
                ).issues
            },
        )
        target_only_projection = replace(
            definition,
            datasets=(
                products,
                replace(
                    definition.datasets[1],
                    relationships=(
                        replace(
                            relation,
                            resolver=replace(
                                relation.resolver,
                                origin=ResolverOrigin.TARGET_CATALOG,
                                dataset_id=None,
                            ),
                        ),
                    ),
                ),
            ),
        )
        self.assertIn(
            "MAPPING_REFERENCE_KEY_INVALID",
            {
                item.code
                for item in self.validator.validate(
                    target_only_projection,
                    selection,
                    schema,
                    governance,
                ).issues
            },
        )

    def test_every_frozen_dataset_and_governed_key_are_required(self) -> None:
        valid = _valid_definition(self.selection, self.governance)
        partner_only = replace(valid, datasets=(valid.datasets[1],))
        ungoverned = replace(
            partner_only,
            datasets=(
                replace(
                    partner_only.datasets[0],
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=("partner.name",),
                            target_fields=("name",),
                        ),
                    ),
                ),
            ),
        )

        result = self.validator.validate(
            ungoverned,
            self.selection,
            self.schema,
            self.governance,
        )
        codes = [item.code for item in result.issues]

        self.assertIn("MAPPING_DATASET_UNMAPPED", codes)
        self.assertIn("MAPPING_BUSINESS_KEY_NOT_GOVERNED", codes)

    def test_hash_tampering_is_rejected(self) -> None:
        definition = _valid_definition(self.selection, self.governance)
        payload = definition.to_dict()
        payload["content_hash"] = "sha256:" + ("0" * 64)

        with self.assertRaisesRegex(ValueError, "content hash"):
            MappingDefinition.from_dict(payload)

    def test_constant_fallback_and_allowlisted_transformations_are_portable(
        self,
    ) -> None:
        constant = ScalarFieldMapping(
            target_field="name",
            value_source=ScalarValueSource.CONSTANT,
            literal_value="  MAIN   COMPANY  ",
            transform=ScalarTransformPolicy(
                trim=True,
                collapse_whitespace=True,
                case_mode="lowercase",
            ),
        )
        fallback = replace(
            constant,
            value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
            source_column_key="company.name",
            literal_value="Fallback",
            transform=replace(constant.transform, empty_as_null=True),
        )
        decimal = replace(
            constant,
            target_field="amount",
            literal_value="1.234,50",
            value_type="decimal",
            transform=ScalarTransformPolicy(decimal_locale="de_DE"),
        )

        self.assertEqual(
            canonicalize_scalar_value(constant, None),
            "main company",
        )
        self.assertEqual(
            canonicalize_scalar_value(fallback, "   "),
            "fallback",
        )
        self.assertEqual(
            canonicalize_scalar_value(decimal, None),
            Decimal("1234.50"),
        )

        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        changed = replace(
            definition,
            datasets=(replace(company, fields=(constant,)), partner),
        )
        self.assertEqual(
            MappingDefinition.from_json(changed.to_json()),
            changed,
        )
        self.assertNotEqual(definition.content_hash, changed.content_hash)

    def test_source_choices_map_to_selection_keys_before_validation(self) -> None:
        mapping = ScalarFieldMapping(
            target_field="lang",
            source_column_key="partner.name",
            transform=ScalarTransformPolicy(case_mode="uppercase"),
            value_mappings=(
                ValueMapping("French (France)", "fr_FR"),
                ValueMapping("German", "de_DE"),
            ),
        )

        self.assertEqual(
            canonicalize_scalar_value(mapping, " French (France) "),
            "fr_FR",
        )
        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        changed = replace(
            definition,
            datasets=(company, replace(partner, fields=(mapping,))),
        )
        self.assertEqual(
            MappingDefinition.from_json(changed.to_json())
            .datasets[1]
            .fields[0]
            .value_mappings,
            mapping.value_mappings,
        )

    def test_selection_value_match_must_target_an_odoo_choice(self) -> None:
        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        language = ScalarFieldMapping(
            target_field="lang",
            source_column_key="partner.name",
            value_mappings=(ValueMapping("French", "not_an_odoo_key"),),
        )
        partner_model = next(
            item for item in self.schema.models if item.name == "res.partner"
        )
        schema = replace(
            self.schema,
            models=tuple(
                replace(
                    item,
                    fields=(
                        *item.fields,
                        SchemaField(
                            name="lang",
                            label="Language",
                            type="selection",
                            required=False,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(
                                ("fr_FR", "French (France)"),
                                ("de_DE", "German"),
                            ),
                        ),
                    ),
                )
                if item is partner_model
                else item
                for item in self.schema.models
            ),
        )
        result = self.validator.validate(
            replace(
                definition,
                datasets=(company, replace(partner, fields=(language,))),
            ),
            self.selection,
            schema,
            self.governance,
        )

        self.assertEqual(result.status, MappingValidationStatus.INVALID)
        self.assertIn(
            "MAPPING_SELECTION_VALUE_INVALID",
            {item.code for item in result.issues},
        )

    def test_selection_literal_fails_when_odoo_exposes_no_choices(self) -> None:
        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        language = ScalarFieldMapping(
            target_field="lang",
            value_source=ScalarValueSource.CONSTANT,
            literal_value="fr_FR",
        )
        partner_model = next(
            item for item in self.schema.models if item.name == "res.partner"
        )
        schema = replace(
            self.schema,
            models=tuple(
                replace(
                    item,
                    fields=(
                        *item.fields,
                        SchemaField(
                            name="lang",
                            label="Language",
                            type="selection",
                            required=False,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                        ),
                    ),
                )
                if item is partner_model
                else item
                for item in self.schema.models
            ),
        )

        result = self.validator.validate(
            replace(
                definition,
                datasets=(company, replace(partner, fields=(language,))),
            ),
            self.selection,
            schema,
            self.governance,
        )

        self.assertEqual(result.status, MappingValidationStatus.INVALID)
        self.assertIn(
            "MAPPING_SELECTION_VALUE_INVALID",
            {item.code for item in result.issues},
        )

    def test_strict_date_boolean_and_datetime_parsing(self) -> None:
        date_mapping = ScalarFieldMapping(
            target_field="date",
            value_source=ScalarValueSource.CONSTANT,
            literal_value="30/07/2026",
            value_type="date",
            transform=ScalarTransformPolicy(date_format="dmy_slash"),
        )
        boolean_mapping = replace(
            date_mapping,
            target_field="active",
            literal_value="yes",
            value_type="boolean",
        )
        datetime_mapping = replace(
            date_mapping,
            target_field="started_at",
            literal_value="2026-07-30T12:30:00+02:00",
            value_type="datetime",
            transform=ScalarTransformPolicy(),
        )

        self.assertEqual(
            canonicalize_scalar_value(date_mapping, None).isoformat(),
            "2026-07-30",
        )
        self.assertIs(
            canonicalize_scalar_value(boolean_mapping, None),
            True,
        )
        self.assertEqual(
            canonicalize_scalar_value(datetime_mapping, None).isoformat(),
            "2026-07-30T10:30:00+00:00",
        )
        with self.assertRaises(ScalarValueError):
            canonicalize_scalar_value(
                replace(boolean_mapping, literal_value="sometimes"),
                None,
            )

    def test_guided_text_rules_transform_and_validate_the_proposed_value(
        self,
    ) -> None:
        mapping = ScalarFieldMapping(
            target_field="x_code",
            value_source=ScalarValueSource.CONSTANT,
            literal_value="  abc-007  ",
            transform=ScalarTransformPolicy(
                trim=True,
                text_steps=(
                    TextTransformStep(
                        search_value="-",
                        replacement_value="",
                    ),
                ),
                case_mode="uppercase",
            ),
            validation=ScalarValidationPolicy(
                exact_length=6,
                segment_location="last",
                segment_length=3,
                character_class="digits",
                pattern=r"[A-Z]{3}[0-9]{3}",
            ),
        )

        self.assertEqual(canonicalize_scalar_value(mapping, None), "ABC007")
        with self.assertRaises(ScalarValueRuleError) as raised:
            canonicalize_scalar_value(
                replace(
                    mapping,
                    validation=replace(mapping.validation, exact_length=7),
                ),
                None,
            )
        self.assertEqual(raised.exception.code, "SOURCE_TEXT_LENGTH_INVALID")

    def test_decimal_rounding_and_safe_formula_are_deterministic(self) -> None:
        mapping = ScalarFieldMapping(
            target_field="amount_total",
            source_column_key="line.quantity",
            value_type="decimal",
            transform=ScalarTransformPolicy(
                formula="column_1 * column_2",
                decimal_places=2,
                rounding_mode="half_up",
            ),
        )

        self.assertEqual(
            canonicalize_scalar_value(
                mapping,
                "2",
                formula_context={"column_1": "2", "column_2": "1.235"},
            ),
            Decimal("2.47"),
        )

    def test_declared_control_total_is_portable_and_requires_numeric_mapping(
        self,
    ) -> None:
        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        control = BusinessControlTotal(
            name="Opening balance",
            target_field="x_amount",
            expected_total="1234.50",
            unit="EUR",
            tolerance="0.01",
        )
        partner_model = next(
            item for item in self.schema.models if item.name == "res.partner"
        )
        schema = replace(
            self.schema,
            models=tuple(
                replace(
                    item,
                    fields=(*item.fields, _field("x_amount", "monetary")),
                )
                if item is partner_model
                else item
                for item in self.schema.models
            ),
        )
        amount = ScalarFieldMapping(
            target_field="x_amount",
            source_column_key="partner.name",
            value_type="decimal",
        )
        changed = replace(
            definition,
            datasets=(
                company,
                replace(
                    partner,
                    fields=(*partner.fields, amount),
                    control_definitions=(
                        BusinessControlDefinition(
                            control_id="control:x_amount",
                            name=control.name,
                            target_field=control.target_field,
                            unit=control.unit,
                            tolerance=control.tolerance,
                        ),
                    ),
                    control_expectations=(
                        MappingControlExpectation(
                            control_id="control:x_amount",
                            expected_total=control.expected_total,
                        ),
                    ),
                ),
            ),
        )

        result = self.validator.validate(
            changed,
            self.selection,
            schema,
            self.governance,
        )
        restored = MappingDefinition.from_json(changed.to_json())

        self.assertNotIn(
            "MAPPING_CONTROL_TOTAL_FIELD_UNMAPPED",
            {item.code for item in result.issues},
        )
        self.assertEqual(
            restored.datasets[1].effective_control_totals,
            (control,),
        )
        self.assertEqual(
            evaluate_scalar_mapping_value(
                amount,
                "2",
                source_values_by_ordinal={1: "2", 2: "1.235"},
            ),
            Decimal("2"),
        )

        missing_expectation = replace(
            changed,
            datasets=(
                company,
                replace(changed.datasets[1], control_expectations=()),
            ),
        )
        missing_result = self.validator.validate(
            missing_expectation,
            self.selection,
            schema,
            self.governance,
        )
        self.assertIn(
            "MAPPING_CONTROL_EXPECTATION_REQUIRED",
            {item.code for item in missing_result.issues},
        )

    def test_unsafe_custom_pattern_and_formula_block_mapping_validation(
        self,
    ) -> None:
        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        unsafe = replace(
            company.fields[0],
            transform=ScalarTransformPolicy(formula="__import__('os')"),
            validation=ScalarValidationPolicy(pattern=r"(a+)+$"),
        )
        definition = replace(
            definition,
            datasets=(replace(company, fields=(unsafe,)), partner),
        )

        result = self.validator.validate(
            definition,
            self.selection,
            self.schema,
            self.governance,
        )

        self.assertEqual(result.status, MappingValidationStatus.INVALID)
        self.assertTrue(
            {"MAPPING_FORMULA_INVALID", "MAPPING_VALUE_RULE_INVALID"}
            <= {item.code for item in result.issues}
        )

    def test_odoo_default_requires_verified_target_evidence(
        self,
    ) -> None:
        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        odoo_default = ScalarFieldMapping(
            target_field="name",
            value_source=ScalarValueSource.ODOO_DEFAULT,
            compare=False,
        )
        definition = replace(
            definition,
            datasets=(replace(company, fields=(odoo_default,)), partner),
        )

        unverified = self.validator.validate(
            definition,
            self.selection,
            self.schema,
            self.governance,
        )
        default_schema = replace(
            self.schema,
            models=tuple(
                replace(
                    model,
                    fields=tuple(
                        replace(
                            field,
                            create_default_present=True,
                            create_default_value="Odoo company",
                        )
                        if model.name == "res.company" and field.name == "name"
                        else field
                        for field in model.fields
                    ),
                )
                for model in self.schema.models
            ),
        )
        verified = self.validator.validate(
            definition,
            self.selection,
            default_schema,
            self.governance,
        )

        self.assertEqual(unverified.status, MappingValidationStatus.INVALID)
        self.assertEqual(
            [item.code for item in unverified.issues],
            ["MAPPING_ODOO_DEFAULT_UNVERIFIED"],
        )
        self.assertEqual(verified.status, MappingValidationStatus.VALID)

    def test_required_field_can_be_explicitly_left_to_odoo(self) -> None:
        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        definition = replace(
            definition,
            datasets=(
                replace(
                    company,
                    fields=(),
                    target_field_dispositions=(
                        TargetFieldDisposition(
                            target_field="name",
                            handling=TargetFieldHandling.ODOO_DEFAULT,
                        ),
                    ),
                ),
                partner,
            ),
        )

        default_schema = replace(
            self.schema,
            models=tuple(
                replace(
                    model,
                    fields=tuple(
                        replace(
                            field,
                            create_default_present=True,
                            create_default_value="Odoo company",
                        )
                        if model.name == "res.company" and field.name == "name"
                        else field
                        for field in model.fields
                    ),
                )
                for model in self.schema.models
            ),
        )
        result = self.validator.validate(
            definition,
            self.selection,
            default_schema,
            self.governance,
        )

        self.assertEqual(result.status, MappingValidationStatus.VALID)
        self.assertEqual(result.issues, ())
        restored = MappingDefinition.from_json(definition.to_json())
        self.assertEqual(
            restored.datasets[0].target_field_dispositions,
            definition.datasets[0].target_field_dispositions,
        )

    def test_only_schema_identified_fields_can_be_marked_odoo_managed(
        self,
    ) -> None:
        definition = _valid_definition(self.selection, self.governance)
        company, partner = definition.datasets
        managed_schema = replace(
            self.schema,
            models=tuple(
                replace(
                    model,
                    fields=tuple(
                        replace(field, required=True)
                        if model.name == "res.partner" and field.name == "tag_ids"
                        else field
                        for field in model.fields
                    ),
                )
                for model in self.schema.models
            ),
        )
        managed_partner = replace(
            partner,
            relationships=tuple(
                item
                for item in partner.relationships
                if item.target_field != "tag_ids"
            ),
            target_field_dispositions=(
                TargetFieldDisposition(
                    target_field="tag_ids",
                    handling=TargetFieldHandling.ODOO_MANAGED,
                ),
            ),
        )

        managed_result = self.validator.validate(
            replace(definition, datasets=(company, managed_partner)),
            self.selection,
            managed_schema,
            self.governance,
        )
        invalid_result = self.validator.validate(
            replace(
                definition,
                datasets=(
                    replace(
                        company,
                        fields=(),
                        target_field_dispositions=(
                            TargetFieldDisposition(
                                target_field="name",
                                handling=TargetFieldHandling.ODOO_MANAGED,
                            ),
                        ),
                    ),
                    partner,
                ),
            ),
            self.selection,
            self.schema,
            self.governance,
        )

        self.assertEqual(
            managed_result.status,
            MappingValidationStatus.VALID_WITH_WARNINGS,
        )
        self.assertEqual(
            [item.code for item in managed_result.issues],
            ["MAPPING_ODOO_MANAGED_UNVERIFIED"],
        )
        self.assertIn(
            "MAPPING_TARGET_FIELD_DISPOSITION_INVALID",
            {item.code for item in invalid_result.issues},
        )

    def test_retired_mapping_payload_is_rejected_before_nested_parsing(self) -> None:
        current = _valid_definition(self.selection, self.governance)
        portable = current.to_dict()
        portable["contract_version"] = 8

        with self.assertRaisesRegex(ValueError, "unsupported"):
            MappingDefinition.from_dict(portable)

    def test_noncurrent_mapping_contract_is_rejected(self) -> None:
        current = _valid_definition(self.selection, self.governance)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            replace(current, contract_version=2)


def _valid_definition(
    selection: SourceSelection,
    governance: SchemaGovernance,
) -> MappingDefinition:
    company = DatasetMapping(
        dataset_id="dataset:companies",
        target_model="res.company",
        source_identity_column_keys=("company.code",),
        target_identity=(
            IdentityComponentMapping(
                source_column_keys=("company.code",),
                target_fields=("x_legacy_code",),
            ),
        ),
        fields=(
            ScalarFieldMapping(
                target_field="name",
                source_column_key="company.name",
            ),
        ),
    )
    partner = DatasetMapping(
        dataset_id="dataset:partners",
        target_model="res.partner",
        source_identity_column_keys=("partner.ref", "partner.company"),
        target_identity=(
            IdentityComponentMapping(
                source_column_keys=("partner.ref",),
                target_fields=("ref",),
            ),
        ),
        target_scope=(
            IdentityComponentMapping(
                source_column_keys=("partner.company",),
                target_fields=("company_id",),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.DATASET,
                    dataset_id=company.dataset_id,
                ),
            ),
        ),
        fields=(
            ScalarFieldMapping(
                target_field="name",
                source_column_key="partner.name",
            ),
        ),
        relationships=(
            RelationshipMapping(
                target_field="category_id",
                kind="many2one",
                source_column_keys=("partner.category",),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.TARGET_CATALOG,
                    model="res.partner.category",
                    key_mappings=(
                        ReferenceKeyMapping("partner.category", "code"),
                    ),
                ),
                categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
            ),
            RelationshipMapping(
                target_field="tag_ids",
                kind="many2many",
                source_column_keys=("partner.tags",),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.TARGET_CATALOG,
                    model="res.partner.category",
                    key_mappings=(
                        ReferenceKeyMapping("partner.tags", "code"),
                    ),
                ),
                operation="add",
                separator=";",
                categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
            ),
        ),
    )
    return MappingDefinition(
        mapping_id="mapping:test",
        source_selection_hash=selection.content_hash,
        schema_hash=governance.content_hash,
        datasets=(company, partner),
    )


def _source_selection() -> SourceSelection:
    return SourceSelection(
        selection_id="selection:test",
        version=1,
        data_version_id="project:test",
        created_at=NOW,
        created_by="Test operator",
        datasets=(
            SourceDataset(
                dataset_id="dataset:companies",
                name="companies",
                source=FileSourceBinding(
                    file_id="file:companies",
                    table_key="csv",
                    source_sha256="a" * 64,
                    catalog_hash="sha256:" + "c" * 64,
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                ),
                row_count=2,
                columns=(
                    SourceDatasetColumn(1, "code", "company.code", "string"),
                    SourceDatasetColumn(2, "name", "company.name", "string"),
                ),
            ),
            SourceDataset(
                dataset_id="dataset:partners",
                name="partners",
                source=FileSourceBinding(
                    file_id="file:partners",
                    table_key="csv",
                    source_sha256="b" * 64,
                    catalog_hash="sha256:" + "d" * 64,
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                ),
                row_count=3,
                columns=(
                    SourceDatasetColumn(1, "ref", "partner.ref", "string"),
                    SourceDatasetColumn(2, "name", "partner.name", "string"),
                    SourceDatasetColumn(
                        3, "company", "partner.company", "string"
                    ),
                    SourceDatasetColumn(
                        4, "category", "partner.category", "string"
                    ),
                    SourceDatasetColumn(5, "tags", "partner.tags", "string"),
                    SourceDatasetColumn(
                        6, "parent_ref", "partner.parent_ref", "string"
                    ),
                ),
            ),
        ),
        content_hash="sha256:source-selection",
    )


def _pinned_odoo_inputs() -> tuple[SourceSelection, OdooSchemaCatalog]:
    binding = OdooSourceBinding(
        capture_selection_hash="sha256:" + "1" * 64,
        model="res.partner",
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        connection_target_hash="sha256:" + "2" * 64,
        schema_scope_hash="sha256:" + "3" * 64,
        read_principal_hash="sha256:" + "4" * 64,
        read_permission_hash="sha256:" + "5" * 64,
        context_hash="sha256:" + "6" * 64,
    )
    selection = SourceSelection(
        selection_id="selection:odoo-pinned",
        version=1,
        data_version_id="project:test",
        created_at=NOW,
        created_by="Test operator",
        datasets=(
            SourceDataset(
                dataset_id="dataset:odoo-partners",
                name="res_partner",
                source=binding,
                row_count=3,
                columns=(
                    SourceDatasetColumn(1, "name", "odoo:name", "string"),
                ),
            ),
        ),
        content_hash="sha256:" + "7" * 64,
    )

    def pinned_field(
        name: str,
        *,
        readonly: bool = False,
        computed: bool = False,
    ) -> SchemaField:
        return SchemaField(
            name=name,
            label=name.replace("_", " ").title(),
            type="char",
            required=False,
            readonly=readonly,
            relation=None,
            relation_field=None,
            selection=(),
            stored=True,
            computed=computed,
            has_inverse=False,
            related=False,
            translated=False,
            company_dependent=False,
            searchable=True,
            sortable=True,
            exportable=True,
        )

    schema = OdooSchemaCatalog(
        workspace_id="workspace:test",
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        captured_at=NOW,
        captured_by="Test operator",
        connection_mode="LOCAL",
        database="odoo19_local",
        odoo_version="19.0",
        models=(
            SchemaModel(
                name="res.partner",
                label="Contact",
                fields=(
                    pinned_field("display_name", readonly=True, computed=True),
                    pinned_field("email"),
                    pinned_field("name"),
                ),
            ),
        ),
        content_hash="sha256:" + "8" * 64,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:" + "9" * 64,
        read_principal_hash=binding.read_principal_hash,
        read_permission_hash=binding.read_permission_hash,
        read_context_hash=binding.context_hash,
        connection_target_hash=binding.connection_target_hash,
    )
    return selection, schema


def _field(
    name: str,
    field_type: str = "char",
    *,
    required: bool = False,
    readonly: bool = False,
    relation: str | None = None,
    relation_field: str | None = None,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=name.replace("_", " ").title(),
        type=field_type,
        required=required,
        readonly=readonly,
        relation=relation,
        relation_field=relation_field,
        selection=(),
    )


def _schema_catalog() -> OdooSchemaCatalog:
    return OdooSchemaCatalog(
        workspace_id="workspace:test",
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        captured_at=NOW,
        captured_by="Test operator",
        connection_mode="LOCAL",
        database="odoo19_local",
        odoo_version="19.0",
        models=(
            SchemaModel(
                name="res.company",
                label="Company",
                fields=(
                    _field("name", required=True),
                    _field("x_legacy_code", required=True),
                ),
            ),
            SchemaModel(
                name="res.partner",
                label="Contact",
                fields=(
                    _field(
                        "category_id",
                        "many2one",
                        relation="res.partner.category",
                    ),
                    _field(
                        "child_ids",
                        "one2many",
                        relation="res.partner",
                        relation_field="parent_id",
                    ),
                    _field(
                        "company_id",
                        "many2one",
                        required=True,
                        relation="res.company",
                    ),
                    _field("name", required=True),
                    _field(
                        "parent_id",
                        "many2one",
                        relation="res.partner",
                    ),
                    _field("ref"),
                    _field(
                        "tag_ids",
                        "many2many",
                        relation="res.partner.category",
                    ),
                ),
            ),
            SchemaModel(
                name="res.partner.category",
                label="Contact Tag",
                fields=(
                    _field("code", required=True),
                    _field("name", required=True),
                ),
            ),
        ),
        content_hash="sha256:schema-catalog",
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:read-credential",
        read_principal_hash="sha256:read-principal",
        read_permission_hash="sha256:read-permission",
        read_context_hash="sha256:read-context",
        connection_target_hash="sha256:connection-target",
    )


def _schema_governance(schema: OdooSchemaCatalog) -> SchemaGovernance:
    return SchemaGovernance(
        governance_id="governance:test",
        version=1,
        workspace_id=schema.workspace_id,
        catalog_hash=schema.content_hash,
        permitted_models=tuple(model.name for model in schema.models),
        business_keys=(
            BusinessKeyDefinition(
                key_id="company-code",
                model="res.company",
                key_fields=("x_legacy_code",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            BusinessKeyDefinition(
                key_id="partner-ref-company",
                model="res.partner",
                key_fields=("ref",),
                scope_fields=("company_id",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            BusinessKeyDefinition(
                key_id="category-code",
                model="res.partner.category",
                key_fields=("code",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
        ),
        recorded_at=NOW,
        recorded_by="Test operator",
    )
