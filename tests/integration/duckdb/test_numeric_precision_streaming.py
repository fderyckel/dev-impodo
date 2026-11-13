"""Verify bounded numeric scans against the exact prepared-value oracle."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb

from impodo.adapters.duckdb.preparation_quality_index import PreparationQualityIndex
from impodo.application.workspace.preparation.preparation_service import (
    prepared_target_numeric_fields,
)
from impodo.domain.compiler.columnar_transformation import (
    ColumnarSupport,
    compile_columnar_transformation_program,
)
from impodo.domain.mapping.contracts import ScalarFieldMapping
from impodo.domain.staging.preparation_session import PreparedCanonicalProjection
from impodo.domain.target_numeric_precision import (
    PreparedNumericField,
    prepared_numeric_precision_finding,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.workspace.errors import WorkspaceError
from tests.integration.columnar import test_polars_transformation as columnar_fixtures

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
MODEL = "x_custom.document"


class _PreparedArtifact:
    """Check the immutable artifact request while exposing a fictional fixture."""

    def __init__(self, path, snapshot) -> None:
        self.path = path
        self.snapshot = snapshot
        self.failure = None
        self.requests = []

    @contextmanager
    def materialize_prepared_snapshot(
        self, workspace_id, storage_key, *, expected_sha256
    ):
        self.requests.append((workspace_id, storage_key, expected_sha256))
        if self.failure is not None:
            raise self.failure
        if (workspace_id, storage_key, expected_sha256) != (
            WORKSPACE_ID,
            self.snapshot.parquet_storage_key,
            self.snapshot.parquet_sha256,
        ):
            raise WorkspaceError("Prepared artifact binding changed")
        yield self.path


class _RecordingCursor:
    """Observe transport boundaries without changing DuckDB cursor ownership."""

    def __init__(self, connection, statements, fetches, transport=None) -> None:
        self.connection = connection
        self.statements = statements
        self.fetches = fetches
        self.statement = ""
        self.transport = (
            transport
            if transport is not None
            else {
                "allow_full_values": False,
                "value_fetchall_rows": [],
                "value_fetchone_rows": 0,
                "failure_executemany_rows": [],
            }
        )

    def execute(self, statement, *args, **kwargs):
        self.statement = statement
        self.statements.append(("execute", statement))
        self.connection.execute(statement, *args, **kwargs)
        return self

    def executemany(self, statement, *args, **kwargs):
        self.statement = statement
        self.statements.append(("executemany", statement))
        if "quality_numeric_failure" in statement:
            self.transport["failure_executemany_rows"].append(len(args[0]))
        self.connection.executemany(statement, *args, **kwargs)
        return self

    def cursor(self):
        return _RecordingCursor(
            self.connection.cursor(), self.statements, self.fetches, self.transport
        )

    def fetchall(self):
        if "prepared_value" in self.statement.casefold():
            if not self.transport["allow_full_values"]:
                raise AssertionError(
                    "Numeric values must not use whole-result fetchall"
                )
            rows = self.connection.fetchall()
            self.transport["value_fetchall_rows"].append(len(rows))
            return rows
        return self.connection.fetchall()

    def fetchmany(self, size=1):
        rows = self.connection.fetchmany(size)
        self.fetches.append((size, len(rows)))
        return rows

    def fetchone(self):
        row = self.connection.fetchone()
        if row is not None and "prepared_value" in self.statement.casefold():
            self.transport["value_fetchone_rows"] += 1
        return row

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class NumericPrecisionStreamingTests(unittest.TestCase):
    """Keep numeric findings exact while value and failure transport stays bounded."""

    def setUp(self) -> None:
        self.connection = duckdb.connect(config={"threads": "1"})
        self.addCleanup(self.connection.close)
        self.connection.execute(
            """
            CREATE TABLE canonical_staging_row (
                run_id VARCHAR, ordinal BIGINT, row_id VARCHAR, dataset VARCHAR,
                source_row BIGINT, target_model VARCHAR, disposition VARCHAR,
                row_json VARCHAR
            );
            CREATE TABLE preparation_lineage (
                session_id VARCHAR, dataset VARCHAR, output_source_row BIGINT,
                physical_dataset_id VARCHAR, physical_source_row BIGINT
            );
            CREATE TABLE canonical_prepared_projection (
                run_id VARCHAR, projection_json VARCHAR,
                prepared_snapshot_hash VARCHAR, dataset_id VARCHAR,
                ordinal_start BIGINT
            );
            CREATE TABLE prepared_snapshot_manifest (
                content_hash VARCHAR, dataset_id VARCHAR, manifest_json VARCHAR
            )
            """
        )
        self.statements = []
        self.fetches = []
        self.recording = _RecordingCursor(
            self.connection, self.statements, self.fetches
        )
        self.host = SimpleNamespace(_artifacts=None)

    def _seed(self, values, *, physical_copies=1):
        records = []
        for ordinal, fields in enumerate(values):
            source_row = ordinal + 2
            row_id = f"row-{ordinal:06d}"
            payload = json.dumps(
                {"target_model": MODEL, "proposed_values": fields},
                ensure_ascii=False,
            )
            self.connection.execute(
                "INSERT INTO canonical_staging_row VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    SESSION_ID,
                    ordinal,
                    row_id,
                    "rows",
                    source_row,
                    MODEL,
                    "CANDIDATE",
                    payload,
                ],
            )
            for copy in range(physical_copies):
                self.connection.execute(
                    "INSERT INTO preparation_lineage VALUES (?, ?, ?, ?, ?)",
                    [SESSION_ID, "rows", source_row, f"source-{copy}", source_row],
                )
            records.append((ordinal, row_id, source_row, fields))
        return records

    def _findings(self, specs):
        return PreparationQualityIndex._bounded_numeric_precision_rows(
            self.host, self.recording, WORKSPACE_ID, SESSION_ID, tuple(specs)
        )

    def _seed_native(self, *, ordinal_start):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        selection = columnar_fixtures._selection()
        selection = replace(
            selection, datasets=(replace(selection.datasets[0], row_count=2),)
        )
        definition = columnar_fixtures._definition(selection)
        definition = replace(
            definition,
            datasets=(
                replace(
                    definition.datasets[0],
                    fields=tuple(
                        field
                        for field in definition.datasets[0].fields
                        if field.target_field == "list_price"
                    ),
                ),
            ),
        )
        source_rows = tuple(
            replace(row, values={**row.values, "price": value})
            for row, value in zip(
                columnar_fixtures._rows()[:2], ("0,003", "12,50"), strict=True
            )
        )
        source_path, source = columnar_fixtures._write_snapshot(
            root, selection, source_rows
        )
        decision = compile_columnar_transformation_program(
            definition, selection, columnar_fixtures.DATASET_ID
        )
        self.assertEqual(decision.support, ColumnarSupport.SUPPORTED)
        self.assertIsNotNone(decision.program)
        program = decision.program
        prepared_path, snapshot = columnar_fixtures._write_prepared_snapshot(
            root, source_path, source, program
        )
        projection = PreparedCanonicalProjection(
            dataset_id=source.dataset_id,
            dataset=source.dataset_name,
            ordinal_start=ordinal_start,
            row_count=2,
            mode=program.target_mode,
            source_hash=source.content_hash,
            physical_dataset_id=source.dataset_id,
            field_sources={},
            program=program,
            set_based_projection=True,
        )
        self.connection.execute(
            "INSERT INTO canonical_prepared_projection VALUES (?, ?, ?, ?, ?)",
            [
                SESSION_ID,
                json.dumps(projection.to_portable_dict()),
                snapshot.content_hash,
                snapshot.dataset_id,
                ordinal_start,
            ],
        )
        self.connection.execute(
            "INSERT INTO prepared_snapshot_manifest VALUES (?, ?, ?)",
            [snapshot.content_hash, snapshot.dataset_id, snapshot.to_json()],
        )
        for offset, source_row in enumerate(source_rows):
            self.connection.execute(
                "INSERT INTO canonical_staging_row VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    SESSION_ID,
                    ordinal_start + offset,
                    f"native-{offset}",
                    "products",
                    source_row.number,
                    program.target_model,
                    "CANDIDATE",
                    "",
                ],
            )
            self.connection.execute(
                "INSERT INTO preparation_lineage VALUES (?, ?, ?, ?, ?)",
                [
                    SESSION_ID,
                    "products",
                    source_row.number,
                    "native-source",
                    source_row.number,
                ],
            )
        artifact = _PreparedArtifact(prepared_path, snapshot)
        self.host._artifacts = artifact
        spec = PreparedNumericField(
            "products", program.target_model, "list_price", (16, 2)
        )
        return artifact, snapshot, projection, spec

    def _expected(self, records, specs, *, physical_copies=1):
        expected = []
        for ordinal, row_id, source_row, fields in records:
            for spec in sorted(specs, key=lambda item: item.field_name):
                value = fields.get(spec.field_name)
                finding = prepared_numeric_precision_finding(
                    row_id=row_id,
                    dataset="rows",
                    source_row=source_row,
                    target_model=MODEL,
                    field_name=spec.field_name,
                    digits=spec.digits,
                    value=value,
                )
                if finding is None:
                    continue
                for copy in range(physical_copies):
                    expected.append(
                        (
                            ordinal,
                            row_id,
                            "rows",
                            source_row,
                            "CANDIDATE",
                            f"source-{copy}",
                            source_row,
                            MODEL,
                            spec.field_name,
                            spec.digits[0],
                            spec.digits[1],
                            finding.required_decimal_places,
                            finding.integral_overflow,
                            finding.zeroed_positive,
                        )
                    )
        return tuple(expected)

    def test_json_values_keep_exact_decimal_boundary_semantics(self) -> None:
        values = (
            "0",
            "1.23000",
            "0.01",
            "0.003",
            "0.005",
            "0.0049",
            "-0.003",
            "1E-9",
            "1E+15",
            "99999999999999.99",
            "100000000000000",
            "NaN",
            "Infinity",
            "-Infinity",
            "bad number",
            "1,23",
            " 1.23 ",
            None,
            True,
        )
        records = self._seed([{"amount": value} for value in values])
        specs = (PreparedNumericField("rows", MODEL, "amount", (16, 2)),)

        actual = self._findings(specs)

        self.assertEqual(actual, self._expected(records, specs))
        self.assertFalse(
            any(
                kind == "executemany" and "quality_numeric_failure" in statement
                for kind, statement in self.statements
            )
        )

    def test_all_invalid_values_keep_field_order_and_every_lineage_link(self) -> None:
        from impodo.application.shared.numeric_performance import (
            collect_numeric_validation,
        )

        records = self._seed(
            [{"z_amount": "0.003", "a_amount": "0.00003"} for _ in range(513)],
            physical_copies=2,
        )
        specs = (
            PreparedNumericField("rows", MODEL, "z_amount", (16, 2)),
            PreparedNumericField("rows", MODEL, "a_amount", (16, 4)),
        )
        with collect_numeric_validation() as observation:
            actual = self._findings(specs)

        self.assertCountEqual(actual, self._expected(records, specs, physical_copies=2))
        row_field_order = tuple((item[0], item[8]) for item in actual)
        self.assertEqual(row_field_order, tuple(sorted(row_field_order)))
        self.assertEqual(len(actual), 513 * 2 * 2)
        self.assertNotIn("0.003", repr(actual))
        self.assertNotIn("0.00003", repr(actual))
        snapshot = observation.snapshot()
        self.assertEqual(snapshot["cells_examined"], 513 * 2)
        self.assertEqual(snapshot["findings_generated"], 513 * 2)
        self.assertGreater(snapshot["value_page_count"], 1)
        self.assertGreater(snapshot["failure_batch_count"], 1)
        self.assertLessEqual(snapshot["max_value_page_rows"], 256)
        self.assertLessEqual(snapshot["max_failure_batch_rows"], 256)
        self.assertLessEqual(snapshot["max_value_page_bytes"], 2 * 1024 * 1024)
        self.assertLessEqual(snapshot["max_failure_batch_bytes"], 2 * 1024 * 1024)
        self.assertEqual(self.recording.transport["value_fetchall_rows"], [])
        self.assertEqual(self.recording.transport["value_fetchone_rows"], 513 * 2)
        failure_inserts = [
            statement
            for kind, statement in self.statements
            if kind == "execute" and "INSERT INTO quality_numeric_failure" in statement
        ]
        self.assertGreater(len(failure_inserts), 1)
        self.assertLessEqual(len(failure_inserts), 6)

    def test_unparseable_and_null_values_do_not_manufacture_findings(self) -> None:
        self._seed([{"amount": None}, {"amount": "invalid"}, {"amount": ""}])
        specs = (PreparedNumericField("rows", MODEL, "amount", (16, 2)),)

        self.assertEqual(self._findings(specs), ())

    def test_timer_capture_failure_preserves_exact_numeric_findings(self) -> None:
        from impodo.application.shared.numeric_performance import (
            collect_numeric_validation,
        )

        records = self._seed([{"amount": "0.003"}, {"amount": "1.23"}])
        specs = (PreparedNumericField("rows", MODEL, "amount", (16, 2)),)

        with (
            collect_numeric_validation() as observation,
            patch(
                "impodo.application.shared.numeric_performance.perf_counter",
                side_effect=OSError("Clock unavailable"),
            ),
        ):
            actual = self._findings(specs)

        self.assertEqual(actual, self._expected(records, specs))
        self.assertEqual(len(actual), 1)
        snapshot = observation.snapshot()
        self.assertEqual(snapshot["cells_examined"], 2)
        self.assertEqual(snapshot["findings_generated"], 1)
        self.assertEqual(snapshot["capture_failure_count"], 1)
        self.assertEqual(snapshot["status"], "partial")

    def test_value_pages_are_bounded_by_utf8_bytes_before_the_row_limit(self) -> None:
        from impodo.application.shared.numeric_performance import (
            collect_numeric_validation,
        )

        value = "١" * 200_000
        self._seed([{"amount": value} for _ in range(8)])
        specs = (PreparedNumericField("rows", MODEL, "amount", (16, 2)),)

        with collect_numeric_validation() as observation:
            actual = self._findings(specs)

        self.assertEqual(len(actual), 8)
        self.assertTrue(all(row[12] for row in actual))
        snapshot = observation.snapshot()
        self.assertEqual(snapshot["cells_examined"], 8)
        self.assertEqual(snapshot["value_page_count"], 2)
        self.assertEqual(snapshot["max_value_page_rows"], 5)
        self.assertEqual(snapshot["max_value_page_bytes"], 2_000_000)
        self.assertNotIn(value, repr(snapshot))

    def test_one_cell_above_the_page_target_keeps_the_existing_hard_allowance(
        self,
    ) -> None:
        from impodo.application.shared.numeric_performance import (
            collect_numeric_validation,
        )

        value = "x" * (2 * 1024 * 1024 + 1)
        self._seed([{"amount": value}])
        specs = (PreparedNumericField("rows", MODEL, "amount", (16, 2)),)

        with collect_numeric_validation() as observation:
            self.assertEqual(self._findings(specs), ())

        snapshot = observation.snapshot()
        self.assertEqual(snapshot["cells_examined"], 1)
        self.assertEqual(snapshot["max_value_page_rows"], 1)
        self.assertEqual(snapshot["max_value_page_bytes"], len(value))

    def test_source_cell_over_the_hard_budget_is_rejected_before_evaluation(
        self,
    ) -> None:
        self._seed([{"amount": "9" * 1025}])
        specs = (PreparedNumericField("rows", MODEL, "amount", (16, 2)),)

        with (
            patch(
                "impodo.adapters.duckdb.preparation_quality_index.QUALITY_FINDING_INDEX_MAX_BYTES",
                1024,
            ),
            patch(
                "impodo.adapters.duckdb.preparation_numeric_precision.prepared_numeric_precision_finding"
            ) as evaluate,
            self.assertRaisesRegex(WorkspaceError, "bounded.*budget"),
        ):
            self._findings(specs)

        evaluate.assert_not_called()

    def test_findings_obey_the_existing_aggregate_evidence_budget(self) -> None:
        self._seed([{"amount": "0.003"} for _ in range(32)])
        specs = (PreparedNumericField("rows", MODEL, "amount", (16, 2)),)

        with (
            patch(
                "impodo.adapters.duckdb.preparation_quality_index.QUALITY_FINDING_INDEX_MAX_BYTES",
                1024,
            ),
            self.assertRaisesRegex(WorkspaceError, "bounded.*budget"),
        ):
            self._findings(specs)

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM canonical_staging_row WHERE run_id = ?",
                [SESSION_ID],
            ).fetchone(),
            (32,),
        )

    def test_duplicate_numeric_metadata_is_rejected_before_value_scan(self) -> None:
        self._seed([{"amount": "0.003"}])
        spec = PreparedNumericField("rows", MODEL, "amount", (16, 2))

        with self.assertRaisesRegex(WorkspaceError, "ambiguous"):
            self._findings((spec, spec))

    def test_both_odoo_majors_use_their_exact_captured_local_and_remote_digits(
        self,
    ) -> None:
        self._seed([{"amount": "0.003"}])
        selection = columnar_fixtures._selection()
        selection = replace(
            selection,
            datasets=(replace(selection.datasets[0], name="rows", row_count=1),),
        )
        base = columnar_fixtures._definition(selection)
        for major, mode, digits in (
            (19, "LOCAL", (16, 2)),
            (19, "REMOTE", (16, 4)),
            (20, "LOCAL", (16, 4)),
            (20, "REMOTE", (16, 2)),
        ):
            with self.subTest(major=major, mode=mode, digits=digits):
                schema = OdooSchemaCatalog(
                    workspace_id=WORKSPACE_ID,
                    policy_hash="sha256:" + "1" * 64,
                    captured_at=columnar_fixtures.NOW,
                    captured_by="manager",
                    connection_mode=mode,
                    database=f"fictional_odoo{major}",
                    odoo_version=f"{major}.0",
                    models=(
                        SchemaModel(
                            name=MODEL,
                            label="Document",
                            fields=(
                                SchemaField(
                                    name="amount",
                                    label="Amount",
                                    type="float",
                                    required=False,
                                    readonly=False,
                                    relation=None,
                                    relation_field=None,
                                    selection=(),
                                    digits=digits,
                                ),
                            ),
                        ),
                    ),
                    content_hash="sha256:" + "2" * 64,
                    origin=SchemaOrigin.LIVE_API,
                    read_credential_binding_hash="sha256:" + "3" * 64,
                    read_principal_hash="sha256:" + "4" * 64,
                    read_permission_hash="sha256:" + "5" * 64,
                    read_context_hash="sha256:" + "6" * 64,
                    connection_target_hash="sha256:" + "7" * 64,
                )
                captured = OdooSchemaCatalog.from_json(schema.to_json())
                definition = replace(
                    base,
                    schema_hash=captured.content_hash,
                    datasets=(
                        replace(
                            base.datasets[0],
                            target_model=MODEL,
                            fields=(
                                ScalarFieldMapping(
                                    target_field="amount",
                                    source_column_key="product.price",
                                    value_type="decimal",
                                ),
                            ),
                        ),
                    ),
                )
                specs = prepared_target_numeric_fields(definition, selection, captured)

                actual = self._findings(specs)

                self.assertEqual(len(specs), 1)
                self.assertEqual(specs[0].digits, digits)
                self.assertEqual(captured.odoo_version, f"{major}.0")
                self.assertEqual(len(actual), 1 if digits[1] == 2 else 0)

    def test_json_and_verified_native_values_share_the_same_exact_evaluation(
        self,
    ) -> None:
        records = self._seed([{"amount": "0.003"}])
        artifact, _snapshot, projection, native_spec = self._seed_native(
            ordinal_start=1
        )
        json_spec = PreparedNumericField("rows", MODEL, "amount", (16, 2))

        actual = self._findings((native_spec, json_spec))

        finding = prepared_numeric_precision_finding(
            row_id="native-0",
            dataset="products",
            source_row=2,
            target_model=projection.program.target_model,
            field_name="list_price",
            digits=(16, 2),
            value="0.003",
        )
        self.assertIsNotNone(finding)
        expected_native = (
            1,
            "native-0",
            "products",
            2,
            "CANDIDATE",
            "native-source",
            2,
            projection.program.target_model,
            "list_price",
            16,
            2,
            finding.required_decimal_places,
            finding.integral_overflow,
            finding.zeroed_positive,
        )
        self.assertEqual(
            actual, (*self._expected(records, (json_spec,)), expected_native)
        )
        self.assertEqual(len(artifact.requests), 1)
        self.assertNotIn("0.003", repr(actual))

    def test_numeric_budget_is_shared_across_json_and_native_sources(self) -> None:
        self._seed([{"amount": "0.003"}, {"amount": "0.003"}])
        _artifact, _snapshot, _projection, native_spec = self._seed_native(
            ordinal_start=2
        )
        json_spec = PreparedNumericField("rows", MODEL, "amount", (16, 2))

        with (
            patch(
                "impodo.adapters.duckdb.preparation_quality_index.QUALITY_FINDING_INDEX_MAX_BYTES",
                1600,
            ),
            self.assertRaisesRegex(WorkspaceError, "bounded.*budget"),
        ):
            self._findings((native_spec, json_spec))

    def test_artifact_failure_discards_temporary_numeric_publication(self) -> None:
        self._seed([{"amount": "0.003"} for _ in range(300)])
        artifact, _snapshot, _projection, native_spec = self._seed_native(
            ordinal_start=300
        )
        artifact.failure = WorkspaceError("Prepared artifact unavailable")
        json_spec = PreparedNumericField("rows", MODEL, "amount", (16, 2))

        with self.assertRaisesRegex(WorkspaceError, "Prepared artifact unavailable"):
            self._findings((native_spec, json_spec))

        tables = {row[0] for row in self.connection.execute("SHOW TABLES").fetchall()}
        self.assertNotIn("quality_numeric_failure", tables)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM canonical_staging_row"
            ).fetchone(),
            (302,),
        )
