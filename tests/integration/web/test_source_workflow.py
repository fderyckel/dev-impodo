"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.data_version.models import DataVersionState
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    relationship_scope_decisions,
)
from impodo.domain.odoo_source_scope import propose_related_odoo_data
from impodo.domain.workspace.contracts import SchemaField
from impodo.web.target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    store_target_credential,
)
from tests.support.browser_scenarios import (
    BytesIO,
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    SourceSelection,
    _BrowserOdooCaptureGateway,
    _created_workspace_id,
    _workspace_data_version_id,
    datetime,
    re,
    replace,
    timezone,
    uuid4,
)


class SourceWorkflowBrowserTests(ProjectSetupBrowserTestCase):
    def test_changing_a_frozen_odoo_source_starts_a_clean_successor(self) -> None:
        context = self.app.state.context
        predecessor_state, _schema = self._frozen_odoo_source_workspace()
        predecessor_workspace = context.migration_workspaces.get(
            predecessor_state.workspace_id,
            actor=context.actor,
        )
        predecessor_data = context.data_versions.get(
            predecessor_workspace.data_version_id,
            actor=context.actor,
        )
        warning_page = self.client.get(
            f"/workspaces/{predecessor_state.workspace_id}/target"
        )
        self.assertIn(
            "Changing this source starts a new Data version.",
            warning_page.text,
        )

        changed_form = {
            "csrf_token": self.csrf,
            "revision": str(predecessor_state.revision),
            "odoo_connection_mode": "REMOTE",
            "odoo_base_url": "https://replacement.example.test",
            "odoo_database": "replacement",
            "read_api_key": "replacement-read-secret",
            "action": "test",
        }
        restart_handoff = self._post(
            f"/workspaces/{predecessor_state.workspace_id}/target",
            changed_form,
        )
        self.assertEqual(restart_handoff.status_code, 307)
        changed = self._post(
            restart_handoff.headers["location"],
            changed_form,
        )

        self.assertEqual(changed.status_code, 303, changed.text)
        successor_workspace_id = re.search(
            r"/workspaces/([^/]+)/target",
            changed.headers["location"],
        ).group(1)
        self.assertNotEqual(
            successor_workspace_id,
            predecessor_state.workspace_id,
        )
        self.assertEqual(
            changed.headers["location"],
            f"/workspaces/{successor_workspace_id}/target#remote-connection-status",
        )
        successor_workspace = context.migration_workspaces.get(
            successor_workspace_id,
            actor=context.actor,
        )
        successor_data = context.data_versions.get(
            successor_workspace.data_version_id,
            actor=context.actor,
        )
        successor_package = (
            context.data_version_source_projection.packages.repository
            .get_source_package(successor_data.data_version_id)
        )
        self.assertIs(predecessor_data.state, DataVersionState.FROZEN)
        self.assertIs(successor_data.state, DataVersionState.DRAFT)
        self.assertEqual(
            successor_data.parent_data_version_id,
            predecessor_data.data_version_id,
        )
        self.assertEqual(successor_data.version_number, 2)
        self.assertEqual(successor_package.datasets, ())
        self.assertIsNotNone(
            context.queries.get_source_selection(predecessor_state.workspace_id)
        )
        self.assertIsNone(
            context.queries.get_source_selection(successor_workspace_id)
        )
        successor_state = context.queries.get(successor_workspace_id)
        self.assertEqual(
            successor_state.odoo_base_url,
            "https://replacement.example.test",
        )
        self.assertEqual(successor_state.odoo_database, "replacement")

        continued = self._post(
            f"/workspaces/{successor_workspace_id}/target",
            {
                "csrf_token": self.csrf,
                "revision": str(successor_state.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://replacement.example.test",
                "odoo_database": "replacement",
                "action": "save",
            },
        )
        self.assertEqual(continued.status_code, 303, continued.text)
        self.assertEqual(
            continued.headers["location"],
            f"/workspaces/{successor_workspace_id}/schema",
        )

    def test_frozen_stage_three_restart_rebuilds_schema_in_a_successor(self) -> None:
        context = self.app.state.context
        predecessor_state, _schema = self._frozen_odoo_source_workspace()
        predecessor_workspace = context.migration_workspaces.get(
            predecessor_state.workspace_id,
            actor=context.actor,
        )
        predecessor_data = context.data_versions.get(
            predecessor_workspace.data_version_id,
            actor=context.actor,
        )
        changed_state = context.workspace_states.update_target(
            predecessor_state.workspace_id,
            actor=context.actor,
            expected_revision=predecessor_state.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://changed-before-fix.example.test",
            odoo_database="changed_before_fix",
            intended_applications=(),
            intended_models=("res.partner",),
        )
        credential = store_target_credential(
            context.secret_store,
            changed_state,
            TargetCredentialRole.READ,
            "changed-source-secret",
            persistent=False,
        )
        audit_stored_target_credential(
            context.workspace_states,
            changed_state,
            TargetCredentialRole.READ,
            credential,
            actor=context.actor,
        )
        captured = self._post(
            f"/workspaces/{changed_state.workspace_id}/schema/capture",
            {"csrf_token": self.csrf, "return_to_sources": "1"},
        )
        self.assertEqual(captured.status_code, 303, captured.text)
        saved_plan = self._post(
            f"/workspaces/{changed_state.workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "changed_contacts",
                "model": "res.partner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(saved_plan.status_code, 303, saved_plan.text)
        selection = context.queries.get_current_odoo_capture_selections(
            changed_state.workspace_id
        )[0]

        assessment_form = {
            "csrf_token": self.csrf,
            "selection_id": selection.selection_id,
            "selection_hash": selection.content_hash,
        }
        restart_handoff = self._post(
            f"/workspaces/{changed_state.workspace_id}/sources/odoo-assessment",
            assessment_form,
        )
        self.assertEqual(restart_handoff.status_code, 307)
        restarted = self._post(
            restart_handoff.headers["location"],
            assessment_form,
        )

        self.assertEqual(restarted.status_code, 303, restarted.text)
        successor_workspace_id = re.search(
            r"/workspaces/([^/]+)/sources",
            restarted.headers["location"],
        ).group(1)
        successor_workspace = context.migration_workspaces.get(
            successor_workspace_id,
            actor=context.actor,
        )
        successor_data = context.data_versions.get(
            successor_workspace.data_version_id,
            actor=context.actor,
        )
        self.assertIs(predecessor_data.state, DataVersionState.FROZEN)
        self.assertIs(successor_data.state, DataVersionState.DRAFT)
        self.assertEqual(
            successor_data.parent_data_version_id,
            predecessor_data.data_version_id,
        )
        self.assertIsNotNone(
            context.queries.get_odoo_schema_catalog(successor_workspace_id)
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(
                successor_workspace_id
            ),
            (),
        )

    def test_changed_odoo_source_scope_blocks_downstream_until_refrozen(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_id = workspace_state.workspace_id
        saved_plan = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "odoo_contacts",
                "model": "res.partner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(saved_plan.status_code, 303)
        context.odoo_capture_publication.publish(
            workspace_id,
            _BrowserOdooCaptureGateway(workspace_state, schema),
            actor=context.actor,
        )
        self.assertTrue(context.navigation.get_source_readiness(workspace_id).ready)

        current = context.queries.get(workspace_id)
        context.workspace_states.update_schema_scope(
            workspace_id,
            actor=context.actor,
            expected_revision=current.revision,
            permitted_models=("res.partner", "uom.uom"),
        )
        expanded_schema = replace(
            schema,
            models=(
                schema.models[0],
                replace(
                    schema.models[0],
                    name="uom.uom",
                    label="Units of Measure",
                ),
            ),
            content_hash="sha256:" + "7" * 64,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id,
            expanded_schema,
            actor=context.actor,
        )
        partial_plan = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "odoo_contacts",
                "model": "res.partner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(partial_plan.status_code, 303)
        expected_location = f"/workspaces/{workspace_id}/sources#capture-plan"

        for path in ("mapping", "summary", "prepare"):
            with self.subTest(path=path):
                response = self.client.get(
                    f"/workspaces/{workspace_id}/{path}",
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], expected_location)

        with patch.object(
            context.preparation_jobs,
            "enqueue",
            wraps=context.preparation_jobs.enqueue,
        ) as enqueue:
            response = self._post(
                f"/workspaces/{workspace_id}/summary/check",
                {"csrf_token": self.csrf},
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], expected_location)
        enqueue.assert_not_called()
        source_page = self.client.get(response.headers["location"])
        self.assertIn("Your source choices changed", source_page.text)
        self.assertIn("still need a capture plan", source_page.text)
        self.assertIn("previous frozen version remains in history", source_page.text)

    def test_source_files_can_change_only_before_table_choices_are_saved(
        self,
    ) -> None:
        context = self.app.state.context
        workspace_state = self.workspaces.create(
            name="Correctable source files",
            source_system="CSV",
        )
        kept = context.intake.accept(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            display_name="customers.csv",
            stream=BytesIO(b"code,name\nC1,Kept\n"),
        )
        current = context.queries.get(workspace_state.workspace_id)
        wrong = context.intake.accept(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=current.revision,
            display_name="wrong.csv",
            stream=BytesIO(b"code,name\nBAD,Wrong\n"),
        )
        current = context.queries.get(workspace_state.workspace_id)
        files_page = self.client.get(f"/workspaces/{workspace_state.workspace_id}/files")
        self.assertEqual(
            files_page.text.count("data-source-file-remove-form"),
            2,
        )
        self.assertIn("data-source-file-remove-dialog", files_page.text)

        wrong_path = (
            context.workspace_states.repository.workspace_directory(workspace_state.workspace_id)
            / "inbox"
            / wrong.stored_name
        )
        removed_draft = self._post(
            f"/workspaces/{workspace_state.workspace_id}/files/{wrong.file_id}/remove",
            {
                "csrf_token": self.csrf,
                "revision": str(current.revision),
                "return_to": "files",
            },
        )
        self.assertEqual(removed_draft.status_code, 303)
        self.assertFalse(wrong_path.exists())
        current = context.queries.get(workspace_state.workspace_id)
        registered = context.workspace_states.register(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=current.revision,
        )

        get_workspace = context.queries.get

        def read_workspace_in_worker(workspace_id):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return get_workspace(workspace_id)

        with (
            patch.object(
                type(context.queries), "get", side_effect=read_workspace_in_worker
            ),
            collect_duckdb_request_timings() as timings,
        ):
            source_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )
        self.assertEqual(source_page.status_code, 200)
        self.assertLessEqual(timings.connection_count, 8)
        self.assertEqual(
            source_page.text.count("data-source-file-remove-form"),
            1,
        )
        self.assertIn(
            f'action="/workspaces/{workspace_state.workspace_id}/sources/files"',
            source_page.text,
        )
        replacement_upload = self.client.post(
            f"/workspaces/{workspace_state.workspace_id}/sources/files",
            data={
                "csrf_token": self.csrf,
                "revision": str(registered.revision),
            },
            files={
                "source_file": (
                    "corrected.csv",
                    b"code,name\nC2,Corrected\n",
                    "text/csv",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(replacement_upload.status_code, 303)
        current = context.queries.get(workspace_state.workspace_id)
        corrected = next(
            item
            for item in current.source_files
            if item.display_name == "corrected.csv"
        )
        removed_registered = self._post(
            f"/workspaces/{workspace_state.workspace_id}/files/{corrected.file_id}/remove",
            {
                "csrf_token": self.csrf,
                "revision": str(current.revision),
                "return_to": "sources",
            },
        )
        self.assertEqual(removed_registered.status_code, 303)
        self.assertEqual(
            removed_registered.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/sources#source-files",
        )
        removed_page = self.client.get(removed_registered.headers["location"])
        self.assertIn(
            "Removed corrected.csv from this Data version.",
            removed_page.text,
        )
        datasets_page = self.client.get(f"/workspaces/{workspace_state.workspace_id}/datasets")
        self.assertEqual(
            datasets_page.text.count("data-source-file-remove-form"),
            1,
        )

        current = context.queries.get(workspace_state.workspace_id)
        now = datetime.now(timezone.utc)
        context.sources.sources.save_source_selection(
            workspace_state.workspace_id,
            SourceSelection(
                selection_id=str(uuid4()),
                version=1,
                data_version_id=_workspace_data_version_id(
                    context,
                    workspace_state.workspace_id,
                ),
                created_at=now,
                created_by=context.actor.identity.display_name,
                datasets=(),
                content_hash="sha256:" + "a" * 64,
            ),
            actor=context.actor,
        )
        blocked = self._post(
            f"/workspaces/{workspace_state.workspace_id}/files/{kept.file_id}/remove",
            {
                "csrf_token": self.csrf,
                "revision": str(current.revision),
                "return_to": "datasets",
            },
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertIn(
            "Source files cannot be changed after table choices are saved",
            blocked.text,
        )
        self.assertNotIn("data-source-file-remove-form", blocked.text)

    def test_odoo_source_page_proposes_related_business_scope(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        product_schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    name="product.template",
                    label="Product",
                    fields=(
                        SchemaField(
                            name="name",
                            label="Product Name",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                            exportable=True,
                        ),
                        SchemaField(
                            name="categ_id",
                            label="Product Category",
                            type="many2one",
                            required=True,
                            readonly=False,
                            relation="product.category",
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        SchemaField(
                            name="company_id",
                            label="Company",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation="res.company",
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        SchemaField(
                            name="activity_ids",
                            label="Activities",
                            type="one2many",
                            required=False,
                            readonly=True,
                            relation="mail.activity",
                            relation_field="res_id",
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        SchemaField(
                            name="x_owner_id",
                            label="Owner",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation="x.owner",
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                    ),
                ),
            ),
        )
        context = self.app.state.context
        model_catalog = SimpleNamespace(
            models=(
                SimpleNamespace(
                    name="product.template",
                    label="Products",
                ),
                SimpleNamespace(
                    name="product.category",
                    label="Product Categories",
                ),
                SimpleNamespace(
                    name="res.company",
                    label="Companies",
                ),
                SimpleNamespace(
                    name="mail.activity",
                    label="Activities",
                ),
                SimpleNamespace(
                    name="x.owner",
                    label="Owners",
                ),
            )
        )

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=product_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn("Review the related data for this migration", page.text)
        self.assertIn("Needed to keep the records meaningful", page.text)
        self.assertIn("Reuse destination setup", page.text)
        self.assertIn("Not part of the business-data move", page.text)
        self.assertIn("No standard default", page.text)
        self.assertIn("Product Category", page.text)
        self.assertIn('name="related_actions"', page.text)
        self.assertIn('value="product.category::CAPTURE_LINKED"', page.text)
        self.assertIn('value="res.company::MATCH_EXISTING"', page.text)
        self.assertIn('value="x.owner::DO_NOT_CAPTURE"', page.text)
        self.assertIn(
            "Needs review &middot; choose whether these links belong in this migration",
            page.text,
        )
        self.assertIn(
            "Recommended by Impodo &middot; needs your review",
            page.text,
        )
        self.assertIn("This link is required", page.text)
        self.assertIn(
            "Before transfer, Impodo must match them to existing destination settings",
            page.text,
        )
        self.assertIn("Not included", page.text)
        self.assertIn("Save related-data decisions", page.text)
        self.assertIn(
            'data-submitting-label="Saving decisions and checking the next relationships..."',
            page.text,
        )
        self.assertNotIn("Review recommended supporting data", page.text)
        self.assertNotIn(
            "Related record types outside this source selection",
            page.text,
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            ),
            (),
        )

    def test_odoo_source_page_saves_unprofiled_related_data_without_backtracking(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            permitted_models=("product.template",),
        )
        product_schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    name="product.template",
                    label="Products",
                    fields=(
                        SchemaField(
                            name="name",
                            label="Product Name",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                            exportable=True,
                        ),
                        SchemaField(
                            name="x_owner_id",
                            label="Owner",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation="x.owner",
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        SchemaField(
                            name="x_reviewer_id",
                            label="Reviewer",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation="x.reviewer",
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                    ),
                ),
            ),
        )
        model_catalog = SimpleNamespace(
            models=(
                SimpleNamespace(
                    name="product.template",
                    label="Products",
                ),
                SimpleNamespace(
                    name="x.owner",
                    label="Owners",
                ),
            )
        )
        capture_schema = AsyncMock(return_value=product_schema)

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=product_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._capture_selected_schema",
                capture_schema,
            ),
        ):
            with collect_duckdb_request_timings() as related_data_timings:
                saved = self._post(
                    f"/workspaces/{workspace_state.workspace_id}"
                    "/sources/odoo-related-data",
                    {
                        "csrf_token": self.csrf,
                        "revision": str(workspace_state.revision),
                        "related_actions": "x.owner::CAPTURE_LINKED",
                    },
                )
            saved_state = context.queries.get(workspace_state.workspace_id)
            repeated = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(saved_state.revision),
                    "related_actions": "x.owner::CAPTURE_LINKED",
                },
            )
            review_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        self.assertEqual(saved.status_code, 303)
        self.assertEqual(repeated.status_code, 303)
        self.assertLessEqual(related_data_timings.connection_count, 8)
        self.assertEqual(
            saved.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}"
            "/sources#related-data-scope",
        )
        self.assertEqual(
            context.queries.get(workspace_state.workspace_id).intended_models,
            ("product.template", "x.owner"),
        )
        self.assertEqual(
            context.queries.get(workspace_state.workspace_id).revision,
            saved_state.revision,
        )
        relationship_scope = (
            context.queries.get_current_odoo_relationship_scope(
                workspace_state.workspace_id
            )
        )
        self.assertIsNotNone(relationship_scope)
        self.assertEqual(relationship_scope.version, 1)
        self.assertEqual(len(relationship_scope.decisions), 1)
        self.assertEqual(
            relationship_scope.decisions[0].identity,
            ("product.template", "x_owner_id"),
        )
        self.assertIs(
            relationship_scope.decisions[0].action,
            OdooRelationshipCaptureAction.CAPTURE_LINKED,
        )
        self.assertIn("relationship-review-unavailable", review_page.text)
        self.assertIn(
            "will not treat an unavailable relationship as excluded",
            review_page.text,
        )
        capture_schema.assert_awaited_once()

    def test_related_data_review_discovers_each_new_graph_level(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            permitted_models=("product.template",),
        )
        base = schema.models[0]
        name = SchemaField(
            name="name",
            label="Name",
            type="char",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            exportable=True,
        )
        product = replace(
            base,
            name="product.template",
            label="Products",
            fields=(
                name,
                SchemaField(
                    name="x_owner_id",
                    label="Owner",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="x.owner",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        owner = replace(
            base,
            name="x.owner",
            label="Owners",
            fields=(
                name,
                SchemaField(
                    name="calendar_id",
                    label="Working Hours",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="resource.calendar",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        calendar = replace(
            base,
            name="resource.calendar",
            label="Working Hours",
            fields=(name,),
        )
        initial_schema = replace(schema, models=(product,))
        owner_schema = replace(schema, models=(product, owner))
        complete_schema = replace(schema, models=(product, owner, calendar))
        current = {"schema": initial_schema}
        model_catalog = SimpleNamespace(
            models=tuple(
                SimpleNamespace(name=model.name, label=model.label)
                for model in complete_schema.models
            )
        )

        async def capture_next(_context, saved_state):
            current["schema"] = (
                owner_schema
                if "resource.calendar" not in saved_state.intended_models
                else complete_schema
            )
            return current["schema"]

        capture_schema = AsyncMock(side_effect=capture_next)
        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=lambda _workspace_id: current["schema"],
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._capture_selected_schema",
                capture_schema,
            ),
        ):
            first = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::CAPTURE_LINKED",
                },
            )
            after_first = context.queries.get(workspace_state.workspace_id)
            first_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )
            second = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(after_first.revision),
                    "related_actions": [
                        "x.owner::CAPTURE_LINKED",
                        "resource.calendar::CAPTURE_LINKED",
                    ],
                },
            )
            second_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )
            after_second = context.queries.get(workspace_state.workspace_id)
            leaf = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(after_second.revision),
                    "related_actions": [
                        "x.owner::MATCH_EXISTING",
                        "resource.calendar::CAPTURE_LINKED",
                    ],
                },
            )
            leaf_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        self.assertEqual(first.status_code, 303)
        self.assertIn("Review 1 newly found relationship", first_page.text)
        self.assertIn(
            'value="resource.calendar::DO_NOT_CAPTURE" selected',
            first_page.text,
        )
        self.assertIn("Needs review", first_page.text)
        self.assertNotIn("relationship-review-complete", first_page.text)
        self.assertEqual(second.status_code, 303)
        self.assertIn("relationship-review-complete", second_page.text)
        self.assertEqual(leaf.status_code, 303)
        self.assertIn("relationship-review-complete", leaf_page.text)
        self.assertNotIn("resource.calendar::", leaf_page.text)
        self.assertEqual(
            frozenset(
                context.queries.get(
                    workspace_state.workspace_id
                ).intended_models
            ),
            frozenset(
                {"product.template", "x.owner"}
            ),
        )
        scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {item.identity for item in scope.decisions},
            {
                ("product.template", "x_owner_id"),
            },
        )
        self.assertIs(
            scope.decisions[0].action,
            OdooRelationshipCaptureAction.MATCH_EXISTING,
        )
        self.assertEqual(capture_schema.await_count, 3)

    def test_recommended_supporting_model_defaults_to_linked_only(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            permitted_models=("product.category", "product.template"),
        )
        product = replace(
            schema.models[0],
            name="product.template",
            label="Products",
            fields=(
                *schema.models[0].fields,
                SchemaField(
                    name="categ_id",
                    label="Product Category",
                    type="many2one",
                    required=True,
                    readonly=False,
                    relation="product.category",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        category = replace(
            schema.models[0],
            name="product.category",
            label="Product Categories",
        )
        product_schema = replace(schema, models=(product, category))
        model_catalog = SimpleNamespace(
            models=(
                SimpleNamespace(
                    name="product.category",
                    label="Product Categories",
                ),
                SimpleNamespace(
                    name="product.template",
                    label="Products",
                ),
            )
        )

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=product_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
                "?model=product.category&edit=1"
            )

        self.assertEqual(page.status_code, 200)
        self.assertRegex(
            page.text,
            r'name="linked_only" value="1"\s+checked',
        )
        self.assertIn("Recommended for this related data", page.text)
        self.assertIn("Archived linked records are included", page.text)

    def test_completed_capture_action_is_inside_current_evidence(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        credential = store_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        schema = replace(
            schema,
            read_credential_binding_hash=credential.binding_hash,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            schema,
            actor=context.actor,
        )

        with collect_duckdb_request_timings() as selection_timings:
            selected = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-selection",
                {
                    "csrf_token": self.csrf,
                    "dataset_name": "odoo_contacts",
                    "model": schema.models[0].name,
                    "field_names": "name",
                    "include_archived": "",
                    "page_size": "100",
                },
            )

        self.assertEqual(selected.status_code, 303)
        self.assertLessEqual(selection_timings.connection_count, 8)
        page = self.client.get(selected.headers["location"])
        current_evidence = page.text.index("Current protected evidence")
        next_action = page.text.index("Capture plans complete")
        section_end = page.text.index("</section>", current_evidence)
        self.assertLess(current_evidence, next_action)
        self.assertLess(next_action, section_end)
        self.assertIn(
            'data-submitting-label="Checking matching records in Odoo..."',
            page.text,
        )

    def test_linked_relationship_review_is_inside_current_evidence(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=workspace_state.revision,
            permitted_models=("product.template", "product.category"),
        )
        base_model = schema.models[0]
        product = replace(
            base_model,
            name="product.template",
            label="Products",
            fields=(
                SchemaField(
                    name="name",
                    label="Product Name",
                    type="char",
                    required=True,
                    readonly=False,
                    relation=None,
                    relation_field=None,
                    selection=(),
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    exportable=True,
                ),
                SchemaField(
                    name="write_date",
                    label="Last Updated",
                    type="datetime",
                    required=False,
                    readonly=True,
                    relation=None,
                    relation_field=None,
                    selection=(),
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    sortable=True,
                    exportable=True,
                ),
                SchemaField(
                    name="categ_id",
                    label="Product Category",
                    type="many2one",
                    required=True,
                    readonly=False,
                    relation="product.category",
                    relation_field=None,
                    selection=(),
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    exportable=True,
                ),
            ),
        )
        category = replace(
            base_model,
            name="product.category",
            label="Product Categories",
            fields=(
                SchemaField(
                    name="name",
                    label="Category Name",
                    type="char",
                    required=True,
                    readonly=False,
                    relation=None,
                    relation_field=None,
                    selection=(),
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    exportable=True,
                ),
                SchemaField(
                    name="write_date",
                    label="Last Updated",
                    type="datetime",
                    required=False,
                    readonly=True,
                    relation=None,
                    relation_field=None,
                    selection=(),
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    sortable=True,
                    exportable=True,
                ),
            ),
        )
        credential = store_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        schema = replace(
            schema,
            models=(product, category),
            read_credential_binding_hash=credential.binding_hash,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            schema,
            actor=context.actor,
        )
        current_state = context.queries.get(workspace_state.workspace_id)
        context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=context.actor,
            expected_revision=current_state.revision,
            permitted_models=current_state.intended_models,
            relationship_decisions=relationship_scope_decisions(
                propose_related_odoo_data(
                    schema.models,
                    include_selected=True,
                ),
                included_models={"product.category"},
            ),
        )

        root = self._post(
            f"/workspaces/{workspace_state.workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "products",
                "model": "product.template",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(root.status_code, 303, root.text)
        linked = self._post(
            f"/workspaces/{workspace_state.workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "product_categories",
                "model": "product.category",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
                "linked_only": "1",
            },
        )
        self.assertEqual(linked.status_code, 303, linked.text)

        page = self.client.get(linked.headers["location"])
        current_evidence = page.text.index("Current protected evidence")
        relationship_review = page.text.index(
            "Relationship fields used to find linked records"
        )
        next_action = page.text.index("Capture plans complete")
        section_end = page.text.index("</section>", current_evidence)
        self.assertLess(current_evidence, relationship_review)
        self.assertLess(relationship_review, next_action)
        self.assertLess(next_action, section_end)
        self.assertIn(
            "Products.Product Category (product.template.categ_id) refers to "
            "product.category",
            page.text,
        )

    def test_source_review_saves_table_choices_without_an_intermediate_page(
        self,
    ) -> None:
        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Inline source confirmation",
                "source_mode": "FILE",
                "source_system_identity": "Fictional ERP",
            },
        )
        workspace_id = _created_workspace_id(self.app, created)
        uploaded = self.client.post(
            f"/workspaces/{workspace_id}/files",
            data={"csrf_token": self.csrf, "revision": "1"},
            files={
                "source_file": (
                    "customers.csv",
                    b"code,name\nC001,Example\n",
                    "text/csv",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303)
        context = self.app.state.context
        workspace_state = context.queries.get(workspace_id)
        registered = self._post(
            f"/workspaces/{workspace_id}/register",
            {
                "csrf_token": self.csrf,
                "revision": str(workspace_state.revision),
            },
        )
        self.assertEqual(registered.status_code, 303)
        self.assertEqual(
            registered.headers["location"],
            f"/workspaces/{workspace_id}/sources#source-files",
        )
        inspection_page = self.client.get(registered.headers["location"])
        self.assertIn("Checked 1 source file.", inspection_page.text)
        self.assertIn("customers.csv", inspection_page.text)
        self.assertIn("Check files again", inspection_page.text)
        self.assertNotIn("Your files have not been checked yet", inspection_page.text)
        catalog = context.queries.get_source_catalogs(workspace_id)[0]
        confirmed = self.client.post(
            f"/workspaces/{workspace_id}/sources/{catalog.file_id}/configure",
            data={
                "csrf_token": self.csrf,
                "action": "confirm",
                "encoding": "utf-8",
                "delimiter": ",",
                "header_row_0": "1",
                "selected_0": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(confirmed.status_code, 303)
        source_page = self.client.get(confirmed.headers["location"])
        self.assertIn("Save the tables for this data version", source_page.text)
        self.assertIn('name="dataset_name_0"', source_page.text)
        self.assertIn("Use 1 to 63 characters", source_page.text)
        self.assertIn("Start with a lowercase letter", source_page.text)
        self.assertIn("Give each table a different name", source_page.text)
        self.assertIn("data-dataset-name", source_page.text)
        self.assertIn(
            f'action="/workspaces/{workspace_id}/datasets/freeze"',
            source_page.text,
        )

        unfinished_page = self.client.get(
            f"/workspaces/{workspace_id}/datasets",
            follow_redirects=False,
        )
        self.assertEqual(unfinished_page.status_code, 303)
        self.assertEqual(
            unfinished_page.headers["location"],
            f"/workspaces/{workspace_id}/sources#table-choices",
        )

        invalid_name = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                "dataset_name_0": "Product-withUoM_v1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(invalid_name.status_code, 422)
        self.assertIn("Start with a lowercase letter from a to z", invalid_name.text)
        self.assertIn(
            "Use only lowercase letters, numbers, and underscores",
            invalid_name.text,
        )

        frozen = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                "dataset_name_0": "customers",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(frozen.status_code, 303)
        saved_page = self.client.get(frozen.headers["location"])
        self.assertIn("Saved source tables", saved_page.text)
        self.assertIn("Tables ready for the next step", saved_page.text)
        self.assertNotIn('name="dataset_name_0"', saved_page.text)

        saved_selection = context.queries.get_source_selection(workspace_id)
        saved_catalogs = context.queries.get_source_catalogs(workspace_id)
        self.assertIsNotNone(saved_selection)
        with patch.object(
            context.sources,
            "freeze_selection",
            side_effect=AssertionError("duplicate request refroze the source"),
        ):
            repeated_freeze = self.client.post(
                f"/workspaces/{workspace_id}/datasets/freeze",
                data={
                    "csrf_token": self.csrf,
                    "dataset_name_0": "customers",
                },
                headers=POST_HEADERS,
                follow_redirects=False,
            )
        self.assertEqual(repeated_freeze.status_code, 303)
        self.assertEqual(
            context.queries.get_source_selection(workspace_id),
            saved_selection,
        )

        changed_freeze = self.client.post(
            f"/workspaces/{workspace_id}/datasets/freeze",
            data={
                "csrf_token": self.csrf,
                "dataset_name_0": "renamed_customers",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(changed_freeze.status_code, 422)
        self.assertIn("already frozen", changed_freeze.text)
        self.assertEqual(
            context.queries.get_source_selection(workspace_id),
            saved_selection,
        )
        source_page = self.client.get(f"/workspaces/{workspace_id}/sources")
        self.assertEqual(source_page.status_code, 200)
        self.assertNotIn("Check files again", source_page.text)
        self.assertIn('class="source-review-fields" disabled', source_page.text)

        with patch(
            "impodo.application.data_version.source_worker.inspect_source_file_isolated",
            side_effect=AssertionError("Saved files must not be reinspected"),
        ):
            rejected = self._post(
                f"/workspaces/{workspace_id}/sources/inspect",
                {"csrf_token": self.csrf},
            )
        self.assertEqual(rejected.status_code, 422)
        self.assertIn("The tables for this Data version are already saved", rejected.text)
        self.assertEqual(context.queries.get_source_selection(workspace_id), saved_selection)

        rejected_preview = self._post(
            f"/workspaces/{workspace_id}/sources/{catalog.file_id}/configure",
            {
                "csrf_token": self.csrf,
                "action": "preview",
                "encoding": "utf-8",
                "delimiter": ",",
                "header_row_0": "1",
            },
        )
        self.assertEqual(rejected_preview.status_code, 422)
        self.assertIn("The tables for this Data version are already saved", rejected_preview.text)
        self.assertEqual(context.queries.get_source_selection(workspace_id), saved_selection)

        with self.assertRaisesRegex(
            MigrationFoundationError,
            "Accepted DataVersion source evidence is immutable",
        ):
            context.sources.sources.save_source_catalogs(
                workspace_id,
                saved_catalogs,
                actor=context.actor,
            )
        self.assertEqual(context.queries.get_source_selection(workspace_id), saved_selection)
        self.assertEqual(context.queries.get_source_catalogs(workspace_id), saved_catalogs)

        hierarchy_page = self.client.get(
            f"/workspaces/{workspace_id}/derived-entities"
        )
        self.assertEqual(hierarchy_page.status_code, 200)
        self.assertIn(
            "Build a hierarchy from separate fields",
            hierarchy_page.text,
        )
        self.assertIn("Several fields form a hierarchy", hierarchy_page.text)
        self.assertIn(
            "models/refresh?return_to=hierarchy",
            hierarchy_page.text,
        )
        self.assertIn('/static/derived-entities.js', hierarchy_page.text)
        for section_id in (
            "lookup-extraction",
            "hierarchy-extraction",
            "create-related-datasets",
        ):
            with self.subTest(section_id=section_id):
                self.assertIn(
                    f'id="{section_id}" data-derived-entity-section>',
                    hierarchy_page.text,
                )
                self.assertIn(
                    f'type="button" data-derived-entity-target="{section_id}"',
                    hierarchy_page.text,
                )
        derived_script = self.client.get("/static/derived-entities.js")
        self.assertEqual(derived_script.status_code, 200)
        self.assertIn("parent.open = true", derived_script.text)
        self.assertIn("revealCurrentTarget", derived_script.text)
        self.assertIn('window.addEventListener("pageshow"', derived_script.text)
