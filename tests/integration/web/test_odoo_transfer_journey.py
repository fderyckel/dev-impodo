"""End-to-end browser evidence for the Odoo-to-Odoo transfer journey."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import re
from unittest.mock import patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.preflight_service import MANIFEST_NAME
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.odoo_comparison import OdooComparisonOutcome
from impodo.domain.shared.models import FieldMetadata, TargetRecord
from impodo.domain.workspace.workbench import SourceMode, WorkspaceStatus
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _BrowserOdooCaptureGateway,
    _browser_schema,
    _created_workspace_id,
    _wait_for_odoo_capture,
)


class OdooTransferJourneyBrowserTests(ProjectSetupBrowserTestCase):
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
            "A supported Odoo 19 or final Odoo 20 source can connect and match "
            "a separate destination of the same major. Cross-major transfer "
            "remains blocked.",
            target_page.text,
        )
        self.assertIn(
            "Nothing is written to an eligible destination until you "
            "explicitly confirm the Stage 6 load.",
            target_page.text,
        )
        self.assertIn("Source access only", target_page.text)
        self.assertNotIn('name="keep_api_key_for_loading"', target_page.text)
        self.assertIn("does not discover models or fields", target_page.text)
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
        self.assertIn("Stage 2 of 6", schema_page.text)
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
        self.assertIn("Stage 2 of 6", source_page.text)
        self.assertIn("Choose the Odoo data to move", source_page.text)
        self.assertIn("Reading the source is safe and read-only", source_page.text)
        self.assertIn('name="filter_field"', source_page.text)
        self.assertIn('name="filter_value"', source_page.text)
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
        with collect_duckdb_request_timings() as selection_timings:
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
        self.assertLessEqual(selection_timings.connection_count, 8)
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
        self.assertIn("Count selected source records", saved_page.text)
        self.assertIn("Capture plans complete", saved_page.text)
        self.assertLess(
            saved_page.text.index("Current protected evidence"),
            saved_page.text.index("Capture plans complete"),
        )
        self.assertIn("Stage 3 of 6", saved_page.text)
        self.assertIn("Count selected source records and continue", saved_page.text)
        self.assertIn(
            'data-submitting-label="Counting selected source records in Odoo..."',
            saved_page.text,
        )
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
            "Count selected source records and continue",
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
        real_assess_prepared = context.odoo_source_capture.assess_prepared

        def assess_prepared(*args, **kwargs):
            from impodo.adapters.duckdb.unit_of_work import _read_databases

            self.assertIsNone(getattr(_read_databases, "connections", None))
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return real_assess_prepared(*args, **kwargs)

        with (
            patch.object(
                context.odoo_source_capture,
                "assess_prepared",
                side_effect=assess_prepared,
            ),
            collect_duckdb_request_timings() as assessment_timings,
        ):
            assessed = self._post(
                f"/workspaces/{workspace_id}/sources/odoo-assessment",
                {
                    "csrf_token": self.csrf,
                    "selection_id": selection.selection_id,
                    "selection_hash": selection.content_hash,
                },
            )
        self.assertEqual(assessed.status_code, 200)
        self.assertLessEqual(assessment_timings.connection_count, 12)
        self.assertIn("Freeze 2 matching records?", assessed.text)
        self.assertIn("1 data request", assessed.text)
        self.assertIn("up to 100 records", assessed.text)
        real_get = context.queries.get
        real_enqueue = context.odoo_capture_jobs.enqueue

        def capture_read(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return real_get(*args, **kwargs)

        def enqueue(*args, **kwargs):
            from impodo.adapters.duckdb.unit_of_work import _read_databases

            self.assertIsNone(getattr(_read_databases, "connections", None))
            self.assertIsNotNone(asyncio.get_running_loop())
            return real_enqueue(*args, **kwargs)

        with (
            patch.object(context.queries, "get", side_effect=capture_read),
            patch.object(context.odoo_capture_jobs, "enqueue", side_effect=enqueue),
            collect_duckdb_request_timings() as capture_timings,
        ):
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
        self.assertLessEqual(capture_timings.connection_count, 8)
        progress_url = started.headers["location"]
        progress_page = self.client.get(progress_url)
        self.assertIn("data-odoo-capture-job", progress_page.text)
        self.assertIn('id="app-sidebar"', progress_page.text)
        self.assertIn("Connect source Odoo", progress_page.text)
        self.assertIn("Select data to download", progress_page.text)
        self.assertIn("Download and freeze", progress_page.text)
        self.assertIn("Capture progress", progress_page.text)
        self.assertIn(
            "For linked-record captures, Impodo then repeats the protected "
            "relationship check",
            progress_page.text,
        )
        self.assertIn("Connect and match destination", progress_page.text)
        finished = _wait_for_odoo_capture(self.client, progress_url, timeout=30.0)
        self.assertEqual(finished["status"], "SUCCEEDED", finished)
        self.assertEqual(finished["completed_rows"], 2)
        self.assertEqual(finished["page_count"], 1)
        self.assertEqual(finished["relationship_check_page_count"], 0)
        calls_after_capture = tuple(gateway.calls)

        frozen_page = self.client.get(finished["redirect_url"])
        self.assertEqual(tuple(gateway.calls), calls_after_capture)
        self.assertIn("Current frozen Odoo source", frozen_page.text)
        self.assertIn("Stage 3 of 6", frozen_page.text)
        self.assertIn("2</dd>", frozen_page.text)
        self.assertIn("Protected history", frozen_page.text)
        self.assertIn("Frozen versions", frozen_page.text)
        self.assertIn("Source download complete", frozen_page.text)
        self.assertIn("The frozen Odoo source is ready", frozen_page.text)
        self.assertIn("Next: connect the destination Odoo instance", frozen_page.text)
        self.assertIn("Connect source Odoo", frozen_page.text)
        self.assertIn("Select data to download", frozen_page.text)
        self.assertIn("Download and freeze", frozen_page.text)
        self.assertIn("Connect and match destination", frozen_page.text)
        self.assertIn("Review transfer", frozen_page.text)
        self.assertIn("Load destination Odoo", frozen_page.text)
        self.assertTrue(
            context.navigation.get_source_readiness(workspace_id).ready
        )

        destination_page = self.client.get(
            f"/workspaces/{workspace_id}/transfer-destination"
        )
        self.assertEqual(destination_page.status_code, 200)
        self.assertIn("Stage 4 of 6", destination_page.text)
        self.assertIn(
            "Connect the Odoo instance that will receive the data",
            destination_page.text,
        )
        self.assertIn("Source and destination stay separate", destination_page.text)
        current = self.app.state.context.queries.get(workspace_id)
        same_database = self._post(
            f"/workspaces/{workspace_id}/transfer-destination",
            {
                "csrf_token": self.csrf,
                "revision": str(current.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://odoo.example.test",
                "odoo_database": "odoo_review",
                "read_api_key": "destination-read-secret",
                "read_api_key_storage": "session",
            },
        )
        self.assertEqual(same_database.status_code, 422)
        self.assertIn(
            "Choose a different Odoo database for the destination",
            same_database.text,
        )

        current = self.app.state.context.queries.get(workspace_id)
        destination_checked = self._post(
            f"/workspaces/{workspace_id}/transfer-destination",
            {
                "csrf_token": self.csrf,
                "revision": str(current.revision),
                "odoo_connection_mode": "REMOTE",
                "odoo_base_url": "https://destination.example.test",
                "odoo_database": "odoo_destination",
                "read_api_key": "destination-read-secret",
                "read_api_key_storage": "session",
            },
        )
        self.assertEqual(destination_checked.status_code, 303)
        self.assertEqual(
            destination_checked.headers["location"],
            f"/workspaces/{workspace_id}/transfer-destination#destination-connected",
        )
        destination_state = self.app.state.context.queries.get(workspace_id)
        self.assertTrue(destination_state.destination_verified)
        self.assertEqual(
            destination_state.destination_odoo_base_url,
            "https://destination.example.test",
        )
        self.assertEqual(
            destination_state.destination_odoo_database,
            "odoo_destination",
        )
        self.assertEqual(
            destination_state.odoo_base_url,
            "https://odoo.example.test",
        )
        self.assertEqual(destination_state.odoo_database, "odoo_review")
        connected_page = self.client.get(destination_checked.headers["location"])
        self.assertIn("Destination connection complete", connected_page.text)
        self.assertIn("Connection complete", connected_page.text)
        self.assertIn("Match destination data", connected_page.text)
        self.assertIn("Destination matching required", connected_page.text)
        self.assertLess(
            connected_page.text.index("Latest destination check"),
            connected_page.text.index("Destination connection complete"),
        )
        connected_source_page = self.client.get(
            f"/workspaces/{workspace_id}/sources#download-complete"
        )
        self.assertIn("The destination connection is verified", connected_source_page.text)
        self.assertIn("Match destination data", connected_source_page.text)

        base_destination_reader = self.app.state.context.destination_match_reader

        def destination_reader_with_required_default(*args):
            metadata, records = base_destination_reader(*args)
            partner = metadata.models["res.partner"]
            return replace(
                metadata,
                models={
                    **metadata.models,
                    "res.partner": replace(
                        partner,
                        fields={
                            **partner.fields,
                            "group_on": FieldMetadata(
                                "group_on",
                                "selection",
                                "Group purchases by",
                                required=True,
                                selection=(("order", "Purchase order"),),
                                company_dependent=False,
                            ),
                        },
                    ),
                },
                create_defaults={
                    **metadata.create_defaults,
                    "res.partner": {
                        **metadata.create_defaults.get("res.partner", {}),
                        "group_on": "order",
                    },
                },
            ), records

        self.app.state.context.destination_match_reader = (
            destination_reader_with_required_default
        )

        matching_page = self.client.get(
            f"/workspaces/{workspace_id}/destination-matching"
        )
        self.assertEqual(matching_page.status_code, 200)
        self.assertIn("Stage 4 of 6", matching_page.text)
        self.assertIn(
            "Tell Impodo how to recognise the same record",
            matching_page.text,
        )
        self.assertIn("data-matching-builder", matching_page.text)
        self.assertIn("data-optional-match-field", matching_page.text)
        self.assertIn("Missing-record policy", matching_page.text)
        self.assertIn("Reuse existing destination records only", matching_page.text)
        frozen_selection = self.app.state.context.queries.get_source_selection(
            workspace_id
        )
        self.assertIsNotNone(frozen_selection)
        assert frozen_selection is not None
        frozen_dataset = frozen_selection.datasets[0]
        matching_checked = self._post(
            f"/workspaces/{workspace_id}/destination-matching",
            {
                "csrf_token": self.csrf,
                "revision": str(destination_state.revision),
                "match_key": (
                    f"{frozen_dataset.dataset_id}::"
                    f"{frozen_dataset.columns[0].stable_key}"
                ),
                "destination_handling": (
                    f"{frozen_dataset.dataset_id}::transfer"
                ),
            },
        )
        self.assertEqual(matching_checked.status_code, 303, matching_checked.text)
        self.assertEqual(
            matching_checked.headers["location"],
            f"/workspaces/{workspace_id}/destination-matching#matching-results",
        )
        matched_state = self.app.state.context.queries.get(workspace_id)
        self.assertIsNotNone(matched_state.destination_match_plan)
        self.assertTrue(matched_state.destination_match_plan.ready)
        matched_page = self.client.get(matching_checked.headers["location"])
        self.assertIn(
            "Destination matching was checked. Complete the remaining decisions",
            matched_page.text,
        )
        self.assertIn("<dt>Reuse</dt>", matched_page.text)
        self.assertIn("<dt>Create</dt>", matched_page.text)
        self.assertNotIn("Next: review the transfer", matched_page.text)
        self.assertIn("Complete values for new records", matched_page.text)
        self.assertIn("Purchase order (order)", matched_page.text)
        self.assertIn("Needs decision", matched_page.text)
        plan_with_destination_managed_field = replace(
            matched_state.destination_match_plan,
            model_matches=tuple(
                replace(item, destination_managed_fields=("parent_path",))
                if item.model == "res.partner"
                else item
                for item in matched_state.destination_match_plan.model_matches
            ),
        )
        matched_state = (
            self.app.state.context.workspace_states.save_destination_match_plan(
                workspace_id,
                actor=self.app.state.context.actor,
                expected_revision=matched_state.revision,
                plan=plan_with_destination_managed_field,
            )
        )
        destination_managed_page = self.client.get(
            matching_checked.headers["location"]
        )
        self.assertIn("Destination-owned evidence", destination_managed_page.text)
        self.assertIn("<code>parent_path</code>", destination_managed_page.text)
        self.assertIn("checked disabled", destination_managed_page.text)
        self.assertIn("Automatic", destination_managed_page.text)
        plan_with_missing_field = replace(
            matched_state.destination_match_plan,
            model_matches=tuple(
                replace(item, missing_fields=("version",))
                if item.model == "res.partner"
                else item
                for item in matched_state.destination_match_plan.model_matches
            ),
        )
        matched_state = self.app.state.context.workspace_states.save_destination_match_plan(
            workspace_id,
            actor=self.app.state.context.actor,
            expected_revision=matched_state.revision,
            plan=plan_with_missing_field,
        )
        field_review_page = self.client.get(matching_checked.headers["location"])
        self.assertIn("Put <code>version</code> aside", field_review_page.text)
        field_excluded = self._post(
            f"/workspaces/{workspace_id}/destination-matching/field-scope",
            {
                "csrf_token": self.csrf,
                "revision": str(matched_state.revision),
                "match_plan_hash": matched_state.destination_match_plan.content_hash,
                "field_key": "res.partner::version",
                "field_action": "exclude",
            },
        )
        self.assertEqual(field_excluded.status_code, 303, field_excluded.text)
        matched_state = self.app.state.context.queries.get(workspace_id)
        matched_contact = matched_state.destination_match_plan.model_matches[0]
        self.assertEqual(matched_contact.excluded_fields, ("version",))
        excluded_page = self.client.get(field_excluded.headers["location"])
        self.assertIn("Put aside for this transfer", excluded_page.text)
        self.assertIn("Include again", excluded_page.text)
        pending_default = next(
            item
            for item in matched_state.destination_match_plan.create_field_decisions
            if item.field_name == "group_on"
        )
        self.assertFalse(pending_default.reviewed)
        confirmed_defaults = self._post(
            f"/workspaces/{workspace_id}/destination-matching/defaults",
            {
                "csrf_token": self.csrf,
                "revision": str(matched_state.revision),
                "match_plan_hash": matched_state.destination_match_plan.content_hash,
                "default_field": "res.partner::group_on",
            },
        )
        self.assertEqual(confirmed_defaults.status_code, 303, confirmed_defaults.text)
        matched_state = self.app.state.context.queries.get(workspace_id)
        confirmed_default = next(
            item
            for item in matched_state.destination_match_plan.create_field_decisions
            if item.field_name == "group_on"
        )
        self.assertTrue(confirmed_default.reviewed)
        self.assertNotIn(
            "group_on",
            matched_state.destination_match_plan.model_matches[0].unresolved_create_fields,
        )
        confirmed_page = self.client.get(confirmed_defaults.headers["location"])
        self.assertIn("Stage 4 complete", confirmed_page.text)
        self.assertIn("Confirmed", confirmed_page.text)
        self.assertIn("Next: review the transfer", confirmed_page.text)
        self.assertIn(
            f'href="/workspaces/{workspace_id}/transfer-order"',
            confirmed_page.text,
        )
        self.assertIn("Validate transfer order", confirmed_page.text)

        order_page = self.client.get(
            f"/workspaces/{workspace_id}/transfer-order"
        )
        self.assertEqual(order_page.status_code, 200)
        self.assertIn("Stage 5 of 6", order_page.text)
        self.assertIn("Put related Odoo records in a safe transfer order", order_page.text)
        order_checked = self._post(
            f"/workspaces/{workspace_id}/transfer-order",
            {
                "csrf_token": self.csrf,
                "revision": str(matched_state.revision),
            },
        )
        self.assertEqual(order_checked.status_code, 303, order_checked.text)
        self.assertEqual(
            order_checked.headers["location"],
            f"/workspaces/{workspace_id}/transfer-order#transfer-order-results",
        )
        ordered_state = self.app.state.context.queries.get(workspace_id)
        self.assertIsNotNone(ordered_state.transfer_order_plan)
        self.assertTrue(ordered_state.transfer_order_plan.ready)
        ordered_page = self.client.get(order_checked.headers["location"])
        self.assertIn("Transfer order is ready", ordered_page.text)
        self.assertIn("Order ready", ordered_page.text)
        self.assertIn("Wave 1", ordered_page.text)
        self.assertIn("Next: choose and approve the transfer policy", ordered_page.text)
        self.assertIn(
            f'href="/workspaces/{workspace_id}/transfer-review"',
            ordered_page.text,
        )

        review_page = self.client.get(
            f"/workspaces/{workspace_id}/transfer-review"
        )
        self.assertEqual(review_page.status_code, 200)
        self.assertIn("Stage 5 of 6", review_page.text)
        self.assertIn("Approve the exact Odoo transfer package", review_page.text)
        review_built = self._post(
            f"/workspaces/{workspace_id}/transfer-review/build",
            {
                "csrf_token": self.csrf,
                "revision": str(ordered_state.revision),
            },
        )
        self.assertEqual(review_built.status_code, 303, review_built.text)
        review_state = self.app.state.context.queries.get(workspace_id)
        self.assertIsNotNone(review_state.transfer_review_package)
        self.assertIsNone(review_state.transfer_review_approval)
        built_page = self.client.get(review_built.headers["location"])
        self.assertIn("Frozen execution scope", built_page.text)
        self.assertIn("Control totals reconcile", built_page.text)
        self.assertIn("Approve exact transfer package", built_page.text)

        review_approved = self._post(
            f"/workspaces/{workspace_id}/transfer-review/approve",
            {
                "csrf_token": self.csrf,
                "revision": str(review_state.revision),
                "confirmation": "approve",
                "reason": "Approved for the destination transfer.",
            },
        )
        self.assertEqual(review_approved.status_code, 303, review_approved.text)
        approved_transfer_state = self.app.state.context.queries.get(workspace_id)
        self.assertIsNotNone(approved_transfer_state.transfer_review_approval)
        approved_transfer_page = self.client.get(
            review_approved.headers["location"]
        )
        self.assertIn("Stage 5 complete", approved_transfer_page.text)
        self.assertIn("Exact transfer package approved", approved_transfer_page.text)
        self.assertIn("Continue to destination preflight", approved_transfer_page.text)

        destination_preflight_page = self.client.get(
            f"/workspaces/{workspace_id}/transfer-preflight"
        )
        self.assertEqual(destination_preflight_page.status_code, 200)
        self.assertIn(
            "Your data will go to odoo_destination",
            destination_preflight_page.text,
        )
        self.assertIn(
            "https://destination.example.test",
            destination_preflight_page.text,
        )
        self.assertIn("Verified destination", destination_preflight_page.text)

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
        readiness_calls_before_comparison = len(self.readiness_calls)

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
        self.assertIn("Run read-only preflight", comparison_page.text)
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
        self.assertEqual(
            len(self.readiness_calls),
            readiness_calls_before_comparison + 1,
        )
        _workspace_id, _metadata_requests, record_requests = self.readiness_calls[-1]
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
