"""Fresh-process runner for the durable Final review scale baseline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import perf_counter, process_time, sleep
from unittest.mock import patch

import psutil
from fastapi.testclient import TestClient

if sys.platform != "win32":
    import resource

from impodo.adapters.artifacts.reporting import WORKBOOK_NAME, write_review_workbook
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.preflight_timing import (
    collect_preflight_publication_timings,
)
from impodo.application import preflight_service as preflight_module
from impodo.application.preflight_service import (
    EXECUTION_SNAPSHOT_NAME,
    MANIFEST_NAME,
)
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.preflight import frozen_input as frozen_input_module
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
    canonical_json_text,
    target_identity_hash,
)
from impodo.web.app import create_local_app
from impodo.web.diagnostics import (
    DIAGNOSTIC_LOG_NAME,
    REQUEST_ID_HEADER,
    LocalDiagnosticRecorder,
)

_LAUNCH_TOKEN = "preflight-scale-launch-token"
_SESSION_SECRET = "preflight-scale-session-secret"
_ACTIVE_PROBE_TIMEOUT_SECONDS = 30
_COMPARISON_TIMEOUT_SECONDS = 900


def _field_value(field: str, index: int):
    if field == "default_code":
        return f"P{index:06d}"
    if field == "name":
        return f"Product {index:06d}"
    if field == "list_price":
        return Decimal(1)
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


def run(root: Path, workspace_id: str, row_count: int) -> dict[str, object]:
    process = psutil.Process()
    request_counts: dict[str, int] = {}
    phases: dict[str, float] = {}
    phase_rss_mib: dict[str, float] = {}
    comparison_metrics: dict[str, object] = {}
    request_probes: dict[str, dict[str, object]] = {}
    reading_started = Event()
    release_reader = Event()
    fingerprint: TargetFingerprint | None = None

    def timed(name, callback):
        def invoke(*args, **kwargs):
            started = perf_counter()
            try:
                return callback(*args, **kwargs)
            finally:
                phases[name] = phases.get(name, 0.0) + perf_counter() - started
                phase_rss_mib[name] = process.memory_info().rss / (1024 * 1024)

        return invoke

    def build_snapshot(metadata_requests, record_requests):
        assert fingerprint is not None
        request_counts["metadata"] = len(metadata_requests)
        request_counts["records"] = len(record_requests)
        request_counts["chunks"] = len(record_requests)
        metadata = MetadataSnapshot(
            fingerprint=fingerprint,
            models={
                request.model: ModelMetadata(
                    model=request.model,
                    description=request.model,
                    fields={field: _field_metadata(field) for field in request.fields},
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
                    values={field: _field_value(field, index) for field in fields},
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

    timed_snapshot = timed("read_snapshot", build_snapshot)

    def read_snapshot(_workspace_state, metadata_requests, record_requests):
        reading_started.set()
        if not release_reader.wait(timeout=_ACTIVE_PROBE_TIMEOUT_SECONDS):
            raise TimeoutError("Timed out waiting for the active-route probes")
        return timed_snapshot(metadata_requests, record_requests)

    diagnostics_directory = root.parent / f"scale-diagnostics-{os.getpid()}"
    recorder = LocalDiagnosticRecorder(diagnostics_directory)
    app = create_local_app(
        root,
        launch_token=_LAUNCH_TOKEN,
        session_secret=_SESSION_SECRET,
        secret_store=MemorySecretStore(),
        readiness_reader=read_snapshot,
        preparation_jobs_enabled=False,
        preflight_jobs_enabled=True,
        odoo_capture_jobs_enabled=False,
        recipe_run_jobs_enabled=False,
        diagnostic_recorder=recorder,
    )
    context = app.state.context
    if context.preflight_jobs is None:
        raise AssertionError("The scale baseline requires background comparison jobs")
    project = context.workspace_states.repository.get(workspace_id)
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

    def measure_request(name, callback):
        wall_started = perf_counter()
        cpu_started = process_time()
        response = callback()
        request_probes[name] = {
            "client_cpu_ms": (process_time() - cpu_started) * 1000,
            "client_elapsed_ms": (perf_counter() - wall_started) * 1000,
            "request_id": response.headers.get(REQUEST_ID_HEADER, ""),
            "status_code": response.status_code,
        }
        return response

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

    def write_report(workspace, run_id, filename, content):
        phase = {
            MANIFEST_NAME: "write_manifest",
            EXECUTION_SNAPSHOT_NAME: "write_execution_snapshot",
        }.get(filename, "write_report")
        return timed(phase, original_manifest)(workspace, run_id, filename, content)

    original_compare = context.preflight.compare

    def compare_with_metrics(*args, **kwargs):
        wall_started = perf_counter()
        cpu_started = process_time()
        with (
            collect_duckdb_request_timings() as database_timings,
            collect_preflight_publication_timings() as publication_timings,
        ):
            try:
                return original_compare(*args, **kwargs)
            finally:
                comparison_metrics.update(
                    {
                        "cpu_seconds": process_time() - cpu_started,
                        "database_connection_count": (
                            database_timings.connection_count
                        ),
                        "database_connection_ms": database_timings.connect_ms,
                        "database_lock_retry_count": (
                            database_timings.lock_retry_count
                        ),
                        "database_lock_wait_ms": database_timings.lock_wait_ms,
                        "database_schema_check_count": (
                            database_timings.schema_check_count
                        ),
                        "database_schema_ms": database_timings.schema_ms,
                        "elapsed_seconds": perf_counter() - wall_started,
                        "publication_timings_ms": dict(
                            publication_timings.milliseconds
                        ),
                        "publication_timing_calls": dict(
                            publication_timings.calls
                        ),
                    }
                )

    def csrf_token(html: str) -> str:
        marker = 'name="csrf_token" value="'
        start = html.find(marker)
        if start < 0:
            raise AssertionError("CSRF token not found")
        start += len(marker)
        end = html.find('"', start)
        if end < 0:
            raise AssertionError("CSRF token is incomplete")
        return html[start:end]

    status_payload: dict[str, object] = {}
    with TestClient(app) as client:
        launched = client.get(
            f"/launch?token={_LAUNCH_TOKEN}",
            follow_redirects=False,
        )
        assert launched.status_code == 303
        csrf = csrf_token(client.get("/projects").text)
        try:
            with (
                patch.object(
                    context.preflight,
                    "compare",
                    new=compare_with_metrics,
                ),
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
                    new=write_report,
                ),
                patch.object(
                    context.preflight.preflight,
                    "save_readiness_report",
                    timed("publish_evidence", original_publish),
                ),
            ):
                started_response = measure_request(
                    "compare_start",
                    lambda: client.post(
                        f"/workspaces/{workspace_id}/summary/compare",
                        data={"csrf_token": csrf},
                        headers={"Origin": "http://testserver"},
                        follow_redirects=False,
                    ),
                )
                if started_response.status_code != 303:
                    raise AssertionError(
                        "Comparison start failed with "
                        f"{started_response.status_code}: "
                        f"{started_response.text[-8_000:]}"
                    )
                progress_url = started_response.headers["location"]
                if not reading_started.wait(timeout=_ACTIVE_PROBE_TIMEOUT_SECONDS):
                    raise TimeoutError("Comparison did not reach the Odoo read phase")
                progress_response = measure_request(
                    "progress_active",
                    lambda: client.get(progress_url),
                )
                status_response = measure_request(
                    "status_active",
                    lambda: client.get(f"{progress_url}/status"),
                )
                health_response = measure_request(
                    "health_active",
                    lambda: client.get("/health"),
                )
                if progress_response.status_code != 200:
                    raise AssertionError(
                        "Active progress request failed with "
                        f"{progress_response.status_code}; url={progress_url}; location="
                        f"{progress_response.headers.get('location', '')}; "
                        f"body={progress_response.text[-2_000:]}"
                    )
                assert status_response.status_code == 200
                assert status_response.json()["status"] not in {
                    "FAILED",
                    "SUCCEEDED",
                }
                assert health_response.status_code == 200
                assert health_response.json() == {"status": "ok"}
                release_reader.set()
                deadline = perf_counter() + _COMPARISON_TIMEOUT_SECONDS
                while perf_counter() < deadline:
                    status_response = client.get(f"{progress_url}/status")
                    status_payload = status_response.json()
                    if status_payload["status"] == "SUCCEEDED":
                        break
                    if status_payload["status"] == "FAILED":
                        raise AssertionError(status_payload)
                    sleep(0.01)
                else:
                    raise TimeoutError("Comparison did not complete")
        finally:
            release_reader.set()

        report = context.preflight.current_report(workspace_id)
        assert report is not None
        summary_response = measure_request(
            "summary_action_rich",
            lambda: client.get(f"/workspaces/{workspace_id}/summary"),
        )
        load_review_response = measure_request(
            "load_review",
            lambda: client.get(f"/workspaces/{workspace_id}/load/review"),
        )
        assert summary_response.status_code == 200
        assert load_review_response.status_code == 200

    recorder.close()
    diagnostic_records = tuple(
        json.loads(line)
        for line in (diagnostics_directory / DIAGNOSTIC_LOG_NAME)
        .read_text(encoding="utf-8")
        .splitlines()
    )
    diagnostics_by_request = {
        record["request_id"]: record
        for record in diagnostic_records
        if record.get("event") == "request_completed"
    }
    route_baselines: dict[str, dict[str, object]] = {}
    for name, probe in request_probes.items():
        request_id = probe.pop("request_id")
        diagnostic = diagnostics_by_request.get(request_id, {})
        route_baselines[name] = {
            **probe,
            "diagnostic_duration_ms": diagnostic.get("duration_ms", 0.0),
            "database_connection_count": diagnostic.get(
                "database_connection_count",
                0,
            ),
            "database_lock_retry_count": diagnostic.get(
                "database_lock_retry_count",
                0,
            ),
            "database_schema_check_count": diagnostic.get(
                "database_schema_check_count",
                0,
            ),
            "server_timings_ms": diagnostic.get("server_timings_ms", {}),
        }

    if sys.platform == "win32":
        peak_bytes = int(process.memory_info().peak_wset)
    else:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = int(peak if sys.platform == "darwin" else peak * 1024)

    database_path = (
        context.preflight.preflight.workspace_directory(workspace_id)
        / "workspace-engine.duckdb"
    )
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
        decision_edges = connection.execute(
            """
            (SELECT ordinal, decision_json
               FROM preflight_decision
              WHERE run_id = ?
              ORDER BY ordinal
              LIMIT 1)
            UNION ALL
            (SELECT ordinal, decision_json
               FROM preflight_decision
              WHERE run_id = ?
              ORDER BY ordinal DESC
              LIMIT 1)
            ORDER BY ordinal
            """,
            [report.run_id, report.run_id],
        ).fetchall()
    assert stored is not None
    first_page = context.preflight.readiness_rows(
        workspace_id,
        report.run_id,
        page=1,
        page_size=1,
    )
    last_page = context.preflight.readiness_rows(
        workspace_id,
        report.run_id,
        page=row_count,
        page_size=1,
    )
    decision_json_edges_match = bool(
        len(decision_edges) == 2
        and tuple(int(item[0]) for item in decision_edges)
        == (0, row_count - 1)
        and str(decision_edges[0][1])
        == canonical_json_text(asdict(first_page.items[0]))
        and str(decision_edges[1][1])
        == canonical_json_text(asdict(last_page.items[0]))
    )

    with context.artifacts.materialize_report(
        workspace_id, report.run_id, MANIFEST_NAME
    ) as manifest_path:
        manifest_bytes = manifest_path.stat().st_size
        workbook_started = perf_counter()
        with context.artifacts.prepare_report(
            workspace_id, report.run_id, WORKBOOK_NAME
        ) as workbook_path:
            write_review_workbook(manifest_path, workbook_path)
        workbook_seconds = perf_counter() - workbook_started
        with context.artifacts.materialize_report(
            workspace_id, report.run_id, WORKBOOK_NAME
        ) as workbook_path:
            workbook_bytes = workbook_path.stat().st_size
    with context.artifacts.materialize_report(
        workspace_id,
        report.run_id,
        EXECUTION_SNAPSHOT_NAME,
    ) as execution_snapshot_path:
        execution_snapshot_bytes = execution_snapshot_path.stat().st_size
    execution_snapshot = context.preflight.current_execution_snapshot(workspace_id)
    assert execution_snapshot is not None
    assert execution_snapshot.preflight_run_id == report.run_id

    database_bytes = sum(
        path.stat().st_size
        for path in database_path.parent.glob("workspace-engine.duckdb*")
        if path.is_file()
    )
    return {
        "schema_version": 1,
        "rows": row_count,
        **comparison_metrics,
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
        "workbook_seconds": workbook_seconds,
        "persisted_decisions": int(stored[0]),
        "decision_json_edges_match": decision_json_edges_match,
        "persisted_snapshots": int(stored[1]),
        "readiness_runs": int(stored[3]),
        "unchanged": report.unchanged_count,
        "result_hash": report.result_hash,
        "route_baselines": route_baselines,
        "status_completed": status_payload.get("status") == "SUCCEEDED",
        "phases": phases,
        "phase_rss_mib": phase_rss_mib,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--rows", type=int, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.root, args.workspace_id, args.rows),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
