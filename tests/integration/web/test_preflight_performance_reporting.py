"""Verify Stage 5 reporting and worker selection at real browser boundaries."""

from dataclasses import replace
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.application.shared.performance import OperationPerformance, bind_performance
from impodo.domain.execution.planner import PreflightRequirementPlan
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetFingerprint
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.domain.workspace.workbench import OdooConnectionMode, SourceMode
from impodo.web.app import create_local_app
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.composition.target_readers import _read_readiness_snapshots
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class PreflightPerformancePublicationTests(HierarchyWorkflowFixture):
    def test_odoo19_local_diagnostics_preserve_evidence(self):
        self.exercise(19, "LOCAL")

    def test_odoo20_local_diagnostics_preserve_evidence(self):
        self.exercise(20, "LOCAL")

    def test_odoo19_remote_diagnostics_preserve_evidence(self):
        self.exercise(19, "REMOTE")

    def test_odoo20_remote_diagnostics_preserve_evidence(self):
        self.exercise(20, "REMOTE")

    def exercise(self, major, mode):
        workspace_id, _ = self.hierarchy(major=major, mode=mode)
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target = HierarchyOdoo(
            self.context.queries.get_odoo_schema_catalog(workspace_id)
        )

        def read(requirements):
            self.assertIsNone(getattr(_read_databases, "connections", None))
            return target.compare(requirements)

        baseline = self.context.preflight.compare(
            workspace_id,
            reader=read,
            actor=self.actor,
            local_operation=run_local_operation_sync,
        )
        original = self.context.preflight.current_execution_snapshot(workspace_id)
        events = []
        collector = OperationPerformance(
            operation_id=str(uuid4()),
            operation="preflight",
            sink=events.append,
        )
        with bind_performance(collector):
            observed = self.context.preflight.compare(
                workspace_id,
                reader=read,
                actor=self.actor,
                local_operation=run_local_operation_sync,
            )
        collector.finish(outcome="succeeded")
        current = self.context.preflight.current_execution_snapshot(workspace_id)
        self.assertEqual(observed.result_hash, baseline.result_hash)
        self.assertEqual(current.root_hash, original.root_hash)
        # A fresh comparison has its own run identity; all other
        # execution evidence must retain the exact same meaning.
        self.assertEqual(
            replace(current, preflight_run_id=original.preflight_run_id).semantic_hash,
            original.semantic_hash,
        )
        summary = events[-1]
        self.assertEqual(summary["outcome"], "succeeded")
        self.assertTrue(
            {"verify", "read_odoo", "compare", "build", "publish"}.issubset(
                summary["phases"]
            )
        )
        self.assertGreater(summary["counters"]["prepared_rows"], 0)
        self.assertGreater(summary["counters"]["database_connection_count"], 0)
        self.assertEqual(summary["counters"]["database_lock_retry_count"], 0)
        self.assertEqual(target.writes, [])
        self.assertNotIn("Fictional hierarchy", repr(events))


class PreflightReadWorkerSelectionTests(TestCase):
    def evidence(
        self, major, source_mode=SourceMode.FILE, mode=OdooConnectionMode.REMOTE
    ):
        state = SimpleNamespace(
            workspace_id=str(uuid4()),
            source_mode=source_mode,
            odoo_connection_mode=mode,
        )
        schema = SimpleNamespace(
            models=(SimpleNamespace(name="res.partner"),),
            odoo_version=f"{major}.0",
            read_credential_binding_hash="credential",
            connection_target_hash="target",
            read_principal_hash="principal",
            read_permission_hash="permissions",
            read_context_hash="context",
        )
        identity = SimpleNamespace(
            target_hash="target",
            principal_hash="principal",
            permission_hash="permissions",
            context_hash="context",
            readable_models=("res.partner",),
        )
        context = SimpleNamespace(
            readiness_reader=None,
            secret_store=Mock(),
            queries=Mock(),
            target_credential_workspace=lambda *_args, **_kwargs: state,
            read_identity_probe=Mock(return_value=identity),
            preflight_read_workers=2,
            local_odoo_reader=Mock(),
        )
        context.queries.get_odoo_schema_catalog.return_value = schema
        fingerprint = TargetFingerprint(
            "target", mode.value, "fictional", f"{major}.0", "2026-11-13T00:00:00Z"
        )
        metadata = MetadataSnapshot(
            fingerprint,
            {
                "res.partner": ModelMetadata(
                    "res.partner", "Contact", {"name": FieldMetadata("name", "char")}
                ),
            },
        )
        records = RecordSnapshot(
            fingerprint, {"res.partner": ()}, {"res.partner": ("name",)}
        )
        requirements = PreflightRequirementPlan(
            (MetadataRequest("res.partner", ("name",)),),
            (
                RecordRequest(
                    "res.partner", ("name",), (("name", "in", ("Fictional",)),)
                ),
            ),
            (),
            1,
            reference_policy_hash=reference_policy_hash(major),
        )
        return context, state, requirements, metadata, records

    def test_remote_file_and_pinned_source_forward_the_selected_worker_limit(self):
        for major in (19, 20):
            for source_mode in (SourceMode.FILE, SourceMode.ODOO):
                for workers in (1, 2):
                    with self.subTest(
                        major=major, source_mode=source_mode, workers=workers
                    ):
                        context, state, requirements, metadata, records = self.evidence(
                            major, source_mode
                        )
                        context.preflight_read_workers = workers
                        with (
                            patch(
                                "impodo.web.composition.target_readers.get_target_credential",
                                return_value=SimpleNamespace(
                                    binding_hash="credential", secret="fictional"
                                ),
                            ),
                            patch(
                                "impodo.web.composition.target_readers._target_json2_config"
                            ),
                            patch(
                                "impodo.web.composition.target_readers.Json2ReadConnector"
                            ) as connector,
                        ):
                            connector.return_value.get_model_metadata.return_value = (
                                metadata
                            )
                            connector.return_value.get_records.return_value = records
                            connector.return_value.check_company_references.return_value = metadata
                            actual = _read_readiness_snapshots(
                                context, state, requirements, actor=LOCAL_ACTOR
                            )
                        connector.return_value.get_records.assert_called_once_with(
                            requirements.record_requests, max_workers=workers
                        )
                        self.assertEqual(actual, (metadata, records))
                        if source_mode is SourceMode.FILE:
                            connector.return_value.check_company_references.assert_called_once()
                        else:
                            connector.return_value.check_company_references.assert_not_called()

    def test_local_shell_keeps_one_complete_read_and_does_not_construct_api_workers(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                context, state, requirements, metadata, records = self.evidence(
                    major, mode=OdooConnectionMode.LOCAL
                )
                profile = object()
                context.local_odoo_reader.get_preflight_snapshots.return_value = (
                    metadata,
                    records,
                )
                with (
                    patch(
                        "impodo.web.composition.target_readers._selected_local_profile",
                        return_value=profile,
                    ),
                    patch(
                        "impodo.web.composition.target_readers.Json2ReadConnector"
                    ) as connector,
                ):
                    actual = _read_readiness_snapshots(
                        context, state, requirements, actor=LOCAL_ACTOR
                    )
                connector.assert_not_called()
                context.local_odoo_reader.get_preflight_snapshots.assert_called_once_with(
                    state,
                    profile,
                    requirements.metadata_requests,
                    requirements.record_requests,
                    related_models=(),
                )
                self.assertEqual(actual, (metadata, records))

    def test_application_configuration_defaults_to_serial_and_supports_explicit_pilot(
        self,
    ):
        disabled = dict(
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
            recipe_run_jobs_enabled=False,
            load_jobs_enabled=False,
        )
        for environment, explicit, expected in (
            ({}, None, 1),
            ({"IMPODO_PREFLIGHT_READ_WORKERS": "2"}, None, 2),
            ({"IMPODO_PREFLIGHT_READ_WORKERS": "2"}, 1, 1),
        ):
            with (
                self.subTest(environment=environment, explicit=explicit),
                TemporaryDirectory() as root,
                patch.dict("os.environ", environment, clear=True),
            ):
                app = create_local_app(
                    root, preflight_read_workers=explicit, **disabled
                )
                self.assertEqual(app.state.context.preflight_read_workers, expected)
        for invalid in (0, 3, True, "2"):
            with (
                self.subTest(invalid=invalid),
                TemporaryDirectory() as root,
                self.assertRaises(ValueError),
            ):
                create_local_app(root, preflight_read_workers=invalid, **disabled)
