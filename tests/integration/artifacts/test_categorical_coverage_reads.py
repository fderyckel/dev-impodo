"""Verify frozen manifest and artifact reads for a multi-table coverage check."""

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.application.workspace.mapping import categorical_coverage
from impodo.application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
    CategoricalSourceRepository,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from impodo.domain.mapping.validation.evidence import CategoricalValueCount
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.errors import WorkspaceError
from tests.integration.artifacts.test_grouped_exact_join_preparation import (
    _publish_snapshot,
    _selection,
)


class CategoricalCoverageReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_id = str(uuid4())
        self.artifacts = LocalArtifactStore(
            Path(self.enterContext(TemporaryDirectory()))
        )
        self.physical = _selection()
        self.snapshots = tuple(
            _publish_snapshot(self.artifacts, self.physical, dataset)
            for dataset in self.physical.datasets
        )
        self.selection = replace(
            self.physical,
            datasets=self.physical.datasets[:2],
            content_hash="sha256:" + "a" * 64,
        )
        self.sources = Mock(spec=CategoricalSourceRepository)
        self.sources.get_source_selection.return_value = self.physical
        self.sources.get_current_source_snapshots.return_value = self.snapshots
        self.service = CategoricalCoverageService(self.sources, self.artifacts)
        field = SchemaField(
            "category",
            "Category",
            "selection",
            False,
            False,
            None,
            None,
            (("Alpha", "Alpha"), ("BE", "Belgium")),
        )
        self.schema = SimpleNamespace(
            models=(SchemaModel("x.record", "Record", (field,)),)
        )
        self.definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=self.selection.content_hash,
            schema_hash="sha256:" + "b" * 64,
            datasets=tuple(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="x.record",
                    fields=(
                        ScalarFieldMapping(
                            target_field="category",
                            source_column_key=dataset.columns[1].stable_key,
                            categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
                        ),
                    ),
                )
                for dataset in self.selection.datasets
            ),
        )

    def collect(self):
        return self.service.collect(
            self.workspace_id, self.definition, self.selection, self.schema
        )

    def test_each_collect_reads_metadata_once_and_verifies_only_mapped_artifacts(self):
        selected = self.snapshots[:2]
        evidence_hashes = []
        for manifests in (self.snapshots, tuple(reversed(self.snapshots))):
            self.sources.reset_mock()
            self.sources.get_current_source_snapshots.return_value = manifests
            with (
                patch.object(
                    categorical_coverage,
                    "validate_snapshot_for_dataset",
                    wraps=categorical_coverage.validate_snapshot_for_dataset,
                ) as validate,
                patch.object(
                    self.artifacts,
                    "materialize_source_snapshot",
                    wraps=self.artifacts.materialize_source_snapshot,
                ) as materialize,
            ):
                evidence = self.collect().evidence
            self.sources.get_source_selection.assert_called_once_with(self.workspace_id)
            self.sources.get_current_source_snapshots.assert_called_once_with(
                self.workspace_id
            )
            self.sources.get_mapping_source_selection.assert_not_called()
            self.assertEqual(
                validate.call_args_list,
                [
                    call(self.physical, dataset, snapshot)
                    for dataset, snapshot in zip(
                        self.selection.datasets, selected, strict=True
                    )
                ],
            )
            self.assertEqual(
                materialize.call_args_list,
                [
                    call(
                        self.physical.data_version_id,
                        item.parquet_storage_key,
                        expected_sha256=item.parquet_sha256,
                    )
                    for item in selected
                ],
            )
            self.assertEqual(
                evidence.effective_source_selection_hash, self.selection.content_hash
            )
            self.assertEqual(
                evidence.source_snapshot_hashes,
                tuple(
                    {
                        "dataset_id": item.dataset_id,
                        "logical_hash": item.logical_hash,
                        "parquet_sha256": item.parquet_sha256,
                    }
                    for item in selected
                ),
            )
            expected_values = (("Alpha", "Beta", "Gamma"), ("BE", "FR", "NL"))
            for field, values in zip(
                evidence.field_results, expected_values, strict=True
            ):
                self.assertEqual(
                    field.distinct_values,
                    tuple(CategoricalValueCount((value,), 1) for value in values),
                )
                self.assertEqual(
                    field.uncovered_values, tuple((value,) for value in values[1:])
                )
            evidence_hashes.append(evidence.content_hash)
        self.assertEqual(evidence_hashes[0], evidence_hashes[1])

    def test_next_collect_rejects_changed_bytes_in_the_second_artifact(self):
        self.collect()
        snapshot = self.snapshots[1]
        with self.artifacts.materialize_source_snapshot(
            self.physical.data_version_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as path:
            path.write_bytes(b"changed snapshot")
        with self.assertRaisesRegex(WorkspaceError, "snapshot could not be verified"):
            self.collect()

    def test_duplicate_manifests_for_a_mapped_table_are_rejected(self):
        self.sources.get_current_source_snapshots.return_value = (
            *self.snapshots,
            self.snapshots[0],
        )
        with self.assertRaisesRegex(
            ValueError, "source-snapshot evidence is duplicated"
        ):
            self.collect()
