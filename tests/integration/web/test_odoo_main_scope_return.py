"""Keep an unchanged main-record choice from discarding linked capture work."""

from dataclasses import replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from unittest.mock import AsyncMock, patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    relationship_scope_decisions,
)
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.odoo_source_scope import propose_related_odoo_data
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.contracts import (
    OdooModelCatalog,
    OdooModelSummary,
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    _register_workspace_through_services,
)
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class _SelectedMainRecords(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.selected = set()
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if (
            tag == "input"
            and attributes.get("name") == "permitted_models"
            and "checked" in attributes
        ):
            self.selected.add(attributes["value"])


class OdooMainScopeReturnTests(ProjectSetupBrowserTestCase):
    def _expanded_workspace(self, major, mode):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        created = self.workspaces.create(
            name="Saved main and linked records",
            source_system=f"Odoo {major}",
            source_mode="ODOO",
        )
        workspace = _register_workspace_through_services(
            context,
            created,
            connection_mode=mode,
            base_url=(
                "http://127.0.0.1:8069"
                if mode is OdooConnectionMode.LOCAL
                else "https://odoo.example.test"
            ),
            database=f"odoo{major}_fixture",
            intended_models=("x.root",),
        )
        credential = store_target_credential(
            self.secrets,
            workspace,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        name = SchemaField(
            name="name",
            label="Name",
            type="char",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            stored=True,
            computed=False,
            related=False,
            company_dependent=False,
            exportable=True,
        )
        write_date = replace(
            name,
            name="write_date",
            label="Last updated on",
            type="datetime",
            required=False,
            readonly=True,
        )
        models = (
            SchemaModel(
                "x.root",
                "Main records",
                (
                    name,
                    write_date,
                    replace(
                        name,
                        name="support_id",
                        label="Supporting record",
                        type="many2one",
                        relation="x.support",
                    ),
                ),
            ),
            SchemaModel("x.support", "Supporting records", (name, write_date)),
        )
        policy_hash = odoo_source_policy_hash(major)
        self.assertIsNotNone(policy_hash)
        schema = OdooSchemaCatalog(
            workspace_id=workspace.workspace_id,
            policy_hash=policy_hash,
            captured_at=datetime.now(UTC),
            captured_by=actor.identity.display_name,
            connection_mode=mode.value,
            database=workspace.odoo_database,
            odoo_version=f"{major}.0",
            models=models,
            content_hash="sha256:" + "5" * 64,
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=credential.binding_hash,
            read_principal_hash="sha256:" + "1" * 64,
            read_permission_hash="sha256:" + "2" * 64,
            read_context_hash="sha256:" + "3" * 64,
            connection_target_hash=target_identity_hash(
                connection_mode=mode.value,
                base_url=workspace.odoo_base_url,
                database=workspace.odoo_database,
            ),
        )
        catalog = OdooModelCatalog(
            workspace_id=workspace.workspace_id,
            connection_target_hash=schema.connection_target_hash,
            policy_hash=policy_hash,
            captured_at=schema.captured_at,
            captured_by=schema.captured_by,
            connection_mode=mode.value,
            database=schema.database,
            odoo_version=schema.odoo_version,
            models=tuple(
                OdooModelSummary(model.name, model.label, (), "base")
                for model in models
            ),
            content_hash="sha256:" + "6" * 64,
            read_credential_binding_hash=credential.binding_hash,
            read_principal_hash=schema.read_principal_hash,
            read_permission_hash=schema.read_permission_hash,
            read_context_hash=schema.read_context_hash,
        )
        context.schema_workspace.schemas.save_odoo_model_catalog(
            workspace.workspace_id, catalog, actor=actor
        )
        decisions = relationship_scope_decisions(
            propose_related_odoo_data(models, include_selected=True),
            actions_by_model={
                "x.support": OdooRelationshipCaptureAction.MATCH_EXISTING,
            },
        )
        workspace = context.workspace_states.update_schema_scope(
            workspace.workspace_id,
            actor=actor,
            expected_revision=workspace.revision,
            permitted_models=("x.root", "x.support"),
            relationship_decisions=decisions,
            relationship_root_models=("x.root",),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace.workspace_id, schema, actor=actor
        )
        for model in models:
            context.sources.define_odoo_capture_selection(
                workspace.workspace_id,
                actor=actor,
                dataset_name=model.name.replace(".", "_"),
                model=model.name,
                field_names=("name",),
                include_archived=False,
                page_size=100,
            )
        return context.queries.get(workspace.workspace_id), schema

    def _saved_evidence(self, workspace_id):
        queries = self.app.state.context.queries
        return (
            queries.get(workspace_id),
            queries.get_odoo_schema_catalog(workspace_id),
            queries.get_current_odoo_capture_selections(workspace_id),
            queries.get_current_odoo_relationship_scope(workspace_id),
        )

    def test_same_main_records_keep_expanded_work_for_both_majors_and_modes(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    workspace, _schema = self._expanded_workspace(major, mode)
                    workspace_id = workspace.workspace_id
                    before = self._saved_evidence(workspace_id)
                    page = self.client.get(f"/workspaces/{workspace_id}/schema")
                    self.assertEqual(page.status_code, 200, page.text)
                    self.assertEqual(
                        _SelectedMainRecords(page.text).selected, {"x.root"}
                    )
                    with (
                        patch(
                            "impodo.web.routers.schema._capture_selected_schema",
                            new_callable=AsyncMock,
                        ) as capture,
                        collect_duckdb_request_timings() as timings,
                        count_duckdb_statements() as statements,
                    ):
                        response = self._post(
                            f"/workspaces/{workspace_id}/schema",
                            {
                                "csrf_token": self.csrf,
                                "revision": str(workspace.revision),
                                "permitted_models": "x.root",
                            },
                        )
                    self.assertEqual(response.status_code, 303, response.text)
                    self.assertEqual(
                        response.headers["location"],
                        f"/workspaces/{workspace_id}/sources#selection-saved",
                    )
                    capture.assert_not_awaited()
                    self.assertEqual(self._saved_evidence(workspace_id), before)
                    # Admission uses registry + workspace; the command uses
                    # those + Data version + engine; redirect reads use
                    # workspace + engine. The exact fixture performs seven
                    # store checks and 85 SQL statements across those owners.
                    self.assertLessEqual(timings.connection_count, 8)
                    self.assertLessEqual(timings.schema_check_count, 7)
                    self.assertLessEqual(len(statements), 85)
                    self.assertLessEqual(
                        sum(
                            "information_schema" in sql.lower()
                            or "duckdb_" in sql.lower()
                            for sql in statements
                        ),
                        7,
                    )

    def test_changed_main_records_still_invalidate_and_read_new_schema(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    workspace, schema = self._expanded_workspace(major, mode)
                    workspace_id = workspace.workspace_id

                    async def capture_changed_scope(
                        context, saved, *, actor, captured_schema=schema
                    ):
                        self.assertEqual(saved.intended_models, ("x.support",))
                        self.assertEqual(
                            context.queries.get_current_odoo_capture_selections(
                                saved.workspace_id
                            ),
                            (),
                        )
                        changed = replace(
                            captured_schema,
                            models=(captured_schema.models[1],),
                            content_hash="sha256:" + "7" * 64,
                        )
                        context.schema_workspace.schemas.save_odoo_schema_catalog(
                            saved.workspace_id, changed, actor=actor
                        )
                        return changed

                    with patch(
                        "impodo.web.routers.schema._capture_selected_schema",
                        side_effect=capture_changed_scope,
                    ) as capture:
                        response = self._post(
                            f"/workspaces/{workspace_id}/schema",
                            {
                                "csrf_token": self.csrf,
                                "revision": str(workspace.revision),
                                "permitted_models": "x.support",
                            },
                        )
                    self.assertEqual(response.status_code, 303, response.text)
                    capture.assert_awaited_once()
                    saved, _schema, selections, scope = self._saved_evidence(
                        workspace_id
                    )
                    self.assertGreater(saved.revision, workspace.revision)
                    self.assertEqual(saved.intended_models, ("x.support",))
                    self.assertEqual(selections, ())
                    self.assertIsNone(scope)

    def test_same_main_records_with_stale_revision_are_rejected(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    workspace, _schema = self._expanded_workspace(major, mode)
                    workspace_id = workspace.workspace_id
                    before = self._saved_evidence(workspace_id)
                    with patch(
                        "impodo.web.routers.schema._capture_selected_schema",
                        new_callable=AsyncMock,
                    ) as capture:
                        response = self._post(
                            f"/workspaces/{workspace_id}/schema",
                            {
                                "csrf_token": self.csrf,
                                "revision": str(workspace.revision - 1),
                                "permitted_models": "x.root",
                            },
                        )
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn("workspace changed in another request", response.text)
                    capture.assert_not_awaited()
                    self.assertEqual(self._saved_evidence(workspace_id), before)
