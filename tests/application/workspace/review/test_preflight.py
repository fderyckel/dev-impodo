from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.application.preflight_service import (
    EXECUTION_SNAPSHOT_NAME,
    MANIFEST_NAME,
    PreflightService,
    _validate_snapshot_projection,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
    bind_snapshot_hashes,
    record_snapshot_json,
    record_snapshot_payload,
)
from impodo.domain.preflight.reports import ReadinessReport
from impodo.domain.shared.access import (
    Actor,
    ActorIdentity,
    Capability,
    CapabilityAuthorizationPolicy,
)
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    PreflightResult,
    PreparedRecord,
    TargetFingerprint,
    TargetRecord,
    canonical_json_text,
    target_identity_hash,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode


class PreflightPublicationTests(unittest.TestCase):
    def test_failed_repository_save_deletes_unpublished_manifest(self) -> None:
        workspace_id = str(uuid4())
        migration_project_id = str(uuid4())
        run_id = str(uuid4())
        target_hash = target_identity_hash(
            connection_mode="LOCAL",
            base_url="http://127.0.0.1:8069",
            database="odoo19_test",
        )
        fingerprint = TargetFingerprint(
            target_hash=target_hash,
            connection_mode="LOCAL",
            database="odoo19_test",
            odoo_version="19.0",
            snapshot_timestamp="2026-08-05T12:00:00Z",
        )
        metadata, records = bind_snapshot_hashes(
            MetadataSnapshot(fingerprint=fingerprint, models={}),
            RecordSnapshot(
                fingerprint=fingerprint,
                records={},
                requested_fields={},
            ),
        )
        result = PreflightResult(
            profile_id="browser_mapping",
            source_hashes={},
            fingerprint=fingerprint,
            metadata_snapshot_hash=metadata.content_hash,
            record_snapshot_hash=records.content_hash,
            decisions=(),
            reference_resolutions=(),
            issues=(),
        )
        report = ReadinessReport(
            run_id=run_id,
            workspace_id=workspace_id,
            mapping_id=str(uuid4()),
            mapping_version=1,
            mapping_content_hash="sha256:" + "1" * 64,
            staging_run_id=str(uuid4()),
            staging_content_hash="sha256:" + "2" * 64,
            quality_run_id=str(uuid4()),
            quality_content_hash="sha256:" + "3" * 64,
            normalization_run_id=str(uuid4()),
            normalization_content_hash="sha256:" + "4" * 64,
            normalization_lifecycle_version=1,
            eligible_dataset_hash="sha256:" + "5" * 64,
            frozen_input_hash="sha256:" + "6" * 64,
            requirement_plan_hash="sha256:" + "7" * 64,
            metadata_snapshot_hash=str(metadata.content_hash),
            record_snapshot_hash=str(records.content_hash),
            result_hash=result.semantic_hash,
            manifest_hash="",
            target_hash=target_hash,
            target_database="odoo19_test",
            target_odoo_version="19.0",
            target_snapshot_at="2026-08-05T12:00:00Z",
            target_module_versions={},
            checked_at=datetime.now(timezone.utc),
            checked_by="Test operator",
            datasets=(),
            rows=(),
        )
        repositories = [MagicMock() for _ in range(7)]
        repositories[4].get.return_value = SimpleNamespace(
            project_id=migration_project_id,
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_test",
        )
        repositories[6].save_readiness_report.side_effect = WorkspaceError(
            "injected persistence failure"
        )
        artifacts = MagicMock()
        service = PreflightService(
            staging=repositories[0],
            quality=repositories[1],
            normalization=repositories[2],
            mappings=repositories[3],
            workspaces=repositories[4],
            sources=repositories[5],
            preflight=repositories[6],
            artifacts=artifacts,
            authorization=CapabilityAuthorizationPolicy(),
        )
        service._load_frozen_input = MagicMock(
            return_value=SimpleNamespace(
                plan=SimpleNamespace(semantic_hash="sha256:" + "8" * 64),
                prepared=SimpleNamespace(records=()),
                revision=object(),
                dataset_labels={},
                source_field_labels={},
                staging=object(),
                quality=object(),
                normalization=SimpleNamespace(
                    run_id=report.normalization_run_id,
                    content_hash=report.normalization_content_hash,
                    lifecycle_version=1,
                    eligible_dataset_hash=report.eligible_dataset_hash,
                ),
                content_hash=report.frozen_input_hash,
            )
        )
        service.engine.run = MagicMock(return_value=result)
        requirements = SimpleNamespace(
            metadata_requests=(),
            record_requests=(),
            semantic_hash=report.requirement_plan_hash,
            model_count=0,
            chunk_count=0,
            source_record_count=0,
        )
        actor = Actor(
            identity=ActorIdentity("test", "operator", "Test operator"),
            capabilities=frozenset({Capability.PREFLIGHT_RUN}),
        )

        with (
            patch(
                "impodo.application.preflight_service.plan_preflight_requirements",
                return_value=requirements,
            ),
            patch(
                "impodo.application.preflight_service._readiness_report",
                return_value=report,
            ),
            patch(
                "impodo.application.preflight_service.build_execution_snapshot",
                return_value=SimpleNamespace(
                    to_json=lambda: "{}",
                    semantic_hash="sha256:" + "9" * 64,
                    root_hash="sha256:" + "a" * 64,
                ),
            ),
            self.assertRaisesRegex(WorkspaceError, "injected persistence failure"),
        ):
            service.compare(
                workspace_id,
                reader=MagicMock(return_value=(metadata, records)),
                actor=actor,
            )

        self.assertEqual(artifacts.write_report.call_count, 2)
        self.assertEqual(artifacts.delete_report.call_count, 2)
        self.assertEqual(
            {call.args[2] for call in artifacts.delete_report.call_args_list},
            {MANIFEST_NAME, EXECUTION_SNAPSHOT_NAME},
        )
        manifest_call = next(
            call
            for call in artifacts.write_report.call_args_list
            if call.args[2] == MANIFEST_NAME
        )
        manifest = json.loads(manifest_call.args[3])
        self.assertEqual(
            manifest["preflight_evidence"]["execution_snapshot_hash"],
            "sha256:" + "9" * 64,
        )

    def test_report_cleanup_removes_empty_unpublished_run_directory(self) -> None:
        workspace_id = str(uuid4())
        run_id = str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = LocalArtifactStore(root)

            artifacts.write_report(workspace_id, run_id, MANIFEST_NAME, b"{}\n")
            run_directory = root / "ws" / workspace_id / "reports" / run_id
            self.assertTrue(run_directory.is_dir())

            artifacts.delete_report(workspace_id, run_id, MANIFEST_NAME)

            self.assertFalse(run_directory.exists())


class ReviewWorkbookEvidenceTests(unittest.TestCase):
    def _service(self, *, source_mode: SourceMode = SourceMode.FILE):
        repositories = [MagicMock() for _ in range(7)]
        repositories[4].get.return_value = SimpleNamespace(source_mode=source_mode)
        service = PreflightService(
            staging=repositories[0],
            quality=repositories[1],
            normalization=repositories[2],
            mappings=repositories[3],
            workspaces=repositories[4],
            sources=repositories[5],
            preflight=repositories[6],
            artifacts=MagicMock(),
            authorization=CapabilityAuthorizationPolicy(),
        )
        report = SimpleNamespace(
            run_id=str(uuid4()),
            frozen_input_hash="sha256:" + "a" * 64,
        )
        service.current_report = MagicMock(return_value=report)
        return service, report

    def test_file_source_returns_exact_prepared_values_and_business_labels(
        self,
    ) -> None:
        service, report = self._service()
        record = PreparedRecord(
            dataset="contacts",
            source_row=2,
            target_model="res.partner",
            source_identity=("CUS-001",),
            target_identity=("CUS-001",),
            target_scope=(),
            scalar_values={"name": "Example contact"},
            references={},
            source_trace_id="sha256:" + "b" * 64,
            issues=(),
        )
        schema = SimpleNamespace(
            models=(
                SimpleNamespace(
                    name="res.partner",
                    label="Contact",
                    fields=(SimpleNamespace(name="name", label="Name", required=True),),
                ),
            )
        )
        normalization_hash = "sha256:" + "c" * 64
        group = SimpleNamespace(
            group_id="sha256:" + "d" * 64,
            name="Trim surrounding spaces",
            explanation="Impodo removed spaces around the contact name.",
        )
        effect = SimpleNamespace(
            eligible=True,
            group_id=group.group_id,
            row_id=record.source_trace_id,
            dataset=record.dataset,
            source_row=record.source_row,
            target_field="name",
            before=" Example contact ",
            after="Example contact",
        )
        service.normalization.get_normalization_evaluation.return_value = (
            SimpleNamespace(
                content_hash=normalization_hash,
                groups=(group,),
                effects=(effect,),
            )
        )
        service._load_frozen_input = MagicMock(
            return_value=SimpleNamespace(
                content_hash=report.frozen_input_hash,
                prepared=SimpleNamespace(records=(record,)),
                dataset_labels={"contacts": "Contacts"},
                captured_schema=schema,
                normalization=SimpleNamespace(
                    run_id="normalization-run",
                    content_hash=normalization_hash,
                ),
            )
        )

        evidence = service.review_workbook_evidence("workspace", report.run_id)

        self.assertEqual(evidence.records, (record,))
        self.assertEqual(evidence.target_model_labels, {"res.partner": "Contact"})
        self.assertEqual(
            evidence.target_field_labels,
            {("res.partner", "name"): "Name"},
        )
        self.assertEqual(
            evidence.target_field_required,
            {("res.partner", "name"): True},
        )
        self.assertEqual(evidence.normalization_content_hash, normalization_hash)
        self.assertEqual(len(evidence.cell_effects), 1)
        self.assertEqual(
            evidence.cell_effects[0].source_trace_id, record.source_trace_id
        )
        self.assertEqual(evidence.cell_effects[0].rule_name, group.name)
        service.normalization.get_normalization_evaluation.assert_called_once_with(
            "workspace",
            "normalization-run",
        )

    def test_odoo_source_keeps_business_values_out_of_portable_workbook(self) -> None:
        service, report = self._service(source_mode=SourceMode.ODOO)
        service._load_frozen_input = MagicMock()

        evidence = service.review_workbook_evidence("workspace", report.run_id)

        self.assertIsNone(evidence)
        service._load_frozen_input.assert_not_called()
        service.normalization.get_normalization_evaluation.assert_not_called()

    def test_changed_frozen_input_fails_closed(self) -> None:
        service, report = self._service()
        service._load_frozen_input = MagicMock(
            return_value=SimpleNamespace(content_hash="sha256:" + "c" * 64)
        )

        with self.assertRaisesRegex(ReadinessError, "no longer match"):
            service.review_workbook_evidence("workspace", report.run_id)

    def test_changed_normalization_feedback_fails_closed(self) -> None:
        service, report = self._service()
        service._load_frozen_input = MagicMock(
            return_value=SimpleNamespace(
                content_hash=report.frozen_input_hash,
                normalization=SimpleNamespace(
                    run_id="normalization-run",
                    content_hash="sha256:" + "d" * 64,
                ),
            )
        )
        service.normalization.get_normalization_evaluation.return_value = (
            SimpleNamespace(
                content_hash="sha256:" + "e" * 64,
                groups=(),
                effects=(),
            )
        )

        with self.assertRaisesRegex(ReadinessError, "feedback no longer matches"):
            service.review_workbook_evidence("workspace", report.run_id)


class SnapshotProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fingerprint = TargetFingerprint(
            target_hash="sha256:" + "9" * 64,
            connection_mode="LOCAL",
            database="odoo19_test",
            odoo_version="19.0",
            snapshot_timestamp="2026-08-06T12:00:00Z",
        )
        self.metadata_requests = (MetadataRequest("res.partner", ("name", "ref")),)
        self.record_requests = (
            RecordRequest(
                "res.partner",
                ("name", "ref"),
                (["ref", "=", "C001"],),
            ),
        )

    def test_missing_planned_metadata_field_fails_closed(self) -> None:
        metadata = MetadataSnapshot(
            fingerprint=self.fingerprint,
            models={
                "res.partner": ModelMetadata(
                    model="res.partner",
                    description="Contact",
                    fields={
                        "ref": FieldMetadata(name="ref", type="char"),
                    },
                )
            },
        )
        records = RecordSnapshot(
            fingerprint=self.fingerprint,
            records={"res.partner": ()},
            requested_fields={"res.partner": ("name", "ref")},
        )

        with self.assertRaisesRegex(
            ReadinessError,
            "snapshot is incomplete",
        ) as raised:
            _validate_snapshot_projection(
                self.metadata_requests,
                self.record_requests,
                metadata,
                records,
            )
        self.assertEqual(
            raised.exception.failure_code,
            "ODOO_RESPONSE_INCOMPLETE",
        )

    def test_unplanned_metadata_field_fails_closed(self) -> None:
        metadata = MetadataSnapshot(
            fingerprint=self.fingerprint,
            models={
                "res.partner": ModelMetadata(
                    model="res.partner",
                    description="Contact",
                    fields={
                        "name": FieldMetadata(name="name", type="char"),
                        "ref": FieldMetadata(name="ref", type="char"),
                        "email": FieldMetadata(name="email", type="char"),
                    },
                )
            },
        )
        records = RecordSnapshot(
            fingerprint=self.fingerprint,
            records={"res.partner": ()},
            requested_fields={"res.partner": ("name", "ref")},
        )

        with self.assertRaisesRegex(ReadinessError, "unplanned fields"):
            _validate_snapshot_projection(
                self.metadata_requests,
                self.record_requests,
                metadata,
                records,
            )

    def test_streamed_protected_snapshot_matches_canonical_payload(self) -> None:
        records = RecordSnapshot(
            fingerprint=self.fingerprint,
            records={
                "res.partner": (
                    TargetRecord(
                        model="res.partner",
                        odoo_id=7,
                        values={"ref": "C001", "name": "Contact"},
                    ),
                )
            },
            requested_fields={"res.partner": ("name", "ref")},
        )

        self.assertEqual(
            record_snapshot_json(records),
            canonical_json_text(record_snapshot_payload(records)),
        )


if __name__ == "__main__":
    unittest.main()
