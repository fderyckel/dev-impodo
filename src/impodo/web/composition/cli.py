"""Expose expert preflight commands and safe scenario-definition validation.

Migration stage: primarily H, with source preparation and snapshot commands
that precede offline comparison. Layer: CLI entry point.

Unlike the browser workflow, this path starts from strict YAML contracts. The
profile commands remain read-only. The governed scenario command can also run
one explicitly confirmed profile-driven write against a disposable local Odoo
database through the existing scoped writer and reconciliation services. It
does not share browser lifecycle persistence.

See ``docs/developer/cli/preflight.md``, ``docs/developer/contracts/preflight.md``, and
``tests/integration/artifacts/test_reporting_cli.py``.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Sequence

from impodo.domain.odoo.contracts import ConnectorError
from impodo.domain.scenarios import (
    ScenarioDestinationMode,
    ScenarioRunStatus,
    ScenarioWritePolicy,
)
from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    SnapshotConnector,
    target_record_read_config,
    write_metadata_snapshot,
    write_record_snapshot,
)
from impodo.domain.compiler import compile_profile_document
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.shared.models import (
    PreparedRecord,
    canonical_json_bytes,
    portable_issue,
    portable_value,
)
from impodo.domain.execution.planner import plan_metadata_requests, plan_record_requests
from impodo.adapters.artifacts.profile_loader import ProfileLoadError, load_profile
from impodo.adapters.artifacts.reporting import ReportGenerationError, write_preflight_outputs
from impodo.adapters.scenarios import (
    ProfileScenarioWorkflow,
    ScenarioLoadError,
    load_scenario,
    write_scenario_result,
)
from impodo.application.scenarios import ScenarioRunner
from impodo.application.data_version.source_files import prepare_sources


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit profile, scenario, preflight, and benchmark commands."""

    parser = argparse.ArgumentParser(
        prog="impodo",
        description=(
            "Model-agnostic Odoo migration profiling, preflight, and governed "
            "scenario qualification"
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
        help="optional CSV verification directory for workbook sheets",
    )

    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="run a synthetic in-memory identity-index benchmark",
    )
    benchmark_parser.add_argument("--rows", type=int, default=360_000)

    scenario_parser = subparsers.add_parser(
        "scenario",
        help="validate governed end-to-end scenario definitions",
    )
    scenario_subparsers = scenario_parser.add_subparsers(
        dest="scenario_command",
        required=True,
    )
    scenario_validate_parser = scenario_subparsers.add_parser(
        "validate",
        help="validate a scenario and its contained artifacts without contacting Odoo",
    )
    scenario_validate_parser.add_argument("--definition", required=True)
    scenario_run_parser = scenario_subparsers.add_parser(
        "run",
        help="run governed comparison or a confirmed local disposable round trip",
    )
    scenario_run_parser.add_argument("--definition", required=True)
    scenario_run_parser.add_argument(
        "--connector",
        choices=("snapshot", "json2"),
        required=True,
        help="offline read fixture or live local Odoo JSON-2",
    )
    scenario_run_parser.add_argument(
        "--snapshot",
        help="combined normalized target fixture used with --connector snapshot",
    )
    scenario_run_parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8069",
        help="literal-loopback Odoo URL used with --connector json2",
    )
    scenario_run_parser.add_argument(
        "--database",
        help="disposable impodo_scenario_* database used with --connector json2",
    )
    scenario_run_parser.add_argument(
        "--api-key-file",
        type=Path,
        help="private API-key file used with --connector json2",
    )
    scenario_run_parser.add_argument("--output", required=True)
    scenario_run_parser.add_argument(
        "--evidence-dir",
        help="private durable evidence directory required for a write scenario",
    )
    scenario_run_parser.add_argument(
        "--confirm-disposable-write",
        help="repeat the scenario ID to authorize its declared disposable write",
    )

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
    """Dispatch one CLI command and translate expected failures to exit codes."""

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
        if arguments.command == "scenario":
            return _scenario_command(arguments)
    except (ProfileLoadError, ScenarioLoadError, OSError, ValueError) as exc:
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


def _scenario_command(arguments: argparse.Namespace) -> int:
    if arguments.scenario_command == "run":
        return _scenario_run_command(arguments)
    if arguments.scenario_command != "validate":
        raise ValueError("unknown scenario command")
    loaded = load_scenario(arguments.definition)
    definition = loaded.definition
    payload = {
        "contract_version": definition.contract_version,
        "destination_mode": definition.destination.mode.value,
        "fixture_bytes": loaded.fixture_bytes,
        "fixture_file_count": loaded.fixture_file_count,
        "fixture_hash": loaded.fixture_hash,
        "scenario_hash": loaded.scenario_hash,
        "scenario_id": definition.scenario_id,
        "source_mode": definition.source.mode.value,
        "status": "VALID",
        "stop_after": definition.execution.stop_after.value,
        "write_policy": definition.execution.write_policy.value,
    }
    print(canonical_json_bytes(payload).decode("utf-8"))
    return 0


def _scenario_run_command(arguments: argparse.Namespace) -> int:
    loaded = load_scenario(arguments.definition)
    write_capable = (
        loaded.definition.execution.write_policy
        is ScenarioWritePolicy.DISPOSABLE_SCENARIO_ONLY
    )
    evidence_directory = None
    if write_capable:
        if arguments.confirm_disposable_write != loaded.definition.scenario_id:
            raise ValueError(
                "repeat the scenario ID with --confirm-disposable-write"
            )
        if not arguments.evidence_dir:
            raise ValueError("--evidence-dir is required for a write scenario")
        evidence_directory = Path(arguments.evidence_dir).resolve()
        try:
            evidence_directory.relative_to(loaded.definition_path.parent)
        except ValueError:
            pass
        else:
            raise ValueError(
                "scenario evidence must be outside the immutable definition directory"
            )
    if arguments.connector == "snapshot":
        if write_capable:
            raise ValueError("a write-capable scenario requires --connector json2")
        if not arguments.snapshot:
            raise ValueError("--snapshot is required with --connector snapshot")
        connector = SnapshotConnector(combined_path=arguments.snapshot)
    else:
        definition = loaded.definition
        if definition.destination.mode is not ScenarioDestinationMode.LOCAL_ODOO:
            raise ValueError("the first live scenario runner supports LOCAL_ODOO only")
        if not arguments.database or not arguments.database.startswith(
            "impodo_scenario_"
        ):
            raise ValueError(
                "live scenarios accept only an impodo_scenario_* disposable database"
            )
        api_key = _read_scenario_api_key(arguments.api_key_file)
        config = Json2Config(
            base_url=arguments.base_url.rstrip("/"),
            database=arguments.database,
            api_key=api_key,
            connection_mode="LOCAL",
            relevant_modules=definition.destination.relevant_modules,
        )
        def connector_factory() -> Json2ReadConnector:
            return Json2ReadConnector(target_record_read_config(config))

        connector = connector_factory()
    if write_capable:
        workflow = ProfileScenarioWorkflow(
            loaded,
            connector=connector,
            connector_factory=connector_factory,
            write_config=config,
            evidence_directory=evidence_directory,
        )
        result = ScenarioRunner().run_write(
            loaded.definition,
            fixture_hash=loaded.fixture_hash,
            workflow=workflow,
        )
    else:
        result = ScenarioRunner().run_read_only(
            loaded.definition,
            fixture_hash=loaded.fixture_hash,
            workflow=ProfileScenarioWorkflow(loaded, connector=connector),
        )
    destination = write_scenario_result(result, arguments.output)
    print(
        f"Scenario {result.scenario_id}: {result.status.value}; result {destination}"
    )
    return (
        0
        if result.status
        in {ScenarioRunStatus.PASSED, ScenarioRunStatus.EXPECTED_BLOCK_PASSED}
        else 7
    )


def _read_scenario_api_key(path: Path | None) -> str:
    if path is None:
        raise ValueError("--api-key-file is required with --connector json2")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size > 16 * 1024:
        raise ValueError("scenario API-key file is invalid")
    value = resolved.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("scenario API-key file is empty")
    return value


def _profile_command(arguments: argparse.Namespace) -> int:
    profile = load_profile(arguments.profile)
    plan = compile_profile_document(profile)
    prepared = prepare_sources(plan, arguments.input)
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
    plan = compile_profile_document(profile)
    connector = _connector(arguments)
    snapshot = connector.get_model_metadata(plan_metadata_requests(plan))
    write_metadata_snapshot(
        snapshot,
        arguments.output,
        profile_id=profile.profile.id,
    )
    print(f"Metadata snapshot written to {arguments.output}")
    return 0


def _snapshot_records_command(arguments: argparse.Namespace) -> int:
    profile = load_profile(arguments.profile)
    plan = compile_profile_document(profile)
    prepared = prepare_sources(plan, arguments.input)
    connector = _connector(arguments)
    snapshot = connector.get_records(
        plan_record_requests(plan, prepared.records)
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
    plan = compile_profile_document(profile)
    prepared = prepare_sources(plan, arguments.input)
    connector = SnapshotConnector(
        metadata_path=arguments.metadata,
        records_path=arguments.records,
        expected_profile_id=profile.profile.id,
        expected_source_hashes=prepared.source_hashes,
    )
    metadata = connector.get_model_metadata(plan_metadata_requests(plan))
    records = connector.get_records(
        plan_record_requests(plan, prepared.records)
    )
    result = PreflightEngine().run(plan, prepared, metadata, records)
    manifest, workbook = write_preflight_outputs(
        result,
        arguments.output,
        preview_directory=arguments.preview_dir,
        prepared_records=prepared.records,
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
    return Json2ReadConnector(
        target_record_read_config(Json2Config.from_environment())
    )


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
