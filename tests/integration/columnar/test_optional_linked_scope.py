"""Preserve exact absent relationships across native preparation and Odoo reads."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from uuid import uuid4

from impodo.adapters.polars_transformation import iter_polars_prepared_batches
from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.application.recipe_compilation_service import RecipeCompiler
from impodo.application.workspace.execution.service import execution_api_scope
from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.compiler.columnar_transformation import (
    ColumnarSupport,
    ColumnarTransformationProgram,
    compile_columnar_transformation_program,
)
from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.execution.planner import plan_record_requests
from impodo.domain.execution_snapshot import build_execution_snapshot
from impodo.domain.mapping.contracts import IdentityNullPolicy, MappingDefinition
from impodo.domain.mapping.reference_preparation import ReferencePreparation
from impodo.domain.odoo.contracts import (
    MetadataSnapshot,
    RecordSnapshot,
    bind_snapshot_hashes,
)
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import PreparedBundle, SourceRow
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    target_record_binding_hash,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from tests.application.workspace.execution import test_service as execution_fixtures
from tests.application.workspace.mapping.test_optional_linked_scope import (
    business_keys,
    schema_models,
)
from tests.domain.recipe.test_representative_shapes import _field, _publish
from tests.integration.columnar import test_polars_transformation as fixtures
from tests.support.optional_scope import optional_scope_fixture
from tests.support.practice_reference_cases import _frozen


class OptionalLinkedScopeTests(TestCase):
    def prepare(self, *, preparation=None, rows=None, case=None):
        selection, definition, defaults = case or optional_scope_fixture(
            preparation=preparation
        )
        rows = rows or defaults
        expected, _report = fixtures._python_oracle(definition, selection, rows)
        decision = compile_columnar_transformation_program(
            definition, selection, selection.datasets[0].dataset_id
        )
        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        program = decision.program
        assert program is not None
        self.assertEqual(
            ColumnarTransformationProgram.from_portable_dict(
                program.to_portable_dict()
            ),
            program,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path, source = fixtures._write_snapshot(root, selection, rows)
            prepared_path, prepared = fixtures._write_prepared_snapshot(
                root, path, source, program
            )
            actual = tuple(
                record
                for batch in iter_polars_prepared_batches(
                    prepared_path,
                    prepared,
                    source,
                    program,
                    batch_size=1,
                )
                for record in batch.records
            )
        self.assertEqual(actual, tuple(expected))
        return selection, definition, actual

    def test_blank_and_two_batches_prepare_natively_without_changing_source(self):
        for preparation in (
            None,
            ReferencePreparation(formula="concat(value)"),
            ReferencePreparation(
                formula='concat(value, "-", column_4)',
                bindings=(("column_4", "company"),),
            ),
        ):
            with self.subTest(preparation=preparation):
                _selection, _definition, records = self.prepare(preparation=preparation)
                self.assertEqual([record.issues for record in records], [(), (), ()])
                self.assertEqual(records[0].target_scope, ("L", None))
                suffix = (
                    "-C" if preparation is not None and preparation.bindings else ""
                )
                self.assertEqual(records[1].target_scope[1].key, ("LOT-001" + suffix,))
                self.assertEqual(records[2].target_scope[1].key, ("LOT-002" + suffix,))
                self.assertEqual(records[1].target_scope[1].scope, ("C",))

    def test_nonblank_reference_with_missing_context_still_fails(self):
        _selection, _definition, rows = optional_scope_fixture()
        row = SourceRow(8, {**rows[1].values, "company": None})
        _selection, _definition, records = self.prepare(rows=(row,))
        self.assertTrue(records[0].issues)

    def test_default_policy_still_rejects_blank_scope(self):
        selection, definition, rows = optional_scope_fixture()
        mapping = definition.datasets[0]
        strict = replace(
            mapping.target_scope[-1],
            null_policy=IdentityNullPolicy.REJECT,
            blank_source_column_key=None,
        )
        definition = replace(
            definition,
            datasets=(
                replace(mapping, target_scope=(mapping.target_scope[0], strict)),
            ),
        )
        records, _report = fixtures._python_oracle(definition, selection, rows)
        self.assertTrue(records[0].issues)

    def test_contracts_keep_the_absence_source_and_reject_older_versions(self):
        selection, definition, _rows = optional_scope_fixture()
        self.assertEqual(MappingDefinition.from_json(definition.to_json()), definition)
        with self.assertRaisesRegex(ValueError, "v26"):
            replace(definition, contract_version=25)
        plan = compile_browser_mapping(definition, selection)
        self.assertEqual(plan.contract_version, 5)
        self.assertEqual(CompiledMigrationPlan.from_json(plan.to_json()), plan)
        with self.assertRaisesRegex(ValueError, "v5"):
            CompiledMigrationPlan.model_validate(
                {**plan.model_dump(), "contract_version": 4, "compiler_version": 5}
            )

    def test_recipe_rebinds_absence_source_after_column_reordering(self):
        selection, definition, _rows = optional_scope_fixture()
        compiler = object.__new__(RecipeCompiler)
        _sources, datasets, columns = compiler._source_identity(selection.datasets)
        payload = compiler._mapping(definition.datasets, datasets, columns, {})
        source = replace(
            selection.datasets[0],
            dataset_id="new-stock",
            columns=tuple(
                replace(column, ordinal=index, stable_key=f"new:{column.source_name}")
                for index, column in enumerate(
                    reversed(selection.datasets[0].columns), 1
                )
            ),
        )
        fresh = replace(selection, datasets=(source,))
        _sources, datasets, columns = compiler._source_identity(fresh.datasets)
        bindings = {logical: physical for physical, logical in datasets.items()}
        bindings.update({logical: physical[1] for physical, logical in columns.items()})
        actual = RecipeApplicationCompiler()._mapping_datasets(
            {"mapping": payload, "control_definitions": {}},
            bindings,
            fresh,
            None,
            None,
        )[0]
        self.assertEqual(actual.target_scope[-1].blank_source_column_key, "new:lot")
        self.assertEqual(
            actual.target_scope[-1].null_policy, IdentityNullPolicy.OPTIONAL_SCOPE_NULL
        )

    def test_recipe_publication_declares_the_new_matching_contract(self):
        selection, definition, _rows = optional_scope_fixture()
        stock, batch = schema_models()
        stock = replace(stock, fields=(*stock.fields, _field("quantity", "integer")))
        recipe = _publish(
            base_selection=selection,
            mapping_selection=selection,
            mappings=definition.datasets,
            models=(stock, batch),
            business_keys=business_keys(),
        )
        self.assertEqual(recipe["contract_versions"]["mapping_recipe"], 5)

    def test_newly_required_target_metadata_rejects_the_empty_scope_policy(self):
        from impodo.domain.data_version.metadata import validate_plan_metadata

        selection, definition, _rows = optional_scope_fixture()
        plan = compile_browser_mapping(definition, selection)
        fingerprint = TargetFingerprint(
            fixtures.HASH_A, "LOCAL", "fictional", "20.0", "2026-11-16T00:00:00Z"
        )
        metadata = MetadataSnapshot(
            fingerprint,
            {
                "x.stock": ModelMetadata(
                    "x.stock",
                    "Stock",
                    {
                        "code": FieldMetadata("code", "char"),
                        "location": FieldMetadata("location", "char"),
                        "quantity": FieldMetadata("quantity", "integer"),
                        "batch_id": FieldMetadata(
                            "batch_id", "many2one", relation="x.batch", required=True
                        ),
                    },
                ),
                "x.batch": ModelMetadata(
                    "x.batch",
                    "Batch",
                    {
                        "name": FieldMetadata("name", "char"),
                        "company": FieldMetadata("company", "char"),
                    },
                ),
            },
        )
        issues, _coverage = validate_plan_metadata(plan, metadata)
        self.assertIn(
            "TARGET_IDENTITY_NULL_POLICY_INVALID", {issue.code for issue in issues}
        )

    def test_exact_null_domain_and_comparison_for_both_majors_and_access_modes(self):
        selection, definition, records = self.prepare()
        plan = compile_browser_mapping(definition, selection)
        requests = plan_record_requests(plan, records)
        stock_domains = [
            request.domain for request in requests if request.model == "x.stock"
        ]
        self.assertIn("['batch_id', '=', False]", str(stock_domains))
        self.assertNotIn("['batch_id.name', '=', False]", str(stock_domains))
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    fingerprint = TargetFingerprint(
                        fixtures.HASH_A,
                        mode,
                        "fictional",
                        f"{major}.0",
                        "2026-11-16T00:00:00Z",
                    )
                    metadata = MetadataSnapshot(
                        fingerprint,
                        {
                            "x.stock": ModelMetadata(
                                "x.stock",
                                "Stock",
                                {
                                    "code": FieldMetadata("code", "char"),
                                    "location": FieldMetadata("location", "char"),
                                    "quantity": FieldMetadata("quantity", "integer"),
                                    "batch_id": FieldMetadata(
                                        "batch_id", "many2one", relation="x.batch"
                                    ),
                                },
                            ),
                            "x.batch": ModelMetadata(
                                "x.batch",
                                "Batch",
                                {
                                    "name": FieldMetadata("name", "char"),
                                    "company": FieldMetadata("company", "char"),
                                },
                            ),
                        },
                    )
                    snapshot = RecordSnapshot(
                        fingerprint,
                        {
                            "x.stock": tuple(
                                TargetRecord(
                                    "x.stock",
                                    index,
                                    {
                                        "code": "P",
                                        "location": "L",
                                        "batch_id": batch,
                                        "quantity": 0,
                                    },
                                )
                                for index, batch in enumerate((False, 21, 22), 11)
                            ),
                            "x.batch": tuple(
                                TargetRecord(
                                    "x.batch", index, {"name": name, "company": "C"}
                                )
                                for index, name in ((21, "LOT-001"), (22, "LOT-002"))
                            ),
                        },
                        requested_fields={
                            "x.stock": ("code", "location", "batch_id", "quantity"),
                            "x.batch": ("name", "company"),
                        },
                    )
                    metadata, snapshot = bind_snapshot_hashes(metadata, snapshot)
                    result = PreflightEngine().run(
                        plan, PreparedBundle(records, (), {}), metadata, snapshot
                    )
                    self.assertEqual(
                        [record.target_binding_hash for record in result.decisions],
                        [
                            target_record_binding_hash("x.stock", index)
                            for index in (11, 12, 13)
                        ],
                    )
                    frozen = _frozen(plan, PreparedBundle(records, (), {}))
                    execution_snapshot = build_execution_snapshot(
                        preflight_run_id=str(uuid4()), frozen=frozen, result=result
                    )
                    self.assertEqual(
                        execution_snapshot.rows[0].business_scope, ("L", None)
                    )
                    self.assertEqual(result.counts["UPDATE"], 3)
                    scope = execution_api_scope(execution_snapshot)

                    class Executor(execution_fixtures._Executor):
                        def find_ids_many(self, model, domains):
                            return tuple(
                                self.find_ids(model, domain) for domain in domains
                            )

                        def find_ids(self, model, domain):
                            self.lookups.append((model, tuple(domain)))
                            conditions = {
                                field: value for field, _operator, value in domain
                            }
                            if model == "x.batch":
                                return (
                                    {"LOT-001": 21, "LOT-002": 22}[conditions["name"]],
                                )
                            batch = conditions.get("batch_id")
                            if batch is None:
                                self.assert_empty_scope = (
                                    "batch_id",
                                    "=",
                                    None,
                                ) in domain
                                return (11,)
                            return ({21: 12, 22: 13}[batch],)

                    executor = Executor(scope.semantic_hash)
                    executor.target_hash = execution_snapshot.target_hash
                    service, journal = (
                        execution_fixtures.ExecutionServiceTests()._service(
                            execution_snapshot, mode=OdooConnectionMode(mode)
                        )
                    )
                    run = service.execute(
                        execution_snapshot.workspace_id,
                        expected_snapshot_hash=execution_snapshot.semantic_hash,
                        executor=executor,
                        actor=LOCAL_ACTOR,
                    )
                    self.assertEqual(run.status.value, "COMPLETED")
                    self.assertEqual(
                        sorted(executor.updates),
                        [
                            ("x.stock", index, {"quantity": 10})
                            for index in (11, 12, 13)
                        ],
                    )
                    self.assertTrue(executor.assert_empty_scope)
                    self.assertEqual(journal.run.committed_count, 3)
