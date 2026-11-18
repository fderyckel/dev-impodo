"""Reject changed authoring inputs while preserving concurrent Match data work."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingTargetMode,
    TargetFieldHandling,
)
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import ConnectorTransportError
from impodo.domain.shared.models import target_identity_hash
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.routers import mapping as mapping_routes
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.integration.web.test_mapping_command_performance import MappingCommandFixture
from tests.support.identity import resolve_local_actor


class MappingCommandRecoveryTests(MappingCommandFixture):
    def test_live_reader_change_rejects_inflight_check_and_supersedes_saved_result(self):
        for major, mode in ((19, "LOCAL"), (20, "REMOTE")):
            with self.subTest(major=major, mode=mode):
                workspace_id, entries = self.command_fixture(major, mode)
                context = self.app.state.context
                actor = resolve_local_actor(context)
                first_entries = self.changed_entries(entries, action="draft")
                first = self.post_command(workspace_id, first_entries)
                self.assertEqual(first.status_code, 200, first.text)
                saved = first.json()
                second = self.changed_entries(
                    first_entries, operation_id=str(uuid4()),
                    expected_parent_version=str(saved["expected_parent_version"]),
                    expected_working_draft_version=str(saved["expected_working_draft_version"]),
                )
                schema = context.queries.get_odoo_schema_catalog(workspace_id)
                state = context.queries.get(workspace_id)

                def change_reader(*_args, **_kwargs):
                    if mode == "REMOTE":
                        store_target_credential(
                            self.secrets, state, TargetCredentialRole.READ, "changed-test-key",
                            persistent=False,
                        )
                    else:
                        target = target_identity_hash(
                            connection_mode=mode, base_url=state.odoo_base_url, database=state.odoo_database,
                        )
                        context.workspace_states.select_odoo_company(
                            workspace_id, actor=actor, expected_revision=state.revision,
                            selection=OdooCompanySelection(target, 7, "Another company"),
                        )

                with patch.object(mapping_routes, "_capture_mapping_supporting_values", side_effect=change_reader):
                    rejected = self.post_command(workspace_id, second)
                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertIn("checking access changed", rejected.json()["detail"])
                self.assertEqual(rejected.json()["status"], "rejected")
                self.assertEqual(context.queries.get_odoo_schema_catalog(workspace_id), schema)
                path = f"/workspaces/{workspace_id}/mapping"
                receipt = self.client.get(f"{path}/mutation-receipts/{saved['operation_id']}").json()
                self.assertEqual(receipt["follow_up_status"], "superseded")
                self.assertIsNone(context.queries.get_mapping_submission(workspace_id, saved["expected_parent_version"]))

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

    def test_credential_rebind_during_capture_requires_a_new_check(self):
        workspace_id, entries = self.command_fixture(20, "REMOTE")
        context = self.app.state.context
        actor = resolve_local_actor(context)
        checked_entries = self.changed_entries(
            entries, action="draft", operation_id=str(uuid4())
        )
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        rebound = replace(schema, read_credential_binding_hash="sha256:" + "e" * 64)
        self.assertNotEqual(rebound.read_credential_binding_hash, schema.read_credential_binding_hash)
        original_draft = context.queries.get_mapping_working_draft(workspace_id)

        def change_credential(*_args, **_kwargs):
            run_local_operation_sync(
                context.schema_workspace.schemas.rebind_odoo_schema_access,
                workspace_id,
                rebound,
                actor=actor,
                expected_content_hash=schema.content_hash,
                expected_read_credential_binding_hash=schema.read_credential_binding_hash,
            )
            return ()

        with patch.object(
            mapping_routes, "_capture_mapping_supporting_values", side_effect=change_credential
        ):
            response = self.post_command(workspace_id, checked_entries)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("matching rules changed while checking", response.json()["detail"])
        self.assertEqual(context.queries.get_odoo_schema_catalog(workspace_id), rebound)
        self.assertIsNone(context.queries.get_mapping_revision(workspace_id))
        self.assertEqual(context.queries.get_mapping_working_draft(workspace_id), original_draft)
        receipt = context.mapping_workspace.get_mutation_receipt(
            workspace_id, dict(checked_entries)["operation_id"], actor=actor
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
