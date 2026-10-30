"""Qualify generated hierarchy records through real persisted workflow services."""

from datetime import UTC, datetime

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.serialization import content_hash
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.hierarchy_odoo import HierarchyOdoo
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class HierarchyWorkflowTests(HierarchyWorkflowFixture):
    def test_generated_hierarchy_prepares_all_records_and_keeps_three_seals(self):
        self._complete(19, "LOCAL")

    def test_odoo20_local_hierarchy(self):
        self._complete(20, "LOCAL")

    def test_odoo19_remote_hierarchy(self):
        self._complete(19, "REMOTE")

    def test_odoo20_remote_hierarchy(self):
        self._complete(20, "REMOTE")

    def _complete(self, major, mode):
        workspace_id, _ = self.hierarchy(major=major, mode=mode)
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.ready_count, 18)
        self.assertEqual(quality.quarantined_count, 0)
        self.assertEqual(quality.blocked_count, 0)
        categories = [
            row for row in staging.rows if row.target_model == "product.category"
        ]
        self.assertEqual(len(categories), 12)
        seals = [row for row in categories if row.target_identity == ("Seals",)]
        self.assertEqual(len(seals), 3)
        self.assertEqual(len({str(row.target_scope) for row in seals}), 3)
        roots = [row for row in categories if row.target_scope == (None,)]
        self.assertEqual(
            {row.target_identity for row in roots},
            {("Machines",), ("Maintenance",), ("Tools",)},
        )
        self.approve(workspace_id)
        schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
        target = HierarchyOdoo(schema)
        report = self.context.preflight.compare(
            workspace_id, reader=target.compare, actor=self.actor
        )
        self.assertEqual(report.status, "READY", report)
        self.assertEqual(report.create_count, 13)
        self.assertEqual(report.unchanged_count, 5)
        preview = self.context.execution.current_preview(workspace_id, actor=self.actor)
        self.assertTrue(preview.can_load, preview.scope_error)
        self.assertEqual(preview.snapshot.relationship_plan.completions, ())
        target.scope_hash = preview.api_scope.semantic_hash
        snapshot = preview.snapshot
        read_identity = OdooReadIdentity(
            target_hash=snapshot.target_hash,
            principal_hash=snapshot.read_principal_hash,
            permission_hash=snapshot.read_permission_hash,
            context_hash=snapshot.read_context_hash,
            readable_models=snapshot.readable_models,
            observed_at=datetime.now(UTC).isoformat(),
        )
        write_identity = OdooWriteIdentity(
            target_hash=snapshot.target_hash,
            principal_hash=content_hash("writer"),
            permission_hash=content_hash("write permission"),
            context_hash=snapshot.read_context_hash,
            readable_models=snapshot.readable_models,
            writable_models=snapshot.readable_models,
            observed_at=datetime.now(UTC).isoformat(),
        )
        owner = self.context.target_credential_workspace(
            workspace_id,
            workspace_state=self.context.queries.get(workspace_id),
            actor=self.actor,
        )
        write = store_target_credential(
            self.context.secret_store,
            owner,
            TargetCredentialRole.WRITE,
            "fictional-write-key",
            persistent=False,
        )
        state = self.context.queries.get(workspace_id)
        config = Json2Config(
            base_url=state.odoo_base_url,
            database=state.odoo_database,
            api_key="fictional-key",
            connection_mode=mode,
        )
        writer = Json2WriteExecutor(
            config, preview.api_scope, transport=target.transport
        )
        reader = Json2ReadbackReader(
            config, preview.api_scope, transport=target.transport
        )
        execution = self.context.execution.execute(
            workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=writer,
            actor=self.actor,
            read_identity=read_identity,
            read_credential_binding_hash=snapshot.read_credential_binding_hash,
            write_identity=write_identity,
            write_credential_binding_hash=write.binding_hash,
        )
        self.assertEqual(execution.status.value, "COMPLETED")
        self.assertEqual(execution.committed_count, 13)
        paths = target.category_paths()
        products = {
            values["default_code"]: paths[values["categ_id"]]
            for (model, _), values in target.records.items()
            if model == "product.template"
        }
        self.assertEqual(products["PX101"], ("Machines", "Pumps", "Service", "Seals"))
        self.assertEqual(products["PX102"], ("Machines", "Valves", "Service", "Seals"))
        self.assertEqual(
            products["PX103"], ("Maintenance", "Pumps", "Service", "Seals")
        )
        reconciliation = self.context.reconciliation.reconcile(
            workspace_id,
            expected_execution_run_id=execution.run_id,
            reader=reader,
            actor=self.actor,
            write_identity=write_identity,
            write_credential_binding_hash=write.binding_hash,
        )
        self.assertEqual(reconciliation.status.value, "VERIFIED", reconciliation.rows)
        self.assertEqual(reconciliation.verified_count, 18)
