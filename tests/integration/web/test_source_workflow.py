"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

import asyncio
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.data_version.models import DataVersionState
from impodo.domain.guided_odoo_capture import (
    GuidedOdooCaptureIssueCode,
    GuidedOdooCapturePlanIssue,
    GuidedOdooCapturePlanProposal,
)
from impodo.domain.odoo.contracts import (
    ConnectorAuthorizationError,
    ConnectorTransportError,
    MetadataSnapshot,
)
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    relationship_scope_decisions,
)
from impodo.domain.odoo_source_scope import propose_related_odoo_data
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
)
from impodo.domain.workspace.contracts import (
    OdooModelCatalog,
    OdooModelSummary,
    SchemaField,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.target_credentials import (
    TargetCredentialRole,
    audit_stored_target_credential,
    store_target_credential,
)
from tests.support.browser_scenarios import (
    POST_HEADERS,
    BytesIO,
    ProjectSetupBrowserTestCase,
    SourceSelection,
    _browser_schema,
    _BrowserOdooCaptureGateway,
    _created_workspace_id,
    _workspace_data_version_id,
    datetime,
    re,
    replace,
    timezone,
    uuid4,
)
from tests.support.identity import resolve_local_actor


class SourceWorkflowBrowserTests(ProjectSetupBrowserTestCase):
    def _guided_relationship_capture_workspace(
        self,
        *,
        include_ambiguous_owner: bool = False,
    ):
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        fields_by_name = {
            field.name: field for field in schema.models[0].fields
        }
        name_field = replace(
            fields_by_name["name"],
            required=True,
            readonly=False,
            relation=None,
            stored=True,
            computed=False,
            related=False,
            company_dependent=False,
            exportable=True,
        )
        root_fields = [
            name_field,
            SchemaField(
                name="company_id",
                label="Company",
                type="many2one",
                required=True,
                readonly=False,
                relation="res.company",
                relation_field=None,
                selection=(),
                stored=True,
                computed=False,
                related=False,
                company_dependent=False,
                exportable=True,
            ),
            fields_by_name["write_date"],
        ]
        models = [
            replace(
                schema.models[0],
                name="x.root",
                label="Main records",
                fields=(),
                unique_constraints=(),
            ),
            replace(
                schema.models[0],
                name="res.company",
                label="Companies",
                fields=(
                    replace(name_field, label="Company Name"),
                    replace(
                        name_field,
                        name="vat",
                        label="Tax ID",
                        required=False,
                    ),
                    fields_by_name["write_date"],
                ),
                unique_constraints=(),
            ),
        ]
        if include_ambiguous_owner:
            root_fields.append(
                SchemaField(
                    name="owner_id",
                    label="Owner",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="x.owner",
                    relation_field=None,
                    selection=(),
                    stored=True,
                    computed=False,
                    related=False,
                    company_dependent=False,
                    exportable=True,
                )
            )
            models.append(
                replace(
                    schema.models[0],
                    name="x.owner",
                    label="Owners",
                    fields=(
                        replace(name_field, label="Owner Name"),
                        replace(
                            name_field,
                            name="reference",
                            label="Owner Reference",
                        ),
                        fields_by_name["write_date"],
                    ),
                    unique_constraints=(),
                )
            )
        models[0] = replace(models[0], fields=tuple(root_fields))
        selected_schema = replace(
            schema,
            models=tuple(models),
            content_hash="sha256:" + "6" * 64,
        )
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root",),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            selected_schema,
            actor=resolve_local_actor(context),
        )
        model_catalog = OdooModelCatalog(
            workspace_id=workspace_state.workspace_id,
            connection_target_hash=selected_schema.connection_target_hash,
            policy_hash=selected_schema.policy_hash,
            captured_at=datetime.now(timezone.utc),
            captured_by=resolve_local_actor(context).identity.display_name,
            connection_mode=selected_schema.connection_mode,
            database=selected_schema.database,
            odoo_version=selected_schema.odoo_version,
            models=tuple(
                OdooModelSummary(
                    name=model.name,
                    label=model.label,
                    modules=(),
                    state="base",
                )
                for model in selected_schema.models
            ),
            content_hash="sha256:" + "7" * 64,
            read_credential_binding_hash=(
                selected_schema.read_credential_binding_hash
            ),
            read_principal_hash=selected_schema.read_principal_hash,
            read_permission_hash=selected_schema.read_permission_hash,
            read_context_hash=selected_schema.read_context_hash,
        )
        context.schema_workspace.schemas.save_odoo_model_catalog(
            workspace_state.workspace_id,
            model_catalog,
            actor=resolve_local_actor(context),
        )
        return workspace_state, selected_schema, model_catalog

    def _save_company_relationship_scope(
        self,
        workspace_state,
        selected_schema,
    ):
        context = self.app.state.context
        decisions = relationship_scope_decisions(
            propose_related_odoo_data(
                selected_schema.models,
                include_selected=True,
            ),
            actions_by_model={
                "res.company": OdooRelationshipCaptureAction.MATCH_EXISTING,
            },
        )
        saved_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root", "res.company"),
            relationship_decisions=decisions,
            relationship_root_models=("x.root",),
        )
        return saved_state, decisions

    @staticmethod
    def _metadata_model(model) -> ModelMetadata:
        """Mirror one governed schema model as connector metadata."""

        return ModelMetadata(
            model=model.name,
            description=model.label,
            fields={
                field.name: FieldMetadata(
                    name=field.name,
                    type=field.type,
                    label=field.label,
                    required=field.required,
                    readonly=field.readonly,
                    relation=field.relation,
                    relation_field=field.relation_field,
                    selection=field.selection,
                    stored=field.stored,
                    computed=field.computed,
                    related=field.related,
                    company_dependent=field.company_dependent,
                    exportable=field.exportable,
                )
                for field in model.fields
            },
        )

    def _incremental_relationship_capture_workspace(self):
        """Build a three-level graph backed by exact remote-read fixtures."""

        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("product.template",),
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
        product = replace(
            schema.models[0],
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
                    stored=True,
                    computed=False,
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
            unique_constraints=(),
        )
        owner = replace(
            product,
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
                    stored=True,
                    computed=False,
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
            unique_constraints=(),
        )
        calendar = replace(
            product,
            name="resource.calendar",
            label="Working Hours",
            fields=(name,),
            unique_constraints=(),
        )
        root_schema = replace(
            schema,
            models=(product,),
            content_hash="sha256:" + "8" * 64,
        )
        model_catalog = replace(
            context.queries.get_odoo_model_catalog(workspace_state.workspace_id),
            models=tuple(
                OdooModelSummary(
                    name=model.name,
                    label=model.label,
                    modules=(),
                    state="base",
                )
                for model in (product, owner, calendar)
            ),
            content_hash="sha256:" + "9" * 64,
        )
        context.schema_workspace.schemas.save_odoo_model_catalog(
            workspace_state.workspace_id,
            model_catalog,
            actor=resolve_local_actor(context),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            root_schema,
            actor=resolve_local_actor(context),
        )
        store_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
            "incremental-read-secret",
            persistent=False,
        )

        raw_models = {
            model.name: self._metadata_model(model)
            for model in (product, owner, calendar)
        }
        return workspace_state, root_schema, raw_models

    def test_changing_a_frozen_odoo_source_starts_a_clean_successor(self) -> None:
        context = self.app.state.context
        predecessor_state, _schema = self._frozen_odoo_source_workspace()
        predecessor_workspace = context.migration_workspaces.get(
            predecessor_state.workspace_id,
            actor=resolve_local_actor(context),
        )
        predecessor_data = context.data_versions.get(
            predecessor_workspace.data_version_id,
            actor=resolve_local_actor(context),
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
            actor=resolve_local_actor(context),
        )
        successor_data = context.data_versions.get(
            successor_workspace.data_version_id,
            actor=resolve_local_actor(context),
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
            actor=resolve_local_actor(context),
        )
        invalid_restart = self._post(
            f"/projects/{predecessor_workspace.project_id}/odoo-source-restarts/"
            f"{predecessor_state.workspace_id}?resume=invalid",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(invalid_restart.status_code, 404)
        predecessor_data = context.data_versions.get(
            predecessor_workspace.data_version_id,
            actor=resolve_local_actor(context),
        )
        changed_state = context.workspace_states.update_target(
            predecessor_state.workspace_id,
            actor=resolve_local_actor(context),
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
            actor=resolve_local_actor(context),
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
            actor=resolve_local_actor(context),
        )
        successor_data = context.data_versions.get(
            successor_workspace.data_version_id,
            actor=resolve_local_actor(context),
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
            actor=resolve_local_actor(context),
        )
        self.assertTrue(context.navigation.get_source_readiness(workspace_id).ready)

        current = context.queries.get(workspace_id)
        context.workspace_states.update_schema_scope(
            workspace_id,
            actor=resolve_local_actor(context),
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
            actor=resolve_local_actor(context),
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

    def test_odoo_selection_failure_renders_the_workspace_error(self) -> None:
        workspace_state, _schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        with patch.object(
            context.sources,
            "define_odoo_capture_selection",
            side_effect=WorkspaceError("The capture plan could not be saved"),
        ):
            response = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-selection",
                {
                    "csrf_token": self.csrf,
                    "dataset_name": "odoo_contacts",
                    "model": "res.partner",
                    "field_names": "name",
                    "include_archived": "",
                    "page_size": "100",
                },
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("The capture plan could not be saved", response.text)

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
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            display_name="customers.csv",
            stream=BytesIO(b"code,name\nC1,Kept\n"),
        )
        current = context.queries.get(workspace_state.workspace_id)
        wrong = context.intake.accept(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
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
            actor=resolve_local_actor(context),
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
                created_by=resolve_local_actor(context).identity.display_name,
                datasets=(),
                content_hash="sha256:" + "a" * 64,
            ),
            actor=resolve_local_actor(context),
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
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
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
        self.assertIn("Choose the Odoo data to move", page.text)
        self.assertIn(
            "Impodo prepares safe, bounded capture plans automatically",
            page.text,
        )
        self.assertIn("Linked data needed for this migration", page.text)
        self.assertIn("Needed to keep the records meaningful", page.text)
        self.assertIn("Reuse destination setup", page.text)
        self.assertIn("Not part of the business-data move", page.text)
        self.assertIn("Safe default for an unclassified link", page.text)
        self.assertIn("Product Category", page.text)
        self.assertIn('name="related_actions"', page.text)
        self.assertIn('value="product.category::PRESERVE_LINKED"', page.text)
        self.assertIn('value="res.company::MATCH_EXISTING"', page.text)
        self.assertNotIn('value="res.company::PRESERVE_LINKED"', page.text)
        self.assertIn('value="x.owner::PRESERVE_LINKED"', page.text)
        self.assertIn("Impodo has prepared the safe relationship plan", page.text)
        self.assertIn("You do not need to confirm each record", page.text)
        self.assertIn("Keep linked value", page.text)
        self.assertIn(
            "Advanced: inspect or change relationship handling",
            page.text,
        )
        self.assertIn("Advanced: choose a different outcome", page.text)
        self.assertNotIn("load another selected record type", page.text)
        self.assertIn("Reading the source is safe and read-only", page.text)
        self.assertIn("<summary>Support details</summary>", page.text)
        self.assertIn(
            "Impodo recommends keeping the populated link without turning",
            page.text,
        )
        self.assertIn(
            "Impodo recommends reusing destination setup and blocking when",
            page.text,
        )
        self.assertIn("Transfer related records as migration data", page.text)
        self.assertIn("Use existing destination records only", page.text)
        self.assertIn("This link is required", page.text)
        self.assertIn(
            "Before transfer, Impodo must match them to existing destination settings",
            page.text,
        )
        self.assertIn("Not included", page.text)
        self.assertIn("data-linked-data-form", page.text)
        self.assertIn("data-linked-data-unsaved-notice", page.text)
        self.assertIn("Save linked-data choices", page.text)
        self.assertIn(
            'data-submitting-label="Saving linked-data choices and updating field choices..."',
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

    def test_related_data_remote_read_failure_keeps_current_workspace_evidence(
        self,
    ) -> None:
        workspace_state, selected_schema, model_catalog = (
            self._guided_relationship_capture_workspace()
        )
        context = self.app.state.context
        workspace_state, _decisions = self._save_company_relationship_scope(
            workspace_state,
            selected_schema,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            selected_schema,
            actor=resolve_local_actor(context),
        )
        context.sources.define_odoo_capture_selection(
            workspace_state.workspace_id,
            dataset_name="main_records",
            model="x.root",
            field_names=("name",),
            include_archived=False,
            page_size=100,
            actor=resolve_local_actor(context),
        )
        context.sources.define_odoo_capture_selection(
            workspace_state.workspace_id,
            dataset_name="companies",
            model="res.company",
            field_names=("name",),
            include_archived=False,
            page_size=100,
            linked_only=True,
            actor=resolve_local_actor(context),
        )
        before_state = context.queries.get(workspace_state.workspace_id)
        before_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        before_schema = context.queries.get_odoo_schema_catalog(
            workspace_state.workspace_id
        )
        before_selections = (
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            )
        )
        remote_failure = AsyncMock(
            side_effect=ConnectorTransportError(
                "Odoo JSON-2 fields_get read for x.root timed out after 3 attempts"
            )
        )

        with (
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                remote_failure,
            ),
        ):
            failed = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(before_state.revision),
                    "related_actions": "res.company::CAPTURE_LINKED",
                },
            )

        after_state = context.queries.get(workspace_state.workspace_id)
        self.assertEqual(failed.status_code, 422, failed.text)
        self.assertIn(
            "Odoo JSON-2 fields_get read for x.root timed out after 3 attempts",
            failed.text,
        )
        self.assertEqual(after_state.revision, before_state.revision)
        self.assertEqual(after_state.intended_models, before_state.intended_models)
        self.assertEqual(
            context.queries.get_current_odoo_relationship_scope(
                workspace_state.workspace_id
            ),
            before_scope,
        )
        self.assertEqual(
            context.queries.get_odoo_schema_catalog(
                workspace_state.workspace_id
            ),
            before_schema,
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            ),
            before_selections,
        )
        remote_failure.assert_awaited_once()

    def test_missing_schema_with_retained_roots_offers_same_project_rebuild(
        self,
    ) -> None:
        workspace_state, selected_schema, model_catalog = (
            self._guided_relationship_capture_workspace()
        )
        context = self.app.state.context
        workspace_state, _decisions = self._save_company_relationship_scope(
            workspace_state,
            selected_schema,
        )
        self.assertIsNone(
            context.queries.get_odoo_schema_catalog(workspace_state.workspace_id)
        )
        retained_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertIsNotNone(retained_scope)
        self.assertEqual(retained_scope.root_models, ("x.root",))

        with patch.object(
            context.queries,
            "get_odoo_model_catalog",
            return_value=model_catalog,
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        rebuild_action = (
            f'action="/workspaces/{workspace_state.workspace_id}'
            '/sources/odoo-related-data"'
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn(rebuild_action, page.text)
        self.assertIn(
            f'name="revision" value="{workspace_state.revision}"',
            page.text,
        )
        self.assertIn("Rebuild", page.text)

    def test_relationship_conflict_shows_one_blank_exception_choice(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        shared_model = "x.shared"
        product_schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    name="product.template",
                    label="Product",
                    fields=(
                        SchemaField(
                            name="shared_id",
                            label="Primary shared record",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation=shared_model,
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        SchemaField(
                            name="shared_ids",
                            label="Shared records",
                            type="one2many",
                            required=False,
                            readonly=False,
                            relation=shared_model,
                            relation_field="product_id",
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
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=(
                SimpleNamespace(name="product.template", label="Products"),
                SimpleNamespace(name=shared_model, label="Shared records"),
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
            rejected = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                },
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn("1 relationship exception needs a decision", page.text)
        self.assertIn('id="relationship-exceptions"', page.text)
        self.assertEqual(
            page.text.count(
                'aria-label="Relationship exception for Shared records"'
            ),
            1,
        )
        self.assertIn(
            '<option value="" selected disabled>Choose a deliberate outcome</option>',
            page.text,
        )
        self.assertIn("Why Impodo needs your decision", page.text)
        self.assertIn(
            "<strong>Primary shared record</strong> on Product",
            page.text,
        )
        self.assertIn("&mdash; Keep linked value", page.text)
        self.assertIn("<strong>Shared records</strong> on Product", page.text)
        self.assertIn("&mdash; Do not include", page.text)
        self.assertNotIn(
            'aria-label="Relationship handling for Shared records"',
            page.text,
        )
        self.assertIn(
            '<details class="technical-details">\n'
            '            <summary>Advanced: inspect or change relationship handling</summary>',
            page.text,
        )
        self.assertNotIn(
            '<details class="technical-details" open>\n'
            '            <summary>Advanced: inspect or change relationship handling</summary>',
            page.text,
        )
        self.assertIn("data-linked-data-form", page.text)
        self.assertIn("data-linked-data-unsaved-notice", page.text)
        self.assertIn(
            "Save linked-data choices",
            page.text,
        )
        self.assertEqual(rejected.status_code, 422)

    def test_complete_root_only_review_prepares_capture_plan_automatically(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        fields_by_name = {
            field.name: field for field in schema.models[0].fields
        }
        root_schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    name="x.root",
                    label="Main records",
                    fields=(
                        replace(
                            fields_by_name["name"],
                            required=True,
                            readonly=False,
                            relation=None,
                            stored=True,
                            computed=False,
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        fields_by_name["write_date"],
                    ),
                    unique_constraints=(),
                ),
            ),
            content_hash="sha256:" + "7" * 64,
        )
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root",),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            root_schema,
            actor=resolve_local_actor(context),
        )
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=(SimpleNamespace(name="x.root", label="Main records"),)
        )
        capture_schema = AsyncMock(return_value=root_schema)

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=root_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                capture_schema,
            ),
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )
            prepared = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                },
            )
            result = self.client.get(prepared.headers["location"])

        self.assertIn("Let Impodo prepare the capture plans", page.text)
        self.assertEqual(prepared.status_code, 303)
        self.assertEqual(
            prepared.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}"
            "/sources#capture-next-action",
        )
        selections = context.queries.get_current_odoo_capture_selections(
            workspace_state.workspace_id
        )
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0].model, "x.root")
        self.assertEqual(selections[0].field_names, ("name",))
        self.assertEqual(selections[0].capture_role.value, "ROOT")
        self.assertIn("Capture-plan setup is complete", result.text)
        self.assertIn("1 main record type selected", result.text)
        self.assertIn("Impodo will read all matching main records", result.text)
        self.assertIn("Edit Main records", result.text)
        capture_schema.assert_awaited_once()

    def test_saved_capture_summary_separates_main_records_from_linked_support(
        self,
    ) -> None:
        workspace_state, selected_schema, model_catalog = (
            self._guided_relationship_capture_workspace()
        )
        context = self.app.state.context

        async def persist_selected_schema(
            _context, saved_state, _model_catalog
        ):
            context.schema_workspace.schemas.save_odoo_schema_catalog(
                saved_state.workspace_id,
                selected_schema,
                actor=resolve_local_actor(context),
            )
            return selected_schema

        capture_schema = AsyncMock(side_effect=persist_selected_schema)

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=selected_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                capture_schema,
            ),
        ):
            prepared = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                },
            )
            self.assertEqual(prepared.status_code, 303, prepared.text)
            result = self.client.get(prepared.headers["location"])

        selections = context.queries.get_current_odoo_capture_selections(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {(item.model, item.capture_role.value) for item in selections},
            {
                ("x.root", "ROOT"),
                ("res.company", "LINKED_ONLY"),
            },
        )
        company = next(
            item for item in selections if item.model == "res.company"
        )
        self.assertEqual(company.field_names, ("name",))
        self.assertIn("1 main record type selected", result.text)
        self.assertIn(
            "Impodo also prepared 1 automatic linked-value support record type",
            result.text,
        )
        self.assertIn(
            "Advanced: 1 automatic linked-value support record type",
            result.text,
        )
        self.assertIn("Companies · linked-value support", result.text)
        self.assertIn("Edit Main records", result.text)
        advanced_edit = result.text.index(
            "Advanced: edit automatic linked-value support"
        )
        company_edit = result.text.index("Edit Companies")
        self.assertLess(advanced_edit, company_edit)
        self.assertNotIn("2 Odoo capture plans", result.text)

    def test_exception_editor_shows_only_named_models_and_keeps_explicit_edits(
        self,
    ) -> None:
        workspace_state, selected_schema, model_catalog = (
            self._guided_relationship_capture_workspace(
                include_ambiguous_owner=True
            )
        )
        context = self.app.state.context

        async def persist_selected_schema(
            _context, saved_state, _model_catalog
        ):
            context.schema_workspace.schemas.save_odoo_schema_catalog(
                saved_state.workspace_id,
                selected_schema,
                actor=resolve_local_actor(context),
            )
            return selected_schema

        capture_schema = AsyncMock(side_effect=persist_selected_schema)
        sources_url = f"/workspaces/{workspace_state.workspace_id}/sources"

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=selected_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                capture_schema,
            ),
        ):
            prepared = self._post(
                f"{sources_url}/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::MATCH_EXISTING",
                },
            )
            self.assertEqual(prepared.status_code, 303, prepared.text)
            exception_page = self.client.get(prepared.headers["location"])
            explicit_page = self.client.get(
                f"{sources_url}?model=res.company&edit=1"
                "#capture-plan"
            )

            set_issue = GuidedOdooCapturePlanProposal(
                drafts=(),
                issues=(
                    GuidedOdooCapturePlanIssue(
                        code=GuidedOdooCaptureIssueCode.DATASET_LIMIT,
                        model=None,
                        model_label="Capture plan set",
                        reason="The automatic capture set needs one set-level decision.",
                    ),
                ),
                skipped_models=(),
            )
            with patch(
                "impodo.web.routers.sources.propose_guided_odoo_capture_plans",
                return_value=set_issue,
            ):
                set_issue_page = self.client.get(sources_url)

        self.assertIn("model=x.owner", prepared.headers["location"])
        owner_edit_url = "?model=x.owner&amp;edit=1#capture-plan"
        company_edit_url = "?model=res.company&amp;edit=1#capture-plan"
        root_edit_url = "?model=x.root&amp;edit=1#capture-plan"
        self.assertIn("This record type needs one decision", exception_page.text)
        self.assertIn("Your saved linked-data choices", exception_page.text)
        self.assertIn(
            "<strong>Owners:</strong> Use existing destination records only",
            exception_page.text,
        )
        self.assertIn("Your saved choice", exception_page.text)
        self.assertIn("data-capture-plan-form", exception_page.text)
        self.assertIn(owner_edit_url, exception_page.text)
        self.assertNotIn(company_edit_url, exception_page.text)
        self.assertNotIn(root_edit_url, exception_page.text)
        self.assertIn(
            "Advanced: linked-value support requiring review",
            exception_page.text,
        )

        self.assertIn(owner_edit_url, explicit_page.text)
        self.assertIn(company_edit_url, explicit_page.text)
        self.assertNotIn(root_edit_url, explicit_page.text)
        self.assertIn(
            'name="model" value="res.company"',
            explicit_page.text,
        )

        self.assertIn("1 capture-plan exception remains", set_issue_page.text)
        self.assertIn(
            "The automatic capture set needs one set-level decision",
            set_issue_page.text,
        )
        self.assertNotIn(
            'aria-label="Main Odoo record types"',
            set_issue_page.text,
        )
        self.assertNotIn(
            'aria-label="Linked-value support record types"',
            set_issue_page.text,
        )
        self.assertNotIn(
            f'action="{sources_url}/odoo-selection"',
            set_issue_page.text,
        )

        completed = self._post(
            f"{sources_url}/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "owners",
                "model": "x.owner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
                "linked_only": "1",
            },
        )

        self.assertEqual(completed.status_code, 303, completed.text)
        self.assertEqual(
            completed.headers["location"],
            f"{sources_url}#capture-next-action",
        )
        selections = context.queries.get_current_odoo_capture_selections(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {(item.model, item.capture_role.value) for item in selections},
            {
                ("res.company", "LINKED_ONLY"),
                ("x.owner", "LINKED_ONLY"),
                ("x.root", "ROOT"),
            },
        )

        root_resaved = self._post(
            f"{sources_url}/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "odoo_x_root",
                "model": "x.root",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
                "linked_only": "1",
            },
        )

        self.assertEqual(root_resaved.status_code, 303, root_resaved.text)
        root_selection = next(
            item
            for item in context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            )
            if item.model == "x.root"
        )
        self.assertEqual(root_selection.capture_role.value, "ROOT")

    def test_odoo_source_page_saves_unprofiled_related_data_without_backtracking(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
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
        owner_schema = replace(
            product_schema,
            models=(
                product_schema.models[0],
                replace(
                    schema.models[0],
                    name="x.owner",
                    label="Owners",
                    fields=(
                        SchemaField(
                            name="name",
                            label="Owner Name",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                            exportable=True,
                        ),
                    ),
                ),
            ),
        )
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
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
        async def capture_reachable_schema(
            _context, staged_state, _model_catalog
        ):
            if "x.owner" in staged_state.intended_models:
                return owner_schema
            return product_schema

        capture_schema = AsyncMock(side_effect=capture_reachable_schema)
        saved_schema_reader = context.queries.get_odoo_schema_catalog

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=lambda workspace_id: (
                    saved_schema_reader(workspace_id) or product_schema
                ),
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
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
            after_repeated = context.queries.get(workspace_state.workspace_id)
            overridden_scope = (
                context.queries.get_current_odoo_relationship_scope(
                    workspace_state.workspace_id
                )
            )
            reset_to_recommendation = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(after_repeated.revision),
                    "related_actions": "x.owner::PRESERVE_LINKED",
                },
            )
            review_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertEqual(repeated.status_code, 303)
        self.assertEqual(reset_to_recommendation.status_code, 303)
        # Two bounded schema-closure passes plus one atomic publication stay
        # within a fixed request-level connection budget.
        self.assertLessEqual(related_data_timings.connection_count, 10)
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
            after_repeated.revision,
            saved_state.revision,
        )
        self.assertIs(
            overridden_scope.decisions[0].action,
            OdooRelationshipCaptureAction.CAPTURE_LINKED,
        )
        relationship_scope = (
            context.queries.get_current_odoo_relationship_scope(
                workspace_state.workspace_id
            )
        )
        self.assertIsNotNone(relationship_scope)
        self.assertEqual(relationship_scope.version, 2)
        self.assertEqual(len(relationship_scope.decisions), 1)
        self.assertEqual(
            relationship_scope.decisions[0].identity,
            ("product.template", "x_owner_id"),
        )
        self.assertIs(
            relationship_scope.decisions[0].action,
            OdooRelationshipCaptureAction.PRESERVE_LINKED,
        )
        self.assertIn("relationship-review-unavailable", review_page.text)
        self.assertIn(
            "will not treat an unavailable relationship as excluded",
            review_page.text,
        )
        self.assertNotIn("Continue with Impodo's plan", review_page.text)
        self.assertEqual(capture_schema.await_count, 6)

    def test_related_data_review_preserves_an_explicit_root_also_reached_by_an_edge(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        product = replace(
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
            ),
        )
        owner = replace(
            product,
            name="x.owner",
            label="Owners",
            fields=(product.fields[0],),
        )
        selected_schema = replace(schema, models=(product, owner))
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("product.template", "x.owner"),
        )
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=(
                SimpleNamespace(name="product.template", label="Products"),
                SimpleNamespace(name="x.owner", label="Owners"),
            )
        )
        capture_schema = AsyncMock(return_value=selected_schema)

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=selected_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                capture_schema,
            ),
        ):
            first = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::MATCH_EXISTING",
                },
            )
            after_first = context.queries.get(workspace_state.workspace_id)
            second = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(after_first.revision),
                    "related_actions": "x.owner::MATCH_EXISTING",
                },
            )

        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 303)
        self.assertEqual(
            context.queries.get(workspace_state.workspace_id).intended_models,
            ("product.template", "x.owner"),
        )
        relationship_scope = (
            context.queries.get_current_odoo_relationship_scope(
                workspace_state.workspace_id
            )
        )
        self.assertEqual(
            relationship_scope.root_models,
            ("product.template", "x.owner"),
        )

    def test_related_data_defaults_close_new_graph_levels_in_one_action(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
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
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=tuple(
                SimpleNamespace(name=model.name, label=model.label)
                for model in complete_schema.models
            )
        )

        async def capture_next(_context, saved_state, _model_catalog):
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
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
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
            first_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        self.assertEqual(first.status_code, 303)
        self.assertIn("relationship-review-complete", first_page.text)
        self.assertEqual(
            frozenset(
                context.queries.get(
                    workspace_state.workspace_id
                ).intended_models
            ),
            frozenset(
                {"product.template", "x.owner", "resource.calendar"}
            ),
        )
        scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {item.identity for item in scope.decisions},
            {
                ("product.template", "x_owner_id"),
                ("x.owner", "calendar_id"),
            },
        )
        self.assertEqual(
            {item.identity: item.action for item in scope.decisions},
            {
                ("product.template", "x_owner_id"): (
                    OdooRelationshipCaptureAction.CAPTURE_LINKED
                ),
                ("x.owner", "calendar_id"): (
                    OdooRelationshipCaptureAction.PRESERVE_LINKED
                ),
            },
        )
        self.assertEqual(capture_schema.await_count, 2)

    def test_unreachable_relationships_do_not_create_false_model_conflicts(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root",),
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
            stored=True,
            computed=False,
            related=False,
            company_dependent=False,
            exportable=True,
        )

        def relationship(
            name: str,
            label: str,
            field_type: str,
            relation: str,
            *,
            required: bool = False,
        ) -> SchemaField:
            return SchemaField(
                name=name,
                label=label,
                type=field_type,
                required=required,
                readonly=False,
                relation=relation,
                relation_field=None,
                selection=(),
                stored=True,
                computed=False,
                related=False,
                company_dependent=False,
                exportable=True,
            )

        root = replace(
            base,
            name="x.root",
            label="Main records",
            fields=(
                name,
                relationship("owner_id", "Owner", "many2one", "x.owner"),
                relationship(
                    "employee_ids",
                    "Employees",
                    "many2many",
                    "hr.employee",
                ),
            ),
            unique_constraints=(),
        )
        owner = replace(
            base,
            name="x.owner",
            label="Owners",
            fields=(
                name,
                relationship(
                    "bridge_id",
                    "Bridge",
                    "many2one",
                    "x.bridge",
                    required=True,
                ),
                relationship(
                    "employee_id",
                    "Employee",
                    "many2one",
                    "hr.employee",
                ),
            ),
            unique_constraints=(),
        )
        bridge = replace(
            base,
            name="x.bridge",
            label="Bridges",
            fields=(
                name,
                relationship(
                    "owner_ids",
                    "Owners",
                    "one2many",
                    "x.owner",
                ),
            ),
            unique_constraints=(),
        )
        employee = replace(
            base,
            name="hr.employee",
            label="Employees",
            fields=(name,),
            unique_constraints=(),
        )
        models_by_name = {
            model.name: model for model in (root, owner, bridge, employee)
        }
        current = {"schema": replace(schema, models=(root,))}
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=tuple(
                SimpleNamespace(name=model.name, label=model.label)
                for model in models_by_name.values()
            ),
        )

        async def capture_candidate(_context, saved_state, _model_catalog):
            current["schema"] = replace(
                schema,
                models=tuple(
                    models_by_name[model]
                    for model in saved_state.intended_models
                ),
            )
            return current["schema"]

        capture_schema = AsyncMock(side_effect=capture_candidate)
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
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                capture_schema,
            ),
        ):
            response = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                },
            )

        self.assertEqual(response.status_code, 303, response.text)
        saved_state = context.queries.get(workspace_state.workspace_id)
        self.assertEqual(
            frozenset(saved_state.intended_models),
            frozenset({"x.root", "x.owner", "x.bridge"}),
        )
        scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {item.identity: item.action for item in scope.decisions},
            {
                ("x.owner", "bridge_id"): (
                    OdooRelationshipCaptureAction.PRESERVE_LINKED
                ),
                ("x.root", "employee_ids"): (
                    OdooRelationshipCaptureAction.DO_NOT_CAPTURE
                ),
                ("x.root", "owner_id"): (
                    OdooRelationshipCaptureAction.PRESERVE_LINKED
                ),
            },
        )
        self.assertEqual(capture_schema.await_count, 3)

    def test_related_data_closure_reads_each_record_type_once_and_binds_full_access(
        self,
    ) -> None:
        workspace_state, _root_schema, raw_models = (
            self._incremental_relationship_capture_workspace()
        )
        context = self.app.state.context
        fingerprint = _browser_schema(workspace_state).fingerprint
        full_scope = (
            "product.template",
            "resource.calendar",
            "x.owner",
        )
        full_permission_hash = "sha256:" + "f" * 64
        schema_read_scopes: list[tuple[str, ...]] = []
        identity_scopes: list[tuple[str, ...]] = []
        opened_sessions: list[tuple[str, str]] = []

        class RecordingSchemaSession:
            def read_models(self, models):
                scope = tuple(sorted(models))
                schema_read_scopes.append(scope)
                return MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={model: raw_models[model] for model in scope},
                )

            def verify_identity(self, models):
                scope = tuple(sorted(models))
                identity_scopes.append(scope)
                return OdooReadIdentity(
                    target_hash=fingerprint.target_hash,
                    principal_hash="sha256:" + "1" * 64,
                    permission_hash=full_permission_hash,
                    context_hash="sha256:" + "4" * 64,
                    readable_models=scope,
                    observed_at="2026-10-12T08:00:00Z",
                )

        session = RecordingSchemaSession()

        def open_session(saved_state, api_key):
            opened_sessions.append((saved_state.workspace_id, api_key))
            return session

        with patch.object(
            context,
            "schema_read_session_factory",
            new=open_session,
        ):
            saved = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::CAPTURE_LINKED",
                },
            )

        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertEqual(
            schema_read_scopes,
            [
                ("product.template",),
                ("x.owner",),
                ("resource.calendar",),
            ],
        )
        self.assertEqual(identity_scopes, [full_scope])
        self.assertEqual(
            opened_sessions,
            [(workspace_state.workspace_id, "incremental-read-secret")],
        )
        persisted_schema = context.queries.get_odoo_schema_catalog(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {model.name for model in persisted_schema.models},
            set(full_scope),
        )
        self.assertEqual(
            persisted_schema.read_permission_hash,
            full_permission_hash,
        )

    def test_related_data_rejects_an_overlapping_save_and_releases_after_success(
        self,
    ) -> None:
        workspace_state, _root_schema, raw_models = (
            self._incremental_relationship_capture_workspace()
        )
        context = self.app.state.context
        fingerprint = _browser_schema(workspace_state).fingerprint
        first_read_started = Event()
        release_first_read = Event()

        class BlockingSchemaSession:
            def read_models(self, models):
                scope = tuple(sorted(models))
                if not first_read_started.is_set():
                    first_read_started.set()
                    if not release_first_read.wait(10):
                        raise AssertionError(
                            "Timed out waiting to release the first linked-data save"
                        )
                return MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={model: raw_models[model] for model in scope},
                )

            def verify_identity(self, models):
                scope = tuple(sorted(models))
                return OdooReadIdentity(
                    target_hash=fingerprint.target_hash,
                    principal_hash="sha256:" + "1" * 64,
                    permission_hash="sha256:" + "f" * 64,
                    context_hash="sha256:" + "4" * 64,
                    readable_models=scope,
                    observed_at="2026-10-12T08:00:00Z",
                )

        first_client = type(self.client)(
            self.app,
            cookies=dict(self.client.cookies),
        )
        first_responses = []
        first_errors = []
        path = (
            f"/workspaces/{workspace_state.workspace_id}"
            "/sources/odoo-related-data"
        )
        form = {
            "csrf_token": self.csrf,
            "revision": str(workspace_state.revision),
            "related_actions": "x.owner::CAPTURE_LINKED",
        }

        def submit_first_save() -> None:
            try:
                first_responses.append(
                    first_client.post(
                        path,
                        data=form,
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                )
            except BaseException as error:  # pragma: no cover - assertion aid
                first_errors.append(error)

        with patch.object(
            context,
            "schema_read_session_factory",
            new=lambda _saved_state, _api_key: BlockingSchemaSession(),
        ):
            first_thread = Thread(target=submit_first_save, daemon=True)
            first_thread.start()
            try:
                self.assertTrue(
                    first_read_started.wait(10),
                    "The first linked-data save did not reach its Odoo read",
                )
                overlapping = self._post(path, form)
            finally:
                release_first_read.set()
                first_thread.join(15)
                first_client.close()

            self.assertFalse(first_thread.is_alive())
            self.assertEqual(first_errors, [])
            self.assertEqual(len(first_responses), 1)
            self.assertEqual(first_responses[0].status_code, 303)
            self.assertEqual(overlapping.status_code, 409)
            self.assertEqual(
                overlapping.text,
                "Impodo is already checking linked data for this project. "
                "Wait for it to finish, then refresh.",
            )

            current_state = context.queries.get(workspace_state.workspace_id)
            after_success = self._post(
                path,
                {
                    **form,
                    "revision": str(current_state.revision),
                },
            )

        self.assertEqual(after_success.status_code, 303, after_success.text)

    def test_related_data_releases_the_in_flight_gate_after_failure(self) -> None:
        workspace_state, _root_schema, _raw_models = (
            self._incremental_relationship_capture_workspace()
        )
        context = self.app.state.context
        first_read_started = Event()
        release_first_read = Event()

        class FailingBlockingSchemaSession:
            def read_models(self, _models):
                if not first_read_started.is_set():
                    first_read_started.set()
                    if not release_first_read.wait(10):
                        raise AssertionError(
                            "Timed out waiting to release the first linked-data save"
                        )
                raise ConnectorTransportError(
                    "Odoo stopped responding during the linked-data check"
                )

            def verify_identity(self, _models):  # pragma: no cover - defensive
                raise AssertionError("A failed field read cannot reach identity check")

        first_client = type(self.client)(
            self.app,
            cookies=dict(self.client.cookies),
        )
        first_responses = []
        first_errors = []
        path = (
            f"/workspaces/{workspace_state.workspace_id}"
            "/sources/odoo-related-data"
        )
        form = {
            "csrf_token": self.csrf,
            "revision": str(workspace_state.revision),
            "related_actions": "x.owner::CAPTURE_LINKED",
        }

        def submit_first_save() -> None:
            try:
                first_responses.append(
                    first_client.post(
                        path,
                        data=form,
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                )
            except BaseException as error:  # pragma: no cover - assertion aid
                first_errors.append(error)

        with patch.object(
            context,
            "schema_read_session_factory",
            new=lambda _saved_state, _api_key: FailingBlockingSchemaSession(),
        ):
            first_thread = Thread(target=submit_first_save, daemon=True)
            first_thread.start()
            try:
                self.assertTrue(
                    first_read_started.wait(10),
                    "The first linked-data save did not reach its Odoo read",
                )
                overlapping = self._post(path, form)
            finally:
                release_first_read.set()
                first_thread.join(15)
                first_client.close()

            self.assertFalse(first_thread.is_alive())
            self.assertEqual(first_errors, [])
            self.assertEqual(len(first_responses), 1)
            self.assertEqual(first_responses[0].status_code, 422)
            self.assertEqual(overlapping.status_code, 409)

            after_failure = self._post(path, form)

        self.assertEqual(after_failure.status_code, 422)
        self.assertIn(
            "Odoo stopped responding during the linked-data check",
            after_failure.text,
        )

    def test_failed_final_related_data_access_check_publishes_nothing(
        self,
    ) -> None:
        workspace_state, root_schema, raw_models = (
            self._incremental_relationship_capture_workspace()
        )
        context = self.app.state.context
        fingerprint = _browser_schema(workspace_state).fingerprint
        full_scope = (
            "product.template",
            "resource.calendar",
            "x.owner",
        )
        schema_read_scopes: list[tuple[str, ...]] = []
        identity_scopes: list[tuple[str, ...]] = []
        opened_sessions: list[tuple[str, str]] = []
        before_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        before_selections = (
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            )
        )

        class FailingFinalSchemaSession:
            def read_models(self, models):
                scope = tuple(sorted(models))
                schema_read_scopes.append(scope)
                return MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={model: raw_models[model] for model in scope},
                )

            def verify_identity(self, models):
                scope = tuple(sorted(models))
                identity_scopes.append(scope)
                raise ConnectorAuthorizationError(
                    "Odoo access changed before linked data could be saved"
                )

        session = FailingFinalSchemaSession()

        def open_session(saved_state, api_key):
            opened_sessions.append((saved_state.workspace_id, api_key))
            return session

        with patch.object(
            context,
            "schema_read_session_factory",
            new=open_session,
        ):
            failed = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::CAPTURE_LINKED",
                },
            )

        self.assertEqual(failed.status_code, 422, failed.text)
        self.assertIn(
            "Odoo access changed before linked data could be saved",
            failed.text,
        )
        self.assertEqual(
            schema_read_scopes,
            [
                ("product.template",),
                ("x.owner",),
                ("resource.calendar",),
            ],
        )
        self.assertEqual(identity_scopes, [full_scope])
        self.assertEqual(
            opened_sessions,
            [(workspace_state.workspace_id, "incremental-read-secret")],
        )
        after_state = context.queries.get(workspace_state.workspace_id)
        self.assertEqual(after_state.revision, workspace_state.revision)
        self.assertEqual(
            after_state.intended_models,
            workspace_state.intended_models,
        )
        self.assertEqual(
            context.queries.get_current_odoo_relationship_scope(
                workspace_state.workspace_id
            ),
            before_scope,
        )
        self.assertEqual(
            context.queries.get_odoo_schema_catalog(
                workspace_state.workspace_id
            ),
            root_schema,
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            ),
            before_selections,
        )

    def test_later_relationship_conflict_is_saved_as_resumable_checkpoint(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root",),
        )
        base = schema.models[0]
        write_date = next(
            field for field in base.fields if field.name == "write_date"
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
            company_dependent=False,
            exportable=True,
        )
        root = replace(
            base,
            name="x.root",
            label="Main records",
            fields=(
                name,
                write_date,
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
                SchemaField(
                    name="workcenter_id",
                    label="Work Center",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="mrp.workcenter",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        workcenter = replace(
            base,
            name="mrp.workcenter",
            label="Work Centers",
            fields=(
                name,
                SchemaField(
                    name="resource_calendar_id",
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
        root_schema = replace(
            schema,
            models=(root,),
            content_hash="sha256:" + "8" * 64,
        )
        complete_schema = replace(
            schema,
            models=(root, workcenter, calendar),
            content_hash="sha256:" + "9" * 64,
        )
        stored_model_catalog = context.queries.get_odoo_model_catalog(
            workspace_state.workspace_id
        )
        model_catalog = replace(
            stored_model_catalog,
            models=tuple(
                OdooModelSummary(
                    name=model.name,
                    label=model.label,
                    modules=(),
                    state="base",
                )
                for model in complete_schema.models
            ),
            content_hash="sha256:" + "b" * 64,
        )
        context.schema_workspace.schemas.save_odoo_model_catalog(
            workspace_state.workspace_id,
            model_catalog,
            actor=resolve_local_actor(context),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            root_schema,
            actor=resolve_local_actor(context),
        )
        store_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
            "checkpoint-read-secret",
            persistent=False,
        )
        fingerprint = _browser_schema(workspace_state).fingerprint
        raw_models = {
            model.name: self._metadata_model(model)
            for model in complete_schema.models
        }
        existing_capture = context.sources.define_odoo_capture_selection(
            workspace_state.workspace_id,
            dataset_name="main_records",
            model="x.root",
            field_names=("name",),
            include_archived=False,
            page_size=100,
            actor=resolve_local_actor(context),
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            ),
            (existing_capture,),
        )

        sessions = []

        class RecordingSchemaSession:
            def __init__(self):
                self.schema_reads = []
                self.identity_checks = []

            def read_models(self, models):
                selected = tuple(sorted(models))
                self.schema_reads.append(selected)
                return MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={model: raw_models[model] for model in selected},
                )

            def verify_identity(self, models):
                selected = tuple(sorted(models))
                self.identity_checks.append(selected)
                return OdooReadIdentity(
                    target_hash=fingerprint.target_hash,
                    principal_hash="sha256:" + "1" * 64,
                    permission_hash="sha256:" + "3" * 64,
                    context_hash="sha256:" + "4" * 64,
                    readable_models=selected,
                    observed_at="2026-10-12T08:00:00Z",
                )

        def open_session(saved_state, api_key):
            self.assertEqual(saved_state.workspace_id, workspace_state.workspace_id)
            self.assertEqual(api_key, "checkpoint-read-secret")
            session = RecordingSchemaSession()
            sessions.append(session)
            return session

        with patch.object(
            context,
            "schema_read_session_factory",
            new=open_session,
        ):
            checkpoint = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": [
                        "resource.calendar::PRESERVE_LINKED",
                        "mrp.workcenter::CAPTURE_LINKED",
                    ],
                },
            )
            checkpoint_state = context.queries.get(workspace_state.workspace_id)
            checkpoint_scope = (
                context.queries.get_current_odoo_relationship_scope(
                    workspace_state.workspace_id
                )
            )
            checkpoint_schema = context.queries.get_odoo_schema_catalog(
                workspace_state.workspace_id
            )
            checkpoint_capture_selections = (
                context.queries.get_current_odoo_capture_selections(
                    workspace_state.workspace_id
                )
            )
            review_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )
            resumed = self._post(
                f"/workspaces/{workspace_state.workspace_id}"
                "/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(checkpoint_state.revision),
                    "related_actions": [
                        "resource.calendar::PRESERVE_LINKED",
                        "mrp.workcenter::CAPTURE_LINKED",
                    ],
                },
            )

        self.assertEqual(len(sessions), 2)
        self.assertEqual(
            [session.schema_reads for session in sessions],
            [
                [
                    ("x.root",),
                    ("mrp.workcenter", "resource.calendar"),
                ],
                [
                    ("x.root",),
                    ("mrp.workcenter", "resource.calendar"),
                ],
            ],
        )
        self.assertEqual(
            [session.identity_checks for session in sessions],
            [
                [("mrp.workcenter", "x.root")],
                [
                    (
                        "mrp.workcenter",
                        "resource.calendar",
                        "x.root",
                    )
                ],
            ],
        )
        self.assertEqual(checkpoint.status_code, 303, checkpoint.text)
        self.assertGreater(checkpoint_state.revision, workspace_state.revision)
        self.assertEqual(
            checkpoint.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/sources#related-data-scope",
        )
        self.assertEqual(review_page.status_code, 200)
        self.assertNotIn("We could not complete that action", review_page.text)
        self.assertIn("1 relationship exception needs a decision", review_page.text)
        self.assertIn(
            'aria-label="Relationship exception for Working Hours"',
            review_page.text,
        )
        self.assertIn(
            "<strong>Working Hours</strong> on Main records",
            review_page.text,
        )
        self.assertIn("&mdash; Keep linked value", review_page.text)
        self.assertIn(
            "<strong>Working Hours</strong> on Work Centers",
            review_page.text,
        )
        self.assertIn(
            "&mdash; Transfer related records as migration data",
            review_page.text,
        )
        self.assertEqual(
            {
                item.identity
                for item in checkpoint_scope.decisions
                if item.relation_model == "resource.calendar"
            },
            set(),
        )
        self.assertEqual(
            frozenset(checkpoint_state.intended_models),
            frozenset({"x.root", "mrp.workcenter"}),
        )
        self.assertEqual(
            {model.name for model in checkpoint_schema.models},
            {"x.root", "mrp.workcenter"},
        )
        self.assertEqual(checkpoint_capture_selections, ())
        self.assertEqual(resumed.status_code, 303, resumed.text)
        resumed_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertEqual(
            resumed_scope.explicit_actions,
            {
                "mrp.workcenter": (
                    OdooRelationshipCaptureAction.CAPTURE_LINKED
                ),
                "resource.calendar": (
                    OdooRelationshipCaptureAction.PRESERVE_LINKED
                ),
            },
        )
        self.assertEqual(
            {
                item.action
                for item in resumed_scope.decisions
                if item.relation_model == "resource.calendar"
            },
            {OdooRelationshipCaptureAction.PRESERVE_LINKED},
        )

    def test_recommended_supporting_model_defaults_to_linked_only(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
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
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
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
        decisions = relationship_scope_decisions(
            propose_related_odoo_data(
                product_schema.models,
                include_selected=True,
            ),
            included_models={"product.category"},
        )
        relationship_scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=decisions,
            root_models=("product.template",),
            recorded_at=datetime.now(timezone.utc),
            recorded_by=resolve_local_actor(context).identity.display_name,
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
            patch.object(
                context.queries,
                "get_current_odoo_relationship_scope",
                return_value=relationship_scope,
            ),
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
                "?model=product.category&edit=1"
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn('type="hidden" name="linked_only" value="1"', page.text)
        self.assertNotRegex(
            page.text,
            r'type="checkbox" name="linked_only"',
        )
        self.assertIn("Linked-value support", page.text)
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
            actor=resolve_local_actor(context),
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
            'data-submitting-label="Counting selected source records in Odoo..."',
            page.text,
        )

    def test_linked_relationship_review_is_inside_current_evidence(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
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
            actor=resolve_local_actor(context),
        )
        current_state = context.queries.get(workspace_state.workspace_id)
        context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=current_state.revision,
            permitted_models=current_state.intended_models,
            relationship_decisions=relationship_scope_decisions(
                propose_related_odoo_data(
                    schema.models,
                    include_selected=True,
                ),
                included_models={"product.category"},
            ),
            relationship_root_models=("product.template",),
            relationship_explicit_actions=(
                (
                    "product.category",
                    OdooRelationshipCaptureAction.CAPTURE_LINKED,
                ),
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
                actor=resolve_local_actor(context),
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
