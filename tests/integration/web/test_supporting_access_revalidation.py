"""Renew verified access through real authoring routes without another choice save."""

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.shared.models import OdooReadIdentity, TargetRecord
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.diagnostics import LocalDiagnosticRecorder, RequestDiagnosticsMiddleware, create_diagnostic_bundle
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.integration.web import test_mapping_supporting_recovery as scenarios
from tests.support.browser_scenarios import POST_HEADERS, ProjectSetupBrowserTestCase, _browser_model_catalog
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_correction import MappingForm
from tests.support.identity import resolve_local_actor
from tests.support.recipe_lifecycle import metadata_for_schema


class SupportingAccessRevalidationTests(ProjectSetupBrowserTestCase):
    def ready(self, major, mode):
        workspace_id, _, key, schema = scenarios.related_review_workspace(self, major, mode)
        context = self.app.state.context
        actor = resolve_local_actor(context)
        draft = context.queries.get_mapping_working_draft(workspace_id)
        schema = replace(schema, policy_hash=odoo_source_policy_hash(major))
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor,
        )
        packet = context.schema_workspace.supporting_review_inputs(workspace_id, actor=actor)
        review = context.schema_workspace.supporting.current(workspace_id, actor=actor)
        run_local_operation_sync(context.schema_workspace.supporting.save, packet,
            expected_binding_hash=packet.binding_hash, expected_version=review.version,
            operation_id=str(uuid4()), choices=review.choices, actor=actor)
        run_local_operation_sync(
            context.schema_workspace.govern_complete, workspace_id,
            business_keys=(key,), expected_catalog_hash=schema.content_hash, actor=actor,
        )
        run_local_operation_sync(
            context.mapping_workspace.save_working_draft, workspace_id,
            datasets=draft.definition.datasets, expected_version=draft.version, actor=actor,
        )
        self.configure_readers(workspace_id, schema)
        return workspace_id, schema

    def configure_readers(self, workspace_id, schema, *, changed=None):
        context = self.app.state.context
        metadata = metadata_for_schema(schema)

        def read_schema(*_args):
            self.assertFalse(getattr(_read_databases, "connections", None))
            return metadata

        context.schema_reader = read_schema
        catalogue = _browser_model_catalog(context.queries.get(workspace_id))
        context.model_catalog_reader = lambda *_args: replace(catalogue,
            fingerprint=replace(catalogue.fingerprint, odoo_version=schema.odoo_version),
            records={
                "ir.model": tuple(
                    TargetRecord("ir.model", index + 1, {
                        "model": model.name, "name": model.label, "abstract": False,
                        "transient": False, "modules": "base", "state": "base",
                    })
                    for index, model in enumerate(schema.models)
                ),
            })

        def identity(_workspace, _secret, models):
            self.assertFalse(getattr(_read_databases, "connections", None))
            return OdooReadIdentity(target_hash=schema.connection_target_hash,
                principal_hash="sha256:" + "e" * 64 if changed == "user" else schema.read_principal_hash,
                permission_hash="sha256:" + "e" * 64 if changed == "permissions" else schema.read_permission_hash,
                context_hash=schema.read_context_hash, readable_models=tuple(models),
                observed_at="2026-11-05T12:00:00Z")
        context.read_identity_probe = identity

    def check(self, workspace_id):
        form = MappingForm(self.client.get(f"/workspaces/{workspace_id}/mapping").text)
        return self.client.post(form.action, json={"entries": [*form.entries, ["action", "draft"]]},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf})

    def renew(self, workspace_id):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        state = context.queries.get(workspace_id)
        owner = context.target_credential_workspace(workspace_id, workspace_state=state, actor=actor)
        return store_target_credential(self.secrets, owner, TargetCredentialRole.READ,
            "read-secret", persistent=False)

    def test_key_renewal_keeps_choices_for_both_majors_modes_and_entry_actions(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        recorder = LocalDiagnosticRecorder(Path(self.temporary.name) / "access-diagnostics")
        self.addCleanup(recorder.close)
        self.app.middleware_stack = RequestDiagnosticsMiddleware(self.app.middleware_stack, recorder=recorder)
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                for explicit in (False, True):
                    with self.subTest(major=major, mode=mode, explicit=explicit):
                        workspace_id, schema = self.ready(major, mode)
                        checked = self.check(workspace_id)
                        self.assertEqual(checked.status_code, 200, checked.text[:1000])
                        review = context.schema_workspace.supporting.current(workspace_id, actor=actor)
                        revision = context.queries.get_mapping_revision(workspace_id)
                        draft = context.queries.get_mapping_working_draft(workspace_id)
                        self.renew(workspace_id)
                        with collect_duckdb_request_timings() as timings, count_duckdb_statements() as sql:
                            if explicit:
                                state = context.queries.get(workspace_id)
                                response = self.client.post(f"/workspaces/{workspace_id}/mapping/company/refresh",
                                    data={"csrf_token": self.csrf, "revision": str(state.revision)},
                                    headers=POST_HEADERS, follow_redirects=False)
                                self.assertEqual(response.status_code, 303, response.text[:1000])
                            else:
                                response = self.check(workspace_id)
                                self.assertEqual(response.status_code, 200, response.text[:1000])
                        saved = context.schema_workspace.supporting.current(workspace_id, actor=actor)
                        self.assertEqual(saved.version, review.version + 1)
                        self.assertEqual(saved.choices, review.choices)
                        if explicit:
                            self.assertEqual(context.queries.get_mapping_revision(workspace_id), revision)
                            self.assertEqual(context.queries.get_mapping_working_draft(workspace_id), draft)
                            checked = self.check(workspace_id)
                            self.assertEqual(checked.status_code, 200, checked.text[:1000])
                        page = self.client.get(f"/workspaces/{workspace_id}/mapping")
                        self.assertTrue(page.context["related_data_review"]["current"])
                        self.assertEqual(context.schema_workspace.supporting.current(workspace_id, actor=actor), saved)
                        self.assertEqual(timings.lock_retry_count, 0)
                        self.assertGreater(len(sql), 0)
                        # Exact budgets are pinned after measuring the two bounded command paths.
                        print("ACCESS_BUDGET", major, mode, explicit, timings.connection_count,
                              timings.schema_check_count, len(sql))
        bundle = create_diagnostic_bundle(recorder.path.parent, build_contract=PROCESS_BUILD_CONTRACT)
        with ZipFile(BytesIO(bundle)) as archive:
            transitions = json.loads(archive.read("supporting-access.json"))
        revalidated = [event["supporting_access"] for event in transitions
                       if event["supporting_access"]["outcome"] == "REVALIDATED"]
        self.assertEqual(len(revalidated), 8)
        self.assertEqual({item["odoo_major"] for item in revalidated}, {19, 20})
        self.assertTrue(all(item["changes"] == ["CREDENTIAL_VERSION"] for item in revalidated))

    def test_changed_user_or_permissions_keeps_review_required(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for major in (19, 20):
            for changed in ("user", "permissions"):
                with self.subTest(major=major, changed=changed):
                    workspace_id, schema = self.ready(major, OdooConnectionMode.REMOTE)
                    review = context.schema_workspace.supporting.current(workspace_id, actor=actor)
                    self.renew(workspace_id)
                    self.configure_readers(workspace_id, schema, changed=changed)
                    response = self.check(workspace_id)
                    self.assertEqual(response.status_code, 422, response.text[:1000])
                    self.assertEqual(context.schema_workspace.supporting.current(workspace_id, actor=actor), review)
