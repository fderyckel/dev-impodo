"""Exercise portable saved-data admission across independent installations."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from itertools import product
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.odoo_saved_data_archive import OdooSavedDataArchiveBackend
from impodo.adapters.duckdb.odoo_saved_source_company import (
    OdooSavedSourceCompanyRepository,
)
from impodo.adapters.odoo_saved_data_package import (
    SavedOdooPackageError,
    read_saved_odoo_package,
    write_saved_odoo_package,
)
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.application.odoo_saved_data_archive import OdooSavedDataArchiveService
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_database_binding import OdooDatabaseBindingRole
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.shared.access import AuthorizationError, Capability
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.errors import WorkspaceError
from impodo.web.app import create_local_app
from impodo.web.composition.database_bindings import review_database_binding
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    ProjectWorkspaceBuilder,
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


def _archive(context):
    saved = context.saved_odoo_data
    assert saved is not None
    return OdooSavedDataArchiveService(
        saved_data=saved,
        backend=OdooSavedDataArchiveBackend(
            saved_data=saved,
            workspace_factory=saved.workspaces,
            sources=context.sources.sources,
            schemas=context.schema_workspace.schemas,
            provenance=context.odoo_provenance._provenance,
            artifacts=context.sources.artifacts,
        ),
    )


class SavedOdooArchiveLifecycleTests(ProjectSetupBrowserTestCase):
    def _captured_version(
        self, major, *, connection_mode="REMOTE", protected_filter=False
    ):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        state, schema = self._registered_remote_schema_workspace()
        if connection_mode == "LOCAL":
            state = context.workspace_states.update_target(
                state.workspace_id,
                actor=actor,
                expected_revision=context.queries.get(state.workspace_id).revision,
                odoo_connection_mode="LOCAL",
                odoo_base_url="http://127.0.0.1:9876",
                odoo_database="clean_source",
                intended_applications=(),
                intended_models=("res.partner",),
            )
            schema = replace(
                schema,
                connection_mode="LOCAL",
                database=state.odoo_database,
                connection_target_hash=target_identity_hash(
                    connection_mode="LOCAL",
                    base_url=state.odoo_base_url,
                    database=state.odoo_database,
                ),
            )
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
        schema = replace(
            schema,
            odoo_version=f"{major}.0",
            policy_hash=odoo_source_policy_hash(major),
            read_context_hash=company.context_hash,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            state.workspace_id, schema, actor=actor
        )
        identity = context.identity_resolver.resolve({"authenticated": True})
        review_database_binding(
            context, state, OdooDatabaseBindingRole.SOURCE,
            actor=actor, scope=identity.organization_scope,
            authorization_receipt=identity.authorization,
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
                **(
                    {"filter_field": "name", "filter_value": "Alice"}
                    if protected_filter
                    else {}
                ),
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
        saved = context.saved_odoo_data
        assert saved is not None
        definition = saved.create(
            state.workspace_id, actor=actor, label=f"Clean contacts Odoo {major}"
        )
        definition, version = saved.retain_current(
            definition.project_id,
            definition.saved_data_id,
            actor=actor,
            expected_revision=definition.revision,
            operation_id=str(uuid4()),
        )
        return definition, version, publication

    def test_archive_restores_with_new_keys_and_data_version_without_original_installation(
        self,
    ):
        for major, connection_mode in product((19, 20), ("LOCAL", "REMOTE")):
            with (
                self.subTest(major=major, connection_mode=connection_mode),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                definition, version, publication = self._captured_version(
                    major,
                    connection_mode=connection_mode,
                    protected_filter=connection_mode == "LOCAL",
                )
                context = self.app.state.context
                actor = resolve_local_actor(context)
                original_capture = (
                    context.sources.sources.get_current_odoo_capture_selections(
                        version.workspace_id
                    )[0]
                )
                original_filters = context.sources.capture_filters.read(
                    definition.project_id, original_capture
                )
                origin = context.odoo_provenance.read_current_origins(
                    version.workspace_id,
                    actor=actor,
                    dataset_id=publication.manifests[0].dataset_id,
                )
                source_key_id = (
                    f"data-version:{version.data_version_id}:protected:origin-v2"
                )
                source_key = self.secrets.get(source_key_id)
                path = _archive(context).export_version(
                    definition.project_id,
                    definition.saved_data_id,
                    version.version_number,
                    destination=root / "contacts.impodo",
                    password="portable-password",
                    actor=actor,
                )
                decoded = read_saved_odoo_package(
                    path, root / "inspection", password="portable-password"
                )
                for _name, member in decoded.member_paths:
                    self.assertNotIn(b"read-secret", member.read_bytes())
                    if source_key:
                        self.assertNotIn(source_key.encode(), member.read_bytes())
                self.secrets.delete(source_key_id)
                destination_secrets = MemorySecretStore()
                destination_app = create_local_app(
                    root / "new-installation", secret_store=destination_secrets
                )
                destination = destination_app.state.context
                target_actor = resolve_local_actor(destination)
                placeholder = ProjectWorkspaceBuilder(destination).create(
                    name="Imported clean data", source_system="Odoo", source_mode="ODOO"
                )
                target_project = destination.migration_workspaces.get(
                    placeholder.workspace_id, actor=target_actor
                ).project_id
                with (
                    patch.object(
                        destination,
                        "schema_reader",
                        side_effect=AssertionError("No live source"),
                    ),
                    patch.object(
                        destination,
                        "read_identity_probe",
                        side_effect=AssertionError("No original access"),
                    ),
                ):
                    imported = _archive(destination).import_version(
                        target_project,
                        path,
                        password="portable-password",
                        actor=target_actor,
                    )
                    repeated = _archive(destination).import_version(
                        target_project,
                        path,
                        password="portable-password",
                        actor=target_actor,
                    )
                self.assertEqual(imported, repeated)
                self.assertEqual(imported.latest_version, 1)
                self.assertEqual(imported.odoo_major, major)
                saved = destination.saved_odoo_data
                assert saved is not None
                imported_version = saved.repository.get_version(
                    target_project, imported.saved_data_id, 1
                )
                self.assertIsNone(saved.repository.get_configuration_source_binding(
                    target_project, imported.saved_data_id, imported.configuration_revision
                ))
                with self.assertRaisesRegex(WorkspaceError, "no database-generation review"):
                    saved.require_refresh_source(
                        target_project, imported.saved_data_id, actor=target_actor,
                        expected_revision=imported.revision,
                    )
                self.assertEqual(saved.repository.get_version(
                    target_project, imported.saved_data_id, 1
                ), imported_version)
                self.assertNotEqual(
                    imported_version.data_version_id, version.data_version_id
                )
                self.assertEqual(imported_version.row_counts, version.row_counts)
                imported_capture = (
                    destination.sources.sources.get_current_odoo_capture_selections(
                        imported_version.workspace_id
                    )[0]
                )
                self.assertEqual(
                    destination.sources.capture_filters.read(
                        target_project, imported_capture
                    ),
                    original_filters,
                )
                if connection_mode == "LOCAL":
                    self.assertIsNotNone(
                        original_capture.protected_filter_artifact_hash
                    )
                    self.assertNotEqual(
                        imported_capture.protected_filter_artifact_hash,
                        original_capture.protected_filter_artifact_hash,
                    )
                manifests = destination.odoo_provenance.current_manifests(
                    imported_version.workspace_id, actor=target_actor
                )
                self.assertEqual(
                    manifests[0].data_sha256, publication.manifests[0].data_sha256
                )
                self.assertNotEqual(
                    manifests[0].dataset_id, publication.manifests[0].dataset_id
                )
                self.assertEqual(
                    manifests[0].capture_started_at,
                    publication.manifests[0].capture_started_at,
                )
                self.assertEqual(
                    destination.odoo_provenance.read_current_origins(
                        imported_version.workspace_id,
                        actor=target_actor,
                        dataset_id=manifests[0].dataset_id,
                    ),
                    origin,
                )
                self.assertIsNone(destination_secrets.get(source_key_id))
                self.assertIsNotNone(
                    destination_secrets.get(
                        f"data-version:{imported_version.data_version_id}:protected:origin-v2"
                    )
                )
                restored = saved.workspaces.prepare_restore(
                    imported_version.workspace_id,
                    actor=target_actor,
                    operation_id=str(uuid4()),
                    expected_source_selection_hash=imported_version.source_selection_hash,
                    expected_manifest_hashes=imported_version.manifest_hashes,
                )
                self.assertNotEqual(
                    restored.workspace_id, imported_version.workspace_id
                )
                self.assertFalse(restored.destination_configured)

    def test_wrong_password_and_tampering_create_no_data_versions(self):
        definition, version, _publication = self._captured_version(19)
        context = self.app.state.context
        actor = resolve_local_actor(context)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = _archive(context).export_version(
                definition.project_id,
                definition.saved_data_id,
                version.version_number,
                destination=root / "clean.impodo",
                password="portable-password",
                actor=actor,
            )
            before = context.data_versions.repository.list_data_versions(
                definition.project_id
            )
            facade = _archive(context)
            restricted = replace(
                actor,
                capabilities=actor.capabilities
                - {Capability.PROJECT_ADMIN, Capability.PROTECTED_EVIDENCE_MANAGE},
            )
            with (
                patch.object(
                    facade.backend,
                    "import_version",
                    side_effect=AssertionError("Admission must not start"),
                ),
                self.assertRaises(AuthorizationError),
            ):
                facade.import_version(
                    definition.project_id,
                    path,
                    password="portable-password",
                    actor=restricted,
                )
            decoded = read_saved_odoo_package(
                path, root / "decoded", password="portable-password"
            )
            members = {
                name: member.read_bytes() for name, member in decoded.member_paths
            }
            metadata = json.loads(members["metadata/source.json"])
            source = json.loads(metadata["source_selection"])
            source["content_hash"] = "sha256:" + "0" * 64
            metadata["source_selection"] = json.dumps(source)
            members["metadata/source.json"] = json.dumps(metadata).encode()
            malformed = root / "malformed.impodo"
            write_saved_odoo_package(malformed, members, password="portable-password")
            with self.assertRaisesRegex(WorkspaceError, "selection integrity differs"):
                facade.import_version(
                    definition.project_id,
                    malformed,
                    password="portable-password",
                    actor=actor,
                )
            with self.assertRaises(SavedOdooPackageError):
                _archive(context).import_version(
                    definition.project_id, path, password="wrong-password", actor=actor
                )
            changed = bytearray(path.read_bytes())
            changed[-20] ^= 1
            path.write_bytes(changed)
            with self.assertRaises(SavedOdooPackageError):
                _archive(context).import_version(
                    definition.project_id,
                    path,
                    password="portable-password",
                    actor=actor,
                )
            self.assertEqual(
                context.data_versions.repository.list_data_versions(
                    definition.project_id
                ),
                before,
            )
