"""Qualify current native hashing and active JSON row validation together."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.preparation_stored_run_reader import (
    PreparationStoredRunReader,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.domain.compiler.columnar_transformation import (
    compile_columnar_transformation_program,
)
from impodo.domain.mapping.canonicalization import canonicalize_mapping_definition
from impodo.domain.mapping.contracts import (
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.preparation.normalization import canonical_eligible_dataset_hash
from impodo.domain.preparation.quality import StoredQualityRun
from impodo.domain.preparation.staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    STAGING_CONTRACT_VERSION,
    CanonicalIssue,
    CanonicalRow,
    StagingDatasetRole,
)
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.staging.canonical_projection import canonical_prepared_session_row
from impodo.domain.staging.preparation_session import (
    PreparationSessionBindings,
    PreparedCanonicalProjection,
)
from impodo.domain.staging.transformation_impact import TransformationImpactReport
from impodo.domain.workspace.errors import WorkspaceError
from tests.domain.preparation.test_quality import (
    MAPPING_HASH,
    PHYSICAL_HASH,
    SCHEMA_HASH,
    SOURCE_HASH,
    _workspace_state,
)
from tests.integration.columnar import test_polars_transformation as columnar


class EligibleHashStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = DuckDbWorkspaceDatabase(self.root)
        self.workspace = _workspace_state()
        WorkspaceStateRepository(self.database).initialize_workbench(
            self.workspace, actor=LOCAL_ACTOR
        )
        self.repository = PreparationSessionRepository(self.database)
        self.workspace_id = self.workspace.workspace_id
        self.bindings = PreparationSessionBindings(
            mapping_id="mapping:hash-stream",
            mapping_version=1,
            physical_selection_hash=PHYSICAL_HASH,
            source_selection_hash=PHYSICAL_HASH,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            derived_plan_hash=None,
            compiled_plan_hash=MAPPING_HASH,
            contract_version=STAGING_CONTRACT_VERSION,
            evaluator_version=BROWSER_EVALUATOR_VERSION,
            source_hashes={"contacts": SOURCE_HASH},
        )

    def _json_row(self, ordinal, *, bindings=None):
        bindings = bindings or self.bindings
        return canonical_prepared_session_row(
            dataset="contacts",
            source_row=ordinal + 2,
            target_model="res.partner",
            source_identity=(f"C{ordinal}",),
            target_identity=(f"C{ordinal}",),
            target_scope=(),
            scalar_values={"name": f"Été 夏 {ordinal}\n💡", "absent": None},
            references={},
            issues=(),
            ordinal=ordinal,
            mode="upsert",
            source_hash=SOURCE_HASH,
            source_selection_hash=bindings.source_selection_hash,
            mapping_hash=bindings.mapping_hash,
            schema_hash=bindings.schema_hash,
            field_sources={"name": ("column:name",)},
            physical_dataset_id="dataset:contacts",
        )

    def _finalize(self, session_id, bindings, evidence):
        return self.repository.finalize_direct_session(
            self.workspace_id,
            session_id,
            dataset_evidence=evidence,
            run_issues=(),
            control_totals=(),
            impact_report=TransformationImpactReport(
                mapping_content_hash=bindings.mapping_hash,
                evaluated_count=0,
                changed_count=0,
                fallback_count=0,
                null_count=0,
                invalid_count=0,
                provided_count=0,
                unchanged_count=0,
                rows=(),
                detail_limit=0,
            ),
        )

    def _json_staging(self):
        session = self.repository.begin_direct_session(
            self.workspace_id, self.bindings, actor=LOCAL_ACTOR
        )
        self.repository.append_direct_rows(
            self.workspace_id,
            session.session_id,
            (self._json_row(0), self._json_row(1)),
        )
        return self._finalize(
            session.session_id,
            self.bindings,
            {
                "contacts": (
                    "dataset:contacts",
                    StagingDatasetRole.DIRECT,
                    2,
                    "res.partner",
                )
            },
        )

    def _native_staging(self, *, mixed=False):
        selection = columnar._selection()
        rows = tuple(
            replace(
                columnar._rows()[0],
                number=index + 2,
                values={
                    **columnar._rows()[0].values,
                    "id": f"P{index}",
                    "sku": f"SKU{index}",
                    "name": f'Été-"夏"\\unit\n💡 {index}',
                    "category": "Équipe 夏" if index == 0 else None,
                    "price": "12,3400" if index == 0 else None,
                },
            )
            for index in range(2)
        )
        selection = replace(
            selection, datasets=(replace(selection.datasets[0], row_count=len(rows)),)
        )
        base = columnar._definition(selection)
        definition = canonicalize_mapping_definition(
            replace(
                base,
                datasets=(
                    replace(
                        base.datasets[0],
                        target_scope=(),
                        fields=tuple(
                            field
                            for field in base.datasets[0].fields
                            if field.target_field in {"name", "quantity", "list_price"}
                        ),
                        relationships=(
                            RelationshipMapping(
                                target_field="x_parent_id",
                                kind="many2one",
                                source_column_keys=("product.category",),
                                resolver=RelationshipResolver(
                                    origin=ResolverOrigin.TARGET_CATALOG,
                                    model="x_parent",
                                    key_mappings=(
                                        ReferenceKeyMapping("product.category", "code"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )
        )
        decision = compile_columnar_transformation_program(
            definition, selection, columnar.DATASET_ID
        )
        self.assertIsNotNone(decision.program)
        program = decision.program
        work = self.root / "fixture"
        work.mkdir()
        source_path, source = columnar._write_snapshot(work, selection, rows)
        path, original = columnar._write_prepared_snapshot(
            work, source_path, source, program
        )
        attributes = asdict(original)
        for name in ("workspace_id", "logical_hash", "parquet_storage_key"):
            attributes.pop(name)
        snapshot = PreparedSnapshot.create(workspace_id=self.workspace_id, **attributes)
        artifacts = self.repository._artifacts
        with artifacts.prepare_prepared_snapshot(self.workspace_id) as publication:
            candidate = publication / "prepared.parquet"
            shutil.copyfile(path, candidate)
            artifacts.publish_prepared_snapshot(
                self.workspace_id,
                candidate,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            )
        bindings = replace(
            self.bindings,
            physical_selection_hash=program.source_selection_hash,
            source_selection_hash=program.source_selection_hash,
            mapping_hash=program.mapping_content_hash,
            schema_hash=program.schema_hash,
            compiled_plan_hash=program.content_hash,
            source_hashes={"contacts": SOURCE_HASH, "products": source.content_hash},
        )
        session = self.repository.begin_direct_session(
            self.workspace_id, bindings, actor=LOCAL_ACTOR
        )
        self.repository.bind_prepared_snapshot(
            self.workspace_id, session.session_id, snapshot
        )
        if mixed:
            self.repository.append_direct_rows(
                self.workspace_id,
                session.session_id,
                (self._json_row(0, bindings=bindings),),
            )
        projection = PreparedCanonicalProjection(
            dataset_id=columnar.DATASET_ID,
            dataset="products",
            ordinal_start=int(mixed),
            row_count=len(rows),
            mode=program.target_mode,
            source_hash=source.content_hash,
            physical_dataset_id=columnar.DATASET_ID,
            field_sources={
                "name": ("product.name",),
                "quantity": ("product.quantity",),
                "list_price": ("product.price",),
            },
            program=program,
            set_based_projection=True,
        )
        with artifacts.materialize_prepared_snapshot(
            self.workspace_id,
            snapshot.parquet_storage_key,
            expected_sha256=snapshot.parquet_sha256,
        ) as published_path:
            native = self.repository.append_native_prepared_projection(
                self.workspace_id,
                session.session_id,
                snapshot,
                projection,
                published_path,
            )
        self.assertIsNotNone(native)
        evidence = {
            "products": (
                columnar.DATASET_ID,
                StagingDatasetRole.DIRECT,
                len(rows),
                program.target_model,
            )
        }
        if mixed:
            evidence["contacts"] = (
                "dataset:contacts",
                StagingDatasetRole.DIRECT,
                1,
                "res.partner",
            )
        return self._finalize(session.session_id, bindings, evidence), snapshot

    def _quality(self, staging, eligible_ids):
        return StoredQualityRun(
            workspace_id=self.workspace_id,
            staging_content_hash=staging.content_hash,
            ruleset_hash=MAPPING_HASH,
            mapping_hash=staging.mapping_hash,
            schema_hash=staging.schema_hash,
            retention_context_hash=MAPPING_HASH,
            row_results=(),
            source_accounting=(),
            issues=(),
            quarantine=(),
            effective_dataset_hash=staging.content_hash,
            eligible_row_ids=eligible_ids,
            summary_counts={
                "ready_count": len(eligible_ids),
                "review_count": 0,
                "quarantined_count": 0,
                "excluded_count": len(staging.rows) - len(eligible_ids),
                "blocked_count": 0,
            },
            published_content_hash=content_hash("published-quality"),
        )

    def _assert_hash_parity(self, staging, eligible_ids):
        quality = self._quality(staging, eligible_ids)
        rows = tuple(staging.rows)
        expected = content_hash(
            {
                "effective_dataset_hash": staging.content_hash,
                "quality_content_hash": quality.content_hash,
                "rows": [
                    row.to_portable_dict() for row in rows if row.row_id in eligible_ids
                ],
                "staging_content_hash": staging.content_hash,
            }
        )
        self.assertEqual(canonical_eligible_dataset_hash(staging, quality), expected)
        return rows, quality, expected

    def _execute(self, staging, sql, parameters=()):
        path = (
            self.repository.workspace_directory(self.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(path) as connection:
            connection.execute(sql, [*parameters, staging.rows.canonical_run_id])

    def test_current_native_stream_uses_exact_bytes_without_row_reconstruction(self):
        staging, _snapshot = self._native_staging()
        row_ids = frozenset(row.row_id for row in staging.rows)
        _rows, quality, expected = self._assert_hash_parity(staging, row_ids)
        with patch.object(
            PreparationStoredRunReader,
            "_canonical_row",
            side_effect=AssertionError("Native hash reconstructed a row"),
        ):
            entries = tuple(staging.rows.iter_canonical_hash_rows())
            self.assertEqual(
                canonical_eligible_dataset_hash(staging, quality), expected
            )
        self.assertTrue(all(isinstance(payload, str) for _row_id, payload in entries))
        self.assertEqual({row_id for row_id, _payload in entries}, row_ids)

    def test_active_json_normalizes_defaults_and_uses_json_row_identity(self):
        staging = self._json_staging()
        original = tuple(staging.rows)
        payload = original[0].to_portable_dict()
        payload.pop("target_scope")
        payload["source_row"] = str(payload["source_row"])
        payload["unknown_metadata"] = "This property is not canonical row data"
        self._execute(
            staging,
            "UPDATE canonical_staging_row SET row_json = ?, row_id = ? WHERE ordinal = 0 AND run_id = ?",
            (json.dumps(payload, indent=2), content_hash("different-index-id")),
        )
        entries = tuple(staging.rows.iter_canonical_hash_rows())
        self.assertTrue(
            all(isinstance(value, CanonicalRow) for _row_id, value in entries)
        )
        self.assertEqual(entries[0][0], original[0].row_id)
        self.assertEqual(entries[0][1], original[0])
        self._assert_hash_parity(staging, frozenset((original[0].row_id,)))

    def test_mixed_native_and_json_rows_preserve_order_and_one_hash_contract(self):
        staging, _snapshot = self._native_staging(mixed=True)
        rows = tuple(staging.rows)
        entries = tuple(staging.rows.iter_canonical_hash_rows())
        self.assertIsInstance(entries[0][1], CanonicalRow)
        self.assertTrue(
            all(isinstance(payload, str) for _row_id, payload in entries[1:])
        )
        self.assertEqual(
            [row_id for row_id, _payload in entries], [row.row_id for row in rows]
        )
        self._assert_hash_parity(staging, frozenset((rows[0].row_id, rows[-1].row_id)))

    def test_ineligible_json_is_parsed_and_ordinal_failure_keeps_precedence(self):
        staging = self._json_staging()
        quality = self._quality(staging, frozenset())
        self._execute(
            staging,
            "UPDATE canonical_staging_row SET row_json = '{' WHERE ordinal = 1 AND run_id = ?",
        )
        with self.assertRaisesRegex(WorkspaceError, "canonical row is invalid"):
            canonical_eligible_dataset_hash(staging, quality)
        self._execute(
            staging,
            "UPDATE canonical_staging_row SET ordinal = 11 WHERE ordinal = 1 AND run_id = ?",
        )
        with self.assertRaisesRegex(WorkspaceError, "not contiguous"):
            canonical_eligible_dataset_hash(staging, quality)

    def test_membership_failure_occurs_before_a_later_json_decode(self):
        staging = self._json_staging()
        self._execute(
            staging,
            "UPDATE canonical_staging_row SET row_json = '{' WHERE ordinal = 1 AND run_id = ?",
        )
        failure = RuntimeError("Membership lookup failed")

        class BrokenIds(frozenset):
            def contains_canonical(self, _row_id):
                raise failure

        with self.assertRaises(RuntimeError) as raised:
            canonical_eligible_dataset_hash(
                staging, self._quality(staging, BrokenIds())
            )
        self.assertIs(raised.exception, failure)

    def test_native_issue_overlay_reuses_current_canonical_rows(self):
        staging, _snapshot = self._native_staging()
        issue = CanonicalIssue(
            code="CHECK_WARNING",
            message="Review the prepared value",
            severity="warning",
            dataset="products",
            source_row=2,
            field="name",
        )
        self._execute(
            staging,
            "INSERT INTO canonical_staging_row_issue (run_id, ordinal, issue_ordinal, issue_json) SELECT run_id, ordinal, 0, ? FROM canonical_staging_row WHERE ordinal = 0 AND run_id = ?",
            (json.dumps(issue.to_portable_dict()),),
        )
        entries = tuple(staging.rows.iter_canonical_hash_rows())
        self.assertTrue(
            all(isinstance(payload, CanonicalRow) for _row_id, payload in entries)
        )
        self.assertIn(issue, entries[0][1].issues)
        self._assert_hash_parity(
            staging, frozenset(row_id for row_id, _payload in entries)
        )

    def test_native_artifact_hash_is_still_verified_before_hashing(self):
        staging, snapshot = self._native_staging()
        quality = self._quality(staging, frozenset())
        artifacts = self.repository._artifacts
        path = artifacts._prepared_snapshot_path(
            self.workspace_id, snapshot.parquet_storage_key, create=False
        )
        path.write_bytes(path.read_bytes() + b"corrupt")
        with self.assertRaisesRegex(WorkspaceError, "artifact could not be verified"):
            canonical_eligible_dataset_hash(staging, quality)


if __name__ == "__main__":
    unittest.main()
