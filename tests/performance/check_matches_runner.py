"""Measure Stage 3 Check matches and its result page on isolated synthetic files.

Run `python -m tests.performance.check_matches_runner` on each fixed candidate. Each invocation measures a
first check in a fresh workspace, a new explicit check of the same mapping,
and reopening each result. Use separate fresh processes for repeated samples.
The in-process HTTP client measures server responses, excluding browser paint
and network time. Pass --structure in a separate run to collect query counts
and per-phase DuckDB timings. Each phase includes its database and fictional
Odoo callback durations. Lock wait is part of connection setup. Supporting
save measures the publication callback. Opening and closing its dedicated
scope belongs to supporting_scope. If the save shares the final publication
scope, closing that owner belongs to publication_and_response.
"""

from __future__ import annotations

import argparse
import json
import platform
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict, replace
from time import perf_counter, process_time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import duckdb
import psutil

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.shared.models import TargetRecord
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition import target_readers
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.routers import mapping as mapping_routes
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_correction import MappingForm
from tests.support.mapping_journey import MappingJourneyFixture


class _PostPhases:
    """Partition cumulative counters across the serial phases of one test POST."""

    def __init__(self):
        self.totals = {}
        self.previous = None
        self.phase = None
        self.odoo_seconds = 0.0
        self.in_odoo_callback = False

    def begin(self, statements, db):
        # TestClient and each awaited worker record into this one collector.
        self.statements = statements
        self.db = db
        self.switch("admission")

    def switch(self, phase):
        current = {
            **asdict(self.db),
            "sql": len(self.statements),
            "odoo_fixture_ms": self.odoo_seconds * 1000,
            "wall_ms": perf_counter() * 1000,
        }
        if self.previous is not None:
            self.totals.setdefault(self.phase, Counter()).update({
                key: value - self.previous[key] for key, value in current.items()
            })
        self.previous = current
        self.phase = phase

    def observe_read(self, callback):
        def measured(*args, **kwargs):
            if self.in_odoo_callback:
                return callback(*args, **kwargs)
            start = perf_counter()
            self.in_odoo_callback = True
            try:
                return callback(*args, **kwargs)
            finally:
                self.odoo_seconds += perf_counter() - start
                self.in_odoo_callback = False

        return measured

    def attach(self, fixture, stack):
        # These are the injected adapter boundaries. Calling the original
        # doubles keeps their normal responses and verifies the requested keys.
        for owner, names in (
            (fixture.context, (
                "read_identity_probe", "schema_reader", "model_catalog_reader", "readiness_reader",
            )),
            (fixture.local_odoo_reader, (
                "get_model_metadata", "get_company_access", "get_model_catalog", "get_preflight_snapshots",
            )),
        ):
            for name in names:
                callback = getattr(owner, name)
                if callback is not None:
                    stack.enter_context(patch.object(
                        owner, name, side_effect=self.observe_read(callback),
                    ))
        read_group = target_readers._read_planned_supporting_values

        def read_supporting(*args, **kwargs):
            self.switch("supporting_read")
            try:
                return read_group(*args, **kwargs)
            finally:
                self.switch("supporting_verification")

        stack.enter_context(patch.object(
            target_readers, "_read_planned_supporting_values", read_supporting,
        ))
        capture = target_readers._capture_supporting_values

        def capture_supporting(*args, publish_operation, **kwargs):
            def schedule_publication(publish):
                def save():
                    previous = self.phase
                    self.switch("supporting_save")
                    try:
                        return publish()
                    finally:
                        self.switch(previous)

                previous = self.phase
                self.switch("supporting_scope")
                try:
                    return publish_operation(save)
                finally:
                    self.switch(previous)

            return capture(*args, publish_operation=schedule_publication, **kwargs)

        stack.enter_context(patch.object(
            target_readers, "_capture_supporting_values", capture_supporting,
        ))


class CheckFixture(MappingJourneyFixture):
    """Count fictional Odoo reads and supply the same two category matches."""

    def setUp(self):
        super().setUp()
        self.reads = Counter()

    def read_identity(self, state, key, models):
        self.reads["identity"] += 1
        return super().read_identity(state, key, models)

    def snapshots(self, state, metadata_requests, record_requests):
        self.reads["snapshots"] += 1
        metadata, records = super().snapshots(state, metadata_requests, record_requests)
        requested = {request.model for request in record_requests}
        if "x.category" in requested:
            self.assertEqual(len(record_requests), 1)
            self.assertEqual(
                record_requests[0].domain, (("name", "in", ("Parts", "Tools")),)
            )
            self.reads["record_queries"] += len(record_requests)
            records = replace(
                records,
                records={
                    "x.category": tuple(
                        TargetRecord("x.category", index, {"name": name})
                        for index, name in enumerate(("Tools", "Parts"), 1)
                    )
                },
            )
        return metadata, records

    def read_counts(self):
        return self.reads + Counter(
            {
                "metadata": len(self.schema_calls),
                "catalog": len(self.model_catalog_calls),
                "local_metadata": self.local_odoo_reader.get_model_metadata.call_count,
                "local_company": self.local_odoo_reader.get_company_access.call_count,
            }
        )

    def create_case(self, major, mode, workload, rows):
        linked = workload == "linked"
        files = {}
        for name in ("products", "spares") if linked else ("products",):
            data = ["Reference,Product title,Category"]
            for index in range(rows if linked else 1):
                category = "Tools" if index % 2 == 0 else "Parts"
                data.append(f"{name}-{index},Item {index},{category}")
            files[name] = ("\n".join(data) + "\n").encode()
        workspace_id, selection, _ = self.configure(
            major, mode, files=files, category_is_supporting=True
        )
        state = self.context.queries.get(workspace_id)
        self.context.workspace_states.select_odoo_company(
            workspace_id,
            actor=self.actor,
            expected_revision=state.revision,
            selection=self.access.choices[1],
        )
        reader = self.context.schema_reader

        def count_metadata(*args):
            self.schema_calls.append("metadata")
            return reader(*args)

        self.context.schema_reader = count_metadata
        mappings = []
        for dataset in selection.datasets:
            reference, title, category = (
                column.stable_key for column in dataset.columns
            )
            links = ()
            if linked:
                resolver = RelationshipResolver(
                    ResolverOrigin.TARGET_CATALOG,
                    model="x.category",
                    key_mappings=(ReferenceKeyMapping(category, "name"),),
                )
                links = (
                    RelationshipMapping(
                        "category_id",
                        "many2one",
                        (category,),
                        resolver,
                        categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                        selected_matching_field=True,
                    ),
                )
            fields = (
                () if workload == "missing" else (ScalarFieldMapping("name", title),)
            )
            mappings.append(
                DatasetMapping(
                    dataset.dataset_id,
                    "x.item",
                    source_identity_column_keys=(reference,),
                    target_identity=(IdentityComponentMapping((reference,), ("ref",)),),
                    fields=fields,
                    relationships=links,
                )
            )
        run_local_operation_sync(
            self.context.mapping_workspace.save_working_draft,
            workspace_id,
            datasets=tuple(mappings),
            expected_version=None,
            actor=self.actor,
        )
        return workspace_id

    def request(self, callback, *, structure, phases=None):
        previous = self.read_counts()
        with ExitStack() as stack:
            if structure:
                sql = stack.enter_context(count_duckdb_statements())
                db = stack.enter_context(collect_duckdb_request_timings())
                if phases is not None:
                    phases.begin(sql, db)
            start = perf_counter()
            cpu = process_time()
            response = callback()
            if phases is not None:
                phases.switch(None)
            result = {
                "wall_ms": round((perf_counter() - start) * 1000, 2),
                "cpu_ms": round((process_time() - cpu) * 1000, 2),
                "odoo_fixture_reads": dict(self.read_counts() - previous),
            }
            result["rss_after_mib"] = round(
                psutil.Process().memory_info().rss / (1024 * 1024), 2
            )
            if structure:
                result.update(sql=len(sql), **asdict(db))
                if phases is not None:
                    result["phases"] = phases.totals
                    for key in ("sql", "connection_count", "schema_check_count", "lock_retry_count"):
                        self.assertEqual(
                            sum(phase[key] for phase in phases.totals.values()), result[key],
                        )
                    result["odoo_fixture_ms"] = phases.odoo_seconds * 1000
        self.assertEqual(response.status_code, 200, response.text[:900])
        return response, result


def check_once(fixture, workspace_id, workload, structure):
    """Run a new check and read the saved result twice under the same actor."""
    path = f"/workspaces/{workspace_id}/mapping"
    source_page = fixture.client.get(path + "?step=check")
    fixture.assertEqual(source_page.status_code, 200)
    form = MappingForm(source_page.text)
    entries = [
        entry for entry in form.entries if entry[0] not in {"operation_id", "action"}
    ]
    entries.extend([["operation_id", str(uuid4())], ["action", "draft"]])
    timings = {}
    phases = _PostPhases() if structure else None
    access = mapping_routes.check_matching_access
    capture = mapping_routes._capture_mapping_supporting_values

    async def check_access(*values, **options):
        if phases is not None:
            phases.switch("access")
        timings["access_start"] = perf_counter()
        result = await access(*values, **options)
        timings["access_end"] = perf_counter()
        if phases is not None:
            phases.switch("dispatch_to_supporting")
        return result

    def supporting(*values, **options):
        if phases is not None:
            phases.switch("supporting_inputs")
        timings["support_start"] = perf_counter()
        result = capture(*values, **options)
        timings["support_end"] = perf_counter()
        if phases is not None:
            phases.switch("publication_and_response")
        return result

    def submit():
        timings["start"] = perf_counter()
        response = fixture.client.post(
            form.action,
            json={"entries": entries},
            headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
            follow_redirects=False,
        )
        timings["end"] = perf_counter()
        return response

    with (
        ExitStack() as stack,
        patch.object(mapping_routes, "check_matching_access", check_access),
        patch.object(mapping_routes, "_capture_mapping_supporting_values", supporting),
    ):
        if phases is not None:
            phases.attach(fixture, stack)
        result, post = fixture.request(submit, structure=structure, phases=phases)
    payload = result.json()
    fixture.assertEqual(payload["status"], "committed", payload)
    destination = payload["redirect_url"]
    route = urlsplit(destination)
    step = parse_qs(route.query)["step"][0]
    first_remote = timings.get("access_start", timings["support_start"])
    post["admission_ms"] = round((first_remote - timings["start"]) * 1000, 2)
    post["access_ms"] = round(
        (timings.get("access_end", first_remote) - first_remote) * 1000, 2
    )
    post["supporting_ms"] = round(
        (timings["support_end"] - timings["support_start"]) * 1000, 2
    )
    post["publication_and_response_ms"] = round(
        (timings["end"] - timings["support_end"]) * 1000, 2
    )

    def open_result():
        return fixture.client.get(destination)

    page, get = fixture.request(open_result, structure=structure)
    opened_again, reopen = fixture.request(open_result, structure=structure)
    revision = fixture.context.queries.get_mapping_revision(workspace_id)
    validation = fixture.context.queries.get_mapping_validation(
        workspace_id, revision.version
    )
    fixture.assertIsNone(
        fixture.context.queries.get_mapping_submission(workspace_id, revision.version)
    )
    if workload == "missing":
        fixture.assertEqual(step, "check")
        fixture.assertEqual(route.fragment, "next-step-blockers")
        fixture.assertIn(
            "MAPPING_REQUIRED_FIELD_UNMAPPED",
            {issue.code for issue in validation.issues},
        )
        expected_control = 'id="next-step-blockers"'
    else:
        fixture.assertIn(step, ("check", "confirm"))
        fixture.assertNotEqual(validation.status.value, "INVALID")
        expected_control = (
            "data-confirm-mapping"
            if step == "confirm"
            else 'data-mapping-go-step="confirm"'
        )
    for result_page in (page, opened_again):
        fixture.assertIn(expected_control, result_page.text)
        fixture.assertIn(f'data-step="{step}"', result_page.text)
    fixture.assertFalse(get["odoo_fixture_reads"] or reopen["odoo_fixture_reads"])
    if workload == "linked":
        fixture.assertEqual(len(revision.definition.datasets), 2)
        fixture.assertEqual(post["odoo_fixture_reads"]["record_queries"], 1)
    return {"step": step, "check": post, "result": get, "reopen": reopen}


def run(args):
    fixture = CheckFixture()
    fixture.setUp()
    try:
        workspace_id = fixture.create_case(
            args.major,
            OdooConnectionMode(args.mode),
            args.workload,
            args.rows_per_table,
        )
        return {
            label: check_once(fixture, workspace_id, args.workload, args.structure)
            for label in ("first", "repeat")
        }
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--major", type=int, choices=(19, 20), default=20)
    parser.add_argument("--mode", choices=("LOCAL", "REMOTE"), default="REMOTE")
    parser.add_argument(
        "--workload", choices=("valid", "missing", "linked"), default="valid"
    )
    parser.add_argument("--rows-per-table", type=int, default=500)
    parser.add_argument("--structure", action="store_true")
    args = parser.parse_args()
    if args.rows_per_table < 2:
        parser.error("--rows-per-table must be at least two")
    print(
        json.dumps(
            {
                "major": args.major,
                "mode": args.mode,
                "workload": args.workload,
                "physical_rows": 2 * args.rows_per_table
                if args.workload == "linked"
                else 1,
                "structural_probe": args.structure,
                "build": PROCESS_BUILD_CONTRACT.application_build_id,
                "platform": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "duckdb": duckdb.__version__,
                **run(args),
            }
        )
    )
