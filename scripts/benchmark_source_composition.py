"""Measure exact-join preview and save with deterministic local fixtures.

This qualification is intentionally browser-shaped: source intake is completed
before timing starts, then preview and save each run in their current retained
DuckDB operation scope. Every measured case runs in a fresh process. Timings are
observational; semantic parity and database-lifetime ceilings are hard gates.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
import gc
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
from threading import Event, Thread
from time import perf_counter, process_time
from unittest.mock import patch

import psutil

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(
    os.environ.get("IMPODO_SOURCE_COMPOSITION_SOURCE_ROOT", ROOT / "src")
).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
import impodo.application.workspace.derived_entities as derived_application
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.structural import execute_structural_rules
from impodo.web.diagnostics import parse_server_timing
from impodo.web.exact_join_forms import (
    available_exact_join_selection,
    exact_join_projection_candidates,
)
from impodo.web.presenters.schema import _dataset_choices
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _created_workspace_id,
)


RESULT_PREFIX = "IMPODO_SOURCE_COMPOSITION_JSON="
MIB = 1024 * 1024
CASES = {"narrow": 2, "wide": 22}
CONNECTION_CEILING = 12
SCHEMA_CHECK_CEILING = 10


@dataclass(frozen=True, slots=True)
class Fixture:
    left: bytes
    right: bytes
    payload_columns_per_side: int

    def evidence(self) -> dict[str, object]:
        return {
            "left_sha256": "sha256:" + sha256(self.left).hexdigest(),
            "right_sha256": "sha256:" + sha256(self.right).hexdigest(),
            "left_bytes": len(self.left),
            "right_bytes": len(self.right),
            "columns_per_input": self.payload_columns_per_side + 2,
            "projected_output_columns": self.payload_columns_per_side * 2,
            "mean_data_row_bytes": round(
                (len(self.left) + len(self.right)) / (2 * _data_row_count(self.left)),
                3,
            ),
            "max_data_row_bytes": max(
                max(len(row) for row in self.left.splitlines()[1:]),
                max(len(row) for row in self.right.splitlines()[1:]),
            ),
        }


class _PeakSampler:
    def __init__(self, process: psutil.Process, interval_seconds: float = 0.01) -> None:
        self.process = process
        self.interval_seconds = interval_seconds
        self.stop = Event()
        self.thread = Thread(target=self._sample, daemon=True)
        self.peak_bytes = 0

    def __enter__(self) -> "_PeakSampler":
        self._read()
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop.set()
        self.thread.join()
        self._read()

    def _sample(self) -> None:
        while not self.stop.wait(self.interval_seconds):
            self._read()

    def _read(self) -> None:
        self.peak_bytes = max(self.peak_bytes, int(self.process.memory_info().rss))


class _BrowserHarness(ProjectSetupBrowserTestCase):
    def runTest(self) -> None:  # noqa: N802 - unittest protocol
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows-per-input", type=int, default=2_000)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child-case", choices=tuple(CASES), help=argparse.SUPPRESS)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.rows_per_input < 1 or arguments.runs < 1:
        raise SystemExit("Rows and runs must be positive")
    if arguments.rows_per_input * 2 > 25_000:
        raise SystemExit("The fixture exceeds the current 25,000-row admission limit")
    if arguments.child_case:
        result = _run_child(arguments.child_case, arguments.rows_per_input)
        print(RESULT_PREFIX + json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0

    report = _run_parent(arguments.rows_per_input, arguments.runs)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


def _run_parent(rows_per_input: int, runs: int) -> dict[str, object]:
    cases: dict[str, object] = {}
    for case in CASES:
        case_runs = []
        for run_number in range(1, runs + 1):
            print(
                f"Measuring {case} source composition, run {run_number}/{runs}",
                file=sys.stderr,
                flush=True,
            )
            case_runs.append(_spawn_child(case, rows_per_input))
        cases[case] = {
            "runs": case_runs,
            "summary": _case_summary(case_runs),
        }
    gates = _validate(cases, rows_per_input)
    application_build_ids = {
        run["application_build_id"]
        for case in cases.values()
        for run in case["runs"]
    }
    if len(application_build_ids) != 1:
        raise RuntimeError("Measured processes did not use one application build")
    return {
        "result_schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "revision": _revision(),
        "worktree_dirty": _worktree_dirty(),
        "application_build_id": application_build_ids.pop(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "method": {
            "fresh_process_per_run": True,
            "source_intake_excluded_from_timing": True,
            "timings_are_regression_gates": False,
            "connection_ceiling_per_request": CONNECTION_CEILING,
            "schema_check_ceiling_per_request": SCHEMA_CHECK_CEILING,
            "current_browser_admission_limit_rows": 25_000,
        },
        "rows_per_input": rows_per_input,
        "runs_per_case": runs,
        "cases": cases,
        "gates": gates,
    }


def _spawn_child(case: str, rows_per_input: int) -> dict[str, object]:
    environment = os.environ.copy()
    source_path = str(SOURCE_ROOT)
    environment["PYTHONPATH"] = (
        source_path
        if not environment.get("PYTHONPATH")
        else source_path + os.pathsep + environment["PYTHONPATH"]
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child-case",
            case,
            "--rows-per-input",
            str(rows_per_input),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=900,
    )
    if completed.returncode:
        raise RuntimeError(
            f"Source-composition benchmark child failed ({case}):\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    marked = [
        line.removeprefix(RESULT_PREFIX)
        for line in completed.stdout.splitlines()
        if line.startswith(RESULT_PREFIX)
    ]
    if len(marked) != 1:
        raise RuntimeError("Benchmark child emitted invalid evidence")
    payload = json.loads(marked[0])
    if not isinstance(payload, dict):
        raise RuntimeError("Benchmark evidence is not an object")
    return payload


def _run_child(case: str, rows_per_input: int) -> dict[str, object]:
    fixture = build_fixture(rows_per_input, CASES[case])
    harness = _BrowserHarness(methodName="runTest")
    harness.setUp()
    try:
        workspace_id, selection, by_name = _freeze_fixture(harness, fixture)
        form = _join_form(harness, selection, by_name)
        preview, preview_response = _measure_request(
            lambda: harness.client.post(
                f"/workspaces/{workspace_id}/derived-entities/join/preview",
                data=form,
                headers=POST_HEADERS,
            )
        )
        if preview_response.status_code != 200:
            raise RuntimeError(f"Preview failed: {preview_response.text[:500]}")
        action = f"/workspaces/{workspace_id}/derived-entities/join/save"
        save_form = dict(form)
        save_form["rule_id"] = _hidden_value(preview_response.text, action, "rule_id")
        save_form["expected_parent_version"] = _hidden_value(
            preview_response.text,
            action,
            "expected_parent_version",
        )
        saved, save_response = _measure_request(
            lambda: harness.client.post(
                action,
                data=save_form,
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        )
        if save_response.status_code != 303:
            raise RuntimeError(f"Save failed: {save_response.text[:500]}")
        plan = harness.app.state.context.queries.get_derived_entity_plan(workspace_id)
        if plan is None or len(plan.join_reviews) != 1:
            raise RuntimeError("Saved join review was not persisted")
        review = plan.join_reviews[0].review.to_dict()
        semantic_output = _semantic_output(
            harness,
            workspace_id,
            selection,
            plan,
            by_name,
        )
        semantic_review = {
            key: value
            for key, value in review.items()
            if key not in {"examples", "output_table_hash", "lineage_hash"}
        }
        return {
            "case": case,
            "application_build_id": PROCESS_BUILD_CONTRACT.application_build_id,
            "fixture": fixture.evidence(),
            "rows_per_input": rows_per_input,
            "preview": preview,
            "save": saved,
            "semantic_review": semantic_review,
            "semantic_output": semantic_output,
            "persisted_output_table_hash": review["output_table_hash"],
            "persisted_lineage_hash": review["lineage_hash"],
            "odoo_calls": {
                "connection": len(harness.connection_calls),
                "schema": len(harness.schema_calls),
                "model_catalog": len(harness.model_catalog_calls),
                "read_identity": len(harness.read_identity_calls),
            },
        }
    finally:
        harness.tearDown()


def _semantic_output(harness, workspace_id, selection, plan, by_name):
    """Hash values and physical lineage without random workspace identities."""

    tables, _snapshots = harness.app.state.context.derived_entities._load_join_tables(
        workspace_id,
        selection,
    )
    execution = execute_structural_rules(
        selection=selection,
        loaded_tables=tables,
        rules=plan.rules,
    )
    if len(execution.outputs) != 1:
        raise RuntimeError("Qualification expected one structural output")
    output = execution.outputs[0]
    values = [
        [row.values.get(header) for header in output.table.headers]
        for row in output.table.rows
    ]
    roles = {
        by_name["left_accounts"].dataset_id: "left",
        by_name["right_details"].dataset_id: "right",
    }
    lineage = [
        {
            "output_row": row_number,
            "sources": {
                roles[dataset_id]: list(source_rows)
                for dataset_id, source_rows in sorted(
                    sources.items(),
                    key=lambda item: roles[item[0]],
                )
            },
        }
        for row_number, sources in sorted(output.lineage.items())
    ]
    return {
        "rows": len(values),
        "columns": len(output.table.headers),
        "values_sha256": "sha256:" + sha256(canonical_json_bytes(values)).hexdigest(),
        "lineage_sha256": (
            "sha256:" + sha256(canonical_json_bytes(lineage)).hexdigest()
        ),
    }


def build_fixture(rows: int, payload_columns_per_side: int) -> Fixture:
    left_payload = tuple(f"Left{index:02}" for index in range(payload_columns_per_side))
    right_payload = tuple(
        f"Right{index:02}" for index in range(payload_columns_per_side)
    )
    left_lines = [",".join(("Partition", "Party", *left_payload))]
    right_lines = [",".join(("Partition", "RecId", *right_payload))]
    for index in range(1, rows + 1):
        left_lines.append(
            ",".join(
                (
                    "part-1",
                    str(index),
                    *(_value("L", column, index) for column in left_payload),
                )
            )
        )
        right_lines.append(
            ",".join(
                (
                    "part-1",
                    str(index),
                    *(_value("R", column, index) for column in right_payload),
                )
            )
        )
    return Fixture(
        left=("\n".join(left_lines) + "\n").encode(),
        right=("\n".join(right_lines) + "\n").encode(),
        payload_columns_per_side=payload_columns_per_side,
    )


def _value(side: str, column: str, row: int) -> str:
    return f"{side}-{column}-{row:08}-fixture"


def _data_row_count(csv_bytes: bytes) -> int:
    return len(csv_bytes.splitlines()) - 1


def _freeze_fixture(harness: _BrowserHarness, fixture: Fixture):
    created = harness._post(
        "/projects/new",
        {
            "csrf_token": harness.csrf,
            "display_name": "Source composition scale fixture",
            "source_mode": "FILE",
            "source_system_identity": "Synthetic AX",
        },
    )
    workspace_id = _created_workspace_id(harness.app, created)
    uploaded = harness.client.post(
        f"/workspaces/{workspace_id}/files",
        data={"csrf_token": harness.csrf, "revision": "1"},
        files=[
            ("source_file", ("left.csv", fixture.left, "text/csv")),
            ("source_file", ("right.csv", fixture.right, "text/csv")),
        ],
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    if uploaded.status_code != 303:
        message = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", uploaded.text))
        raise RuntimeError(
            f"Upload failed ({uploaded.status_code}): {message[:1_000]}"
        )
    context = harness.app.state.context
    registered = harness._post(
        f"/workspaces/{workspace_id}/register",
        {
            "csrf_token": harness.csrf,
            "revision": str(context.queries.get(workspace_id).revision),
        },
    )
    if registered.status_code != 303:
        raise RuntimeError(f"Register failed: {registered.text[:500]}")
    catalogs = context.queries.get_source_catalogs(workspace_id)
    for catalog in catalogs:
        confirmed = harness.client.post(
            f"/workspaces/{workspace_id}/sources/{catalog.file_id}/configure",
            data={
                "csrf_token": harness.csrf,
                "action": "confirm",
                "encoding": "utf-8",
                "delimiter": ",",
                "header_row_0": "1",
                "selected_0": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        if confirmed.status_code != 303:
            message = re.sub(
                r"\s+",
                " ",
                re.sub(r"<[^>]+>", " ", confirmed.text),
            )
            raise RuntimeError(
                f"Configure failed ({confirmed.status_code}): {message[:1_000]}"
            )
    choices = _dataset_choices(context, workspace_id)
    names = {
        index: (
            "left_accounts"
            if choice["file_name"] == "left.csv"
            else "right_details"
        )
        for index, choice in enumerate(choices)
    }
    frozen = harness.client.post(
        f"/workspaces/{workspace_id}/datasets/freeze",
        data={
            "csrf_token": harness.csrf,
            **{f"dataset_name_{index}": name for index, name in names.items()},
        },
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    if frozen.status_code != 303:
        message = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", frozen.text))
        raise RuntimeError(
            f"Freeze failed ({frozen.status_code}): {message[:1_000]}"
        )
    selection = context.queries.get_source_selection(workspace_id)
    if selection is None:
        raise RuntimeError("Frozen source selection is missing")
    return workspace_id, selection, {item.name: item for item in selection.datasets}


def _join_form(harness: _BrowserHarness, selection, by_name) -> dict[str, object]:
    left = by_name["left_accounts"]
    right = by_name["right_details"]
    candidates = exact_join_projection_candidates(
        available_exact_join_selection(selection, None)
    )
    left_columns = {column.source_name: column for column in left.columns}
    right_columns = {column.source_name: column for column in right.columns}
    selected = {
        (dataset.dataset_id, column.source_name): (
            f"left_{column.source_name.lower()}"
            if dataset.dataset_id == left.dataset_id
            else f"right_{column.source_name.lower()}"
        )
        for dataset, column in candidates
        if column.source_name not in {"Partition", "Party", "RecId"}
    }
    fields: dict[str, object] = {
        "csrf_token": harness.csrf,
        "expected_parent_version": "",
        "output_dataset_name": "joined_customer",
        "left_dataset_id": left.dataset_id,
        "right_dataset_id": right.dataset_id,
        "kind": "LEFT",
        "left_dataset_use": "preparation_only",
        "right_dataset_use": "preparation_only",
        "key_left_1": f"{left.dataset_id}|{left_columns['Partition'].stable_key}",
        "key_right_1": f"{right.dataset_id}|{right_columns['Partition'].stable_key}",
        "key_type_1": "string",
        "key_left_2": f"{left.dataset_id}|{left_columns['Party'].stable_key}",
        "key_right_2": f"{right.dataset_id}|{right_columns['RecId'].stable_key}",
        "key_type_2": "integer",
    }
    for index in range(3, 6):
        fields[f"key_left_{index}"] = ""
        fields[f"key_right_{index}"] = ""
        fields[f"key_type_{index}"] = "string"
    fields["projection"] = [
        str(index)
        for index, (dataset, column) in enumerate(candidates)
        if (dataset.dataset_id, column.source_name) in selected
    ]
    fields["projection_name"] = [
        selected.get((dataset.dataset_id, column.source_name), column.source_name)
        for dataset, column in candidates
    ]
    return fields


def _hidden_value(page: str, action: str, name: str) -> str:
    form = re.search(
        rf'<form method="post" action="{re.escape(action)}">(.*?)</form>',
        page,
        re.DOTALL,
    )
    if form is None:
        raise RuntimeError(f"Form {action} was not rendered")
    value = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', form.group(1))
    if value is None:
        raise RuntimeError(f"Field {name} was not rendered")
    return value.group(1)


def _measure_request(request) -> tuple[dict[str, object], object]:
    phases: defaultdict[str, float] = defaultdict(float)

    def timed(name, function):
        def wrapper(*args, **kwargs):
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                phases[name] += (perf_counter() - started) * 1_000

        return wrapper

    process = psutil.Process()
    gc.collect()
    baseline = int(process.memory_info().rss)
    cpu_started = process_time()
    wall_started = perf_counter()
    with (
        patch.object(
            derived_application,
            "load_source_snapshot_table",
            timed(
                "source_snapshot_load",
                derived_application.load_source_snapshot_table,
            ),
        ),
        patch.object(
            derived_application,
            "review_exact_join",
            timed("exact_join_review", derived_application.review_exact_join),
        ),
        patch.object(
            derived_application,
            "execute_structural_rules",
            timed("structural_execution", derived_application.execute_structural_rules),
        ),
        collect_duckdb_request_timings() as database_timings,
        _PeakSampler(process) as sampler,
    ):
        response = request()
    wall_ms = (perf_counter() - wall_started) * 1_000
    cpu_ms = (process_time() - cpu_started) * 1_000
    ending = int(process.memory_info().rss)
    measured = sum(phases.values())
    return (
        {
            "status_code": response.status_code,
            "wall_ms": round(wall_ms, 3),
            "cpu_ms": round(cpu_ms, 3),
            "baseline_rss_mib": round(baseline / MIB, 3),
            "peak_rss_mib": round(sampler.peak_bytes / MIB, 3),
            "peak_rss_delta_mib": round(max(0, sampler.peak_bytes - baseline) / MIB, 3),
            "ending_rss_mib": round(ending / MIB, 3),
            "response_bytes": len(response.content),
            "application_phases_ms": {
                **{key: round(value, 3) for key, value in sorted(phases.items())},
                "other_request_work": round(max(0.0, wall_ms - measured), 3),
            },
            "server_timings_ms": parse_server_timing(
                response.headers.get("server-timing", "")
            ),
            "database": {
                "connect_ms": round(database_timings.connect_ms, 3),
                "schema_ms": round(database_timings.schema_ms, 3),
                "lock_wait_ms": round(database_timings.lock_wait_ms, 3),
                "connection_count": database_timings.connection_count,
                "schema_check_count": database_timings.schema_check_count,
                "lock_retry_count": database_timings.lock_retry_count,
            },
        },
        response,
    )


def _case_summary(runs: list[dict[str, object]]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for request_name in ("preview", "save"):
        request_runs = [run[request_name] for run in runs]
        assert all(isinstance(item, dict) for item in request_runs)
        summary[request_name] = {
            metric: round(
                statistics.median(float(item[metric]) for item in request_runs),
                3,
            )
            for metric in ("wall_ms", "cpu_ms", "peak_rss_delta_mib", "response_bytes")
        }
        phase_names = sorted(
            {name for item in request_runs for name in item["application_phases_ms"]}
        )
        summary[request_name]["median_application_phases_ms"] = {
            name: round(
                statistics.median(
                    float(item["application_phases_ms"].get(name, 0.0))
                    for item in request_runs
                ),
                3,
            )
            for name in phase_names
        }
    return summary


def _validate(cases: dict[str, object], rows_per_input: int) -> dict[str, object]:
    for case, payload in cases.items():
        runs = payload["runs"]
        fixture_hashes = {
            (run["fixture"]["left_sha256"], run["fixture"]["right_sha256"])
            for run in runs
        }
        semantics = {json.dumps(run["semantic_review"], sort_keys=True) for run in runs}
        semantic_outputs = {
            json.dumps(run["semantic_output"], sort_keys=True) for run in runs
        }
        if len(fixture_hashes) != 1:
            raise RuntimeError(f"{case} fixture hashes drifted across fresh processes")
        if len(semantics) != 1:
            raise RuntimeError(f"{case} join semantics drifted across fresh processes")
        if len(semantic_outputs) != 1:
            raise RuntimeError(
                f"{case} output values or physical lineage drifted across fresh processes"
            )
        for run in runs:
            review = run["semantic_review"]
            if review["actual_output_rows"] != rows_per_input or review["findings"]:
                raise RuntimeError(f"{case} did not preserve the declared join grain")
            if any(run["odoo_calls"].values()):
                raise RuntimeError(
                    f"{case} contacted Odoo during local source composition"
                )
            for request_name in ("preview", "save"):
                database = run[request_name]["database"]
                if not 0 < database["connection_count"] <= CONNECTION_CEILING:
                    raise RuntimeError(
                        f"{case} {request_name} exceeded connection ownership"
                    )
                if database["schema_check_count"] > SCHEMA_CHECK_CEILING:
                    raise RuntimeError(
                        f"{case} {request_name} exceeded schema ownership"
                    )
    return {
        "deterministic_fixture_hashes": "passed",
        "exact_result_parity_across_fresh_processes": "passed",
        "normalized_output_and_lineage_parity": "passed",
        "persisted_complete_review": "passed",
        "database_lifetime_ceilings": "passed",
        "zero_odoo_requests": "passed",
        "supported_limit_changed": False,
    }


def _revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def _worktree_dirty() -> bool:
    return bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
    )


if __name__ == "__main__":
    raise SystemExit(main())
