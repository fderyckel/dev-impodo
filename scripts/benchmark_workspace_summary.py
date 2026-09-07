"""Measure one existing Summary page without retaining business data.

The benchmark composes the local application against an existing project root,
authenticates an in-process browser session, and measures one cold request plus
warm repetitions. It performs no preparation, Odoo call, or form submission.
Reports contain only route-level timings, counts, status codes, and payload
sizes; the workspace identifier and rendered HTML are never written.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import tempfile
from time import perf_counter
from uuid import UUID

from fastapi.testclient import TestClient

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.web.app import create_local_app
from impodo.web.diagnostics import (
    DIAGNOSTIC_LOG_NAME,
    REQUEST_ID_HEADER,
    LocalDiagnosticRecorder,
    parse_server_timing,
)
from impodo.web.launcher import default_project_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--project-root", type=Path, default=default_project_root())
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--quality-status",
        choices=("", "ready", "review", "quarantined", "blocked"),
        default="quarantined",
    )
    parser.add_argument("--output", type=Path)
    return parser


def benchmark(arguments: argparse.Namespace) -> dict[str, object]:
    workspace_id = str(UUID(arguments.workspace_id))
    project_root = arguments.project_root.expanduser().resolve()
    if not project_root.is_dir():
        raise ValueError("Project root was not found")
    if arguments.runs < 2:
        raise ValueError("Use at least two runs: one cold and one warm")
    if arguments.runs > 100:
        raise ValueError("Summary benchmark runs cannot exceed 100")

    with tempfile.TemporaryDirectory(prefix="impodo-summary-benchmark-") as temporary:
        recorder = LocalDiagnosticRecorder(Path(temporary) / "diagnostics")
        app = create_local_app(
            project_root,
            launch_token="summary-benchmark-launch",
            session_secret="summary-benchmark-session",
            secret_store=MemorySecretStore(),
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
            load_jobs_enabled=False,
            diagnostic_recorder=recorder,
        )
        route = f"/workspaces/{workspace_id}/summary"
        if arguments.quality_status:
            route += f"?quality_status={arguments.quality_status}#quality-rows"
        samples: list[dict[str, object]] = []
        with TestClient(app) as client:
            launched = client.get(
                "/launch?token=summary-benchmark-launch",
                follow_redirects=False,
            )
            if launched.status_code != 303:
                raise RuntimeError("Could not authenticate the benchmark session")
            for run_number in range(1, arguments.runs + 1):
                started = perf_counter()
                response = client.get(route, follow_redirects=False)
                client_wall_ms = (perf_counter() - started) * 1000
                if response.status_code != 200:
                    raise RuntimeError(
                        "Summary benchmark did not return a complete page "
                        f"(status {response.status_code})"
                    )
                samples.append(
                    {
                        "client_wall_ms": round(client_wall_ms, 3),
                        "request_id": response.headers.get(REQUEST_ID_HEADER, ""),
                        "response_bytes": len(response.content),
                        "run": run_number,
                        "server_timings_ms": parse_server_timing(
                            response.headers.get("Server-Timing", "")
                        ),
                        "status_code": response.status_code,
                    }
                )

        recorder.close()
        records = _request_records(recorder.path.parent)
        for sample in samples:
            request_id = str(sample.pop("request_id"))
            record = records.get(request_id, {})
            sample["diagnostic_wall_ms"] = record.get("duration_ms")
            for field in (
                "database_connection_count",
                "database_schema_check_count",
                "database_lock_retry_count",
            ):
                sample[field] = int(record.get(field, 0))

    cold = samples[0]
    warm = samples[1:]
    return {
        "application_build_id": PROCESS_BUILD_CONTRACT.application_build_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cold": cold,
        "filter": {"quality_status": arguments.quality_status},
        "report_schema_version": 1,
        "route_template": "/workspaces/{workspace_id}/summary",
        "summary": {
            "cold_client_wall_ms": cold["client_wall_ms"],
            "warm_median_client_wall_ms": statistics.median(
                float(sample["client_wall_ms"]) for sample in warm
            ),
            "warm_median_database_connections": statistics.median(
                int(sample["database_connection_count"]) for sample in warm
            ),
            "warm_median_database_schema_checks": statistics.median(
                int(sample["database_schema_check_count"]) for sample in warm
            ),
            "warm_median_server_timings_ms": _median_server_timings(warm),
            "warm_run_count": len(warm),
        },
        "warm": warm,
    }


def _request_records(directory: Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for path in sorted(directory.glob(f"{DIAGNOSTIC_LOG_NAME}*")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event") != "request_completed":
                continue
            request_id = record.get("request_id")
            if isinstance(request_id, str):
                records[request_id] = record
    return records


def _median_server_timings(samples: list[dict[str, object]]) -> dict[str, float]:
    names = set.intersection(
        *(
            set(dict(sample["server_timings_ms"]))
            for sample in samples
        )
    )
    return {
        name: round(
            statistics.median(
                float(dict(sample["server_timings_ms"])[name])
                for sample in samples
            ),
            3,
        )
        for name in sorted(names)
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = benchmark(arguments)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Summary benchmark failed: {error}")
        return 2
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        output = arguments.output.expanduser().resolve()
        if not output.parent.is_dir():
            print("Summary benchmark failed: output directory was not found")
            return 2
        output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
