from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from impodo.access import (
    Actor,
    ActorIdentity,
    Capability,
    CapabilityAuthorizationPolicy,
)
from impodo.application.preflight_service import MANIFEST_NAME, PreflightService
from impodo.artifacts import LocalArtifactStore
from impodo.connectors import (
    MetadataSnapshot,
    RecordSnapshot,
    bind_snapshot_hashes,
)
from impodo.domain.preflight.reports import ReadinessReport
from impodo.models import PreflightResult, TargetFingerprint, target_identity_hash
from impodo.projects import OdooConnectionMode
from impodo.workspace_errors import WorkspaceError


class PreflightPublicationTests(unittest.TestCase):
    def test_failed_repository_save_deletes_unpublished_manifest(self) -> None:
        project_id = str(uuid4())
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
            project_id=project_id,
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
            project_id=project_id,
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
            projects=repositories[4],
            sources=repositories[5],
            preflight=repositories[6],
            artifacts=artifacts,
            authorization=CapabilityAuthorizationPolicy(),
        )
        service._load_frozen_input = MagicMock(
            return_value=SimpleNamespace(
                plan=SimpleNamespace(
                    semantic_hash="sha256:" + "8" * 64
                ),
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
            self.assertRaisesRegex(WorkspaceError, "injected persistence failure"),
        ):
            service.compare(
                project_id,
                reader=MagicMock(return_value=(metadata, records)),
                actor=actor,
            )

        artifacts.write_report.assert_called_once()
        artifacts.delete_report.assert_called_once()
        self.assertEqual(
            artifacts.delete_report.call_args.args,
            artifacts.write_report.call_args.args[:3],
        )

    def test_report_cleanup_removes_empty_unpublished_run_directory(self) -> None:
        project_id = str(uuid4())
        run_id = str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / project_id).mkdir()
            artifacts = LocalArtifactStore(root)

            artifacts.write_report(project_id, run_id, MANIFEST_NAME, b"{}\n")
            run_directory = root / project_id / "reports" / run_id
            self.assertTrue(run_directory.is_dir())

            artifacts.delete_report(project_id, run_id, MANIFEST_NAME)

            self.assertFalse(run_directory.exists())


if __name__ == "__main__":
    unittest.main()
