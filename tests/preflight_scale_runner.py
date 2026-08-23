"""Child-process runner for the durable Slice 5 preflight scale probe."""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import resource
import sys
from time import perf_counter
from unittest.mock import patch

import psutil

from impodo.application import preflight_service as preflight_module
from impodo.application.preflight_service import (
    EXECUTION_SNAPSHOT_NAME,
    MANIFEST_NAME,
)
from impodo.connectors import MetadataSnapshot, RecordSnapshot
from impodo.domain.preflight import frozen_input as frozen_input_module
from impodo.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    target_identity_hash,
)
from impodo.reporting import WORKBOOK_NAME, write_review_workbook
from impodo.web.app import create_local_app


def _field_value(field: str, index: int):
    if field == "default_code":
        return f"P{index:06d}"
    if field == "name":
        return f"Product {index:06d}"
    if field == "list_price":
        return Decimal("1")
    if field.startswith("x_scale_"):
        column = int(field.rsplit("_", 1)[1])
        return f"value-{column:02d}-{index % 100:02d}"
    raise AssertionError(f"Unexpected planned field: {field}")


def _field_metadata(field: str) -> FieldMetadata:
    return FieldMetadata(
        name=field,
        type="float" if field == "list_price" else "char",
        label=field.replace("_", " ").title(),
        required=field in {"default_code", "name"},
    )


def run(root: Path, project_id: str, row_count: int) -> dict[str, object]:
    app = create_local_app(root)
    context = app.state.context
    project = context.projects.repository.get(project_id)
    fingerprint = TargetFingerprint(
        target_hash=target_identity_hash(
            connection_mode=project.odoo_connection_mode.value,
            base_url=project.odoo_base_url,
            database=project.odoo_database,
        ),
        connection_mode=project.odoo_connection_mode.value,
        database=project.odoo_database,
        odoo_version="19.0",
        snapshot_timestamp="2026-08-06T12:00:00Z",
        module_versions={"base": "19.0.1.0"},
    )
    request_counts: dict[str, int] = {}
    phases: dict[str, float] = {}
    phase_rss_mib: dict[str, float] = {}

    def timed(name, callback):
        def invoke(*args, **kwargs):
            started = perf_counter()
            try:
                return callback(*args, **kwargs)
            finally:
                phases[name] = phases.get(name, 0.0) + perf_counter() - started
                phase_rss_mib[name] = (
                    psutil.Process().memory_info().rss / (1024 * 1024)
                )

        return invoke

    def read_snapshot(requirements):
        metadata_requests = requirements.metadata_requests
        record_requests = requirements.record_requests
        request_counts["metadata"] = len(metadata_requests)
        request_counts["records"] = len(record_requests)
        request_counts["chunks"] = len(record_requests)
        metadata = MetadataSnapshot(
            fingerprint=fingerprint,
            models={
                request.model: ModelMetadata(
                    model=request.model,
                    description=request.model,
                    fields={
                        field: _field_metadata(field)
                        for field in request.fields
                    },
                )
                for request in metadata_requests
            },
        )
        requested_fields = {
            request.model: request.fields for request in record_requests
        }
        records = {
            model: tuple(
                TargetRecord(
                    model=model,
                    odoo_id=index + 1,
                    values={
                        field: _field_value(field, index)
                        for field in fields
                    },
                )
                for index in range(row_count)
            )
            for model, fields in requested_fields.items()
        }
        return metadata, RecordSnapshot(
            fingerprint=fingerprint,
            records=records,
            requested_fields=requested_fields,
        )

    original_load = context.preflight._load_frozen_input
    original_staging_run = context.preflight.staging.get_canonical_staging_run
    original_quality_run = context.preflight.quality.get_quality_run
    original_dry_run = context.preflight.normalization.get_normalization_dry_run
    original_build_frozen = preflight_module.build_frozen_preflight_input
    original_eligible_hash = frozen_input_module.canonical_eligible_dataset_hash
    original_adapt = frozen_input_module.canonical_rows_to_prepared_bundle
    original_portable_guard = frozen_input_module.assert_no_numeric_odoo_ids
    original_plan = preflight_module.plan_preflight_requirements
    original_bind = preflight_module.bind_snapshot_hashes
    original_engine = context.preflight.engine.run
    original_report = preflight_module._readiness_report
    original_manifest = context.artifacts.write_report
    original_publish = context.preflight.preflight.save_readiness_report
    started = perf_counter()
    with (
        patch.object(
            context.preflight,
            "_load_frozen_input",
            timed("load_frozen_input", original_load),
        ),
        patch.object(
            context.preflight.staging,
            "get_canonical_staging_run",
            timed("load_staging_rows", original_staging_run),
        ),
        patch.object(
            context.preflight.quality,
            "get_quality_run",
            timed("load_quality_rows", original_quality_run),
        ),
        patch.object(
            context.preflight.normalization,
            "get_normalization_dry_run",
            timed("load_normalization", original_dry_run),
        ),
        patch(
            "impodo.application.preflight_service.build_frozen_preflight_input",
            timed("build_frozen_input", original_build_frozen),
        ),
        patch.object(
            frozen_input_module,
            "canonical_eligible_dataset_hash",
            timed("eligible_hash", original_eligible_hash),
        ),
        patch.object(
            frozen_input_module,
            "canonical_rows_to_prepared_bundle",
            timed("adapt_rows", original_adapt),
        ),
        patch.object(
            frozen_input_module,
            "assert_no_numeric_odoo_ids",
            timed("frozen_portable_guard", original_portable_guard),
        ),
        patch(
            "impodo.application.preflight_service.plan_preflight_requirements",
            timed("plan_requirements", original_plan),
        ),
        patch(
            "impodo.application.preflight_service.bind_snapshot_hashes",
            timed("bind_snapshots", original_bind),
        ),
        patch.object(
            context.preflight.engine,
            "run",
            timed("engine", original_engine),
        ),
        patch(
            "impodo.application.preflight_service._readiness_report",
            timed("build_report", original_report),
        ),
        patch.object(
            context.artifacts,
            "write_report",
            timed("write_manifest", original_manifest),
        ),
        patch.object(
            context.preflight.preflight,
            "save_readiness_report",
            timed("publish_evidence", original_publish),
        ),
    ):
        report = context.preflight.compare(
            project_id,
            reader=timed("read_snapshot", read_snapshot),
            actor=context.actor,
        )
    elapsed = perf_counter() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = int(peak if sys.platform == "darwin" else peak * 1024)

    database_path = root / project_id / "workspace-engine.duckdb"
    with context.preflight.staging._connect(database_path) as connection:
        stored = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM preflight_decision WHERE run_id = ?),
                (SELECT COUNT(*) FROM preflight_target_snapshot WHERE run_id = ?),
                COALESCE((
                    SELECT SUM(LENGTH(snapshot_json))
                      FROM preflight_target_snapshot
                     WHERE run_id = ?
                ), 0),
                (SELECT COUNT(*) FROM readiness_run)
            """,
            [report.run_id, report.run_id, report.run_id],
        ).fetchone()
    assert stored is not None

    with context.artifacts.materialize_report(
        project_id, report.run_id, MANIFEST_NAME
    ) as manifest_path:
        manifest_bytes = manifest_path.stat().st_size
        with context.artifacts.prepare_report(
            project_id, report.run_id, WORKBOOK_NAME
        ) as workbook_path:
            write_review_workbook(manifest_path, workbook_path)
        with context.artifacts.materialize_report(
            project_id, report.run_id, WORKBOOK_NAME
        ) as workbook_path:
            workbook_bytes = workbook_path.stat().st_size
    with context.artifacts.materialize_report(
        project_id,
        report.run_id,
        EXECUTION_SNAPSHOT_NAME,
    ) as execution_snapshot_path:
        execution_snapshot_bytes = execution_snapshot_path.stat().st_size
    execution_snapshot = context.preflight.current_execution_snapshot(project_id)
    assert execution_snapshot is not None
    assert execution_snapshot.preflight_run_id == report.run_id

    database_bytes = sum(
        path.stat().st_size
        for path in database_path.parent.glob("workspace-engine.duckdb*")
        if path.is_file()
    )
    return {
        "rows": row_count,
        "elapsed_seconds": elapsed,
        "peak_mib": peak_bytes / (1024 * 1024),
        "database_mib": database_bytes / (1024 * 1024),
        "metadata_requests": request_counts.get("metadata", 0),
        "record_requests": request_counts.get("records", 0),
        "domain_chunks": request_counts.get("chunks", 0),
        "target_rows": row_count,
        "result_pages": request_counts.get("records", 0),
        "snapshot_bytes": int(stored[2]),
        "manifest_bytes": manifest_bytes,
        "execution_snapshot_bytes": execution_snapshot_bytes,
        "execution_snapshot_hash": execution_snapshot.semantic_hash,
        "workbook_bytes": workbook_bytes,
        "persisted_decisions": int(stored[0]),
        "persisted_snapshots": int(stored[1]),
        "readiness_runs": int(stored[3]),
        "unchanged": report.unchanged_count,
        "result_hash": report.result_hash,
        "phases": phases,
        "phase_rss_mib": phase_rss_mib,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--rows", type=int, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.root, args.project_id, args.rows),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
