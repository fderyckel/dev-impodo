"""Verify offline restore workspaces retain exact DataVersion-owned evidence."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.odoo_saved_data_projection import (
    OdooSavedDataProjectionRepository,
)
from impodo.adapters.duckdb.odoo_saved_source_company import (
    OdooSavedSourceCompanyRepository,
)
from impodo.application.odoo_saved_data_workspaces import OdooSavedDataWorkspaceService
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.workspace.errors import WorkspaceError
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    _browser_schema,
    _BrowserOdooCaptureGateway,
)
from tests.support.identity import resolve_local_actor


class _MajorCaptureGateway(_BrowserOdooCaptureGateway):
    def probe_schema(self, request, context, *, cancellation=None):
        snapshot = _browser_schema(self.workspace_state)
        return replace(
            snapshot,
            fingerprint=replace(
                snapshot.fingerprint, odoo_version=self.schema.odoo_version
            ),
        )


class SavedOdooWorkspaceProjectionTests(ProjectSetupBrowserTestCase):
    def _service(self):
        context = self.app.state.context
        adapter = OdooSavedDataProjectionRepository(
            context.sources.sources._database,
            foundation=context.migration_workspaces.repository,
            sources=context.sources.sources,
            schemas=context.schema_workspace.schemas,
            provenance=context.odoo_provenance._provenance,
            artifacts=context.sources.artifacts,
        )
        return OdooSavedDataWorkspaceService(
            projects=context.migration_projects,
            data_versions=context.data_versions,
            runs=context.migration_runs,
            workspaces=context.migration_workspaces,
            workspace_states=context.workspace_states,
            projections=context.data_version_source_projection.projections,
            source_restarts=context.odoo_source_restarts,
            projection_repository=adapter,
        )

    def _capture(self, major, *, source_company=False):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        state, schema = self._registered_remote_schema_workspace()
        if source_company:
            company = OdooCompanySelection(
                schema.connection_target_hash, 1, "Source company"
            )
            OdooSavedSourceCompanyRepository(
                context.sources.sources._database
            ).set_source_company(
                state.workspace_id,
                company,
                actor=actor,
                expected_revision=context.queries.get(state.workspace_id).revision,
            )
            state = context.queries.get(state.workspace_id)
            schema = replace(schema, read_context_hash=company.context_hash)
        schema = replace(
            schema,
            odoo_version=f"{major}.0",
            policy_hash=odoo_source_policy_hash(major),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            state.workspace_id, schema, actor=actor
        )
        response = self._post(
            f"/workspaces/{state.workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "odoo_contacts",
                "model": "res.partner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(response.status_code, 303, response.text)
        publication = context.odoo_capture_publication.publish(
            state.workspace_id, _MajorCaptureGateway(state, schema), actor=actor
        )
        context.data_version_source_projection.accept_odoo_capture(
            state.workspace_id,
            publication.source_selection,
            publication.source_snapshots,
            publication.manifests,
            actor=actor,
        )
        return state, schema, publication

    def test_source_company_is_preserved_on_restore_and_refresh_without_selecting_destination(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                source, _schema, _publication = self._capture(
                    major, source_company=True
                )
                context = self.app.state.context
                actor = resolve_local_actor(context)
                self.assertIsNotNone(source.odoo_company)
                self.assertIsNone(source.destination_company)
                service = self._service()
                restored = service.prepare_restore(
                    source.workspace_id, actor=actor, operation_id=str(uuid4())
                )
                refreshed = service.prepare_refresh(
                    source.workspace_id, actor=actor, operation_id=str(uuid4())
                )
                self.assertEqual(restored.odoo_company, source.odoo_company)
                self.assertEqual(refreshed.odoo_company, source.odoo_company)
                self.assertIsNone(restored.destination_company)
                self.assertIsNone(refreshed.destination_company)
                with self.assertRaisesRegex(WorkspaceError, "fresh capture"):
                    OdooSavedSourceCompanyRepository(
                        context.sources.sources._database
                    ).set_source_company(
                        source.workspace_id,
                        OdooCompanySelection(
                            source.odoo_company.target_hash, 2, "Another source company"
                        ),
                        actor=actor,
                        expected_revision=context.queries.get(
                            source.workspace_id
                        ).revision,
                    )

    def test_restore_uses_frozen_data_without_source_access_and_retries_same_workspace(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                context = self.app.state.context
                actor = resolve_local_actor(context)
                source, schema, publication = self._capture(major)
                service = self._service()
                operation_id = str(uuid4())
                expected = {
                    "actor": actor,
                    "operation_id": operation_id,
                    "expected_source_selection_hash": publication.source_selection.content_hash,
                    "expected_manifest_hashes": tuple(
                        (item.model, item.content_hash)
                        for item in publication.manifests
                    ),
                }
                with (
                    patch.object(
                        context,
                        "read_identity_probe",
                        side_effect=AssertionError("No live source access"),
                    ),
                    patch.object(
                        context,
                        "schema_reader",
                        side_effect=AssertionError("No source schema refresh"),
                    ),
                ):
                    restored = service.prepare_restore(source.workspace_id, **expected)
                    repeated = service.prepare_restore(source.workspace_id, **expected)
                self.assertEqual(repeated.workspace_id, restored.workspace_id)
                self.assertNotEqual(restored.workspace_id, source.workspace_id)
                self.assertFalse(restored.destination_configured)
                owner = context.migration_workspaces.get(
                    restored.workspace_id, actor=actor
                )
                self.assertEqual(
                    owner.data_version_id, publication.source_selection.data_version_id
                )
                projected_schema = (
                    context.schema_workspace.schemas.get_odoo_schema_catalog(
                        restored.workspace_id
                    )
                )
                self.assertEqual(
                    projected_schema,
                    replace(schema, workspace_id=restored.workspace_id),
                )
                self.assertEqual(
                    context.sources.sources.get_source_selection(restored.workspace_id),
                    publication.source_selection,
                )
                self.assertEqual(
                    context.odoo_provenance.current_manifests(
                        restored.workspace_id, actor=actor
                    ),
                    publication.manifests,
                )
                for manifest in publication.manifests:
                    self.assertEqual(
                        context.odoo_provenance.read_current_origins(
                            restored.workspace_id,
                            actor=actor,
                            dataset_id=manifest.dataset_id,
                        ),
                        context.odoo_provenance.read_current_origins(
                            source.workspace_id,
                            actor=actor,
                            dataset_id=manifest.dataset_id,
                        ),
                    )

    def test_projection_rejects_a_changed_saved_manifest_before_seeding_source(self):
        source, _schema, publication = self._capture(19)
        service = self._service()
        with self.assertRaisesRegex(WorkspaceError, "manifests changed"):
            service.prepare_restore(
                source.workspace_id,
                actor=resolve_local_actor(self.app.state.context),
                operation_id=str(uuid4()),
                expected_source_selection_hash=publication.source_selection.content_hash,
                expected_manifest_hashes=(
                    (publication.manifests[0].model, "sha256:" + "0" * 64),
                ),
            )
        self.assertEqual(
            self.app.state.context.sources.sources.get_source_selection(
                source.workspace_id
            ),
            publication.source_selection,
        )

    def test_refresh_creates_draft_successor_without_reusing_live_schema_or_data(self):
        source, _schema, publication = self._capture(20)
        context = self.app.state.context
        actor = resolve_local_actor(context)
        operation_id = str(uuid4())
        service = self._service()
        refreshed = service.prepare_refresh(
            source.workspace_id, actor=actor, operation_id=operation_id
        )
        repeated = service.prepare_refresh(
            source.workspace_id, actor=actor, operation_id=operation_id
        )
        self.assertEqual(refreshed.workspace_id, repeated.workspace_id)
        owner = context.migration_workspaces.get(refreshed.workspace_id, actor=actor)
        self.assertNotEqual(
            owner.data_version_id, publication.source_selection.data_version_id
        )
        self.assertIsNone(
            context.schema_workspace.schemas.get_odoo_schema_catalog(
                refreshed.workspace_id
            )
        )
        self.assertEqual(
            context.sources.sources.get_current_odoo_capture_selections(
                refreshed.workspace_id
            ),
            (),
        )
        self.assertIsNone(
            context.sources.sources.get_source_selection(refreshed.workspace_id)
        )
