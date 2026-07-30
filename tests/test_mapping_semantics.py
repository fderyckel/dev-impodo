from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import unittest

from impodo.mapping_semantics import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    MappingCompiler,
    MappingSemanticValidator,
    MappingValidationResult,
    MappingValidationStatus,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarTransformPolicy,
    ScalarValueError,
    ScalarValueSource,
    SchemaGovernance,
    canonicalize_scalar_value,
)
from impodo.workspace import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


class MappingSemanticValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = _source_selection()
        self.schema = _schema_catalog()
        self.governance = _schema_governance(self.schema)
        self.validator = MappingSemanticValidator()

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
        reversed_definition = replace(
            definition,
            datasets=tuple(reversed(definition.datasets)),
        )
        self.assertEqual(
            definition.content_hash,
            reversed_definition.content_hash,
        )
        self.assertEqual(
            MappingCompiler().compile(reversed_definition).definition.datasets,
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
                "TARGET_IDENTITY_UNIQUENESS",
            },
        )

    def test_unsafe_relationships_and_dependency_cycles_are_blocking(
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
        self.assertIn("MAPPING_DEPENDENCY_CYCLE", codes)
        self.assertIn("MAPPING_ONE2MANY_OWNER_INVALID", codes)
        self.assertIn("MAPPING_RELATED_MODEL_INCORRECT", codes)
        self.assertIn("MAPPING_RELATION_POLICY_UNSAFE", codes)
        self.assertIn("MAPPING_BUSINESS_KEY_NOT_GOVERNED", codes)

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

    def test_odoo_default_is_explicit_and_requires_warning_acknowledgement(
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

        result = self.validator.validate(
            definition,
            self.selection,
            self.schema,
            self.governance,
        )

        self.assertEqual(
            result.status,
            MappingValidationStatus.VALID_WITH_WARNINGS,
        )
        self.assertEqual(
            [item.code for item in result.issues],
            ["MAPPING_ODOO_DEFAULT_UNVERIFIED"],
        )

    def test_version_two_mapping_hash_remains_readable(self) -> None:
        current = _valid_definition(self.selection, self.governance)
        version_two = replace(current, contract_version=2)

        payload = version_two.to_dict()
        serialized_field = payload["datasets"][0]["fields"][0]

        self.assertNotIn("value_source", serialized_field)
        self.assertNotIn("literal_value", serialized_field)
        self.assertNotIn("transform", serialized_field)
        self.assertEqual(
            MappingDefinition.from_dict(payload),
            version_two,
        )


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
        project_id="project:test",
        created_at=NOW,
        created_by="Test operator",
        datasets=(
            SourceDataset(
                dataset_id="dataset:companies",
                name="companies",
                file_id="file:companies",
                table_key="csv",
                source_sha256="a" * 64,
                catalog_hash="sha256:companies",
                encoding="utf-8",
                delimiter=",",
                header_row=1,
                row_count=2,
                columns=(
                    SourceDatasetColumn(1, "code", "company.code", "string"),
                    SourceDatasetColumn(2, "name", "company.name", "string"),
                ),
            ),
            SourceDataset(
                dataset_id="dataset:partners",
                name="partners",
                file_id="file:partners",
                table_key="csv",
                source_sha256="b" * 64,
                catalog_hash="sha256:partners",
                encoding="utf-8",
                delimiter=",",
                header_row=1,
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
        project_id="project:test",
        target_hash="sha256:target",
        captured_at=NOW,
        captured_by="Test operator",
        environment="DEV",
        database="odoo19_dev",
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
    )


def _schema_governance(schema: OdooSchemaCatalog) -> SchemaGovernance:
    return SchemaGovernance(
        governance_id="governance:test",
        version=1,
        project_id=schema.project_id,
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
