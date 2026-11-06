"""Renew verified access through real authoring routes without another choice save."""

import json
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from uuid import uuid4
from unittest.mock import patch

from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.application.shared.build_contract import PROCESS_BUILD_CONTRACT
from impodo.domain.shared.models import OdooReadIdentity, TargetRecord
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.diagnostics import (
    LocalDiagnosticRecorder,
    RequestDiagnosticsMiddleware,
    create_diagnostic_bundle,
)
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.integration.web import test_mapping_supporting_recovery as scenarios
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_model_catalog,
)
from tests.support.database_probe import count_duckdb_statements
from tests.support.hierarchy_correction import MappingForm
from tests.support.identity import resolve_local_actor
from tests.support.recipe_lifecycle import metadata_for_schema


class SupportingAccessRevalidationTests(ProjectSetupBrowserTestCase):
    def ready(self, major, mode):
        workspace_id, _, key, schema = scenarios.related_review_workspace(
            self, major, mode
        )
        context = self.app.state.context
        actor = resolve_local_actor(context)
        draft = context.queries.get_mapping_working_draft(workspace_id)
        schema = replace(schema, policy_hash=odoo_source_policy_hash(major))
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        packet = context.schema_workspace.supporting_review_inputs(
            workspace_id, actor=actor
        )
        review = context.schema_workspace.supporting.current(workspace_id, actor=actor)
        run_local_operation_sync(
            context.schema_workspace.supporting.save,
            packet,
            expected_binding_hash=packet.binding_hash,
            expected_version=review.version,
            operation_id=str(uuid4()),
            choices=review.choices,
            actor=actor,
        )
        run_local_operation_sync(
            context.schema_workspace.govern_complete,
            workspace_id,
            business_keys=(key,),
            expected_catalog_hash=schema.content_hash,
            actor=actor,
        )
        run_local_operation_sync(
            context.mapping_workspace.save_working_draft,
            workspace_id,
            datasets=draft.definition.datasets,
            expected_version=draft.version,
            actor=actor,
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
        context.model_catalog_reader = lambda *_args: replace(
            catalogue,
            fingerprint=replace(
                catalogue.fingerprint, odoo_version=schema.odoo_version
            ),
            records={
                "ir.model": tuple(
                    TargetRecord(
                        "ir.model",
                        index + 1,
                        {
                            "model": model.name,
                            "name": model.label,
                            "abstract": False,
                            "transient": False,
                            "modules": "base",
                            "state": "base",
                        },
                    )
                    for index, model in enumerate(schema.models)
                )
            },
        )

        def identity(_workspace, _secret, models):
            self.assertFalse(getattr(_read_databases, "connections", None))
            return OdooReadIdentity(
                target_hash=schema.connection_target_hash,
                principal_hash="sha256:" + "e" * 64
                if changed == "user"
                else schema.read_principal_hash,
                permission_hash="sha256:" + "e" * 64
                if changed == "permissions"
                else schema.read_permission_hash,
                context_hash=schema.read_context_hash,
                readable_models=tuple(models),
                observed_at="2026-11-05T12:00:00Z",
            )

        context.read_identity_probe = identity

    def check(self, workspace_id, form=None):
        form = form or MappingForm(
            self.client.get(f"/workspaces/{workspace_id}/mapping").text
        )
        return self.client.post(
            form.action,
            json={"entries": [*form.entries, ["action", "draft"]]},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )

    def renew(self, workspace_id):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        state = context.queries.get(workspace_id)
        owner = context.target_credential_workspace(
            workspace_id, workspace_state=state, actor=actor
        )
        return store_target_credential(
            self.secrets,
            owner,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )

    def test_key_renewal_keeps_choices_for_both_majors_modes_and_entry_actions(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        with closing(
            LocalDiagnosticRecorder(
                Path(self.temporary.name) / "access-diagnostics"
            )
        ) as recorder:
            self.app.middleware_stack = RequestDiagnosticsMiddleware(
                self.app.middleware_stack, recorder=recorder
            )
            for major in (19, 20):
                for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                    for explicit in (False, True):
                        with self.subTest(major=major, mode=mode, explicit=explicit):
                            workspace_id, schema = self.ready(major, mode)
                            checked = self.check(workspace_id)
                            self.assertEqual(checked.status_code, 200, checked.text[:1000])
                            review = context.schema_workspace.supporting.current(
                                workspace_id, actor=actor
                            )
                            revision = context.queries.get_mapping_revision(workspace_id)
                            draft = context.queries.get_mapping_working_draft(workspace_id)
                            self.renew(workspace_id)
                            form = MappingForm(
                                self.client.get(f"/workspaces/{workspace_id}/mapping").text
                            )
                            state = context.queries.get(workspace_id)
                            with count_duckdb_statements() as sql:
                                if explicit:
                                    response = self.client.post(
                                        f"/workspaces/{workspace_id}/mapping/company/refresh",
                                        data={
                                            "csrf_token": self.csrf,
                                            "revision": str(state.revision),
                                        },
                                        headers=POST_HEADERS,
                                        follow_redirects=False,
                                    )
                                    self.assertEqual(
                                        response.status_code, 303, response.text[:1000]
                                    )
                                else:
                                    response = self.check(workspace_id, form)
                                    self.assertEqual(
                                        response.status_code, 200, response.text[:1000]
                                    )
                            saved = context.schema_workspace.supporting.current(
                                workspace_id, actor=actor
                            )
                            self.assertEqual(saved.version, review.version + 1)
                            self.assertEqual(saved.choices, review.choices)
                            repository = context.schema_workspace.schemas.local
                            path = (
                                repository.workspace_directory(workspace_id)
                                / "workspace-engine.duckdb"
                            )
                            with repository._connect(path) as connection:
                                original = connection.execute(
                                    "SELECT review_json FROM supporting_data_review WHERE version = ?",
                                    [review.version],
                                ).fetchone()[0]
                                event = connection.execute(
                                    "SELECT event_type FROM audit_event WHERE event_type = 'SUPPORTING_ACCESS_REVALIDATED' ORDER BY event_id DESC LIMIT 1"
                                ).fetchone()
                            self.assertEqual(original, review.to_json())
                            self.assertEqual(event[0], "SUPPORTING_ACCESS_REVALIDATED")
                            if explicit:
                                self.assertEqual(
                                    context.queries.get_mapping_revision(workspace_id),
                                    revision,
                                )
                                self.assertEqual(
                                    context.queries.get_mapping_working_draft(workspace_id),
                                    draft,
                                )
                                checked = self.check(workspace_id)
                                self.assertEqual(
                                    checked.status_code, 200, checked.text[:1000]
                                )
                            page = self.client.get(f"/workspaces/{workspace_id}/mapping")
                            self.assertTrue(page.context["related_data_review"]["current"])
                            self.assertEqual(
                                context.schema_workspace.supporting.current(
                                    workspace_id, actor=actor
                                ),
                                saved,
                            )
                            # The request middleware owns the timing collector. Read
                            # its terminal record rather than nesting a second one.
                            request_id = response.headers["X-Impodo-Request-ID"]
                            metrics = next(
                                json.loads(line)
                                for line in recorder.path.read_text(
                                    encoding="utf-8"
                                ).splitlines()
                                if json.loads(line).get("request_id") == request_id
                            )
                            self.assertGreater(metrics["database_connection_count"], 0)
                            self.assertEqual(metrics["database_lock_retry_count"], 0)
                            # Explicit refresh has two local scopes (10 owners, 7
                            # store contracts). Check matches also owns admission,
                            # validation and receipts (25 owners, 19 contracts).
                            self.assertLessEqual(
                                metrics["database_connection_count"], 10 if explicit else 25
                            )
                            self.assertLessEqual(
                                metrics["database_schema_check_count"],
                                7 if explicit else 19,
                            )
                            self.assertLessEqual(len(sql), 200 if explicit else 421)
            bundle = create_diagnostic_bundle(
                recorder.path.parent, build_contract=PROCESS_BUILD_CONTRACT
            )
            with ZipFile(BytesIO(bundle)) as archive:
                transitions = json.loads(archive.read("supporting-access.json"))
            revalidated = [
                event["supporting_access"]
                for event in transitions
                if event["supporting_access"]["outcome"] == "REVALIDATED"
            ]
            self.assertEqual(len(revalidated), 8)
            self.assertEqual({item["odoo_major"] for item in revalidated}, {19, 20})
            self.assertTrue(
                all(item["changes"] == ["CREDENTIAL_VERSION"] for item in revalidated)
            )

    def test_rebind_rejects_changed_choices_and_rolls_back_failed_audit(self):
        workspace_id, _ = self.ready(20, OdooConnectionMode.REMOTE)
        context = self.app.state.context
        actor = resolve_local_actor(context)
        self.assertEqual(self.check(workspace_id).status_code, 200)
        service = context.schema_workspace.supporting
        previous = service.current(workspace_id, actor=actor)
        revision = context.queries.get_mapping_revision(workspace_id)
        summary = service.summary(workspace_id, actor=actor)
        rebound = replace(
            previous,
            version=previous.version + 1,
            operation_id=str(uuid4()),
            binding_hash="sha256:" + "f" * 64,
        )
        repository = context.schema_workspace.schemas.local
        with self.assertRaises(WorkspaceError):
            repository.rebind_supporting_review(
                workspace_id,
                replace(rebound, choices=()),
                previous=previous,
                actor=actor,
            )
        with self.assertRaises(WorkspaceError):
            repository.rebind_supporting_review(
                workspace_id,
                rebound,
                previous=replace(previous, binding_hash="sha256:" + "e" * 64),
                actor=actor,
            )
        with patch.object(
            repository,
            "_insert_workspace_audit",
            side_effect=RuntimeError("interrupted audit"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted audit"):
                repository.rebind_supporting_review(
                    workspace_id, rebound, previous=previous, actor=actor
                )
        self.assertEqual(service.current(workspace_id, actor=actor), previous)
        self.assertEqual(service.summary(workspace_id, actor=actor), summary)
        self.assertEqual(context.queries.get_mapping_revision(workspace_id), revision)

    def test_changed_user_or_permissions_keeps_review_required(self):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        for major in (19, 20):
            for changed in ("user", "permissions", "company", "key_during_read"):
                with self.subTest(major=major, changed=changed):
                    workspace_id, schema = self.ready(major, OdooConnectionMode.REMOTE)
                    review = context.schema_workspace.supporting.current(
                        workspace_id, actor=actor
                    )
                    self.renew(workspace_id)
                    if changed == "company":
                        state = context.queries.get(workspace_id)
                        state = context.workspace_states.select_odoo_company(
                            workspace_id,
                            actor=actor,
                            expected_revision=state.revision,
                            selection=OdooCompanySelection(
                                schema.connection_target_hash, 106, "Example Group"
                            ),
                        )
                        schema = replace(
                            schema, read_context_hash=state.odoo_company.context_hash
                        )
                    self.configure_readers(workspace_id, schema, changed=changed)
                    if changed == "key_during_read":
                        read = context.schema_reader

                        def change_key_during_read(*args):
                            self.renew(workspace_id)
                            return read(*args)

                        context.schema_reader = change_key_during_read
                    response = self.check(workspace_id)
                    self.assertEqual(response.status_code, 422, response.text[:1000])
                    if changed in {"user", "permissions"}:
                        self.assertIsNotNone(
                            context.queries.get_odoo_schema_catalog(
                                workspace_id
                            ).pending_refresh
                        )
                    elif changed == "company":
                        self.assertEqual(
                            response.json()["failure_code"],
                            "SUPPORTING_REVIEW_REQUIRED",
                        )
                    self.assertEqual(
                        context.schema_workspace.supporting.current(
                            workspace_id, actor=actor
                        ),
                        review,
                    )
