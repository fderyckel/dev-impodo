"""Selected Odoo names retain their companies through preparation and comparison."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import duckdb

from impodo.adapters.duckdb.native_prepared_projection import projected_encoded_rows_sql
from impodo.adapters.polars_transformation import (
    iter_polars_prepared_batches,
    prepared_intent_columns,
)
from impodo.application.workspace.mapping.supporting_values import (
    plan_mapping_supporting_values,
)
from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.compiler.columnar_transformation import (
    compile_columnar_transformation_program,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
    matched_reference_components,
)
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import PreparedBundle
from impodo.domain.shared.models import (
    Classification,
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)
from impodo.domain.staging.preparation_session import PreparedCanonicalProjection
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.supporting_lookups import portable_supporting_value
from tests.integration.columnar import test_polars_transformation as fixtures


class ScopedValueMatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.selection = fixtures._selection()
        self.rows = tuple(
            replace(row, values={**row.values, "category": value})
            for row, value in zip(
                fixtures._rows()[:3], ("Retail", "Other", ""), strict=True
            )
        )
        self.selection = replace(
            self.selection,
            datasets=(
                replace(
                    self.selection.datasets[0],
                    row_count=len(self.rows),
                ),
            ),
        )
        definition = fixtures._definition(self.selection)
        relationships = tuple(
            RelationshipMapping(
                target_field=field,
                kind="many2one",
                source_column_keys=("product.category",),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.TARGET_CATALOG,
                    model=model,
                    key_mappings=(ReferenceKeyMapping("product.category", "name"),),
                    scope_mappings=(
                        ReferenceKeyMapping("product.category", "company_id"),
                    ),
                    value_mappings=tuple(
                        ValueMapping(source, portable_supporting_value((name, company)))
                        for source, company in (
                            ("Retail", "United Caps Wiltz"),
                            ("Other", "UNITED CAPS FRANCE"),
                        )
                    ),
                ),
                categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
            )
            for field, model, name in (
                ("property_account_position_id", "account.fiscal.position", "DOM_S"),
                ("property_payment_term_id", "account.payment.term", "30FMNT"),
            )
        )
        mapping = replace(
            definition.datasets[0],
            target_model="res.partner",
            fields=(),
            target_scope=(),
            target_identity=(IdentityComponentMapping(("product.id",), ("ref",)),),
            relationships=relationships,
        )
        self.definition = replace(definition, datasets=(mapping,))

    def _prepare(self):
        expected, _ = fixtures._python_oracle(
            self.definition, self.selection, self.rows
        )
        decision = compile_columnar_transformation_program(
            self.definition, self.selection, fixtures.DATASET_ID
        )
        self.assertIsNotNone(decision.program)
        program = decision.program
        path, snapshot = fixtures._write_snapshot(self.root, self.selection, self.rows)
        prepared_path, prepared = fixtures._write_prepared_snapshot(
            self.root, path, snapshot, program
        )
        actual = tuple(
            record
            for batch in iter_polars_prepared_batches(
                prepared_path,
                prepared,
                snapshot,
                program,
                batch_size=1,
            )
            for record in batch.records
        )
        self.assertEqual(actual, expected)
        return actual, program, snapshot, prepared_path

    def test_python_native_and_persisted_projection_keep_separate_key_and_company(self):
        records, program, snapshot, path = self._prepare()
        self.assertEqual(
            MappingDefinition.from_json(self.definition.to_json()), self.definition
        )
        for field, name in (
            ("property_account_position_id", "DOM_S"),
            ("property_payment_term_id", "30FMNT"),
        ):
            self.assertEqual(records[0].references[field].key, (name,))
            self.assertEqual(records[0].references[field].scope, ("United Caps Wiltz",))
            self.assertEqual(
                records[1].references[field].scope, ("UNITED CAPS FRANCE",)
            )
            self.assertIsNone(records[2].references[field])
        projection = PreparedCanonicalProjection(
            dataset_id=fixtures.DATASET_ID,
            dataset="products",
            ordinal_start=0,
            row_count=3,
            mode=program.target_mode,
            source_hash=snapshot.content_hash,
            physical_dataset_id=fixtures.DATASET_ID,
            field_sources={},
            program=program,
            set_based_projection=True,
        )
        with duckdb.connect() as connection:
            encoded = connection.execute(
                projected_encoded_rows_sql(projection), [str(path), 0, 3]
            ).fetchall()
            for index, row in enumerate(encoded):
                references = json.loads(row[-1])["references"]
                for field in (
                    "property_account_position_id",
                    "property_payment_term_id",
                ):
                    if index == 2:
                        self.assertIsNone(references[field])
                    else:
                        self.assertEqual(
                            references[field]["key"],
                            list(records[index].references[field].key),
                        )
                        self.assertEqual(
                            references[field]["scope"],
                            list(records[index].references[field].scope),
                        )
            for intent in prepared_intent_columns(program):
                self.assertEqual(len(intent.value_aliases), 2)
                self.assertEqual(intent.relationship_scope_fields, ("company_id",))
                values = connection.execute(
                    f'SELECT "{intent.value_aliases[0]}", "{intent.value_aliases[1]}" FROM read_parquet(?)',
                    [str(path)],
                ).fetchall()
                self.assertEqual(values[0][1], "United Caps Wiltz")

    def test_scoped_leave_blank_applies_before_expansion_and_preserves_comparison(self):
        before = self.definition.content_hash
        self.definition = replace(self.definition, datasets=(replace(
            self.definition.datasets[0], relationships=tuple(replace(
                item, blank_source_values=("Other",),
                resolver=replace(item.resolver, value_mappings=tuple(
                    match for match in item.resolver.value_mappings if match.source_value != "Other"
                )),
            ) for item in self.definition.datasets[0].relationships),
        ),))
        expected_hash = self.definition.content_hash
        self.assertNotEqual(before, expected_hash)
        records, _, _, _ = self._prepare()
        self.assertEqual(self.definition.content_hash, expected_hash)
        for relation in self.definition.datasets[0].relationships:
            self.assertIsNotNone(records[0].references[relation.target_field])
            self.assertIsNone(records[1].references[relation.target_field])
            self.assertIsNone(records[2].references[relation.target_field])
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._assert_scoped_comparison(records, major=major, mode=mode)

    def test_selected_target_name_equal_to_a_blank_source_label_is_preserved(self):
        original = self.definition
        root = self.root
        for scoped in (False, True):
            with self.subTest(scoped=scoped):
                self.root = root / str(scoped)
                self.root.mkdir()
                self.definition = replace(original, datasets=(replace(
                    original.datasets[0], relationships=tuple(replace(
                        item, blank_source_values=("Other",), resolver=replace(
                            item.resolver,
                            scope_mappings=item.resolver.scope_mappings if scoped else (),
                            value_mappings=(ValueMapping("Retail", portable_supporting_value(("Other", "United Caps Wiltz")) if scoped else "Other"),),
                        ),
                    ) for item in original.datasets[0].relationships),
                ),))
                records, _, _, _ = self._prepare()
                for relation in self.definition.datasets[0].relationships:
                    reference = records[0].references[relation.target_field]
                    self.assertEqual(reference.key, ("Other",))
                    self.assertEqual(reference.scope, ("United Caps Wiltz",) if scoped else ())
                    self.assertIsNone(records[1].references[relation.target_field])

    def test_comparison_resolves_same_names_in_different_companies_and_blocks_wrong_company(
        self,
    ):
        records, _, _, _ = self._prepare()
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._assert_scoped_comparison(records, major=major, mode=mode)

    def _assert_scoped_comparison(self, records, *, major, mode):
        plan = compile_browser_mapping(self.definition, self.selection)
        fingerprint = TargetFingerprint(
            target_hash=fixtures.HASH_A,
            connection_mode=mode,
            database="scope-test",
            odoo_version=f"{major}.0",
            snapshot_timestamp="2026-11-01T00:00:00Z",
        )
        fields = {
            "ref": FieldMetadata(name="ref", type="char"),
            **{
                relation.target_field: FieldMetadata(
                    name=relation.target_field,
                    type="many2one",
                    relation=relation.resolver.model,
                )
                for relation in self.definition.datasets[0].relationships
            },
        }
        metadata = MetadataSnapshot(
            fingerprint=fingerprint,
            models={
                "res.partner": ModelMetadata(
                    model="res.partner", description="Customers", fields=fields
                ),
                **{
                    model: ModelMetadata(
                        model=model,
                        description=model,
                        fields={
                            "name": FieldMetadata(name="name", type="char"),
                            "company_id": FieldMetadata(
                                name="company_id",
                                type="many2one",
                                relation="res.company",
                            ),
                        },
                    )
                    for model in ("account.fiscal.position", "account.payment.term")
                },
            },
        )
        parents = {
            relation.resolver.model: tuple(
                TargetRecord(
                    model=relation.resolver.model,
                    odoo_id=index,
                    values={"name": name, "company_id": [index, company]},
                )
                for index, company in enumerate(
                    ("United Caps Wiltz", "UNITED CAPS FRANCE"), start=1
                )
            )
            for relation, name in zip(
                self.definition.datasets[0].relationships,
                ("DOM_S", "30FMNT"),
                strict=True,
            )
        }

        def compare(parents):
            return PreflightEngine().run(
                plan,
                PreparedBundle(records, (), {}),
                metadata,
                RecordSnapshot(
                    fingerprint=fingerprint,
                    records={"res.partner": (), **parents},
                    requested_fields={
                        "res.partner": tuple(fields),
                        **{model: ("name", "company_id") for model in parents},
                    },
                ),
            )

        result = compare(parents)
        self.assertTrue(
            all(
                item.classification is Classification.CREATE
                for item in result.decisions
            ),
            result.issues,
        )
        self.assertTrue(
            all(item.status == "RESOLVED" for item in result.reference_resolutions)
        )
        wrong_company = {model: items[1:] for model, items in parents.items()}
        result = compare(wrong_company)
        self.assertEqual(result.decisions[0].classification, Classification.BLOCKED)
        self.assertTrue(
            any(
                item.code == "REFERENCE_NOT_FOUND"
                for item in result.decisions[0].issues
            )
        )
        duplicates = {
            model: (*items, replace(items[0], odoo_id=3))
            for model, items in parents.items()
        }
        result = compare(duplicates)
        self.assertEqual(result.decisions[0].classification, Classification.BLOCKED)
        self.assertTrue(
            any(
                item.code == "REFERENCE_AMBIGUOUS"
                for item in result.decisions[0].issues
            )
        )

    def test_supporting_reads_use_selected_name_company_pairs(self):
        from types import SimpleNamespace

        schema = SimpleNamespace(
            models=(
                SchemaModel(
                    "res.partner",
                    "Customers",
                    tuple(
                        SchemaField(
                            relation.target_field,
                            relation.target_field,
                            "many2one",
                            False,
                            False,
                            relation.resolver.model,
                            None,
                            (),
                        )
                        for relation in self.definition.datasets[0].relationships
                    ),
                ),
            )
        )
        requirements = plan_mapping_supporting_values(self.definition.datasets, schema)
        self.assertEqual(len(requirements), 2)
        for requirement in requirements:
            self.assertEqual(requirement.scope_fields, ("company_id",))
            self.assertEqual(requirement.sources, ())
            self.assertEqual(
                {value[1] for value in requirement.constant_values},
                {"United Caps Wiltz", "UNITED CAPS FRANCE"},
            )

    def test_scoped_choices_reject_missing_or_malformed_components(self):
        for value in ("DOM_S", '["DOM_S"]', '["DOM_S",null]', '["DOM_S",""]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                matched_reference_components(value, 2)
        literal_name = '["DOM_S","United Caps Wiltz"]'
        self.assertEqual(matched_reference_components(literal_name, 1), (literal_name,))

    def test_matching_health_counts_selected_companies_and_unmatched_choices(self):
        from impodo.application.workspace.mapping.order_service import (
            _relationship_source_key_counts,
        )

        relation = self.definition.datasets[0].relationships[0]
        counts, blank, incomplete = _relationship_source_key_counts(
            relation,
            (("Retail",), ("Other",), ("Retail",), ("Unknown",), (None,)),
            target_fields=("name", "company_id"),
            expected_width=2,
        )
        self.assertEqual(
            dict(counts),
            {("DOM_S", "United Caps Wiltz"): 2, ("DOM_S", "UNITED CAPS FRANCE"): 1},
        )
        self.assertEqual((blank, incomplete), (1, 1))
