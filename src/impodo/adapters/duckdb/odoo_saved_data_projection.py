"""Attach retained DataVersion evidence to a fresh restore workbench.

Only immutable metadata is projected. Parquet and encrypted origin artifacts
remain under their existing DataVersion owner, and destination evidence is
never copied. Saved-data workspace orchestration owns authorization.
"""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Protocol

import duckdb

from impodo.application.data_version.source_snapshots import (
    validate_snapshot_for_dataset,
)
from impodo.application.shared.artifacts import DataVersionSourceArtifactStore
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_capture import OdooCaptureSelection
from impodo.domain.odoo_provenance import OdooCaptureManifest
from impodo.domain.odoo_relationship_scope import OdooRelationshipScope
from impodo.domain.odoo_source_capture import validate_odoo_capture_selection_reference
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.shared.access import Actor
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SourceSelection
from impodo.domain.workspace.errors import WorkspaceError

from .database import DuckDbWorkspaceDatabase
from .migration_foundation_repository import MigrationFoundationRepository
from .odoo_provenance_repository import OdooProvenanceRepository
from .odoo_saved_source_company import OdooSavedSourceCompanyRepository
from .repository import DuckDbRepository
from .source_repository import SourceRepository


class SavedOdooSchemaReader(Protocol):
    """Read the authenticated source schema without opening Odoo."""

    def get_odoo_schema_catalog(
        self, workspace_id: str
    ) -> OdooSchemaCatalog | None: ...


class OdooSavedDataProjectionRepository(DuckDbRepository):
    """Seed pristine restore caches after verifying their immutable lineage."""

    def __init__(
        self,
        database: DuckDbWorkspaceDatabase,
        *,
        foundation: MigrationFoundationRepository,
        sources: SourceRepository,
        schemas: SavedOdooSchemaReader,
        provenance: OdooProvenanceRepository,
        artifacts: DataVersionSourceArtifactStore,
    ) -> None:
        super().__init__(database)
        self.foundation = foundation
        self.sources = sources
        self.schemas = schemas
        self.provenance = provenance
        self.artifacts = artifacts

    def set_source_company(
        self,
        workspace_id: str,
        selection: OdooCompanySelection,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> None:
        """Clone source context without altering a restore's destination."""
        OdooSavedSourceCompanyRepository(self._database).set_source_company(
            workspace_id, selection, actor=actor, expected_revision=expected_revision
        )

    def attach(
        self,
        source_workspace_id: str,
        workspace_id: str,
        *,
        actor: Actor,
        expected_source_selection_hash: str | None = None,
        expected_manifest_hashes: tuple[tuple[str, str], ...] = (),
    ) -> tuple[str, ...]:
        """Project a complete checked capture in one target-engine transaction."""

        source = self.foundation.get_migration_workspace(source_workspace_id)
        target = self.foundation.get_migration_workspace(workspace_id)
        if source_workspace_id == workspace_id or (
            source.project_id != target.project_id
            or source.data_version_id != target.data_version_id
        ):
            raise MigrationFoundationError(
                "Restore source projection has different lineage"
            )
        self._assert_workspace_mutable(workspace_id)
        selection = self.sources.get_source_selection(source_workspace_id)
        schema = self.schemas.get_odoo_schema_catalog(source_workspace_id)
        captures = self.sources.get_current_odoo_capture_selections(source_workspace_id)
        snapshots = self.sources.get_current_source_snapshots(source_workspace_id)
        manifests = self.provenance.get_currents(source_workspace_id)
        scope = self.sources.get_current_odoo_relationship_scope(source_workspace_id)
        if selection is None or schema is None:
            raise WorkspaceError("Saved Odoo source evidence is incomplete")
        if (
            selection.data_version_id != target.data_version_id
            or schema.workspace_id != source_workspace_id
        ):
            raise WorkspaceError("Saved Odoo source evidence has different lineage")
        if (
            expected_source_selection_hash is not None
            and selection.content_hash != expected_source_selection_hash
        ):
            raise WorkspaceError("Saved Odoo source selection changed")
        actual_hashes = tuple(
            sorted((item.model, item.content_hash) for item in manifests)
        )
        if (
            expected_manifest_hashes
            and tuple(sorted(expected_manifest_hashes)) != actual_hashes
        ):
            raise WorkspaceError("Saved Odoo capture manifests changed")
        self._validate(selection, schema, captures, snapshots, manifests)
        for manifest in manifests:
            encrypted = self.provenance.read_encrypted(source_workspace_id, manifest)
            if "sha256:" + sha256(encrypted).hexdigest() != manifest.provenance_sha256:
                raise WorkspaceError(
                    "Saved Odoo protected relationship evidence changed"
                )
        for snapshot in snapshots:
            with self.artifacts.materialize_source_snapshot(
                snapshot.data_version_id,
                snapshot.parquet_storage_key,
                expected_sha256=snapshot.parquet_sha256,
            ):
                pass

        projected_schema = replace(schema, workspace_id=workspace_id)
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT selection_json FROM source_selection WHERE singleton_id = 1"
                ).fetchone()
                if current is not None:
                    if SourceSelection.from_json(str(current[0])) != selection:
                        raise WorkspaceError(
                            "Restore workspace already has another source"
                        )
                    self._verify_existing(
                        connection, projected_schema, captures, manifests
                    )
                else:
                    if (
                        connection.execute(
                            "SELECT 1 FROM odoo_schema_catalog LIMIT 1"
                        ).fetchone()
                        is not None
                    ):
                        raise WorkspaceError(
                            "Restore projection requires a pristine workspace"
                        )
                    self._insert(
                        connection,
                        projected_schema,
                        selection,
                        captures,
                        snapshots,
                        manifests,
                        scope,
                    )
                    revision = self._workspace_revision(connection)
                    self._insert_workspace_audit(
                        connection,
                        revision=revision,
                        event_type="SAVED_ODOO_SOURCE_PROJECTED",
                        detail=f"{len(manifests)} retained source datasets",
                        actor=actor,
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return tuple(sorted(item.dataset_id for item in selection.datasets))

    @staticmethod
    def _validate(
        selection: SourceSelection,
        schema: OdooSchemaCatalog,
        captures: tuple[OdooCaptureSelection, ...],
        snapshots: tuple[SourceSnapshot, ...],
        manifests: tuple[OdooCaptureManifest, ...],
    ) -> None:
        datasets = {item.dataset_id: item for item in selection.datasets}
        captures_by_id = {item.dataset_id: item for item in captures}
        snapshots_by_id = {item.dataset_id: item for item in snapshots}
        manifests_by_id = {item.dataset_id: item for item in manifests}
        if (
            not datasets
            or any(
                set(mapping) != set(datasets)
                for mapping in (captures_by_id, snapshots_by_id, manifests_by_id)
            )
            or len(captures_by_id) != len(captures)
            or len(manifests_by_id) != len(manifests)
            or len(snapshots_by_id) != len(snapshots)
        ):
            raise WorkspaceError("Saved Odoo capture is incomplete")
        if {item.model for item in captures} != {item.name for item in schema.models}:
            raise WorkspaceError("Saved Odoo schema does not cover the selected models")
        for dataset_id, dataset in datasets.items():
            capture = captures_by_id[dataset_id]
            snapshot = snapshots_by_id[dataset_id]
            manifest = manifests_by_id[dataset_id]
            validate_odoo_capture_selection_reference(capture, schema)
            validate_snapshot_for_dataset(selection, dataset, snapshot)
            if (
                capture.data_version_id != selection.data_version_id
                or manifest.data_version_id != selection.data_version_id
                or not manifest.binds_selection(capture)
                or manifest.data_logical_hash != snapshot.data_logical_hash
                or manifest.data_sha256 != snapshot.parquet_sha256
                or manifest.data_storage_key != snapshot.parquet_storage_key
                or manifest.row_count != snapshot.row_count
            ):
                raise WorkspaceError("Saved Odoo capture bindings are inconsistent")

    @staticmethod
    def _verify_existing(
        connection: duckdb.DuckDBPyConnection,
        schema: OdooSchemaCatalog,
        captures: tuple[OdooCaptureSelection, ...],
        manifests: tuple[OdooCaptureManifest, ...],
    ) -> None:
        row = connection.execute(
            "SELECT catalog_json FROM odoo_schema_catalog WHERE singleton_id = 1"
        ).fetchone()
        hashes = connection.execute(
            "SELECT revision.content_hash FROM odoo_capture_manifest_current AS current "
            "JOIN odoo_capture_manifest_revision AS revision USING (manifest_id)"
        ).fetchall()
        plan_hashes = connection.execute(
            "SELECT revision.content_hash FROM odoo_capture_selection_current AS current "
            "JOIN odoo_capture_selection_revision AS revision USING (selection_id, version)"
        ).fetchall()
        if (
            row is None
            or OdooSchemaCatalog.from_json(str(row[0])) != schema
            or {str(item[0]) for item in hashes}
            != {item.content_hash for item in manifests}
            or {str(item[0]) for item in plan_hashes}
            != {item.content_hash for item in captures}
        ):
            raise WorkspaceError("Restore source projection changed after provisioning")

    @staticmethod
    def _insert(
        connection: duckdb.DuckDBPyConnection,
        schema: OdooSchemaCatalog,
        selection: SourceSelection,
        captures: tuple[OdooCaptureSelection, ...],
        snapshots: tuple[SourceSnapshot, ...],
        manifests: tuple[OdooCaptureManifest, ...],
        scope: OdooRelationshipScope | None,
    ) -> None:
        connection.execute(
            "INSERT INTO odoo_schema_catalog VALUES (1, ?)", [schema.to_json()]
        )
        connection.execute(
            "INSERT INTO source_selection VALUES (1, ?)", [selection.to_json()]
        )
        connection.executemany(
            "INSERT INTO odoo_capture_selection_revision VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                [
                    item.selection_id,
                    item.version,
                    item.content_hash,
                    item.model,
                    item.created_at.isoformat(),
                    item.created_by,
                    item.to_json(),
                ]
                for item in captures
            ],
        )
        connection.executemany(
            "INSERT INTO odoo_capture_selection_current VALUES (?, ?, ?)",
            [[item.model, item.selection_id, item.version] for item in captures],
        )
        connection.executemany(
            "INSERT INTO source_snapshot_manifest VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                [
                    item.content_hash,
                    item.dataset_id,
                    item.logical_hash,
                    item.parquet_sha256,
                    item.parquet_storage_key,
                    item.created_at.isoformat(),
                    item.to_json(),
                ]
                for item in snapshots
            ],
        )
        connection.executemany(
            "INSERT INTO source_snapshot_current VALUES (?, ?)",
            [[item.dataset_id, item.content_hash] for item in snapshots],
        )
        connection.executemany(
            "INSERT INTO odoo_capture_manifest_revision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [
                    item.manifest_id,
                    item.content_hash,
                    item.selection_hash,
                    item.dataset_id,
                    item.row_count,
                    item.data_storage_key,
                    item.data_size_bytes,
                    item.provenance_size_bytes,
                    item.provenance_storage_key,
                    item.retention_until.isoformat(),
                    item.capture_finished_at.isoformat(),
                    item.to_json(),
                ]
                for item in manifests
            ],
        )
        connection.executemany(
            "INSERT INTO odoo_capture_manifest_current VALUES (?, ?)",
            [[item.dataset_id, item.manifest_id] for item in manifests],
        )
        if scope is not None:
            connection.execute(
                "INSERT INTO odoo_relationship_scope_revision VALUES (?, ?, ?, ?, ?, ?)",
                [
                    scope.scope_id,
                    scope.version,
                    scope.content_hash,
                    scope.recorded_at.isoformat(),
                    scope.recorded_by,
                    scope.to_json(),
                ],
            )
            connection.execute(
                "INSERT INTO odoo_relationship_scope_current VALUES (1, ?, ?)",
                [scope.scope_id, scope.version],
            )
