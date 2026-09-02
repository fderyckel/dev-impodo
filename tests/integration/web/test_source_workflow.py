"""Focused browser evidence for one Impodo capability."""

from __future__ import annotations

from unittest.mock import patch

from tests.support.browser_scenarios import (
    BytesIO,
    MANIFEST_NAME,
    OdooComparisonOutcome,
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    RecordSnapshot,
    SourceMode,
    SourceSelection,
    TargetRecord,
    WorkspaceStatus,
    _BrowserOdooCaptureGateway,
    _browser_schema,
    _created_workspace_id,
    _wait_for_odoo_capture,
    _workspace_data_version_id,
    datetime,
    re,
    replace,
    timedelta,
    timezone,
    uuid4,
)


class SourceWorkflowBrowserTests(ProjectSetupBrowserTestCase):
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

        source_page = self.client.get(f"/workspaces/{workspace_state.workspace_id}/sources")
        self.assertEqual(source_page.status_code, 200)
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

    def test_odoo_source_setup_skips_file_export_and_opens_schema_first(
        self,
    ) -> None:
        new_page = self.client.get("/projects/new")
        self.assertIn(">Files<", new_page.text)
        self.assertIn(">Data already in Odoo<", new_page.text)

        created = self._post(
            "/projects/new",
            {
                "csrf_token": self.csrf,
                "display_name": "Odoo product cleanup",
                "source_system_identity": "Odoo 19",
                "source_mode": "ODOO",
            },
        )
        self.assertEqual(created.status_code, 303)
        workspace_id = _created_workspace_id(self.app, created)
        data_project_id = self.app.state.context.migration_workspaces.get(
            workspace_id,
            actor=self.app.state.context.actor,
        ).project_id
        self.assertEqual(
            created.headers["location"],
            f"/projects/{data_project_id}",
        )
        project_page = self.client.get(created.headers["location"])
        self.assertIn(
            f'href="/workspaces/{workspace_id}/overview"',
            project_page.text,
        )
        target_page = self.client.get(f"/workspaces/{workspace_id}/target")
        self.assertIn("Connect the Odoo source", target_page.text)
        self.assertIn(
            "Moving records between two Odoo databases is not available yet.",
            target_page.text,
        )
        self.assertIn("Source access only", target_page.text)
        self.assertNotIn('name="keep_api_key_for_loading"', target_page.text)
        self.assertIn("It does not discover models or fields", target_page.text)
        files = self.client.get(
            f"/workspaces/{workspace_id}/files",
            follow_redirects=False,
        )
        self.assertEqual(files.status_code, 303)
        self.assertEqual(files.headers["location"], f"/workspaces/{workspace_id}/target")

        unchecked = self._post(
            f"/workspaces/{workspace_id}/target",
            {
                "csrf_token": self.csrf,
                "revision": "1",
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.test",
                "odoo_database": "odoo_review",
                "read_api_key": "read-secret",
                "action": "save",
            },
        )
        self.assertEqual(unchecked.status_code, 422)
        self.assertIn("Check the Odoo connection before continuing", unchecked.text)

        checked = self._post(
            f"/workspaces/{workspace_id}/target",
            {
                "csrf_token": self.csrf,
                "revision": "2",
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.test",
                "odoo_database": "odoo_review",
                "action": "test",
            },
        )
        self.assertEqual(checked.status_code, 303)
        self.assertEqual(
            self.read_identity_calls,
            [(workspace_id, "read-secret", ("res.users",))],
        )

        target = self._post(
            f"/workspaces/{workspace_id}/target",
            {
                "csrf_token": self.csrf,
                "revision": "3",
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.test",
                "odoo_database": "odoo_review",
                "action": "save",
            },
        )
        self.assertEqual(
            target.headers["location"],
            f"/workspaces/{workspace_id}/schema",
        )
        workspace_state = self.app.state.context.workspace_states.repository.get(workspace_id)
        self.assertEqual(workspace_state.status, WorkspaceStatus.REGISTERED)
        self.assertEqual(workspace_state.source_mode, SourceMode.ODOO)
        self.assertEqual(workspace_state.source_system, "Odoo 19")
        self.assertEqual(workspace_state.source_files, ())

        schema_page = self.client.get(f"/workspaces/{workspace_id}/schema")
        self.assertIn("Stage 2 of 8", schema_page.text)
        self.assertIn("Select data to download", schema_page.text)
        self.assertIn("Choose the Odoo source record type", schema_page.text)

        refreshed = self._post(
            f"/workspaces/{workspace_id}/schema/models/refresh",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(refreshed.status_code, 303)
        current = self.app.state.context.workspace_states.repository.get(workspace_id)
        scoped = self._post(
            f"/workspaces/{workspace_id}/schema",
            {
                "csrf_token": self.csrf,
                "revision": str(current.revision),
                "permitted_models": "res.partner",
            },
        )
        self.assertEqual(scoped.status_code, 303)
        self.assertEqual(
            scoped.headers["location"],
            f"/workspaces/{workspace_id}/sources#capture-plan",
        )
        self.assertEqual(self.schema_calls, [(workspace_id, "read-secret")])

        source_page = self.client.get(f"/workspaces/{workspace_id}/sources")
        self.assertEqual(source_page.status_code, 200)
        self.assertIn("Stage 2 of 8", source_page.text)
        self.assertIn("Define a bounded Odoo capture", source_page.text)
        self.assertIn("Freezing is read-only", source_page.text)
        render_schema = self.app.state.context.queries.get_odoo_schema_catalog(
            workspace_id
        )
        self.assertIsNotNone(render_schema)
        assert render_schema is not None
        base_model = render_schema.models[0]
        product_model = replace(
            base_model,
            name="product.template",
            label="Product",
            fields=tuple(
                replace(field, label="Product Name")
                if field.name == "name"
                else field
                for field in base_model.fields
            ),
        )
        uom_model = replace(
            base_model,
            name="uom.uom",
            label="Unit of Measure",
            fields=tuple(
                replace(field, label="Unit of Measure Name")
                if field.name == "name"
                else field
                for field in base_model.fields
            ),
        )
        with patch.object(
            self.app.state.context.queries,
            "get_odoo_schema_catalog",
            return_value=replace(
                render_schema,
                models=(product_model, uom_model),
            ),
        ):
            product_page = self.client.get(
                f"/workspaces/{workspace_id}/sources?model=product.template"
            )
            uom_page = self.client.get(
                f"/workspaces/{workspace_id}/sources?model=uom.uom"
            )
        self.assertIn("Product Name", product_page.text)
        self.assertNotIn("Unit of Measure Name", product_page.text)
        self.assertIn("Unit of Measure Name", uom_page.text)
        self.assertNotIn("Product Name", uom_page.text)
        calls_before_selection = len(self.schema_calls)
        selected = self._post(
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
        self.assertEqual(selected.status_code, 303)
        self.assertEqual(
            selected.headers["location"],
            f"/workspaces/{workspace_id}/sources#capture-next-action",
        )
        self.assertEqual(len(self.schema_calls), calls_before_selection)
        selection = (
            self.app.state.context.sources.sources
            .get_current_odoo_capture_selection(workspace_id)
        )
        self.assertIsNotNone(selection)
        self.assertEqual(selection.field_names, ("name",))
        saved_page = self.client.get(selected.headers["location"])
        self.assertIn("Capture plan version 1", saved_page.text)
        self.assertIn("Check matching records", saved_page.text)
        self.assertIn("Capture plans complete", saved_page.text)
        self.assertIn("Stage 3 of 8", saved_page.text)
        self.assertIn("Check matching records and continue", saved_page.text)
        self.assertIn("Review and freeze the Odoo source", saved_page.text)
        self.assertIn("Edit saved capture plans", saved_page.text)
        self.assertNotIn("Eligible fields from", saved_page.text)
        self.assertNotIn("Freeze these Odoo records", saved_page.text)
        self.assertIn("Ready to download", saved_page.text)
        completed_schema_page = self.client.get(
            f"/workspaces/{workspace_id}/schema"
        )
        self.assertIn(
            "All Odoo capture plans are ready",
            completed_schema_page.text,
        )
        self.assertIn(
            f'href="/workspaces/{workspace_id}/sources#selection-saved"',
            completed_schema_page.text,
        )

        replaced_key = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-read-credential",
            {
                "csrf_token": self.csrf,
                "read_api_key": "replacement-read-secret",
            },
        )
        self.assertEqual(replaced_key.status_code, 303)
        credential_mismatch_page = self.client.get(
            f"/workspaces/{workspace_id}/sources#selection-saved"
        )
        self.assertIn(
            "Refresh Odoo details and continue",
            credential_mismatch_page.text,
        )
        self.assertIn(
            f'action="/workspaces/{workspace_id}/schema/capture"',
            credential_mismatch_page.text,
        )
        refreshed_binding = self._post(
            f"/workspaces/{workspace_id}/schema/capture",
            {
                "csrf_token": self.csrf,
                "return_to_sources": "1",
            },
        )
        self.assertEqual(refreshed_binding.status_code, 303)
        self.assertEqual(
            refreshed_binding.headers["location"],
            f"/workspaces/{workspace_id}/sources#selection-saved",
        )
        refreshed_source_page = self.client.get(
            refreshed_binding.headers["location"]
        )
        self.assertIn(
            "Check matching records and continue",
            refreshed_source_page.text,
        )

        context = self.app.state.context
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertIsNotNone(schema)
        assert schema is not None
        partner = schema.models[0]
        stale_schema = replace(
            schema,
            models=(
                replace(
                    partner,
                    fields=tuple(
                        replace(field, stored=False)
                        if field.name == "name"
                        else field
                        for field in partner.fields
                    ),
                ),
            ),
        )
        with patch.object(
            context.queries,
            "get_odoo_schema_catalog",
            return_value=stale_schema,
        ):
            repair_page = self.client.get(f"/workspaces/{workspace_id}/sources")
            self.assertIn("This capture plan needs review", repair_page.text)
            self.assertIn("Review and save capture plan", repair_page.text)
            self.assertNotIn("Freeze these Odoo records", repair_page.text)
            blocked_plan = self._post(
                f"/workspaces/{workspace_id}/sources/odoo-capture",
                {
                    "csrf_token": self.csrf,
                    "selection_id": selection.selection_id,
                    "selection_hash": selection.content_hash,
                    "confirm_capture": "1",
                },
            )
            self.assertEqual(blocked_plan.status_code, 422)
            self.assertIn("not eligible", blocked_plan.text)
            self.assertIsNone(context.odoo_capture_jobs.active(workspace_id))

        original_schema_reader = context.schema_reader
        changed_snapshot = _browser_schema(workspace_state)
        changed_partner = changed_snapshot.models["res.partner"]
        changed_snapshot = replace(
            changed_snapshot,
            models={
                "res.partner": replace(
                    changed_partner,
                    fields={
                        **changed_partner.fields,
                        "name": replace(
                            changed_partner.fields["name"],
                            required=False,
                        ),
                    },
                )
            },
        )
        context.schema_reader = lambda _workspace_state, _api_key: changed_snapshot
        change_check = self._post(
            f"/workspaces/{workspace_id}/schema/capture",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(change_check.status_code, 303)
        attention_page = self.client.get(f"/workspaces/{workspace_id}/sources")
        self.assertIn("Odoo data needs attention", attention_page.text)
        self.assertIn("Review Odoo changes", attention_page.text)
        blocked_by_change = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-capture",
            {
                "csrf_token": self.csrf,
                "selection_id": selection.selection_id,
                "selection_hash": selection.content_hash,
                "confirm_capture": "1",
            },
        )
        self.assertEqual(blocked_by_change.status_code, 422)
        self.assertIn("Review the checked Odoo changes", blocked_by_change.text)
        context.schema_reader = original_schema_reader
        cleared_check = self._post(
            f"/workspaces/{workspace_id}/schema/capture",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(cleared_check.status_code, 303)

        current_schema = context.queries.get_odoo_schema_catalog(workspace_id)
        legacy_gateway = _BrowserOdooCaptureGateway(
            workspace_state,
            current_schema,
        )
        legacy_gateway.identity_context_hash = "sha256:" + "8" * 64
        context.source_capture_factory = (
            lambda selected_workspace_state, _secret: legacy_gateway
        )
        refresh_required = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-assessment",
            {
                "csrf_token": self.csrf,
                "selection_id": selection.selection_id,
                "selection_hash": selection.content_hash,
            },
        )
        self.assertEqual(refresh_required.status_code, 422)
        self.assertIn("earlier verification format", refresh_required.text)
        self.assertIn("Refresh Odoo details", refresh_required.text)
        self.assertNotIn("Check matching records", refresh_required.text)

        stale = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-capture",
            {
                "csrf_token": self.csrf,
                "selection_id": selection.selection_id,
                "selection_hash": "sha256:" + "0" * 64,
                "confirm_capture": "1",
            },
        )
        self.assertEqual(stale.status_code, 422)
        self.assertIn("out of date", stale.text)

        unchecked = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-capture",
            {
                "csrf_token": self.csrf,
                "selection_id": selection.selection_id,
                "selection_hash": selection.content_hash,
                "confirm_capture": "1",
            },
        )
        self.assertEqual(unchecked.status_code, 422)
        self.assertIn(
            "Check the current number of matching records",
            unchecked.text,
        )

        schema = self.app.state.context.queries.get_odoo_schema_catalog(workspace_id)
        self.assertIsNotNone(schema)
        gateway = _BrowserOdooCaptureGateway(workspace_state, schema)
        self.app.state.context.source_capture_factory = (
            lambda selected_workspace_state, _secret: gateway
        )
        assessed = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-assessment",
            {
                "csrf_token": self.csrf,
                "selection_id": selection.selection_id,
                "selection_hash": selection.content_hash,
            },
        )
        self.assertEqual(assessed.status_code, 200)
        self.assertIn("Freeze 2 matching records?", assessed.text)
        self.assertIn("1 data request", assessed.text)
        self.assertIn("up to 100 records", assessed.text)
        started = self._post(
            f"/workspaces/{workspace_id}/sources/odoo-capture",
            {
                "csrf_token": self.csrf,
                "selection_id": selection.selection_id,
                "selection_hash": selection.content_hash,
                "confirm_capture": "1",
            },
        )
        self.assertEqual(started.status_code, 303)
        progress_url = started.headers["location"]
        progress_page = self.client.get(progress_url)
        self.assertIn("data-odoo-capture-job", progress_page.text)
        finished = _wait_for_odoo_capture(self.client, progress_url)
        self.assertEqual(finished["status"], "SUCCEEDED", finished)
        self.assertEqual(finished["completed_rows"], 2)
        self.assertEqual(finished["page_count"], 1)
        calls_after_capture = tuple(gateway.calls)

        frozen_page = self.client.get(finished["redirect_url"])
        self.assertEqual(tuple(gateway.calls), calls_after_capture)
        self.assertIn("Current frozen Odoo source", frozen_page.text)
        self.assertIn("Stage 3 of 8", frozen_page.text)
        self.assertIn("2</dd>", frozen_page.text)
        self.assertIn("Protected history", frozen_page.text)
        self.assertIn("Frozen versions", frozen_page.text)
        self.assertIn("Source download complete", frozen_page.text)
        self.assertIn("The frozen Odoo source is ready", frozen_page.text)
        self.assertIn("Destination unavailable", frozen_page.text)
        self.assertIn("Connect source Odoo", frozen_page.text)
        self.assertIn("Select data to download", frozen_page.text)
        self.assertIn("Download and freeze", frozen_page.text)
        self.assertIn("Connect destination Odoo", frozen_page.text)
        self.assertIn("Match destination data", frozen_page.text)
        self.assertIn("Validate transfer order", frozen_page.text)
        self.assertIn("Review transfer", frozen_page.text)
        self.assertIn("Load destination Odoo", frozen_page.text)

        mapping_page = self.client.get(f"/workspaces/{workspace_id}/mapping")
        self.assertEqual(mapping_page.status_code, 200)
        self.assertIn("Update only the records selected from Odoo", mapping_page.text)
        self.assertIn("Allow Impodo to update this field", mapping_page.text)
        self.assertIn('value="odoo_pinned_update"', mapping_page.text)
        self.assertNotIn("Which column uniquely identifies each row?", mapping_page.text)
        self.assertEqual(tuple(gateway.calls), calls_after_capture)

        source_selection = (
            self.app.state.context.queries.get_mapping_source_selection(workspace_id)
        )
        self.assertIsNotNone(source_selection)
        assert source_selection is not None
        name_row = re.search(
            r'data-target-field="name".*?name="scalar_value_source_0_(\d+)"',
            mapping_page.text,
            re.DOTALL,
        )
        self.assertIsNotNone(name_row)
        assert name_row is not None
        field_index = name_row.group(1)
        dataset = source_selection.datasets[0]
        source_column = dataset.columns[0]
        mapping_entries = [
            ["csrf_token", self.csrf],
            ["action", "draft"],
            ["expected_parent_version", ""],
            ["expected_working_draft_version", ""],
            ["editable_dataset_id", dataset.dataset_id],
            ["target_model_0", "res.partner"],
            ["mode_0", "odoo_pinned_update"],
            ["visible_scalar_target_0", "name"],
            [f"scalar_value_source_0_{field_index}", "source"],
            [f"scalar_source_0_{field_index}", source_column.stable_key],
            [f"scalar_type_0_{field_index}", "string"],
            [f"scalar_case_0_{field_index}", "uppercase"],
            [f"scalar_compare_0_{field_index}", "1"],
            ["approved_write_field_0", "name"],
        ]
        checked = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": mapping_entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(checked.status_code, 200, checked.text)
        revision = self.app.state.context.queries.get_mapping_revision(workspace_id)
        working = self.app.state.context.queries.get_mapping_working_draft(workspace_id)
        self.assertIsNotNone(revision)
        self.assertIsNotNone(working)
        assert revision is not None
        assert working is not None
        mapping_entries[1] = ["action", "submit"]
        mapping_entries[2] = ["expected_parent_version", str(revision.version)]
        mapping_entries[3] = [
            "expected_working_draft_version",
            str(working.version),
        ]
        submitted = self.client.post(
            f"/workspaces/{workspace_id}/mapping/save",
            json={"entries": mapping_entries},
            headers={**POST_HEADERS, "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        prepared_page = self.client.get(f"/workspaces/{workspace_id}/prepare")
        self.assertEqual(prepared_page.status_code, 200)
        self.assertIn("Prepare data", prepared_page.text)
        self.assertEqual(tuple(gateway.calls), calls_after_capture)
        normalization = self.app.state.context.preparation.prepare(
            workspace_id,
            actor=self.app.state.context.actor,
        )
        self.assertEqual(
            normalization.eligible_record_count,
            2,
            self.app.state.context.quality.current_summary(workspace_id),
        )
        normalization_page = self.client.get(f"/workspaces/{workspace_id}/normalization")
        self.assertEqual(normalization_page.status_code, 200)
        self.assertIn("Review what Impodo prepared", normalization_page.text)
        self.assertEqual(tuple(gateway.calls), calls_after_capture)
        approved = self._post(
            f"/workspaces/{workspace_id}/normalization/approve",
            {
                "csrf_token": self.csrf,
                "run_id": normalization.run_id,
                "lifecycle_version": str(normalization.lifecycle_version),
            },
        )
        self.assertEqual(approved.status_code, 303)
        self.assertEqual(
            approved.headers["location"],
            f"/workspaces/{workspace_id}/summary",
        )
        approved_page = self.client.get(approved.headers["location"])
        self.assertIn("Compare the approved data with Odoo", approved_page.text)
        self.assertIn("approved data with Odoo", approved_page.text)
        self.assertEqual(tuple(gateway.calls), calls_after_capture)

        def pinned_reader(
            selected_workspace_state,
            metadata_requests,
            record_requests,
        ):
            self.readiness_calls.append(
                (
                    selected_workspace_state.workspace_id,
                    metadata_requests,
                    record_requests,
                )
            )
            available = _browser_schema(selected_workspace_state)
            metadata = replace(
                available,
                models={
                    request.model: replace(
                        available.models[request.model],
                        fields={
                            field: available.models[request.model].fields[field]
                            for field in request.fields
                        },
                    )
                    for request in metadata_requests
                },
            )
            requested_fields = record_requests[0].fields
            return metadata, RecordSnapshot(
                fingerprint=metadata.fingerprint,
                records={
                    "res.partner": (
                        TargetRecord(
                            "res.partner",
                            11,
                            {
                                "name": "Alice",
                                "write_date": gateway.now.isoformat(),
                            },
                        ),
                        TargetRecord(
                            "res.partner",
                            12,
                            {
                                "name": "Bob",
                                "write_date": (
                                    gateway.now + timedelta(seconds=1)
                                ).isoformat(),
                            },
                        ),
                    )
                },
                requested_fields={"res.partner": requested_fields},
            )

        self.app.state.context.readiness_reader = pinned_reader
        compared = self._post(
            f"/workspaces/{workspace_id}/summary/compare",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(compared.status_code, 303, compared.text)
        comparison_page = self.client.get(compared.headers["location"])
        self.assertIn("Comparison complete", comparison_page.text)
        self.assertIn("Ready to update", comparison_page.text)
        self.assertIn("2 records ready to update", comparison_page.text)
        self.assertNotIn("New in Odoo", comparison_page.text)
        self.assertNotIn("Create review workbook", comparison_page.text)
        self.assertIn("Load destination Odoo", comparison_page.text)
        self.assertIn("Not yet available", comparison_page.text)
        report = self.app.state.context.preflight.current_report(workspace_id)
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.create_count, 0)
        self.assertEqual(report.update_count, 2)
        self.assertEqual(report.blocked_count, 0)
        protected_comparison = (
            self.app.state.context.preflight.current_odoo_comparison(
                workspace_id,
                actor=self.app.state.context.actor,
            )
        )
        self.assertIsNotNone(protected_comparison)
        assert protected_comparison is not None
        self.assertEqual(
            tuple(item.odoo_id for item in protected_comparison.rows),
            (11, 12),
        )
        self.assertEqual(
            {item.outcome for item in protected_comparison.rows},
            {OdooComparisonOutcome.UPDATE},
        )
        with self.app.state.context.artifacts.materialize_report(
            workspace_id,
            report.run_id,
            MANIFEST_NAME,
        ) as manifest_path:
            portable_manifest = manifest_path.read_text("utf-8")
        self.assertNotIn('"odoo_id"', portable_manifest)
        self.assertNotIn("Alice", portable_manifest)
        self.assertEqual(len(self.readiness_calls), 1)
        _workspace_id, _metadata_requests, record_requests = self.readiness_calls[0]
        self.assertEqual(len(record_requests), 1)
        self.assertEqual(record_requests[0].fields, ("name", "write_date"))
        self.assertEqual(record_requests[0].domain, (["id", "in", [11, 12]],))

        def conflicting_reader(
            selected_workspace_state,
            metadata_requests,
            record_requests,
        ):
            metadata, records = pinned_reader(
                selected_workspace_state,
                metadata_requests,
                record_requests,
            )
            return metadata, replace(
                records,
                records={
                    "res.partner": (
                        TargetRecord(
                            "res.partner",
                            11,
                            {
                                "name": "Changed elsewhere",
                                "write_date": (
                                    gateway.now + timedelta(minutes=1)
                                ).isoformat(),
                            },
                        ),
                        records.records["res.partner"][1],
                    )
                },
            )

        self.app.state.context.readiness_reader = conflicting_reader
        blocked_compare = self._post(
            f"/workspaces/{workspace_id}/summary/compare",
            {"csrf_token": self.csrf},
        )
        self.assertEqual(blocked_compare.status_code, 303, blocked_compare.text)
        blocked_page = self.client.get(blocked_compare.headers["location"])
        self.assertIn("Refresh the captured Odoo records", blocked_page.text)
        self.assertIn("Needs refresh", blocked_page.text)
        self.assertNotIn("data-preflight-compare", blocked_page.text)
        blocked_report = self.app.state.context.preflight.current_report(workspace_id)
        self.assertIsNotNone(blocked_report)
        assert blocked_report is not None
        self.assertEqual(blocked_report.blocked_count, 1)
        blocked_artifact = self.app.state.context.preflight.current_odoo_comparison(
            workspace_id,
            actor=self.app.state.context.actor,
        )
        self.assertIsNotNone(blocked_artifact)
        assert blocked_artifact is not None
        self.assertEqual(
            blocked_artifact.rows[0].outcome,
            OdooComparisonOutcome.CONCURRENT_FIELD_CHANGE,
        )

        previous_source_hash = source_selection.content_hash
        refresh_gateway = _BrowserOdooCaptureGateway(workspace_state, schema)
        refreshed_publication = (
            self.app.state.context.odoo_capture_publication.publish(
                workspace_id,
                refresh_gateway,
                actor=self.app.state.context.actor,
            )
        )
        self.assertNotEqual(
            refreshed_publication.source_selection.content_hash,
            previous_source_hash,
        )
        self.assertIsNone(
            self.app.state.context.queries.get_mapping_revision(workspace_id)
        )
        self.assertIsNone(
            self.app.state.context.preflight.current_staging(workspace_id)
        )
        self.assertIsNone(
            self.app.state.context.normalization.current_summary(workspace_id)
        )
        self.assertIsNone(
            self.app.state.context.preflight.current_report(workspace_id)
        )
        self.assertIsNone(
            self.app.state.context.preflight.current_odoo_comparison(
                workspace_id,
                actor=self.app.state.context.actor,
            )
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
