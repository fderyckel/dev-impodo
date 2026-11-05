"""Keep recovery reasons and outcomes through privacy-checked diagnostic export."""

import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from zipfile import ZipFile

from fastapi import Request

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.workspace.supporting_review import (
    SupportingReviewProblem,
    SupportingReviewRequiredError,
)
from impodo.web.diagnostics import LocalDiagnosticRecorder, create_diagnostic_bundle
from impodo.web.supporting_access_diagnostics import (
    record_supporting_access,
    sanitize_supporting_access,
)
from impodo.web.supporting_diagnostics import set_diagnostic_supporting_failure


class SupportingAccessDiagnosticTests(unittest.TestCase):
    def test_both_majors_export_exact_changes_and_recovery_after_health_noise(self):
        for major in (19, 20):
            with self.subTest(major=major), TemporaryDirectory() as tmp:
                previous = SimpleNamespace(
                    odoo_version=f"{major}.0",
                    pending_refresh=None,
                    read_credential_binding_hash="private-old-key-binding",
                    read_context_hash="private-company",
                    read_principal_hash="private-user",
                    read_permission_hash="private-permission",
                    models=(),
                    connection_target_hash="private-target",
                    policy_hash="private-policy",
                )
                current = SimpleNamespace(
                    **{
                        **vars(previous),
                        "read_credential_binding_hash": "private-new-key-binding",
                    }
                )
                before = SimpleNamespace(version=3, choices=(object(),))
                after = SimpleNamespace(version=4, choices=before.choices)
                request = Request({"type": "http", "state": {}})
                with collect_duckdb_request_timings() as timings:
                    record_supporting_access(
                        request,
                        trigger="CHECK_MATCHES",
                        previous=previous,
                        current=current,
                        before=before,
                        after=after,
                        outcome="REVALIDATED",
                    )
                self.assertEqual(timings.connection_count, 0)
                recorder = LocalDiagnosticRecorder(Path(tmp))
                recorder.record_request(
                    request_id="recovery",
                    method="POST",
                    status_code=200,
                    route_template="/workspaces/{workspace_id}/mapping/save",
                    duration_ms=1,
                    supporting_access=request.state.diagnostic_supporting_access,
                )
                for index in range(5):
                    recorder.record_request(
                        request_id=f"health-{index}",
                        method="GET",
                        route_template="/health",
                        status_code=200,
                        duration_ms=1,
                    )
                recorder.close()
                with ZipFile(
                    BytesIO(
                        create_diagnostic_bundle(
                            tmp, build_contract=PROCESS_BUILD_CONTRACT, max_records=2
                        )
                    )
                ) as archive:
                    transitions = json.loads(archive.read("supporting-access.json"))
                    self.assertEqual(len(transitions), 1)
                    self.assertEqual(
                        transitions[0]["supporting_access"],
                        {
                            "trigger": "CHECK_MATCHES",
                            "outcome": "REVALIDATED",
                            "changes": ["CREDENTIAL_VERSION"],
                            "odoo_major": major,
                            "previous_review_version": 3,
                            "review_version": 4,
                            "choice_count": 1,
                        },
                    )
                    self.assertEqual(
                        json.loads(archive.read("manifest.json"))[
                            "supporting_access_record_count"
                        ],
                        1,
                    )
                    self.assertNotIn(
                        "private-",
                        b"\n".join(
                            archive.read(name) for name in archive.namelist()
                        ).decode(),
                    )

    def test_export_resanitizes_untrusted_codes_types_and_business_details(self):
        for raw in (
            None,
            [],
            {},
            {"trigger": [], "outcome": "REVALIDATED"},
            {"trigger": "CHECK_MATCHES", "outcome": ["secret"]},
        ):
            self.assertIsNone(sanitize_supporting_access(raw))
        with TemporaryDirectory() as tmp:
            recorder = LocalDiagnosticRecorder(Path(tmp))
            recorder._write(
                {
                    "event": "request_completed",
                    "status_code": 303,
                    "supporting_access": {
                        "trigger": "COMPANY_REFRESH",
                        "outcome": "REVIEW_REQUIRED",
                        "changes": ["COMPANY_CONTEXT", "secret", ["secret"]],
                        "review_version": True,
                        "choice_count": -1,
                        "odoo_major": 99,
                        "company": "secret",
                        "token": "secret",
                    },
                }
            )
            recorder.close()
            with ZipFile(
                BytesIO(
                    create_diagnostic_bundle(tmp, build_contract=PROCESS_BUILD_CONTRACT)
                )
            ) as archive:
                failures = json.loads(archive.read("recent-failures.json"))
                self.assertEqual(
                    failures[0]["supporting_access"],
                    {
                        "trigger": "COMPANY_REFRESH",
                        "outcome": "REVIEW_REQUIRED",
                        "changes": ["COMPANY_CONTEXT"],
                    },
                )
                self.assertNotIn(
                    "secret", archive.read("supporting-access.json").decode()
                )

    def test_handled_stale_review_exports_problem_without_exception_text(self):
        request = Request({"type": "http", "state": {}})
        set_diagnostic_supporting_failure(
            request,
            SupportingReviewRequiredError(
                SupportingReviewProblem.STALE, "private-cause"
            ),
        )
        self.assertEqual(
            request.state.diagnostic_validation_failure,
            {
                "failure_code": "SUPPORTING_REVIEW_REQUIRED",
                "stage": "supporting_review",
                "problem": "STALE",
            },
        )
