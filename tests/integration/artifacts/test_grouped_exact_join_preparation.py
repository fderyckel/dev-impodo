from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.exact_join_group_evaluator import (
    DuckDbExactJoinGroupEvaluator,
)
from impodo.adapters.duckdb.grouped_exact_join_source import (
    DuckDbGroupedExactJoinSource,
)
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.application.data_version.source_snapshots import (
    SourceSnapshotCandidateWriter,
    source_snapshot_schema,
)
from impodo.application.workspace.derived_entities import (
    DerivedEntityWorkspaceService,
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
)
from impodo.application.workspace.preparation.grouped_exact_join_preparation import (
    bounded_exact_join_group,
    prepare_bounded_grouped_exact_join_session,
)
from impodo.application.workspace.preparation.preparation_capability import (
    compile_preparation_capability,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from impodo.domain.preparation.source import SourceRow
from impodo.domain.preparation.staging_contracts import StagingDatasetRole
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    ExactJoinInputSnapshotEvidence,
    SavedExactJoinReview,
    SourceDatasetUse,
    SourceDatasetUsePolicy,
    mapping_source_selection,
)
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus

_SCHEMA_HASH = "sha256:" + "9" * 64


class GroupedExactJoinPreparationTests(unittest.TestCase):
    def test_three_table_join_prepares_generic_model_with_complete_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = DuckDbWorkspaceDatabase(root / "databases")
            artifacts = LocalArtifactStore(root / "artifacts")
            workspace = WorkspaceState(
                workspace_id=str(uuid4()),
                name="Generic grouped preparation",
                source_system="CSV",
                status=WorkspaceStatus.REGISTERED,
                registered_at=datetime.now(UTC),
            )
            WorkspaceStateRepository(database).initialize_workbench(
                workspace,
                actor=LOCAL_ACTOR,
            )
            selection = _selection()
            snapshots = tuple(
                _publish_snapshot(artifacts, selection, dataset)
                for dataset in selection.datasets
            )
            primary, first, second = selection.datasets
            request = ExactJoinGroupRequest(
                output_dataset_name="records_complete",
                primary_dataset_id=primary.dataset_id,
                primary_key_column_keys=("key",),
                key_value_types=("integer",),
                additions=(
                    ExactJoinGroupAddition(first.dataset_id, ("key",)),
                    ExactJoinGroupAddition(second.dataset_id, ("key",)),
                ),
                projections=(
                    ExactJoinGroupProjection(primary.dataset_id, "key", "Record ID"),
                    ExactJoinGroupProjection(primary.dataset_id, "name", "Name"),
                    ExactJoinGroupProjection(first.dataset_id, "region", "Region"),
                    ExactJoinGroupProjection(second.dataset_id, "email", "Email"),
                ),
            )
            rules = DerivedEntityWorkspaceService._join_group_rules(
                request,
                selection,
                None,
            )
            snapshots_by_id = {item.dataset_id: item for item in snapshots}
            with ExitStack() as stack:
                paths = {
                    snapshot.dataset_id: stack.enter_context(
                        artifacts.materialize_source_snapshot(
                            selection.data_version_id,
                            snapshot.parquet_storage_key,
                            expected_sha256=snapshot.parquet_sha256,
                        )
                    )
                    for snapshot in snapshots
                }
                preview = DuckDbExactJoinGroupEvaluator(database).review(
                    request=request,
                    rules=rules,
                    selection=selection,
                    snapshots=snapshots_by_id,
                    paths=paths,
                )
            self.assertTrue(preview.can_save)
            evidence = tuple(
                ExactJoinInputSnapshotEvidence(
                    dataset_id=snapshot.dataset_id,
                    logical_hash=snapshot.logical_hash,
                    parquet_sha256=snapshot.parquet_sha256,
                )
                for snapshot in snapshots
            )
            plan = DerivedEntityPlan(
                plan_id=str(uuid4()),
                version=1,
                workspace_id=workspace.workspace_id,
                source_selection_hash=selection.content_hash,
                rules=rules,
                updated_at=datetime.now(UTC),
                updated_by="Test operator",
                dataset_uses=tuple(
                    SourceDatasetUsePolicy(
                        dataset.dataset_id,
                        SourceDatasetUse.PREPARATION_ONLY,
                    )
                    for dataset in selection.datasets
                ),
                join_reviews=tuple(
                    SavedExactJoinReview(
                        rule_id=rule.rule_id,
                        rule_hash=content_hash(rule.to_dict()),
                        source_selection_hash=selection.content_hash,
                        input_snapshots=evidence,
                        review=review,
                    )
                    for rule, review in zip(rules, preview.reviews, strict=True)
                ),
            )
            effective = mapping_source_selection(selection, plan)
            self.assertEqual(len(effective.datasets), 1)
            output = effective.datasets[0]
            by_name = {column.source_name: column for column in output.columns}
            definition = MappingDefinition(
                mapping_id=str(uuid4()),
                source_selection_hash=effective.content_hash,
                schema_hash=_SCHEMA_HASH,
                datasets=(
                    DatasetMapping(
                        dataset_id=output.dataset_id,
                        target_model="x_generic.record",
                        source_identity_column_keys=(
                            by_name["Record ID"].stable_key,
                        ),
                        target_identity=(
                            IdentityComponentMapping(
                                source_column_keys=(
                                    by_name["Record ID"].stable_key,
                                ),
                                target_fields=("external_key",),
                                value_type="integer",
                            ),
                        ),
                        fields=(
                            ScalarFieldMapping(
                                target_field="display_name",
                                source_column_key=by_name["Name"].stable_key,
                            ),
                            ScalarFieldMapping(
                                target_field="region_code",
                                source_column_key=by_name["Region"].stable_key,
                            ),
                            ScalarFieldMapping(
                                target_field="email_address",
                                source_column_key=by_name["Email"].stable_key,
                            ),
                        ),
                    ),
                ),
            )
            capability = compile_preparation_capability(
                definition=definition,
                physical_selection=selection,
                effective_selection=effective,
                source_snapshots=snapshots,
                derived_plan=plan,
                current_ruleset=None,
                reference_bundle=None,
            )
            self.assertTrue(capability.admitted)
            self.assertIsNotNone(
                bounded_exact_join_group(selection, effective, plan)
            )
            sessions = PreparationSessionRepository(database, artifacts)
            prepared = prepare_bounded_grouped_exact_join_session(
                workspace,
                definition,
                1,
                selection,
                effective,
                plan,
                artifacts,
                None,
                sessions,
                DuckDbGroupedExactJoinSource(database),
                actor=LOCAL_ACTOR,
                source_snapshots=snapshots,
            )

            rows = tuple(prepared.run.rows)
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row.target_model == "x_generic.record" for row in rows))
            self.assertTrue(all(len(row.lineage.physical_sources) == 3 for row in rows))
            self.assertEqual(prepared.run.derived_plan_hash, plan.content_hash)
            self.assertEqual(len(prepared.run.datasets), 1)
            dataset_evidence = prepared.run.datasets[0]
            self.assertEqual(dataset_evidence.role, StagingDatasetRole.JOIN)
            self.assertEqual(dataset_evidence.input_rows, 9)
            self.assertEqual(dataset_evidence.input_rows_used, 9)


def _selection() -> SourceSelection:
    definitions = (
        (
            "primary",
            (
                SourceDatasetColumn(1, "Record ID", "key", "integer"),
                SourceDatasetColumn(2, "Name", "name", "string"),
            ),
        ),
        (
            "regions",
            (
                SourceDatasetColumn(1, "Record ID", "key", "integer"),
                SourceDatasetColumn(2, "Region", "region", "string"),
            ),
        ),
        (
            "emails",
            (
                SourceDatasetColumn(1, "Record ID", "key", "integer"),
                SourceDatasetColumn(2, "Email", "email", "string"),
            ),
        ),
    )
    datasets = tuple(
        SourceDataset(
            dataset_id=f"dataset:{index:024x}",
            name=name,
            source=FileSourceBinding(
                file_id=str(uuid4()),
                table_key=f"{name}.csv",
                source_sha256=f"{index:064x}",
                catalog_hash="sha256:" + f"{index + 10:064x}",
                encoding="utf-8",
                delimiter=",",
                header_row=1,
            ),
            row_count=3,
            columns=columns,
        )
        for index, (name, columns) in enumerate(definitions, start=1)
    )
    return SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        created_at=datetime.now(UTC),
        created_by="Test operator",
        datasets=datasets,
        content_hash="sha256:" + "3" * 64,
    )


def _publish_snapshot(
    artifacts: LocalArtifactStore,
    selection: SourceSelection,
    dataset: SourceDataset,
) -> SourceSnapshot:
    schema = source_snapshot_schema(dataset)
    values = {
        "primary": ((1, "Alpha"), (2, "Beta"), (3, "Gamma")),
        "regions": ((1, "BE"), (2, "FR"), (3, "NL")),
        "emails": (
            (1, "alpha@example.test"),
            (2, "beta@example.test"),
            (3, "gamma@example.test"),
        ),
    }[dataset.name]
    with artifacts.prepare_source_snapshot(selection.data_version_id) as workspace:
        writer = SourceSnapshotCandidateWriter(workspace, schema, batch_rows=3)
        writer.append_source_rows(
            tuple(
                SourceRow(
                    number=index,
                    values={
                        dataset.columns[0].source_name: key,
                        dataset.columns[1].source_name: value,
                    },
                )
                for index, (key, value) in enumerate(values, start=1)
            )
        )
        candidate = writer.finalize()
        snapshot = SourceSnapshot.create(
            data_version_id=selection.data_version_id,
            dataset_id=dataset.dataset_id,
            dataset_name=dataset.name,
            source=dataset.source,
            physical_selection_hash=selection.content_hash,
            schema=schema,
            row_count=candidate.row_count,
            data_logical_hash=candidate.data_logical_hash,
            parquet_sha256=candidate.parquet_sha256,
            created_at=selection.created_at,
        )
        artifacts.publish_source_snapshot(
            selection.data_version_id,
            candidate.path,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        )
    return snapshot


if __name__ == "__main__":
    unittest.main()
