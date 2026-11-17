"""Reject changed authoring inputs while preserving concurrent Match data work."""

from unittest.mock import patch
from uuid import uuid4

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    TargetFieldHandling,
)
from impodo.domain.odoo.contracts import ConnectorTransportError
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.routers import mapping as mapping_routes
from tests.integration.web.test_mapping_command_performance import MappingCommandFixture
from tests.support.identity import resolve_local_actor


class MappingCommandRecoveryTests(MappingCommandFixture):
    def test_committed_default_decision_survives_a_later_check_conflict(self):
        workspace_id, dataset, business_key = self._mapping_ready_workspace(
            scalar_field_count=1,
            required_scalar_indexes=(0,),
            verified_default_scalar_indexes=(0,),
        )
        context = self.app.state.context
        actor = resolve_local_actor(context)
        identity = dataset.columns[0]
        revision, _ = run_local_operation_sync(
            context.mapping_workspace.check_definition,
            workspace_id,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(identity.stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(identity.stable_key,),
                            target_fields=business_key.key_fields,
                        ),
                    ),
                ),
            ),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=actor,
        )
        operation_id = str(uuid4())
        entries = [
            ["csrf_token", self.csrf],
            ["action", "set_disposition:0:field_0000:odoo_default"],
            ["operation_id", operation_id],
            ["expected_parent_version", str(revision.version)],
            ["expected_working_draft_version", "1"],
        ]
        check_definition = context.mapping_workspace.check_definition

        def concurrent_check(*args, **kwargs):
            decided = context.queries.get_mapping_working_draft(workspace_id)
            context.mapping_workspace.save_working_draft(
                workspace_id,
                datasets=decided.definition.datasets,
                expected_version=decided.version,
                actor=actor,
            )
            return check_definition(*args, **kwargs)

        with patch.object(
            context.mapping_workspace,
            "check_definition",
            side_effect=concurrent_check,
        ):
            response = self.bounded_command(workspace_id, entries, opens=8)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "committed")
        self.assertEqual(response.json()["follow_up_status"], "conflict")
        current = context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(current.version, 3)
        self.assertEqual(
            current.definition.datasets[0].target_field_dispositions[0].handling,
            TargetFieldHandling.ODOO_DEFAULT,
        )
        receipt = context.mapping_workspace.get_mutation_receipt(
            workspace_id,
            operation_id,
            actor=actor,
        )
        self.assertEqual(receipt.state.value, "COMMITTED")

    def test_changed_draft_during_remote_capture_rejects_original_operation(self):
        workspace_id, entries = self.command_fixture(20, "REMOTE")
        context = self.app.state.context
        actor = resolve_local_actor(context)
        before = context.queries.get_mapping_working_draft(workspace_id)
        checked_entries = self.changed_entries(
            entries,
            action="draft",
            operation_id=str(uuid4()),
        )
        operation_id = dict(checked_entries)["operation_id"]

        def concurrent_edit(*_args, **_kwargs):
            run_local_operation_sync(
                context.mapping_workspace.save_working_draft,
                workspace_id,
                datasets=before.definition.datasets,
                expected_version=before.version,
                actor=actor,
            )
            return ()

        with patch.object(
            mapping_routes,
            "_capture_mapping_supporting_values",
            side_effect=concurrent_edit,
        ):
            response = self.post_command(workspace_id, checked_entries)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["operation_id"], operation_id)
        self.assertEqual(response.json()["failure_code"], "MAPPING_VERSION_CONFLICT")
        self.assertEqual(
            context.queries.get_mapping_working_draft(workspace_id).version, 2
        )
        self.assertIsNone(context.queries.get_mapping_revision(workspace_id))
        receipt = context.mapping_workspace.get_mutation_receipt(
            workspace_id, operation_id, actor=actor
        )
        self.assertEqual(receipt.state.value, "REJECTED")

    def test_changed_governance_during_capture_preserves_new_rules_and_rejects_check(
        self,
    ):
        workspace_id, entries = self.command_fixture(19, "REMOTE")
        context = self.app.state.context
        actor = resolve_local_actor(context)
        before = context.queries.get_schema_governance(workspace_id)
        checked_entries = self.changed_entries(
            entries, action="draft", operation_id=str(uuid4())
        )

        def concurrent_governance(*_args, **_kwargs):
            run_local_operation_sync(
                context.schema_workspace.govern,
                workspace_id,
                business_keys=before.business_keys,
                actor=actor,
            )
            return ()

        with patch.object(
            mapping_routes,
            "_capture_mapping_supporting_values",
            side_effect=concurrent_governance,
        ):
            response = self.post_command(workspace_id, checked_entries)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn(
            "matching rules changed while checking", response.json()["detail"]
        )
        self.assertEqual(
            context.queries.get_schema_governance(workspace_id).version,
            before.version + 1,
        )
        self.assertIsNone(context.queries.get_mapping_revision(workspace_id))
        receipt = context.mapping_workspace.get_mutation_receipt(
            workspace_id,
            dict(checked_entries)["operation_id"],
            actor=actor,
        )
        self.assertEqual(receipt.state.value, "REJECTED")

    def test_remote_failure_rejects_same_receipt_and_preserves_draft(self):
        workspace_id, entries = self.command_fixture(20, "REMOTE")
        context = self.app.state.context
        before = context.queries.get_mapping_working_draft(workspace_id)
        checked_entries = self.changed_entries(
            entries, action="draft", operation_id=str(uuid4())
        )
        with patch.object(
            mapping_routes,
            "check_matching_access",
            side_effect=ConnectorTransportError("fictional remote failure"),
        ):
            response = self.bounded_command(workspace_id, checked_entries, opens=12)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(
            response.json()["operation_id"], dict(checked_entries)["operation_id"]
        )
        self.assertEqual(response.json()["failure_code"], "ODOO_READ_FAILED")
        self.assertEqual(
            context.queries.get_mapping_working_draft(workspace_id), before
        )
        self.assertIsNone(context.queries.get_mapping_revision(workspace_id))
