"""Command-line interface for the read-only profiler."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from typing import Sequence

from .connectors import (
    ConnectorError,
    Json2Config,
    Json2ReadConnector,
    SnapshotConnector,
    write_metadata_snapshot,
    write_record_snapshot,
)
from .engine import PreflightEngine
from .models import PreparedRecord, canonical_json_bytes, portable_issue, portable_value
from .planner import plan_metadata_requests, plan_record_requests
from .profile import ProfileLoadError, load_profile
from .reporting import ReportGenerationError, write_preflight_outputs
from .source import prepare_sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="impodo",
        description=(
            "Model-agnostic, read-only Odoo migration profiling and preflight"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile_parser = subparsers.add_parser(
        "profile",
        help="prepare and validate source rows without connecting to Odoo",
    )
    _add_profile_input(profile_parser)
    profile_parser.add_argument(
        "--output",
        default="build/profile/prepared-records.json",
        help="portable prepared-record JSON output",
    )

    metadata_parser = subparsers.add_parser(
        "snapshot-metadata",
        help="capture only profile-required Odoo model metadata",
    )
    metadata_parser.add_argument("--profile", required=True)
    _add_connector_options(metadata_parser)
    metadata_parser.add_argument("--output", required=True)

    records_parser = subparsers.add_parser(
        "snapshot-records",
        help="capture relevant target records in batches",
    )
    _add_profile_input(records_parser)
    _add_connector_options(records_parser)
    records_parser.add_argument("--output", required=True)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="classify source candidates entirely offline from saved snapshots",
    )
    _add_profile_input(preflight_parser)
    preflight_parser.add_argument("--metadata", required=True)
    preflight_parser.add_argument("--records", required=True)
    preflight_parser.add_argument("--output", required=True)
    preflight_parser.add_argument(
        "--preview-dir",
        help="optional rendered PNG verification directory for workbook sheets",
    )

    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="run a synthetic in-memory identity-index benchmark",
    )
    benchmark_parser.add_argument("--rows", type=int, default=360_000)

    return parser


def _add_profile_input(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", required=True, help="YAML profile")
    parser.add_argument(
        "--input",
        required=True,
        help="directory containing profile-declared CSV/XLSX source files",
    )


def _add_connector_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--connector",
        choices=("snapshot", "json2"),
        required=True,
        help="offline fixture or live read-only Odoo JSON-2",
    )
    parser.add_argument(
        "--snapshot",
        help="combined normalized fixture used with --connector snapshot",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "profile":
            return _profile_command(arguments)
        if arguments.command == "snapshot-metadata":
            return _snapshot_metadata_command(arguments)
        if arguments.command == "snapshot-records":
            return _snapshot_records_command(arguments)
        if arguments.command == "preflight":
            return _preflight_command(arguments)
        if arguments.command == "benchmark":
            return _benchmark_command(arguments)
    except (ProfileLoadError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except ConnectorError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    except ReportGenerationError as exc:
        print(str(exc), file=sys.stderr)
        return 6
    parser.error("unknown command")
    return 2


def _profile_command(arguments: argparse.Namespace) -> int:
    profile = load_profile(arguments.profile)
    prepared = prepare_sources(profile, arguments.input)
    payload = {
        "profile": {"id": profile.profile.id},
        "source_hashes": prepared.source_hashes,
        "records": [_portable_prepared(record) for record in prepared.records],
        "issues": [portable_issue(issue) for issue in prepared.issues],
    }
    destination = Path(arguments.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_json_bytes(payload) + b"\n")
    blocked = sum(1 for record in prepared.records if record.blocked)
    print(
        f"Prepared {len(prepared.records)} rows; blocked {blocked}; "
        f"output {destination}"
    )
    return 0


def _snapshot_metadata_command(arguments: argparse.Namespace) -> int:
    profile = load_profile(arguments.profile)
    connector = _connector(arguments)
    snapshot = connector.get_model_metadata(plan_metadata_requests(profile))
    write_metadata_snapshot(
        snapshot,
        arguments.output,
        profile_id=profile.profile.id,
    )
    print(f"Metadata snapshot written to {arguments.output}")
    return 0


def _snapshot_records_command(arguments: argparse.Namespace) -> int:
    profile = load_profile(arguments.profile)
    prepared = prepare_sources(profile, arguments.input)
    connector = _connector(arguments)
    snapshot = connector.get_records(
        plan_record_requests(profile, prepared.records)
    )
    write_record_snapshot(
        snapshot,
        arguments.output,
        profile_id=profile.profile.id,
        source_hashes=prepared.source_hashes,
    )
    print(f"Record snapshot written to {arguments.output}")
    return 0


def _preflight_command(arguments: argparse.Namespace) -> int:
    profile = load_profile(arguments.profile)
    prepared = prepare_sources(profile, arguments.input)
    connector = SnapshotConnector(
        metadata_path=arguments.metadata,
        records_path=arguments.records,
        expected_profile_id=profile.profile.id,
        expected_source_hashes=prepared.source_hashes,
    )
    metadata = connector.get_model_metadata(plan_metadata_requests(profile))
    records = connector.get_records(
        plan_record_requests(profile, prepared.records)
    )
    result = PreflightEngine().run(profile, prepared, metadata, records)
    manifest, workbook = write_preflight_outputs(
        result,
        arguments.output,
        preview_directory=arguments.preview_dir,
    )
    counts = result.counts
    print(
        " | ".join(
            f"{classification} {counts[classification]}"
            for classification in (
                "CREATE",
                "UPDATE",
                "UNCHANGED",
                "AMBIGUOUS",
                "BLOCKED",
            )
        )
    )
    print(f"Manifest: {manifest}")
    print(f"Review workbook: {workbook}")
    print(f"Semantic hash: {result.semantic_hash}")
    return 0


def _connector(arguments: argparse.Namespace):
    if arguments.connector == "snapshot":
        if not arguments.snapshot:
            raise ValueError("--snapshot is required with --connector snapshot")
        return SnapshotConnector(combined_path=arguments.snapshot)
    return Json2ReadConnector(Json2Config.from_environment())


def _portable_prepared(record: PreparedRecord) -> dict[str, object]:
    return {
        "dataset": record.dataset,
        "source_row": record.source_row,
        "target_model": record.target_model,
        "source_identity": portable_value(record.source_identity),
        "target_identity": portable_value(record.target_identity),
        "target_scope": portable_value(record.target_scope),
        "scalar_values": portable_value(record.scalar_values),
        "references": portable_value(record.references),
        "blocked": record.blocked,
        "issues": [portable_issue(issue) for issue in record.issues],
    }


def _benchmark_command(arguments: argparse.Namespace) -> int:
    if arguments.rows < 1:
        raise ValueError("--rows must be positive")
    started = time.perf_counter()
    index = {f"KEY-{number:07d}": number for number in range(arguments.rows)}
    checksum = sum(
        index[f"KEY-{number:07d}"]
        for number in range(0, arguments.rows, max(1, arguments.rows // 1_000))
    )
    elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                "rows": arguments.rows,
                "elapsed_seconds": round(elapsed, 6),
                "checksum": checksum,
                "strategy": "indexed-dictionary",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
