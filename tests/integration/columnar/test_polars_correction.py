from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import polars as pl

from impodo.application.correction_service import (
    CorrectionReviewService,
    CorrectionTargetIndexEntry,
    build_completed_load_target_index,
)
from impodo.adapters.polars_correction import (
    CorrectionComparisonError,
    iter_polars_correction_candidate_batches,
    write_polars_correction_candidates,
)
from impodo.adapters.polars_transformation import write_polars_prepared_snapshot
from impodo.domain.compiler.columnar_transformation import (
    ColumnarSupport,
    compile_columnar_transformation_program,
)
from impodo.domain.correction import CorrectionValueKind
from impodo.domain.execution.models import (
    ExecutionRowStatus,
    ExecutionRunStatus,
)
from impodo.domain.execution.odoo_readback import ReadbackRecord
from impodo.domain.mapping.canonicalization import canonicalize_mapping_definition
from impodo.domain.mapping.contracts import (
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.reconciliation import (
    ReconciliationRowStatus,
    ReconciliationRunStatus,
)
from impodo.domain.shared.models import target_record_binding_hash

from tests.integration.columnar.test_polars_transformation import (
    DATASET_ID,
    NOW,
    _definition,
    _rows,
    _selection,
    _write_snapshot,
)


class PolarsCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        selection = _selection()
        dataset = replace(selection.datasets[0], row_count=1)
        self.selection = replace(selection, datasets=(dataset,))
        source_row = _rows()[0]
        clean_row = replace(
            source_row,
            values={
                **source_row.values,
                "required_text": "ok",
                "validated_required": "ABC",
            },
        )
        self.source_path, self.source_snapshot = _write_snapshot(
            self.root,
            self.selection,
            (clean_row,),
        )
        self.previous_definition = _definition(self.selection)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_direct_selection_and_casing_changes_share_one_sparse_diff(self) -> None:
        dataset = self.previous_definition.datasets[0]
        corrected_fields = []
        for field in dataset.fields:
            if field.target_field == "name":
                corrected_fields.append(
                    replace(
                        field,
                        transform=replace(field.transform, case_mode="uppercase"),
                    )
                )
            elif field.target_field == "category":
                corrected_fields.append(
                    replace(
                        field,
                        value_mappings=(
                            ValueMapping("Retail", "consumer"),
                            ValueMapping("Wholesale", "trade"),
                        ),
                    )
                )
            else:
                corrected_fields.append(field)
        corrected_definition = canonicalize_mapping_definition(
            replace(
                self.previous_definition,
                datasets=(replace(dataset, fields=tuple(corrected_fields)),),
            )
        )

        previous_program, previous_path, previous_snapshot = self._prepare(
            self.previous_definition,
            "previous.parquet",
        )
        corrected_program, corrected_path, corrected_snapshot = self._prepare(
            corrected_definition,
            "corrected.parquet",
        )
        with patch(
            "impodo.adapters.polars_correction.pl.scan_parquet",
            wraps=pl.scan_parquet,
        ) as scan_parquet:
            artifact = write_polars_correction_candidates(
                previous_path,
                previous_snapshot,
                previous_program,
                corrected_path,
                corrected_snapshot,
                corrected_program,
                self.root / "candidates.parquet",
            )
        scanned_paths = tuple(
            Path(call.args[0]).resolve() for call in scan_parquet.call_args_list
        )
        self.assertEqual(scanned_paths.count(previous_path.resolve()), 1)
        self.assertEqual(scanned_paths.count(corrected_path.resolve()), 1)
        self.assertEqual(scanned_paths.count(artifact.path), 1)

        candidates = tuple(
            candidate
            for batch in iter_polars_correction_candidate_batches(artifact)
            for candidate in batch
        )
        by_field = {candidate.target_field: candidate for candidate in candidates}
        self.assertEqual(artifact.candidate_count, 2)
        self.assertEqual(set(by_field), {"category", "name"})
        self.assertEqual(
            (by_field["name"].previous, by_field["name"].corrected),
            ("alpha one", "ALPHA ONE"),
        )
        self.assertEqual(
            (by_field["category"].previous, by_field["category"].corrected),
            ("retail", "consumer"),
        )
        self.assertTrue(
            all(
                candidate.value_kind is CorrectionValueKind.SCALAR
                for candidate in candidates
            )
        )

    def test_many2one_key_changes_use_the_same_candidate_contract(self) -> None:
        dataset = self.previous_definition.datasets[0]
        previous_relationship = RelationshipMapping(
            target_field="parent_id",
            kind="many2one",
            source_column_keys=("product.category",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.DATASET,
                dataset_id=DATASET_ID,
            ),
        )
        corrected_relationship = replace(
            previous_relationship,
            source_column_keys=("product.scope",),
        )
        previous_definition = canonicalize_mapping_definition(
            replace(
                self.previous_definition,
                datasets=(
                    replace(dataset, relationships=(previous_relationship,)),
                ),
            )
        )
        corrected_definition = canonicalize_mapping_definition(
            replace(
                self.previous_definition,
                datasets=(
                    replace(dataset, relationships=(corrected_relationship,)),
                ),
            )
        )
        previous_program, previous_path, previous_snapshot = self._prepare(
            previous_definition,
            "previous-relationship.parquet",
        )
        corrected_program, corrected_path, corrected_snapshot = self._prepare(
            corrected_definition,
            "corrected-relationship.parquet",
        )
        artifact = write_polars_correction_candidates(
            previous_path,
            previous_snapshot,
            previous_program,
            corrected_path,
            corrected_snapshot,
            corrected_program,
            self.root / "relationship-candidates.parquet",
        )

        candidates = tuple(
            candidate
            for batch in iter_polars_correction_candidate_batches(artifact)
            for candidate in batch
        )
        relationship = next(
            candidate
            for candidate in candidates
            if candidate.target_field == "parent_id"
        )
        self.assertIs(relationship.value_kind, CorrectionValueKind.MANY2ONE)
        self.assertNotEqual(relationship.previous, relationship.corrected)

    def test_review_reads_exact_ids_and_blocks_relationships_until_qualified(
        self,
    ) -> None:
        dataset = self.previous_definition.datasets[0]
        previous_relationship = RelationshipMapping(
            target_field="parent_id",
            kind="many2one",
            source_column_keys=("product.category",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.DATASET,
                dataset_id=DATASET_ID,
            ),
        )
        corrected_relationship = replace(
            previous_relationship,
            source_column_keys=("product.scope",),
        )
        previous_definition = canonicalize_mapping_definition(
            replace(
                self.previous_definition,
                datasets=(
                    replace(dataset, relationships=(previous_relationship,)),
                ),
            )
        )
        corrected_definition = canonicalize_mapping_definition(
            replace(
                self.previous_definition,
                datasets=(
                    replace(dataset, relationships=(corrected_relationship,)),
                ),
            )
        )
        previous_program, previous_path, previous_snapshot = self._prepare(
            previous_definition,
            "previous-review-relationship.parquet",
        )
        corrected_program, corrected_path, corrected_snapshot = self._prepare(
            corrected_definition,
            "corrected-review-relationship.parquet",
        )
        artifact = write_polars_correction_candidates(
            previous_path,
            previous_snapshot,
            previous_program,
            corrected_path,
            corrected_snapshot,
            corrected_program,
            self.root / "review-relationship-candidates.parquet",
        )
        reader = _FakeReadbackReader({71: {"parent_id": [90, "Retail"]}})
        review = CorrectionReviewService().review(
            iter_polars_correction_candidate_batches(artifact),
            (
                CorrectionTargetIndexEntry(
                    dataset="products",
                    source_row=2,
                    row_id="row-2",
                    target_model="product.template",
                    odoo_id=71,
                    completed_disposition="UPDATE",
                    target_binding_hash=target_record_binding_hash(
                        "product.template",
                        71,
                    ),
                ),
            ),
            reader=reader,
            expected_target_hash=reader.target_hash,
            expected_reader_scope_hash=reader.scope_hash,
        )

        self.assertFalse(review.can_apply)
        self.assertEqual(
            tuple(item.code for item in review.blockers),
            ("RELATIONSHIP_NOT_QUALIFIED",),
        )
        self.assertEqual(reader.calls, [])

    def test_review_classifies_ready_already_corrected_and_conflict_by_exact_id(
        self,
    ) -> None:
        dataset = self.previous_definition.datasets[0]
        corrected_fields = tuple(
            replace(
                field,
                transform=replace(field.transform, case_mode="uppercase"),
            )
            if field.target_field == "name"
            else (
                replace(
                    field,
                    value_mappings=(
                        ValueMapping("Retail", "consumer"),
                        ValueMapping("Wholesale", "trade"),
                    ),
                )
                if field.target_field == "category"
                else field
            )
            for field in dataset.fields
        )
        corrected_definition = canonicalize_mapping_definition(
            replace(
                self.previous_definition,
                datasets=(replace(dataset, fields=corrected_fields),),
            )
        )
        previous_program, previous_path, previous_snapshot = self._prepare(
            self.previous_definition,
            "previous-review.parquet",
        )
        corrected_program, corrected_path, corrected_snapshot = self._prepare(
            corrected_definition,
            "corrected-review.parquet",
        )
        artifact = write_polars_correction_candidates(
            previous_path,
            previous_snapshot,
            previous_program,
            corrected_path,
            corrected_snapshot,
            corrected_program,
            self.root / "review-candidates.parquet",
        )
        target = CorrectionTargetIndexEntry(
            dataset="products",
            source_row=2,
            row_id="row-2",
            target_model="product.template",
            odoo_id=71,
            completed_disposition="UPDATE",
            target_binding_hash=target_record_binding_hash(
                "product.template",
                71,
            ),
        )
        reader = _FakeReadbackReader(
            {71: {"category": "retail", "name": "ALPHA ONE"}}
        )
        review = CorrectionReviewService().review(
            iter_polars_correction_candidate_batches(artifact),
            (target,),
            reader=reader,
            expected_target_hash=reader.target_hash,
            expected_reader_scope_hash=reader.scope_hash,
        )

        self.assertTrue(review.can_apply)
        self.assertEqual(len(review.ready_fields), 1)
        self.assertEqual(
            review.ready_fields[0].decision.candidate.target_field,
            "category",
        )
        self.assertEqual(review.already_corrected_count, 1)
        self.assertEqual(
            reader.calls,
            [("product.template", (71,), ("category", "name"))],
        )

        conflict_reader = _FakeReadbackReader(
            {71: {"category": "manual", "name": "ALPHA ONE"}}
        )
        conflict = CorrectionReviewService().review(
            iter_polars_correction_candidate_batches(artifact),
            (target,),
            reader=conflict_reader,
            expected_target_hash=conflict_reader.target_hash,
            expected_reader_scope_hash=conflict_reader.scope_hash,
        )
        self.assertFalse(conflict.can_apply)
        self.assertEqual(
            tuple(item.code for item in conflict.blockers),
            ("CONCURRENT_FIELD_CHANGE",),
        )

    def test_moving_output_to_another_target_field_fails_closed(self) -> None:
        dataset = self.previous_definition.datasets[0]
        corrected_definition = canonicalize_mapping_definition(
            replace(
                self.previous_definition,
                datasets=(
                    replace(
                        dataset,
                        fields=tuple(
                            replace(field, target_field="display_name")
                            if field.target_field == "name"
                            else field
                            for field in dataset.fields
                        ),
                    ),
                ),
            )
        )
        previous_program, previous_path, previous_snapshot = self._prepare(
            self.previous_definition,
            "previous-scope.parquet",
        )
        corrected_program, corrected_path, corrected_snapshot = self._prepare(
            corrected_definition,
            "corrected-scope.parquet",
        )

        with self.assertRaisesRegex(
            CorrectionComparisonError,
            "preserve the writable target-field scope",
        ):
            write_polars_correction_candidates(
                previous_path,
                previous_snapshot,
                previous_program,
                corrected_path,
                corrected_snapshot,
                corrected_program,
                self.root / "scope-candidates.parquet",
            )

    def test_completed_load_index_joins_unchanged_and_written_exact_ids(self) -> None:
        target_hash = "sha256:" + "9" * 64
        snapshot_hash = "sha256:" + "8" * 64
        root_hash = "sha256:" + "7" * 64
        record_hash = "sha256:" + "6" * 64
        unchanged_binding = target_record_binding_hash("product.template", 70)
        update_binding = target_record_binding_hash("product.template", 71)
        snapshot = SimpleNamespace(
            workspace_id="11111111-1111-4111-8111-111111111111",
            semantic_hash=snapshot_hash,
            root_hash=root_hash,
            preflight_run_id="22222222-2222-4222-8222-222222222222",
            target_hash=target_hash,
            target_database="impodo-test",
            record_snapshot_hash=record_hash,
            rows=(
                SimpleNamespace(
                    row_id="unchanged",
                    dataset="products",
                    source_row=2,
                    target_model="product.template",
                    disposition="UNCHANGED",
                    target_binding_hash=unchanged_binding,
                ),
                SimpleNamespace(
                    row_id="updated",
                    dataset="products",
                    source_row=3,
                    target_model="product.template",
                    disposition="UPDATE",
                    target_binding_hash=update_binding,
                ),
            ),
        )
        execution = SimpleNamespace(
            status=ExecutionRunStatus.COMPLETED,
            workspace_id=snapshot.workspace_id,
            snapshot_hash=snapshot_hash,
            snapshot_root_hash=root_hash,
            preflight_run_id=snapshot.preflight_run_id,
            target_hash=target_hash,
            target_database=snapshot.target_database,
            run_id="33333333-3333-4333-8333-333333333333",
            rows=(
                SimpleNamespace(
                    row_id="updated",
                    dataset="products",
                    source_row=3,
                    target_model="product.template",
                    status=ExecutionRowStatus.COMMITTED,
                    odoo_id=71,
                ),
            ),
        )
        reconciliation = SimpleNamespace(
            status=ReconciliationRunStatus.VERIFIED,
            workspace_id=snapshot.workspace_id,
            execution_run_id=execution.run_id,
            snapshot_hash=snapshot_hash,
            target_hash=target_hash,
            target_database=snapshot.target_database,
            total_count=2,
            rows=(
                SimpleNamespace(
                    row_id="updated",
                    dataset="products",
                    source_row=3,
                    target_model="product.template",
                    status=ReconciliationRowStatus.VERIFIED,
                    odoo_id=71,
                ),
            ),
        )
        records = SimpleNamespace(
            fingerprint=SimpleNamespace(target_hash=target_hash),
            content_hash=record_hash,
            complete=True,
            records={
                "product.template": (
                    SimpleNamespace(odoo_id=70),
                    SimpleNamespace(odoo_id=71),
                )
            },
        )

        entries = build_completed_load_target_index(
            snapshot,
            execution,
            reconciliation,
            records,
        )

        self.assertEqual(
            tuple((item.row_id, item.odoo_id) for item in entries),
            (("unchanged", 70), ("updated", 71)),
        )

    def _prepare(self, definition, filename: str):
        decision = compile_columnar_transformation_program(
            definition,
            self.selection,
            DATASET_ID,
        )
        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        assert decision.program is not None
        path = self.root / filename
        candidate = write_polars_prepared_snapshot(
            self.source_path,
            self.source_snapshot,
            decision.program,
            path,
        )
        snapshot = PreparedSnapshot.create(
            workspace_id="11111111-1111-4111-8111-111111111111",
            dataset_id=self.source_snapshot.dataset_id,
            dataset_name=self.source_snapshot.dataset_name,
            source_snapshot_hash=self.source_snapshot.content_hash,
            mapping_hash=decision.program.mapping_content_hash,
            schema_hash=decision.program.schema_hash,
            transformation_program_hash=decision.program.content_hash,
            row_count=candidate.row_count,
            physical_schema_hash=candidate.physical_schema_hash,
            parquet_sha256=candidate.parquet_sha256,
            created_at=NOW,
        )
        return decision.program, path, snapshot


class _FakeReadbackReader:
    target_hash = "sha256:" + "d" * 64
    scope_hash = "sha256:" + "e" * 64
    imports_external_ids = False

    def __init__(self, values_by_id) -> None:
        self.values_by_id = values_by_id
        self.calls = []

    def read_ids(self, model, identifiers, fields):
        self.calls.append((model, tuple(identifiers), tuple(fields)))
        return tuple(
            ReadbackRecord(
                odoo_id=identifier,
                values={
                    field: self.values_by_id[identifier][field]
                    for field in fields
                },
            )
            for identifier in identifiers
            if identifier in self.values_by_id
        )


if __name__ == "__main__":
    unittest.main()
