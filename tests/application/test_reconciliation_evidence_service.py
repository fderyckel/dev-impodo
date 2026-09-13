from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.application.reconciliation_evidence_service import (
    RECONCILIATION_DETAIL_STORAGE_NAME,
    ReconciliationEvidenceService,
)
from impodo.domain.reconciliation import ReconciliationRun, ReconciliationRunStatus
from impodo.domain.reconciliation_detail import (
    ReconciliationDetailArtifact,
    ReconciliationFieldDifference,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.workspace.errors import WorkspaceError


HASH = "sha256:" + "1" * 64
TARGET_HASH = "sha256:" + "2" * 64


class _Authorization:
    def __init__(self) -> None:
        self.calls = []

    def require(self, actor, capability, *, workspace_id):
        self.calls.append((actor, capability, workspace_id))
        return SimpleNamespace(workspace_id=workspace_id)


class _MemoryArtifacts:
    def __init__(self) -> None:
        self.values = {}

    def write_report(self, workspace_id, run_id, filename, content):
        self.values[(workspace_id, run_id, filename)] = content

    def delete_report(self, workspace_id, run_id, filename):
        self.values.pop((workspace_id, run_id, filename), None)

    @contextmanager
    def materialize_report(self, workspace_id, run_id, filename):
        content = self.values[(workspace_id, run_id, filename)]
        yield SimpleNamespace(read_bytes=lambda: content)


def _report() -> ReconciliationRun:
    return ReconciliationRun(
        reconciliation_id=str(uuid4()),
        workspace_id=str(uuid4()),
        execution_run_id=str(uuid4()),
        snapshot_hash=HASH,
        target_hash=TARGET_HASH,
        target_database="target",
        status=ReconciliationRunStatus.VERIFIED,
        verified_at=datetime.now(timezone.utc),
        verified_by="Local operator",
        unchanged_count=0,
        rows=(),
    )


def _detail(report: ReconciliationRun) -> ReconciliationDetailArtifact:
    return ReconciliationDetailArtifact(
        reconciliation_id=report.reconciliation_id,
        workspace_id=report.workspace_id,
        execution_run_id=report.execution_run_id,
        snapshot_hash=report.snapshot_hash,
        target_hash=report.target_hash,
        differences=(
            ReconciliationFieldDifference(
                row_id="product-1",
                dataset="products",
                source_row=2,
                source_trace_id="trace-1",
                target_model="product.template",
                operation="CREATE",
                odoo_id=42,
                field="weight",
                expected_value=Decimal("0.003"),
                observed_value=0.0,
                field_type="float",
                target_digits=(16, 2),
                reason_code="TARGET_NUMERIC_PRECISION_LOSS",
            ),
        ),
    )


class ReconciliationEvidenceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authorization = _Authorization()
        self.artifacts = _MemoryArtifacts()
        self.service = ReconciliationEvidenceService(
            self.authorization,
            self.artifacts,
        )

    def test_round_trip_is_plain_local_json_and_hash_verified(self):
        report = _report()
        detail = _detail(report)

        candidate = self.service.prepare(report, detail, actor=LOCAL_ACTOR)
        self.service.publish(report, candidate)

        self.assertEqual(candidate.manifest.storage_name, RECONCILIATION_DETAIL_STORAGE_NAME)
        self.assertTrue(candidate.content.startswith(b'{"contract_version":1'))
        self.assertIn(b'"expected_value"', candidate.content)
        self.assertEqual(
            self.service.open(report, candidate.manifest, actor=LOCAL_ACTOR),
            detail,
        )
        self.assertEqual(len(self.authorization.calls), 2)

    def test_tampered_local_json_is_rejected(self):
        report = _report()
        candidate = self.service.prepare(report, _detail(report), actor=LOCAL_ACTOR)
        self.service.publish(report, candidate)
        key = (report.workspace_id, report.reconciliation_id, candidate.manifest.storage_name)
        self.artifacts.values[key] = candidate.content + b" "

        with self.assertRaisesRegex(WorkspaceError, "size changed"):
            self.service.open(report, candidate.manifest, actor=LOCAL_ACTOR)


if __name__ == "__main__":
    unittest.main()
