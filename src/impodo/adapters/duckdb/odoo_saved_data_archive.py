"""Export exact retained Odoo evidence and admit password-protected archives.

Metadata is selected explicitly; workspace databases, credentials, source
encryption keys and destination approvals never enter the transport package.
"""

from __future__ import annotations

import json
import tempfile
from contextlib import ExitStack
from pathlib import Path

import polars as pl

from impodo.adapters.odoo_saved_data_package import (
    read_saved_odoo_package,
    write_saved_odoo_package,
)
from impodo.application.data_version.source_snapshots import (
    validate_source_snapshot_path,
)
from impodo.application.odoo_saved_data_service import SavedOdooDataService
from impodo.application.odoo_saved_data_workspaces import OdooSavedDataWorkspaceService
from impodo.application.shared.artifacts import DataVersionSourceArtifactStore
from impodo.domain.odoo_saved_data import SavedOdooDataDefinition, SavedOdooDataVersion
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.errors import WorkspaceError

from .odoo_provenance_repository import OdooProvenanceRepository
from .odoo_saved_data_archive_import import OdooSavedDataArchiveImporter
from .odoo_saved_data_archive_payload import decode_payload, encode_origins
from .run_aware_schema_repository import RunAwareSchemaRepository
from .source_repository import SourceRepository


class OdooSavedDataArchiveBackend:
    """Keep local registry work separate from encrypted artifact streaming."""

    def __init__(
        self,
        *,
        saved_data: SavedOdooDataService,
        workspace_factory: OdooSavedDataWorkspaceService,
        sources: SourceRepository,
        schemas: RunAwareSchemaRepository,
        provenance: OdooProvenanceRepository,
        artifacts: DataVersionSourceArtifactStore,
    ) -> None:
        self.saved_data = saved_data
        self.workspace_factory = workspace_factory
        self.sources = sources
        self.schemas = schemas
        self.provenance = provenance
        self.artifacts = artifacts

    def export_version(
        self,
        definition: SavedOdooDataDefinition,
        version: SavedOdooDataVersion,
        *,
        destination: Path,
        password: str,
        actor: Actor,
    ) -> Path:
        """Encrypt selected frozen evidence without a source connection."""
        with (
            tempfile.TemporaryDirectory(prefix="impodo-saved-archive-") as directory,
            ExitStack() as stack,
        ):
            root = Path(directory)
            metadata, snapshots, members = self.saved_data.local_operation(
                lambda: self._export_metadata(definition, version, actor, root)
            )
            for index, snapshot in enumerate(snapshots):
                members[f"payload/model-{index}.parquet"] = stack.enter_context(
                    self.artifacts.materialize_source_snapshot(
                        snapshot.data_version_id,
                        snapshot.parquet_storage_key,
                        expected_sha256=snapshot.parquet_sha256,
                    )
                )
            members["metadata/source.json"] = metadata
            write_saved_odoo_package(Path(destination), members, password=password)
        return Path(destination)

    def _export_metadata(self, definition, version, actor, root):
        owner = self.saved_data.access.require(
            actor, Capability.PROTECTED_EVIDENCE_READ, workspace_id=version.workspace_id
        )
        if (
            owner.project_id != definition.project_id
            or owner.data_version_id != version.data_version_id
        ):
            raise WorkspaceError("The saved archive belongs to another source")
        state = self.saved_data.states.repository.get(version.workspace_id)
        source = self.sources.get_source_selection(version.workspace_id)
        schema = self.schemas.get_odoo_schema_catalog(version.workspace_id)
        snapshots = self.sources.get_current_source_snapshots(version.workspace_id)
        captures = self.sources.get_current_odoo_capture_selections(
            version.workspace_id
        )
        manifests = self.provenance.get_currents(version.workspace_id)
        scope = self.sources.get_current_odoo_relationship_scope(version.workspace_id)
        if (
            source is None
            or schema is None
            or state.odoo_company is None
            or state.odoo_connection_mode is None
            or source.content_hash != version.source_selection_hash
            or tuple(sorted((item.model, item.content_hash) for item in manifests))
            != version.manifest_hashes
        ):
            raise WorkspaceError("The saved archive evidence is incomplete or changed")
        by_snapshot = {item.dataset_id: item for item in snapshots}
        by_capture = {item.dataset_id: item for item in captures}
        datasets = []
        members: dict[str, Path | bytes] = {}
        ordered_snapshots = []
        for index, manifest in enumerate(
            sorted(manifests, key=lambda item: item.model)
        ):
            capture = by_capture.get(manifest.dataset_id)
            snapshot = by_snapshot.get(manifest.dataset_id)
            if (
                capture is None
                or snapshot is None
                or not manifest.binds_selection(capture)
            ):
                raise WorkspaceError("The saved archive model evidence is incomplete")
            protected = self.saved_data.provenance.read_current_origins(
                version.workspace_id, actor=actor, dataset_id=manifest.dataset_id
            )
            if protected is None:
                raise WorkspaceError(
                    "The saved archive relationship evidence is missing"
                )
            origin_name = f"payload/model-{index}.origins.json"
            origin_path = root / f"model-{index}.origins.json"
            origin_path.write_bytes(encode_origins(*protected))
            origin_path.chmod(0o600)
            members[origin_name] = origin_path
            filters = capture.filter_clauses
            if capture.protected_filter_artifact_hash is not None:
                if self.saved_data.sources.capture_filters is None:
                    raise WorkspaceError("The saved source filter is unavailable")
                filters = self.saved_data.sources.capture_filters.read(
                    definition.project_id, capture
                )
            datasets.append(
                {
                    "selection": capture.to_json(),
                    "snapshot": snapshot.to_json(),
                    "manifest": manifest.to_json(),
                    "filters": [item.to_dict() for item in filters],
                    "data_member": f"payload/model-{index}.parquet",
                    "origin_member": origin_name,
                }
            )
            ordered_snapshots.append(snapshot)
        metadata = canonical_json(
            {
                "format_version": 1,
                "label": definition.label,
                "all_fields": json.loads(definition.config_json).get("all_fields")
                is True,
                "source_system": state.source_system,
                "source_mode": state.odoo_connection_mode.value,
                "source_url": state.odoo_base_url,
                "source_database": state.odoo_database,
                "company": state.odoo_company.to_json(),
                "schema": schema.to_json(),
                "scope": scope.to_json() if scope is not None else None,
                "source_selection": source.to_json(),
                "datasets": datasets,
            }
        ).encode("utf-8")
        return metadata, tuple(ordered_snapshots), members

    def import_version(
        self,
        project_id: str,
        path: Path,
        *,
        password: str,
        actor: Actor,
    ) -> SavedOdooDataDefinition:
        """Verify the whole archive before admitting any new registry roots."""
        with tempfile.TemporaryDirectory(prefix="impodo-saved-admission-") as directory:
            contents = read_saved_odoo_package(
                Path(path), Path(directory) / "verified", password=password
            )
            paths = dict(contents.member_paths)
            try:
                payload = decode_payload(
                    contents.path("metadata/source.json").read_bytes(), paths
                )
                evidence = {item.name: item for item in contents.manifest.members}
                for dataset in payload.datasets:
                    member = evidence[dataset.data_member]
                    if (
                        member.sha256 != dataset.snapshot.parquet_sha256
                        or member.size_bytes != dataset.manifest.data_size_bytes
                    ):
                        raise WorkspaceError(
                            "Saved Odoo data bytes differ from their source manifest"
                        )
                    validate_source_snapshot_path(dataset.path, dataset.snapshot)
                    if (
                        pl.scan_parquet(dataset.path).select(pl.len()).collect().item()
                        != dataset.snapshot.row_count
                    ):
                        raise WorkspaceError("Saved Odoo data row counts differ")
            except (
                AttributeError,
                KeyError,
                TypeError,
                ValueError,
                OSError,
                RecursionError,
                pl.exceptions.PolarsError,
            ) as error:
                if isinstance(error, WorkspaceError):
                    raise
                raise WorkspaceError(
                    "The saved Odoo archive content is invalid"
                ) from error
            package_hash = content_hash(contents.manifest.to_dict())
            importer = OdooSavedDataArchiveImporter(
                saved_data=self.saved_data,
                workspace_factory=self.workspace_factory,
                sources=self.sources,
                schemas=self.schemas,
                provenance=self.provenance,
                artifacts=self.artifacts,
            )
            return importer.admit(
                project_id, payload, package_hash=package_hash, actor=actor
            )
