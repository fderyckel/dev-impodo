"""Contact authoring uses optional child intent and exports handled check failures."""

from contextlib import closing
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

from fastapi.testclient import TestClient

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.diagnostics import LocalDiagnosticRecorder, RequestDiagnosticsMiddleware
from tests.integration.web.test_supporting_diagnostics import supporting_error
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase
from tests.support.identity import resolve_local_actor


class SupportingMappingIntentBrowserTests(ProjectSetupBrowserTestCase):
    def configure(self, version="19.0", mode=OdooConnectionMode.LOCAL):
        workspace_id, dataset, key = self._mapping_ready_workspace(scalar_field_count=0, connection_mode=mode)
        context = self.app.state.context
        actor = resolve_local_actor(context)
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        fields = tuple(SchemaField(
            name, label, "one2many", False, False, related, inverse, (),
        ) for name, label, related, inverse in (
            ("activity_ids", "Activities", "mail.activity", "res_id"),
            ("invoice_ids", "Invoices", "account.move", "partner_id"),
            ("user_ids", "Users", "res.users", "partner_id"),
        ))
        schema = replace(schema, odoo_version=version, content_hash="sha256:" + "8" * 64,
                         models=(replace(schema.models[0], fields=(*schema.models[0].fields, *fields)),))
        context.schema_workspace.schemas.save_odoo_schema_catalog(workspace_id, schema, actor=actor)
        context.schema_workspace.govern_complete(workspace_id, business_keys=(key,), expected_catalog_hash=schema.content_hash, actor=actor)
        packet = context.schema_workspace.supporting_review_inputs(workspace_id, actor=actor)
        review = context.schema_workspace.supporting.save(
            packet, expected_binding_hash=packet.binding_hash, expected_version=0,
            operation_id=str(uuid4()), choices=(), actor=actor,
        )
        return workspace_id, dataset, key, schema, review

    def test_check_matches_http_accepts_empty_stale_review_without_rewriting_it(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for version in ("19.0", "20.0"):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(version=version, mode=mode):
                    workspace_id, dataset, key, _schema, review = self.configure(version, mode)
                    stale = replace(review, version=2, operation_id=str(uuid4()), binding_hash="sha256:" + "1" * 64)
                    context.schema_workspace.schemas.save_supporting_review(
                        workspace_id, stale, expected_version=review.version, actor=actor,
                    )
                    entries = [
                        ["csrf_token", self.csrf], ["action", "save_progress"],
                        ["operation_id", str(uuid4())], ["expected_parent_version", ""],
                        ["expected_working_draft_version", ""], ["editable_dataset_id", dataset.dataset_id],
                        ["target_model_0", "res.partner"], ["mode_0", "upsert"], ["on_existing_0", "block"],
                        ["source_identity_0", dataset.columns[0].stable_key], ["business_key_0", key.key_id],
                        ["identity_source_0_0", dataset.columns[0].stable_key],
                    ]
                    headers = {**POST_HEADERS, "X-CSRF-Token": self.csrf}
                    saved = self.client.post(f"/workspaces/{workspace_id}/mapping/save", json={"entries": entries}, headers=headers)
                    self.assertEqual(saved.status_code, 200, saved.text)
                    draft = context.queries.get_mapping_working_draft(workspace_id)
                    operation_id = str(uuid4())
                    entries = [[name, "draft" if name == "action" else operation_id if name == "operation_id"
                                else str(draft.version) if name == "expected_working_draft_version" else value]
                               for name, value in entries]
                    with collect_duckdb_request_timings() as timings:
                        checked = self.client.post(f"/workspaces/{workspace_id}/mapping/save", json={"entries": entries}, headers=headers)
                    self.assertEqual(checked.status_code, 200, checked.text)
                    self.assertEqual(timings.lock_retry_count, 0)
                    self.assertLessEqual(timings.connection_count, 35)
                    self.assertLessEqual(timings.schema_check_count, 14)
                    revision = context.queries.get_mapping_revision(workspace_id)
                    self.assertEqual(revision.definition.content_hash, draft.definition.content_hash)
                    self.assertEqual(context.queries.get_mapping_validation(workspace_id, revision.version).status.value, "VALID")
                    self.assertEqual(context.schema_workspace.supporting.current(workspace_id, actor=actor), stale)
                    receipt = self.client.get(f"/workspaces/{workspace_id}/mapping/mutation-receipts/{operation_id}")
                    self.assertEqual(receipt.json()["status"], "committed")

    def test_local_remote_authoring_and_preparation_accept_unused_children_for_both_majors(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for version in ("19.0+e", "20.0+e"):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(version=version, mode=mode):
                    workspace_id, dataset, _key, schema, review = self.configure(version, mode)
                    mappings = (DatasetMapping(
                        dataset.dataset_id, "res.partner",
                        source_identity_column_keys=(dataset.columns[0].stable_key,),
                        target_identity=(IdentityComponentMapping((dataset.columns[0].stable_key,), ("ref",)),),
                    ),)
                    before = len(self.readiness_calls)
                    with collect_duckdb_request_timings() as timings:
                        run_local_operation_sync(
                            context.mapping_workspace.supporting_decisions.require_mapping,
                            workspace_id, mappings, schema, None,
                        )
                    # Review, workspace/access, source and preparation share the
                    # same bounded metadata operation used by existing reviews.
                    self.assertLessEqual(timings.connection_count, 4)
                    self.assertLessEqual(timings.schema_check_count, 4)
                    self.assertEqual(timings.lock_retry_count, 0)
                    _revision, validation = run_local_operation_sync(
                        context.mapping_workspace.check_definition, workspace_id, datasets=mappings,
                        expected_parent_version=None, expected_working_draft_version=None, actor=actor,
                    )
                    self.assertFalse([x for x in validation.issues if x.severity == "error"], validation.issues)
                    packet = context.schema_workspace.supporting_review_inputs(workspace_id, actor=actor)
                    run_local_operation_sync(
                        context.mapping_workspace.supporting_decisions.require_preparation,
                        workspace_id, mappings, context.schema_workspace.schemas,
                        packet.workspace, packet.selection, packet.preparation,
                    )
                    self.assertEqual(context.schema_workspace.supporting.current(workspace_id, actor=actor), review)
                    self.assertEqual(context.queries.get(workspace_id).intended_models, ("res.partner",))
                    self.assertEqual(len(self.readiness_calls), before)

    def test_handled_check_failure_keeps_draft_and_is_correlated_in_downloaded_bundle(self):
        workspace_id, dataset, key, _schema, _review = self.configure()
        context = self.app.state.context
        # Method-scoped owners close before the fixture removes its directory.
        # unittest cleanups run after tearDown, which is too late on Windows.
        with (
            closing(LocalDiagnosticRecorder(Path(self.temporary.name) / "diagnostics")) as recorder,
            closing(TestClient(RequestDiagnosticsMiddleware(self.app, recorder=recorder))) as client,
        ):
            self.app.state.diagnostic_recorder = recorder
            client.cookies.update(self.client.cookies)
            entries = [
                ["csrf_token", self.csrf], ["action", "save_progress"],
                ["operation_id", str(uuid4())], ["expected_parent_version", ""],
                ["expected_working_draft_version", ""], ["editable_dataset_id", dataset.dataset_id],
                ["target_model_0", "res.partner"], ["mode_0", "upsert"], ["on_existing_0", "block"],
                ["source_identity_0", dataset.columns[0].stable_key], ["business_key_0", key.key_id],
                ["identity_source_0_0", dataset.columns[0].stable_key],
            ]
            headers = {**POST_HEADERS, "X-CSRF-Token": self.csrf}
            saved = client.post(f"/workspaces/{workspace_id}/mapping/save", json={"entries": entries}, headers=headers)
            self.assertEqual(saved.status_code, 200, saved.text)
            draft = context.queries.get_mapping_working_draft(workspace_id)
            operation_id = str(uuid4())
            entries = [[name, "draft" if name == "action" else operation_id if name == "operation_id"
                        else str(draft.version) if name == "expected_working_draft_version" else value]
                       for name, value in entries]
            # Inject a genuine domain failure at its consumer boundary. This tests
            # the route's caught-exception path, rather than uncaught middleware errors.
            with patch.object(context.mapping_workspace.supporting_decisions, "require_mapping", side_effect=supporting_error("19.0")):
                rejected = client.post(f"/workspaces/{workspace_id}/mapping/save", json={"entries": entries}, headers=headers)
            self.assertEqual(rejected.status_code, 422, rejected.text)
            self.assertEqual(rejected.json()["failure_code"], "SUPPORTING_PLAN_INCOMPLETE")
            self.assertIn("res.partner.line_0_ids", rejected.json()["detail"])
            self.assertEqual(context.queries.get_mapping_working_draft(workspace_id), draft)
            receipt = client.get(f"/workspaces/{workspace_id}/mapping/mutation-receipts/{operation_id}")
            self.assertEqual(receipt.json()["failure_code"], "SUPPORTING_PLAN_INCOMPLETE")
            bundle = client.post("/diagnostics/bundle", data={"csrf_token": self.csrf}, headers=POST_HEADERS)
            self.assertEqual(bundle.status_code, 200, bundle.text[:200])
            with ZipFile(BytesIO(bundle.content)) as archive:
                failures = json.loads(archive.read("recent-failures.json"))
            failed = next(x for x in failures if x.get("mapping_operation_id") == operation_id)
            self.assertEqual(failed["request_id"], rejected.headers["X-Impodo-Request-ID"])
            self.assertEqual(failed["working_draft_version"], draft.version)
            self.assertEqual(failed["mapping_action"], "CHECK_MATCHES")
            self.assertEqual(failed["validation_failure"]["issues"][0]["field"], "line_0_ids")
