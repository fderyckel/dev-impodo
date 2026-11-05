"""Handled validation failures survive recording and privacy-checked ZIP export."""

from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4
from zipfile import ZipFile

from fastapi import Request

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.workspace.supporting_closure import close_supporting_plan
from impodo.domain.workspace.supporting_errors import SupportingPlanIncompleteError
from impodo.domain.workspace.supporting_review import SupportingChoiceAction, SupportingMappingConflict, SupportingReviewProblem, SupportingReviewRequiredError
from impodo.web.diagnostics import LocalDiagnosticRecorder, create_diagnostic_bundle
from impodo.web.supporting_diagnostics import (
    MAX_SUPPORTING_DIAGNOSTIC_ISSUES,
    sanitize_supporting_failure,
    set_diagnostic_supporting_failure,
)
from tests.domain.workspace.test_supporting_plan import dataset, model, relation


def supporting_error(version="20.0+e", count=1):
    fields = tuple(replace(
        relation(f"line_{i}_ids", "x.line", kind="one2many"), relation_field="partner_id"
    ) for i in range(count))
    schema = SimpleNamespace(odoo_version=version, content_hash="schema", models=(model("res.partner", *fields),))
    mappings = (DatasetMapping("contacts", "res.partner"),)
    closed = close_supporting_plan(schema=schema, datasets=(dataset("contacts"),), mappings=mappings,
                                   choices=(), source_hash="source", preparation_hash=None)
    return SupportingPlanIncompleteError(closed.proposal, schema, mappings, ())


class SupportingDiagnosticTests(unittest.TestCase):
    def test_mapping_conflicts_export_field_names_and_bounds_without_dataset_values(self):
        with TemporaryDirectory() as tmp:
            error = SupportingReviewRequiredError(SupportingReviewProblem.MAPPING_CONFLICT,
                "private-cause", conflicts=tuple(SupportingMappingConflict("mrp.bom", f"x_product_{i}",
                    "private-dataset", SupportingChoiceAction.REUSE) for i in range(25)))
            request = Request({"type": "http", "state": {}})
            set_diagnostic_supporting_failure(request, error)
            recorder = LocalDiagnosticRecorder(Path(tmp))
            recorder.record_request(request_id="conflict", method="POST", route_template="/mapping/save",
                status_code=422, duration_ms=1, validation_failure=request.state.diagnostic_validation_failure)
            recorder.close()
            with ZipFile(BytesIO(create_diagnostic_bundle(tmp, build_contract=PROCESS_BUILD_CONTRACT))) as archive:
                failure = json.loads(archive.read("recent-failures.json"))[0]["validation_failure"]
                self.assertEqual(failure["issues"][0]["field"], "x_product_0")
                self.assertEqual(failure["issue_count"], 25)
                self.assertEqual(len(failure["issues"]), 20)
                self.assertTrue(failure["issues_truncated"])
                self.assertNotIn("private-", b"\n".join(archive.read(name) for name in archive.namelist()).decode())

    def test_both_majors_keep_codes_counts_and_identifiers_without_extra_reads(self):
        for major in (19, 20):
            with self.subTest(major=major), TemporaryDirectory() as tmp:
                error = supporting_error(f"{major}.0+e", 25)
                request = Request({"type": "http", "state": {}})
                with collect_duckdb_request_timings() as timings:
                    set_diagnostic_supporting_failure(request, error)
                self.assertEqual(timings.connection_count, 0)
                operation_id = str(uuid4())
                recorder = LocalDiagnosticRecorder(Path(tmp))
                recorder.record_request(
                    request_id="request-id", method="POST",
                    route_template="/workspaces/{workspace_id}/mapping/save",
                    status_code=422, duration_ms=10, mapping_operation_id=operation_id,
                    working_draft_version=30, mapping_action="CHECK_MATCHES",
                    validation_failure=request.state.diagnostic_validation_failure,
                )
                recorder.close()
                bundle = create_diagnostic_bundle(tmp, build_contract=PROCESS_BUILD_CONTRACT)
                with ZipFile(BytesIO(bundle)) as archive:
                    failure = json.loads(archive.read("recent-failures.json"))[0]
                    manifest = json.loads(archive.read("manifest.json"))
                    record = json.loads(archive.read("diagnostics.jsonl"))
                self.assertEqual(failure, record)
                self.assertEqual(failure["mapping_operation_id"], operation_id)
                self.assertEqual(failure["mapping_action"], "CHECK_MATCHES")
                self.assertEqual(failure["working_draft_version"], 30)
                detail = failure["validation_failure"]
                self.assertEqual(detail["odoo_major"], major)
                self.assertEqual(detail["failure_code"], "SUPPORTING_PLAN_INCOMPLETE")
                self.assertEqual(detail["issue_count"], 25)
                self.assertTrue(detail["issues_truncated"])
                self.assertEqual(len(detail["issues"]), MAX_SUPPORTING_DIAGNOSTIC_ISSUES)
                self.assertEqual(detail["issues"][0]["source_model"], "res.partner")
                self.assertEqual(detail["issues"][0]["related_model"], "x.line")
                self.assertEqual(detail["issues"][0]["field_type"], "one2many")
                self.assertEqual(detail["issues"][0]["trigger"], "REQUIRED_RELATIONSHIP")
                self.assertTrue(detail["issues"][0]["required"])
                self.assertFalse(detail["issues"][0]["used_by_mapping"])
                self.assertEqual(detail["issues"][0]["saved_choice"], "UNSET")
                self.assertEqual(manifest["failure_record_count"], 1)
                self.assertIn("model and field identifiers", manifest["privacy"])

    def test_export_resanitizes_nested_metadata_and_retains_fast_failures_before_health_noise(self):
        with TemporaryDirectory() as tmp:
            recorder = LocalDiagnosticRecorder(Path(tmp))
            request = Request({"type": "http", "state": {}})
            set_diagnostic_supporting_failure(request, supporting_error())
            failure = request.state.diagnostic_validation_failure
            failure["formula"] = "secret-formula"
            failure["issues"][0].update(
                source_model="https://private.example", field="<script>secret</script>",
                field_label="Private label", source_value="Private customer", reason="INCOMING_UNAVAILABLE",
            )
            failure["issues"].append({"reason": ["bad-type"], "token": "secret-token"})
            # Simulate a stale writer or hand-edited log; export must validate again.
            recorder._write({
                "event": "request_completed", "request_id": "failed",
                "method": "POST", "status_code": 422, "duration_ms": 2, "slow": False,
                "validation_failure": failure, "mapping_action": ["secret-token"],
                "exception_text": "secret-exception", "cookie": "secret-cookie",
            })
            for i in range(5):
                recorder.record_request(request_id=f"health-{i}", method="GET", route_template="/health",
                                        status_code=200, duration_ms=1)
            recorder.close()
            bundle = create_diagnostic_bundle(tmp, build_contract=PROCESS_BUILD_CONTRACT, max_records=2)
            with ZipFile(BytesIO(bundle)) as archive:
                encoded = "\n".join(archive.read(name).decode() for name in archive.namelist())
                failures = json.loads(archive.read("recent-failures.json"))
                latest = archive.read("diagnostics.jsonl").decode()
            self.assertEqual(len(failures), 1)
            self.assertNotIn('"request_id":"failed"', latest)
            self.assertTrue(failures[0]["validation_failure"]["issues_truncated"])
            for private in ("private.example", "script", "secret", "Private label", "Private customer", "source_value", "exception_text", "cookie"):
                self.assertNotIn(private, encoded)

    def test_unknown_codes_and_wrong_types_are_not_exportable(self):
        for value in (None, [], {"failure_code": "private-text"},
                      {"failure_code": "SUPPORTING_PLAN_INCOMPLETE", "issue_count": True, "issues": []}):
            self.assertIsNone(sanitize_supporting_failure(value))

    def test_recent_failure_summary_has_its_own_bound(self):
        with TemporaryDirectory() as tmp:
            recorder = LocalDiagnosticRecorder(Path(tmp))
            for i in range(110):
                recorder.record_request(request_id=f"failed-{i}", method="POST", route_template="/mapping/save",
                                        status_code=422, duration_ms=1)
            recorder.close()
            bundle = create_diagnostic_bundle(tmp, build_contract=PROCESS_BUILD_CONTRACT, max_records=2)
            with ZipFile(BytesIO(bundle)) as archive:
                failures = json.loads(archive.read("recent-failures.json"))
            self.assertEqual(len(failures), 100)
            self.assertEqual(failures[0]["request_id"], "failed-10")
            self.assertEqual(failures[-1]["request_id"], "failed-109")
