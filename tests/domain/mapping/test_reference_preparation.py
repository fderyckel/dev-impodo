"""Prepare exact linked references while retaining original row evidence."""

from dataclasses import replace
from pathlib import PurePath
from unittest import TestCase

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ValueMapping,
)
from impodo.domain.mapping.reference_preparation import (
    ReferencePart,
    ReferencePreparation,
    ReferencePreparationError,
    preparation_source_keys,
    prepare_reference_value,
    validate_reference_preparation,
)
from impodo.domain.preparation.source import SourceRow, SourceTable
from impodo.domain.mapping.validation.validator import MappingSemanticValidator
from impodo.domain.staging.evaluator import evaluate_browser_mapping
from tests.domain.mapping.test_validation import (
    _schema_catalog,
    _schema_governance,
    _source_selection,
    _valid_definition,
)


class ReferencePreparationTests(TestCase):
    def formula(self):
        return ReferenceKeyMapping(
            "site",
            "complete_name",
            ReferencePreparation(
                formula='concat(value, "/Stock/", column_5, "/", column_6)',
                bindings=(("column_5", "warehouse"), ("column_6", "bin")),
            ),
        )

    def test_formula_and_parts_preserve_identifiers(self):
        values = {"site": "0WIL", "warehouse": "0100", "bin": "100"}
        expected = "0WIL/Stock/0100/100"
        self.assertEqual(prepare_reference_value(self.formula(), values), expected)
        combined = replace(
            self.formula(),
            preparation=ReferencePreparation(
                parts=(
                    ReferencePart(source_column_key="site"),
                    ReferencePart(literal="/Stock/"),
                    ReferencePart(source_column_key="warehouse"),
                    ReferencePart(literal="/"),
                    ReferencePart(source_column_key="bin"),
                )
            ),
        )
        self.assertEqual(prepare_reference_value(combined, values), expected)
        self.assertEqual(values["warehouse"], "0100")
        self.assertEqual(
            preparation_source_keys(combined), ("site", "warehouse", "bin")
        )

    def test_blank_dependencies_do_not_create_partial_references(self):
        reference = self.formula()
        self.assertIsNone(prepare_reference_value(reference, {}))
        with self.assertRaisesRegex(ReferencePreparationError, "needed.*blank"):
            prepare_reference_value(reference, {"site": "0WIL", "warehouse": "0100"})

    def test_unused_source_and_literal_only_formula_have_no_blank_guard(self):
        reference = ReferenceKeyMapping(
            "unused",
            "code",
            ReferencePreparation(
                formula='concat("prefix/", column_8)', bindings=(("column_8", "code"),)
            ),
        )
        self.assertEqual(preparation_source_keys(reference), ("code",))
        self.assertEqual(
            prepare_reference_value(reference, {"code": "001"}), "prefix/001"
        )
        fixed = replace(reference, preparation=ReferencePreparation(formula='"001"'))
        self.assertEqual(prepare_reference_value(fixed, {}), "001")

    def test_malformed_draft_survives_but_cannot_run(self):
        reference = ReferenceKeyMapping(
            "code", "code", ReferencePreparation(formula="value +")
        )
        self.assertEqual(reference.preparation.formula, "value +")
        with self.assertRaises(ValueError):
            validate_reference_preparation(reference)
        with self.assertRaises(ReferencePreparationError):
            prepare_reference_value(reference, {"code": "001"})

    def test_contract_roundtrip_hash_and_version_guard(self):
        mapping = DatasetMapping(
            "dataset:partners",
            "res.partner",
            relationships=(
                RelationshipMapping(
                    "parent_id",
                    "many2one",
                    ("site",),
                    RelationshipResolver(
                        ResolverOrigin.TARGET_CATALOG,
                        model="res.partner",
                        key_mappings=(self.formula(),),
                    ),
                ),
            ),
        )
        definition = MappingDefinition(
            "test", "sha256:source", "sha256:schema", (mapping,)
        )
        self.assertEqual(MappingDefinition.from_json(definition.to_json()), definition)
        with self.assertRaisesRegex(ValueError, "v25"):
            replace(definition, contract_version=24)
        direct = replace(
            mapping.relationships[0].resolver.key_mappings[0], preparation=None
        )
        plain = replace(
            mapping,
            relationships=(
                replace(
                    mapping.relationships[0],
                    resolver=replace(
                        mapping.relationships[0].resolver, key_mappings=(direct,)
                    ),
                ),
            ),
        )
        historical = replace(definition, contract_version=24, datasets=(plain,))
        self.assertNotIn('"preparation"', historical.to_json())
        self.assertEqual(MappingDefinition.from_json(historical.to_json()), historical)

    def evaluate(self, owner, *, blank=False):
        original = _source_selection()
        source = original.datasets[1]
        selection = replace(
            original, datasets=(source,), content_hash="sha256:" + "a" * 64
        )
        reference = ReferenceKeyMapping(
            "partner.company",
            "code",
            ReferencePreparation(
                formula='concat(value, "/", column_4)',
                bindings=(("column_4", "partner.category"),),
            ),
        )
        resolver = RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG, model="x.related", key_mappings=(reference,)
        )
        component = IdentityComponentMapping(
            ("partner.company",), ("parent_id",), resolver=resolver
        )
        mapping = DatasetMapping(
            source.dataset_id,
            "res.partner",
            source_identity_column_keys=("partner.ref",),
            target_identity=(component,)
            if owner == "identity"
            else (IdentityComponentMapping(("partner.ref",), ("ref",)),),
            target_scope=(component,) if owner == "scope" else (),
            relationships=(
                RelationshipMapping(
                    "parent_id", "many2one", ("partner.company",), resolver
                ),
            )
            if owner == "relationship"
            else (),
            fields=(ScalarFieldMapping("name", "partner.company"),),
        )
        definition = MappingDefinition(
            "test", selection.content_hash, "sha256:" + "b" * 64, (mapping,)
        )
        headers = tuple(column.source_name for column in source.columns)
        values = ("P1", "Partner", "001", None if blank else "0100", "Tag", "Parent")
        table = SourceTable(
            source.name,
            PurePath("fictional.csv"),
            headers,
            (SourceRow(2, dict(zip(headers, values, strict=True))),),
            source.source_evidence_hash,
        )
        return evaluate_browser_mapping(
            workspace_id="workspace",
            definition=definition,
            physical_selection=selection,
            effective_selection=selection,
            plan=None,
            loaded_tables={source.dataset_id: table},
            collect_transformation_impact=True,
        )

    def test_row_runtime_prepares_each_owner_without_overwriting_scalar(self):
        for owner in ("identity", "scope", "relationship"):
            with self.subTest(owner=owner):
                result = self.evaluate(owner)
                row = result.canonical_run.rows[0]
                reference = (
                    row.references["parent_id"]
                    if owner == "relationship"
                    else row.target_scope[0]
                    if owner == "scope"
                    else row.target_identity[0]
                )
                self.assertEqual(reference.key, ("001/0100",))
                self.assertEqual(row.proposed_values["name"], "001")
                self.assertEqual(row.source_identity, ("P1",))
                self.assertEqual(row.issues, ())

    def test_invalid_prepared_reference_blocks_each_owner(self):
        for owner in ("identity", "scope", "relationship"):
            with self.subTest(owner=owner):
                row = self.evaluate(owner, blank=True).canonical_run.rows[0]
                self.assertIn(
                    "REFERENCE_PREPARATION_INVALID",
                    {issue.code for issue in row.issues},
                )

    def test_both_odoo_versions_validate_preparation_and_source_bindings(self):
        selection = _source_selection()
        for major in (19, 20):
            schema = replace(_schema_catalog(), odoo_version=f"{major}.0")
            governance = _schema_governance(schema)
            definition = _valid_definition(selection, governance)
            company, partner = definition.datasets
            relation = partner.relationships[0]
            for formula, extra_key, expected_code in (
                ('concat(value, "/", column_6)', "partner.parent_ref", None),
                (
                    'concat(value, "/", column_6)',
                    "missing",
                    "MAPPING_SOURCE_COLUMN_UNKNOWN",
                ),
                (
                    '__import__("os")',
                    "partner.parent_ref",
                    "MAPPING_REFERENCE_PREPARATION_INVALID",
                ),
            ):
                with self.subTest(major=major, formula=formula, extra_key=extra_key):
                    prepared = replace(
                        relation.resolver.key_mappings[0],
                        preparation=ReferencePreparation(
                            formula=formula, bindings=(("column_6", extra_key),)
                        ),
                    )
                    changed = replace(
                        partner,
                        relationships=(
                            replace(
                                relation,
                                resolver=replace(
                                    relation.resolver, key_mappings=(prepared,)
                                ),
                            ),
                            *partner.relationships[1:],
                        ),
                    )
                    result = MappingSemanticValidator().validate(
                        replace(definition, datasets=(company, changed)),
                        selection,
                        schema,
                        governance,
                    )
                    if expected_code is None:
                        self.assertEqual(result.issues, ())
                    else:
                        self.assertIn(
                            expected_code, {issue.code for issue in result.issues}
                        )

    def test_preparation_rejects_incoming_fallback_and_reviewed_choice_override(self):
        selection, schema = _source_selection(), _schema_catalog()
        governance = _schema_governance(schema)
        definition = _valid_definition(selection, governance)
        company, partner = definition.datasets
        relation = partner.relationships[0]
        reference = replace(
            relation.resolver.key_mappings[0],
            preparation=ReferencePreparation(formula='concat("prefix/", value)'),
        )
        for change in (
            {
                "origin": ResolverOrigin.TARGET_THEN_DATASET,
                "dataset_id": partner.dataset_id,
            },
            {"value_mappings": (ValueMapping("source", "destination"),)},
        ):
            with self.subTest(change=change):
                resolver = replace(
                    relation.resolver, key_mappings=(reference,), **change
                )
                changed = replace(
                    partner,
                    relationships=(
                        replace(relation, resolver=resolver),
                        *partner.relationships[1:],
                    ),
                )
                result = MappingSemanticValidator().validate(
                    replace(definition, datasets=(company, changed)),
                    selection,
                    schema,
                    governance,
                )
                self.assertIn(
                    "MAPPING_REFERENCE_PREPARATION_INVALID",
                    {item.code for item in result.issues},
                )
