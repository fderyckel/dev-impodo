"""Measure file upload, registration, inspection and configuration in temporary stores.

The optional input directory supplies read-only CSV/XLSX copies. The benchmark
never opens a running application's DuckDB stores or contacts Odoo. Output
contains timings and counts, not filenames, source values or query parameters.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from contextlib import ExitStack
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from openpyxl import Workbook

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements


def measure(function, expected_status):
    """Capture one normal HTTP transition without bypassing file workers."""
    with (
        count_duckdb_statements() as statements,
        collect_duckdb_request_timings() as timings,
    ):
        started = perf_counter()
        response = function()
        elapsed = (perf_counter() - started) * 1000
    if response.status_code != expected_status or response.history:
        raise AssertionError(f"Unexpected response: {response.status_code}")
    return {
        "elapsed_ms": elapsed,
        "sql_statements": len(statements),
        "column_catalogue_reads": sum("duckdb_columns()" in sql for sql in statements),
        **asdict(timings),
    }


def catalog_signature(catalogs):
    """Compare inspection meaning privately while ignoring receipt timestamps."""
    payloads = []
    for catalog in catalogs:
        payload = json.loads(catalog.to_json())
        payload.pop("inspected_at")
        payload.pop("file_id")
        if payload["format"] == "CSV":
            for table in payload["tables"]:
                table.pop("name")
        payloads.append(payload)
    payloads.sort(key=lambda item: item["display_name"])
    return sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()


def exercise(inputs):
    """Run real upload and source-check commands in a disposable workspace."""
    fixture = ProjectSetupBrowserTestCase()
    fixture.setUp()
    try:
        context = fixture.app.state.context
        workspace = fixture.workspaces.create(
            name="File performance fixture", source_system="Files"
        )
        prefix = f"/workspaces/{workspace.workspace_id}/"
        results = {
            "files_empty": measure(
                lambda: fixture.client.get(prefix + "files", follow_redirects=False),
                200,
            )
        }
        with ExitStack() as stack:
            uploads = [
                (
                    "source_file",
                    (
                        f"input-{index}{path.suffix}",
                        stack.enter_context(path.open("rb")),
                        "application/octet-stream",
                    ),
                )
                for index, path in enumerate(inputs)
            ]
            results["upload"] = measure(
                lambda: fixture.client.post(
                    prefix + "files",
                    data={
                        "csrf_token": fixture.csrf,
                        "revision": str(workspace.revision),
                    },
                    files=uploads,
                    headers=POST_HEADERS,
                    follow_redirects=False,
                ),
                303,
            )
        results["files_uploaded"] = measure(
            lambda: fixture.client.get(prefix + "files", follow_redirects=False), 200
        )
        current = context.queries.get(workspace.workspace_id)
        results["register_and_inspect"] = measure(
            lambda: fixture._post(
                prefix + "register",
                {"csrf_token": fixture.csrf, "revision": str(current.revision)},
            ),
            303,
        )
        catalogs = context.queries.get_source_catalogs(workspace.workspace_id)
        signature = catalog_signature(catalogs)
        results["sources_page"] = measure(
            lambda: fixture.client.get(prefix + "sources", follow_redirects=False), 200
        )
        results["reinspect"] = measure(
            lambda: fixture._post(
                prefix + "sources/inspect",
                {"csrf_token": fixture.csrf},
            ),
            303,
        )
        assert signature == catalog_signature(
            context.queries.get_source_catalogs(workspace.workspace_id)
        )
        # A per-file preview uses the same guarded inspector with explicit
        # options. Choose the smaller file to keep the benchmark focused.
        catalog = min(catalogs, key=lambda c: c.source_size_bytes)
        form = {"csrf_token": fixture.csrf, "action": "preview"}
        if catalog.format == "CSV":
            form.update(
                encoding=catalog.encoding or "", delimiter=catalog.delimiter or ""
            )
        for index, table in enumerate(catalog.tables):
            if table.header_row is not None:
                form[f"header_row_{index}"] = str(table.header_row)
        results["configure_preview"] = measure(
            lambda: fixture._post(
                prefix + f"sources/{catalog.file_id}/configure",
                form,
            ),
            303,
        )
        assert signature == catalog_signature(
            context.queries.get_source_catalogs(workspace.workspace_id)
        )
        assert not fixture.connection_calls and not fixture.schema_calls
        return results, signature
    finally:
        fixture.tearDown()


def generated_inputs(directory, rows, columns):
    """Generate fictional wide and narrow workbooks with sparse cells."""
    result = []
    for index, width in enumerate((columns, 3)):
        path = directory / f"input-{index}.xlsx"
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Fictional source")
        sheet.append([f"Field {column}" for column in range(width)])
        for row in range(rows):
            sheet.append(
                [
                    f"Value {row}-{column}"
                    if column % 4 == 0 or column == width - 1
                    else None
                    for column in range(width)
                ]
            )
        workbook.save(path)
        workbook.close()
        result.append(path)
    return result


def main():
    """Warm once and print matched, value-free samples and medians."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-directory", type=Path)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--rows", type=int, default=2_000)
    parser.add_argument("--columns", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.samples, args.rows, args.columns) < 1:
        parser.error("Samples, rows and columns must be positive")
    with TemporaryDirectory() as temporary:
        inputs = (
            sorted(
                p
                for p in args.input_directory.iterdir()
                if p.suffix.casefold() in {".csv", ".xlsx"}
            )
            if args.input_directory
            else generated_inputs(Path(temporary), args.rows, args.columns)
        )
        if not inputs:
            parser.error("No CSV or XLSX inputs found")
        signatures = [sha256(path.read_bytes()).hexdigest() for path in inputs]
        exercise(inputs)
        samples = [exercise(inputs) for _ in range(args.samples)]
        assert len({signature for _, signature in samples}) == 1
        assert signatures == [sha256(path.read_bytes()).hexdigest() for path in inputs]
        routes = {
            route: {
                "median": {
                    key: statistics.median(sample[0][route][key] for sample in samples)
                    for key in samples[0][0][route]
                },
                "samples": [sample[0][route] for sample in samples],
            }
            for route in samples[0][0]
        }
        result = {
            "evidence": "isolated temporary stores and normal resource-bounded source workers",
            "application_build_id": PROCESS_BUILD_CONTRACT.application_build_id,
            "python_version": sys.version.split()[0],
            "input_file_bytes": [path.stat().st_size for path in inputs],
            "catalog_semantic_signature": samples[0][1],
            "source_bytes_unchanged": True,
            "no_odoo_calls": True,
            "routes": routes,
        }
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(
            json.dumps(
                {route: value["median"] for route, value in routes.items()}, indent=2
            )
        )


if __name__ == "__main__":
    main()
