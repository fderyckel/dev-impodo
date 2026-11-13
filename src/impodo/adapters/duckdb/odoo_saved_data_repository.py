"""Persist saved Odoo configurations and complete data references in the registry.

Each command owns one bounded registry connection and transaction. Immutable
configuration and data rows retain their hashes; only the definition's current
pointers advance. Actual model values remain under DataVersion ownership.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import datetime

import duckdb

from impodo.domain.odoo_saved_data import (
    SavedOdooCaptureOperation,
    SavedOdooDataDefinition,
    SavedOdooDataVersion,
    SavedOdooRestoreBinding,
    SavedOdooRestoreProfile,
)
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationFoundationError,
    MigrationNotFoundError,
    MigrationOperationReplayError,
    MigrationStorageCompatibilityError,
    require_hash,
    require_revision,
    require_uuid,
)
from impodo.domain.serialization import canonical_json

from .migration_foundation_database import MigrationFoundationDatabase
from .schema.migration_registry import ensure_migration_registry_schema

_DEFINITION_COLUMNS = (
    "saved_data_id, project_id, revision, configuration_revision, "
    "latest_version, definition_json, content_hash"
)
_VERSION_COLUMNS = (
    "version_json, content_hash, project_id, saved_data_id, version_number, "
    "configuration_revision, workspace_id, data_version_id, operation_id"
)


class DuckDbSavedOdooDataRepository:
    """Retain reusable source definitions and their immutable captured versions."""

    def __init__(self, database: MigrationFoundationDatabase) -> None:
        self.database = database

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
        with self.database.connect(self.database.registry_path) as connection:
            ensure_migration_registry_schema(connection, self.database.registry_path)
            if write:
                connection.begin()
            try:
                yield connection
                if write:
                    connection.commit()
            except Exception as error:
                if write:
                    connection.rollback()
                if isinstance(
                    error, (duckdb.TransactionException, duckdb.ConstraintException)
                ):
                    raise MigrationConflictError(
                        "Saved Odoo data changed; reload before retrying"
                    ) from error
                raise

    def create_definition(
        self,
        definition: SavedOdooDataDefinition,
        *,
        source_binding_hash: str | None,
    ) -> SavedOdooDataDefinition:
        """Create initial capture meaning; an exact replay returns its existing row."""

        if source_binding_hash is not None:
            require_hash(source_binding_hash, "source_binding_hash")
        if (
            definition.revision != 1
            or definition.configuration_revision != 1
            or definition.latest_version is not None
        ):
            raise MigrationFoundationError(
                "A saved configuration must start at revision 1"
            )
        with self._connect(write=True) as connection:
            existing = connection.execute(
                f"SELECT {_DEFINITION_COLUMNS} FROM saved_odoo_data_definition WHERE saved_data_id = ?",
                [definition.saved_data_id],
            ).fetchone()
            if existing is not None:
                current = self._definition(existing)
                initial = connection.execute(
                    """SELECT definition_json, content_hash
                       FROM saved_odoo_data_configuration
                       WHERE saved_data_id = ? AND configuration_revision = 1""",
                    [definition.saved_data_id],
                ).fetchone()
                if initial is None:
                    raise self._corruption()
                created = self._decode_definition(str(initial[0]), str(initial[1]))
                if created != definition:
                    raise MigrationOperationReplayError(
                        "Saved configuration identity is already used"
                    )
                if (
                    source_binding_hash is not None
                    and self._source_binding(connection, definition.saved_data_id, 1)
                    != source_binding_hash
                ):
                    raise MigrationOperationReplayError(
                        "Saved configuration source review differs; rebind its source"
                    )
                return current
            self._workspace(
                connection, definition.project_id, definition.source_workspace_id
            )
            connection.execute(
                "INSERT INTO saved_odoo_data_identity VALUES (?, ?)",
                [definition.saved_data_id, definition.project_id],
            )
            connection.execute(
                "INSERT INTO saved_odoo_data_definition VALUES (?, ?, ?, ?, ?, ?, ?)",
                self._definition_values(definition),
            )
            self._insert_configuration(connection, definition)
            self._insert_source_binding(connection, definition, source_binding_hash)
        return definition

    def get_definition(
        self, project_id: str, saved_data_id: str
    ) -> SavedOdooDataDefinition:
        """Read one definition inside the exact requested Project."""

        self._identifiers(project_id, saved_data_id)
        with self._connect() as connection:
            return self._get_definition(connection, project_id, saved_data_id)

    def list_definitions(
        self, project_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[SavedOdooDataDefinition, ...]:
        """List a bounded page without opening workspaces or captured artifacts."""

        require_uuid(project_id, "project_id")
        self._page(limit, offset)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT {_DEFINITION_COLUMNS} FROM saved_odoo_data_definition
                    WHERE project_id = ? ORDER BY saved_data_id LIMIT ? OFFSET ?""",
                [project_id, limit, offset],
            ).fetchall()
        return tuple(self._definition(row) for row in rows)

    def save_definition(
        self,
        definition: SavedOdooDataDefinition,
        *,
        expected_revision: int,
        source_binding_hash: str | None,
    ) -> SavedOdooDataDefinition:
        """Append configuration meaning with an optimistic current-pointer update."""

        require_revision(expected_revision)
        if source_binding_hash is not None:
            require_hash(source_binding_hash, "source_binding_hash")
        if definition.revision != expected_revision:
            raise MigrationConflictError("Saved configuration revision is stale")
        with self._connect(write=True) as connection:
            current = self._get_definition(
                connection, definition.project_id, definition.saved_data_id
            )
            if current.revision != expected_revision:
                raise MigrationConflictError("Saved configuration revision is stale")
            immutable = (
                "project_id",
                "source_target_hash",
                "source_company_hash",
                "odoo_major",
                "created_at",
            )
            if any(
                getattr(current, name) != getattr(definition, name)
                for name in immutable
            ):
                raise MigrationFoundationError(
                    "A different source needs a separate saved configuration"
                )
            if (
                current.latest_version != definition.latest_version
                or current.configuration_revision != definition.configuration_revision
            ):
                raise MigrationConflictError("Saved configuration pointers changed")
            self._workspace(
                connection, definition.project_id, definition.source_workspace_id
            )
            if (
                current.label == definition.label
                and current.config_json == definition.config_json
                and current.source_workspace_id == definition.source_workspace_id
            ):
                return current
            updated = replace(
                definition,
                revision=expected_revision + 1,
                configuration_revision=current.configuration_revision + 1,
            )
            self._insert_configuration(connection, updated)
            self._insert_source_binding(connection, updated, source_binding_hash)
            self._update_definition(connection, updated, expected_revision)
        return updated

    def rebind_source_definition(
        self,
        definition: SavedOdooDataDefinition,
        *,
        expected_revision: int,
        source_binding_hash: str,
    ) -> SavedOdooDataDefinition:
        """Accept reviewed replacement source meaning without moving saved data."""

        require_revision(expected_revision)
        require_hash(source_binding_hash, "source_binding_hash")
        if definition.revision != expected_revision:
            raise MigrationConflictError("Saved configuration revision is stale")
        with self._connect(write=True) as connection:
            current = self._get_definition(
                connection, definition.project_id, definition.saved_data_id
            )
            if current.revision != expected_revision:
                raise MigrationConflictError("Saved configuration revision is stale")
            if any(
                getattr(current, name) != getattr(definition, name)
                for name in (
                    "project_id",
                    "source_target_hash",
                    "odoo_major",
                    "created_at",
                )
            ):
                raise MigrationFoundationError(
                    "Source rebind must keep the same database and Odoo major"
                )
            if (
                current.latest_version != definition.latest_version
                or current.configuration_revision != definition.configuration_revision
            ):
                raise MigrationConflictError("Saved configuration pointers changed")
            if current.source_workspace_id == definition.source_workspace_id:
                raise MigrationFoundationError(
                    "Review the replacement source in a fresh workspace before rebinding"
                )
            self._workspace(
                connection, definition.project_id, definition.source_workspace_id
            )
            updated = replace(
                definition,
                revision=expected_revision + 1,
                configuration_revision=current.configuration_revision + 1,
            )
            self._insert_configuration(connection, updated)
            self._insert_source_binding(connection, updated, source_binding_hash)
            self._update_definition(connection, updated, expected_revision)
        return updated

    def get_configuration_source_binding(
        self, project_id: str, saved_data_id: str, configuration_revision: int
    ) -> str | None:
        """Read exact source-generation review without rewriting version JSON."""

        self._identifiers(project_id, saved_data_id)
        require_revision(configuration_revision, "configuration_revision")
        with self._connect() as connection:
            self._get_definition(connection, project_id, saved_data_id)
            return self._source_binding(
                connection, saved_data_id, configuration_revision
            )

    @staticmethod
    def _source_binding(
        connection: duckdb.DuckDBPyConnection,
        saved_data_id: str,
        configuration_revision: int,
    ) -> str | None:
        row = connection.execute(
            """SELECT source_binding_hash FROM saved_odoo_source_binding
               WHERE saved_data_id = ? AND configuration_revision = ?""",
            [saved_data_id, configuration_revision],
        ).fetchone()
        return require_hash(str(row[0]), "source_binding_hash") if row else None

    @staticmethod
    def _insert_source_binding(
        connection: duckdb.DuckDBPyConnection,
        definition: SavedOdooDataDefinition,
        source_binding_hash: str | None,
    ) -> None:
        if source_binding_hash is not None:
            reviewed = connection.execute(
                """SELECT 1 FROM odoo_workspace_database_binding b
                   JOIN odoo_database_generation_current c
                     ON c.organization_id = b.organization_id
                    AND c.scope_hash = b.scope_hash
                    AND c.generation_hash = b.generation_hash
                   WHERE b.content_hash = ? AND b.project_id = ?
                     AND b.workspace_id = ? AND b.role = 'SOURCE'""",
                [
                    source_binding_hash,
                    definition.project_id,
                    definition.source_workspace_id,
                ],
            ).fetchone()
            if reviewed is None:
                raise MigrationConflictError(
                    "The source database generation changed before saving its review; review and rebind the source again"
                )
            connection.execute(
                "INSERT INTO saved_odoo_source_binding VALUES (?, ?, ?)",
                [
                    definition.saved_data_id,
                    definition.configuration_revision,
                    source_binding_hash,
                ],
            )

    def get_configuration(
        self, project_id: str, saved_data_id: str, configuration_revision: int
    ) -> SavedOdooDataDefinition:
        """Read immutable capture meaning independently of its current version."""

        self._identifiers(project_id, saved_data_id)
        require_revision(configuration_revision, "configuration_revision")
        with self._connect() as connection:
            self._get_definition(connection, project_id, saved_data_id)
            row = connection.execute(
                """SELECT definition_json, content_hash FROM saved_odoo_data_configuration
                   WHERE saved_data_id = ? AND configuration_revision = ?""",
                [saved_data_id, configuration_revision],
            ).fetchone()
            if row is None:
                raise MigrationNotFoundError(
                    "Saved configuration revision was not found"
                )
            definition = self._decode_definition(str(row[0]), str(row[1]))
            if (
                definition.saved_data_id != saved_data_id
                or definition.project_id != project_id
                or definition.configuration_revision != configuration_revision
            ):
                raise self._corruption()
            return definition

    def list_versions(
        self, project_id: str, saved_data_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[SavedOdooDataVersion, ...]:
        """Read one bounded version page under its owning saved configuration."""

        self._identifiers(project_id, saved_data_id)
        self._page(limit, offset)
        with self._connect() as connection:
            self._get_definition(connection, project_id, saved_data_id)
            rows = connection.execute(
                f"""SELECT {_VERSION_COLUMNS} FROM saved_odoo_data_version
                    WHERE project_id = ? AND saved_data_id = ?
                    ORDER BY version_number DESC LIMIT ? OFFSET ?""",
                [project_id, saved_data_id, limit, offset],
            ).fetchall()
        versions = tuple(self._version(row) for row in rows)
        if any(
            item.project_id != project_id or item.saved_data_id != saved_data_id
            for item in versions
        ):
            raise self._corruption()
        return versions

    def get_version(
        self, project_id: str, saved_data_id: str, version_number: int
    ) -> SavedOdooDataVersion:
        """Read one hash-checked immutable complete data version."""

        self._identifiers(project_id, saved_data_id)
        require_revision(version_number, "version_number")
        with self._connect() as connection:
            self._get_definition(connection, project_id, saved_data_id)
            return self._get_version(
                connection, project_id, saved_data_id, version_number
            )

    def append_version(
        self,
        version: SavedOdooDataVersion,
        *,
        expected_revision: int,
        source_binding_hash: str | None,
    ) -> tuple[SavedOdooDataDefinition, SavedOdooDataVersion]:
        """Publish one complete version and advance its pointer in one transaction."""

        require_revision(expected_revision)
        if source_binding_hash is not None:
            require_hash(source_binding_hash, "source_binding_hash")
        with self._connect(write=True) as connection:
            current = self._get_definition(
                connection, version.project_id, version.saved_data_id
            )
            replay = connection.execute(
                f"SELECT {_VERSION_COLUMNS} FROM saved_odoo_data_version WHERE operation_id = ?",
                [version.operation_id],
            ).fetchone()
            if replay is not None:
                previous = self._version(replay)
                if previous != version:
                    raise MigrationOperationReplayError(
                        "Capture operation was reused for different data"
                    )
                return current, previous
            if current.revision != expected_revision:
                raise MigrationConflictError(
                    "Saved configuration changed during capture"
                )
            if version.configuration_revision != current.configuration_revision:
                raise MigrationConflictError(
                    "Capture used an earlier configuration revision"
                )
            if version.version_number != (current.latest_version or 0) + 1:
                raise MigrationConflictError("Saved data version number is stale")
            if source_binding_hash is not None:
                reviewed = connection.execute(
                    """SELECT 1 FROM saved_odoo_source_binding s
                       JOIN odoo_workspace_database_binding b
                         ON b.content_hash = s.source_binding_hash
                       JOIN odoo_database_generation_current c
                         ON c.organization_id = b.organization_id
                        AND c.scope_hash = b.scope_hash
                        AND c.generation_hash = b.generation_hash
                       WHERE s.saved_data_id = ? AND s.configuration_revision = ?
                         AND s.source_binding_hash = ? AND b.project_id = ?
                         AND b.workspace_id = ? AND b.role = 'SOURCE'""",
                    [
                        version.saved_data_id,
                        version.configuration_revision,
                        source_binding_hash,
                        version.project_id,
                        current.source_workspace_id,
                    ],
                ).fetchone()
                if reviewed is None:
                    raise MigrationConflictError(
                        "The source database generation changed during capture. Review and rebind its source; the previous saved version remains available"
                    )
            self._workspace(
                connection,
                version.project_id,
                version.workspace_id,
                data_version_id=version.data_version_id,
                frozen=True,
            )
            connection.execute(
                "INSERT INTO saved_odoo_data_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    version.saved_data_id,
                    version.project_id,
                    version.version_number,
                    version.configuration_revision,
                    version.workspace_id,
                    version.data_version_id,
                    version.operation_id,
                    canonical_json(asdict(version)),
                    version.content_hash,
                ],
            )
            updated = replace(
                current,
                latest_version=version.version_number,
                revision=current.revision + 1,
                updated_at=max(current.updated_at, version.captured_at),
            )
            self._update_definition(connection, updated, expected_revision)
        return updated, version

    def get_version_for_operation(
        self, project_id: str, saved_data_id: str, operation_id: str
    ) -> SavedOdooDataVersion | None:
        """Resolve a completed capture retry without recapturing source records."""

        self._identifiers(project_id, saved_data_id)
        require_uuid(operation_id, "operation_id")
        with self._connect() as connection:
            self._get_definition(connection, project_id, saved_data_id)
            row = connection.execute(
                f"SELECT {_VERSION_COLUMNS} FROM saved_odoo_data_version WHERE operation_id = ?",
                [operation_id],
            ).fetchone()
        if row is None:
            return None
        version = self._version(row)
        if version.project_id != project_id or version.saved_data_id != saved_data_id:
            raise MigrationOperationReplayError(
                "Capture operation belongs to another saved configuration"
            )
        return version

    def retained_data_version(self, data_version_id: str) -> bool:
        """Tell authorized cleanup whether immutable saved references retain data."""

        require_uuid(data_version_id, "data_version_id")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT EXISTS(SELECT 1 FROM saved_odoo_data_version WHERE data_version_id = ?)
                   OR EXISTS(SELECT 1 FROM saved_odoo_capture_operation WHERE data_version_id = ?)""",
                [data_version_id, data_version_id],
            ).fetchone()
        return bool(row and row[0])

    def bind_capture_operation(
        self, operation: SavedOdooCaptureOperation
    ) -> SavedOdooCaptureOperation:
        """Retain one exact refresh workspace before remote publication begins."""

        with self._connect(write=True) as connection:
            current = self._get_definition(
                connection, operation.project_id, operation.saved_data_id
            )
            existing = self._capture_operation(connection, operation.operation_id)
            if existing is not None:
                if existing != operation:
                    raise MigrationOperationReplayError(
                        "Capture operation already belongs to another request"
                    )
                return existing
            if (
                current.revision != operation.expected_revision
                or current.configuration_revision != operation.configuration_revision
            ):
                raise MigrationConflictError(
                    "Saved configuration changed before capture started"
                )
            self._workspace(
                connection,
                operation.project_id,
                operation.workspace_id,
                data_version_id=operation.data_version_id,
            )
            connection.execute(
                "INSERT INTO saved_odoo_capture_operation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    operation.operation_id,
                    operation.project_id,
                    operation.saved_data_id,
                    operation.expected_revision,
                    operation.configuration_revision,
                    operation.workspace_id,
                    operation.data_version_id,
                    canonical_json(asdict(operation)),
                    operation.content_hash,
                ],
            )
        return operation

    def get_capture_operation(
        self, project_id: str, saved_data_id: str, operation_id: str
    ) -> SavedOdooCaptureOperation | None:
        """Recover a pending capture without provisioning a second workspace."""

        self._identifiers(project_id, saved_data_id)
        require_uuid(operation_id, "operation_id")
        with self._connect() as connection:
            self._get_definition(connection, project_id, saved_data_id)
            operation = self._capture_operation(connection, operation_id)
        if operation is not None and (
            operation.project_id != project_id
            or operation.saved_data_id != saved_data_id
        ):
            raise MigrationOperationReplayError(
                "Capture operation belongs to another saved configuration"
            )
        return operation

    def _capture_operation(
        self, connection: duckdb.DuckDBPyConnection, operation_id: str
    ) -> SavedOdooCaptureOperation | None:
        row = connection.execute(
            """SELECT operation_json, content_hash, operation_id, project_id,
               saved_data_id, expected_revision, configuration_revision, workspace_id,
               data_version_id FROM saved_odoo_capture_operation WHERE operation_id = ?""",
            [operation_id],
        ).fetchone()
        if row is None:
            return None
        payload = self._payload(
            str(row[0]), {item.name for item in fields(SavedOdooCaptureOperation)}
        )
        try:
            operation = SavedOdooCaptureOperation(
                operation_id=str(payload["operation_id"]),
                project_id=str(payload["project_id"]),
                saved_data_id=str(payload["saved_data_id"]),
                expected_revision=int(str(payload["expected_revision"])),
                configuration_revision=int(str(payload["configuration_revision"])),
                workspace_id=str(payload["workspace_id"]),
                data_version_id=str(payload["data_version_id"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise self._corruption() from error
        if operation.content_hash != str(row[1]) or tuple(row[2:]) != (
            operation.operation_id,
            operation.project_id,
            operation.saved_data_id,
            operation.expected_revision,
            operation.configuration_revision,
            operation.workspace_id,
            operation.data_version_id,
        ):
            raise self._corruption()
        self._workspace(
            connection,
            operation.project_id,
            operation.workspace_id,
            data_version_id=operation.data_version_id,
        )
        return operation

    def bind_restore(self, binding: SavedOdooRestoreBinding) -> SavedOdooRestoreBinding:
        """Bind a fresh workspace to exactly one retained source generation."""

        with self._connect(write=True) as connection:
            existing = self._binding(connection, binding.workspace_id)
            if existing is not None:
                if existing != binding:
                    raise MigrationOperationReplayError(
                        "Restore workspace is already bound"
                    )
                return existing
            version = self._get_version(
                connection,
                binding.project_id,
                binding.saved_data_id,
                binding.version_number,
            )
            if (
                version.workspace_id != binding.source_workspace_id
                or version.data_version_id != binding.data_version_id
            ):
                raise MigrationFoundationError(
                    "Restore source does not match the saved version"
                )
            self._workspace(
                connection,
                binding.project_id,
                binding.workspace_id,
                data_version_id=binding.data_version_id,
                frozen=True,
            )
            connection.execute(
                "INSERT INTO saved_odoo_restore_binding VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    binding.workspace_id,
                    binding.project_id,
                    binding.saved_data_id,
                    binding.version_number,
                    binding.source_workspace_id,
                    binding.data_version_id,
                    binding.generation_id,
                    canonical_json(asdict(binding)),
                    binding.content_hash,
                ],
            )
        return binding

    def get_restore_binding(self, workspace_id: str) -> SavedOdooRestoreBinding | None:
        """Read a restore binding after the caller has authorized the workspace."""

        require_uuid(workspace_id, "workspace_id")
        with self._connect() as connection:
            return self._binding(connection, workspace_id)

    def list_restore_bindings(
        self, project_id: str, saved_data_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[SavedOdooRestoreBinding, ...]:
        """Page all restore generations for a saved definition in one bulk read."""

        self._identifiers(project_id, saved_data_id)
        self._page(limit, offset)
        with self._connect() as connection:
            self._get_definition(connection, project_id, saved_data_id)
            rows = connection.execute(
                """SELECT binding_json, content_hash, workspace_id FROM saved_odoo_restore_binding
                   WHERE project_id = ? AND saved_data_id = ?
                   ORDER BY json_extract_string(binding_json, '$.created_at'), workspace_id
                   LIMIT ? OFFSET ?""",
                [project_id, saved_data_id, limit, offset],
            ).fetchall()
        bindings = tuple(
            self._decode_binding(str(row[0]), str(row[1]), str(row[2])) for row in rows
        )
        if any(
            item.project_id != project_id or item.saved_data_id != saved_data_id
            for item in bindings
        ):
            raise self._corruption()
        return bindings

    def list_project_restore_bindings(
        self, project_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[SavedOdooRestoreBinding, ...]:
        """Page journals across saved configurations in an authorized Project."""

        require_uuid(project_id, "project_id")
        self._page(limit, offset)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT binding_json, content_hash, workspace_id FROM saved_odoo_restore_binding
                   WHERE project_id = ? ORDER BY workspace_id LIMIT ? OFFSET ?""",
                [project_id, limit, offset],
            ).fetchall()
        bindings = tuple(
            self._decode_binding(str(row[0]), str(row[1]), str(row[2])) for row in rows
        )
        if any(item.project_id != project_id for item in bindings):
            raise self._corruption()
        return bindings

    def save_restore_profile(
        self, profile: SavedOdooRestoreProfile, *, expected_revision: int | None
    ) -> SavedOdooRestoreProfile:
        """Remember destination choices under optimistic revision control."""

        if expected_revision is not None:
            require_revision(expected_revision)
            if profile.revision != expected_revision:
                raise MigrationConflictError("Restore profile revision is stale")
        elif profile.revision != 1:
            raise MigrationFoundationError("A restore profile must start at revision 1")
        with self._connect(write=True) as connection:
            self._get_definition(connection, profile.project_id, profile.saved_data_id)
            if profile.credential_workspace_id is not None:
                self._workspace(
                    connection, profile.project_id, profile.credential_workspace_id
                )
            row = connection.execute(
                """SELECT profile_json, content_hash, profile_id, project_id,
                   saved_data_id, revision, credential_workspace_id
                   FROM saved_odoo_restore_profile WHERE profile_id = ?""",
                [profile.profile_id],
            ).fetchone()
            if row is None:
                if expected_revision is not None:
                    raise MigrationNotFoundError("Restore profile was not found")
                connection.execute(
                    "INSERT INTO saved_odoo_restore_profile VALUES (?, ?, ?, ?, ?, ?, ?)",
                    self._profile_values(profile),
                )
                return profile
            current = self._profile(row)
            if expected_revision is None:
                if current != profile:
                    raise MigrationOperationReplayError(
                        "Restore profile identity is already used"
                    )
                return current
            if current.revision != expected_revision:
                raise MigrationConflictError(
                    "Restore profile changed in another request"
                )
            if (
                current.project_id != profile.project_id
                or current.saved_data_id != profile.saved_data_id
                or current.created_at != profile.created_at
            ):
                raise MigrationFoundationError(
                    "Restore profile ownership cannot change"
                )
            updated = replace(profile, revision=expected_revision + 1)
            updated_row = connection.execute(
                """UPDATE saved_odoo_restore_profile SET revision = ?, credential_workspace_id = ?,
                   profile_json = ?, content_hash = ? WHERE profile_id = ?
                   AND project_id = ? AND saved_data_id = ? AND revision = ? RETURNING profile_id""",
                [
                    updated.revision,
                    updated.credential_workspace_id,
                    canonical_json(asdict(updated)),
                    updated.content_hash,
                    updated.profile_id,
                    updated.project_id,
                    updated.saved_data_id,
                    expected_revision,
                ],
            ).fetchone()
            if updated_row is None:
                raise MigrationConflictError("Restore profile revision is stale")
        return updated

    def get_restore_profile(
        self, project_id: str, saved_data_id: str, profile_id: str
    ) -> SavedOdooRestoreProfile:
        """Read one remembered destination inside its exact capture definition."""

        self._identifiers(project_id, saved_data_id)
        require_uuid(profile_id, "profile_id")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT profile_json, content_hash, profile_id, project_id,
                   saved_data_id, revision, credential_workspace_id FROM saved_odoo_restore_profile
                   WHERE project_id = ? AND saved_data_id = ? AND profile_id = ?""",
                [project_id, saved_data_id, profile_id],
            ).fetchone()
        if row is None:
            raise MigrationNotFoundError(
                "Restore profile was not found in this saved configuration"
            )
        return self._profile(row)

    def list_restore_profiles(
        self, project_id: str, saved_data_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[SavedOdooRestoreProfile, ...]:
        """List bounded destination settings without contacting Odoo or secrets."""

        self._identifiers(project_id, saved_data_id)
        self._page(limit, offset)
        with self._connect() as connection:
            self._get_definition(connection, project_id, saved_data_id)
            rows = connection.execute(
                """SELECT profile_json, content_hash, profile_id, project_id,
                   saved_data_id, revision, credential_workspace_id FROM saved_odoo_restore_profile
                   WHERE project_id = ? AND saved_data_id = ? ORDER BY profile_id LIMIT ? OFFSET ?""",
                [project_id, saved_data_id, limit, offset],
            ).fetchall()
        return tuple(self._profile(row) for row in rows)

    @staticmethod
    def _profile_values(profile: SavedOdooRestoreProfile) -> list[object]:
        return [
            profile.profile_id,
            profile.project_id,
            profile.saved_data_id,
            profile.revision,
            profile.credential_workspace_id,
            canonical_json(asdict(profile)),
            profile.content_hash,
        ]

    def _profile(self, row: tuple[object, ...]) -> SavedOdooRestoreProfile:
        payload = self._payload(
            str(row[0]), {item.name for item in fields(SavedOdooRestoreProfile)}
        )
        try:
            profile = SavedOdooRestoreProfile(
                profile_id=str(payload["profile_id"]),
                project_id=str(payload["project_id"]),
                saved_data_id=str(payload["saved_data_id"]),
                label=str(payload["label"]),
                revision=int(str(payload["revision"])),
                connection_mode=str(payload["connection_mode"]),
                base_url=str(payload["base_url"]),
                database=str(payload["database"]),
                company_name=str(payload["company_name"]),
                credential_workspace_id=None
                if payload["credential_workspace_id"] is None
                else str(payload["credential_workspace_id"]),
                settings_json=str(payload["settings_json"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
                updated_at=datetime.fromisoformat(str(payload["updated_at"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise self._corruption() from error
        if profile.content_hash != str(row[1]) or tuple(
            self._profile_values(profile)[:5]
        ) != tuple(row[2:]):
            raise self._corruption()
        return profile

    def _binding(
        self, connection: duckdb.DuckDBPyConnection, workspace_id: str
    ) -> SavedOdooRestoreBinding | None:
        row = connection.execute(
            "SELECT binding_json, content_hash FROM saved_odoo_restore_binding WHERE workspace_id = ?",
            [workspace_id],
        ).fetchone()
        if row is None:
            return None
        binding = self._decode_binding(str(row[0]), str(row[1]), workspace_id)
        source = self._get_version(
            connection,
            binding.project_id,
            binding.saved_data_id,
            binding.version_number,
        )
        if (
            source.workspace_id != binding.source_workspace_id
            or source.data_version_id != binding.data_version_id
        ):
            raise self._corruption()
        self._workspace(
            connection,
            binding.project_id,
            binding.workspace_id,
            data_version_id=binding.data_version_id,
            frozen=True,
        )
        return binding

    def _decode_binding(
        self, encoded: str, expected_hash: str, workspace_id: str
    ) -> SavedOdooRestoreBinding:
        payload = self._payload(
            encoded, {item.name for item in fields(SavedOdooRestoreBinding)}
        )
        try:
            binding = SavedOdooRestoreBinding(
                project_id=str(payload["project_id"]),
                workspace_id=str(payload["workspace_id"]),
                saved_data_id=str(payload["saved_data_id"]),
                version_number=int(str(payload["version_number"])),
                source_workspace_id=str(payload["source_workspace_id"]),
                data_version_id=str(payload["data_version_id"]),
                generation_id=str(payload["generation_id"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise self._corruption() from error
        if (
            binding.workspace_id != workspace_id
            or binding.content_hash != expected_hash
        ):
            raise self._corruption()
        return binding

    def _get_definition(
        self, connection: duckdb.DuckDBPyConnection, project_id: str, saved_data_id: str
    ) -> SavedOdooDataDefinition:
        row = connection.execute(
            f"SELECT {_DEFINITION_COLUMNS} FROM saved_odoo_data_definition WHERE project_id = ? AND saved_data_id = ?",
            [project_id, saved_data_id],
        ).fetchone()
        if row is None:
            raise MigrationNotFoundError(
                "Saved Odoo configuration was not found in this project"
            )
        return self._definition(row)

    def _get_version(
        self,
        connection: duckdb.DuckDBPyConnection,
        project_id: str,
        saved_data_id: str,
        version_number: int,
    ) -> SavedOdooDataVersion:
        row = connection.execute(
            f"SELECT {_VERSION_COLUMNS} FROM saved_odoo_data_version WHERE project_id = ? AND saved_data_id = ? AND version_number = ?",
            [project_id, saved_data_id, version_number],
        ).fetchone()
        if row is None:
            raise MigrationNotFoundError("Saved Odoo data version was not found")
        version = self._version(row)
        if (version.project_id, version.saved_data_id, version.version_number) != (
            project_id,
            saved_data_id,
            version_number,
        ):
            raise self._corruption()
        return version

    @staticmethod
    def _workspace(
        connection: duckdb.DuckDBPyConnection,
        project_id: str,
        workspace_id: str,
        *,
        data_version_id: str | None = None,
        frozen: bool = False,
    ) -> None:
        row = connection.execute(
            """SELECT w.project_id, w.data_version_id, d.state, d.project_id
               FROM migration_workspace w JOIN data_version d ON d.data_version_id = w.data_version_id
               WHERE w.workspace_id = ?""",
            [workspace_id],
        ).fetchone()
        if row is None or str(row[0]) != project_id or str(row[3]) != project_id:
            raise MigrationNotFoundError(
                "Saved Odoo workspace was not found in this project"
            )
        if data_version_id is not None and str(row[1]) != data_version_id:
            raise MigrationFoundationError(
                "Saved Odoo workspace belongs to another data version"
            )
        if frozen and str(row[2]) != "FROZEN":
            raise MigrationFoundationError(
                "Only a complete accepted data version can be saved"
            )

    @staticmethod
    def _definition_values(definition: SavedOdooDataDefinition) -> list[object]:
        return [
            definition.saved_data_id,
            definition.project_id,
            definition.revision,
            definition.configuration_revision,
            definition.latest_version,
            canonical_json(asdict(definition)),
            definition.content_hash,
        ]

    def _update_definition(
        self,
        connection: duckdb.DuckDBPyConnection,
        definition: SavedOdooDataDefinition,
        expected_revision: int,
    ) -> None:
        values = self._definition_values(definition)
        row = connection.execute(
            """UPDATE saved_odoo_data_definition SET revision = ?, configuration_revision = ?,
               latest_version = ?, definition_json = ?, content_hash = ?
               WHERE saved_data_id = ? AND project_id = ? AND revision = ? RETURNING saved_data_id""",
            [
                *values[2:],
                definition.saved_data_id,
                definition.project_id,
                expected_revision,
            ],
        ).fetchone()
        if row is None:
            raise MigrationConflictError("Saved Odoo data revision is stale")

    @staticmethod
    def _insert_configuration(
        connection: duckdb.DuckDBPyConnection, definition: SavedOdooDataDefinition
    ) -> None:
        connection.execute(
            "INSERT INTO saved_odoo_data_configuration VALUES (?, ?, ?, ?)",
            [
                definition.saved_data_id,
                definition.configuration_revision,
                canonical_json(asdict(definition)),
                definition.content_hash,
            ],
        )

    def _definition(self, row: tuple[object, ...]) -> SavedOdooDataDefinition:
        definition = self._decode_definition(str(row[5]), str(row[6]))
        if tuple(self._definition_values(definition)[:5]) != tuple(row[:5]):
            raise self._corruption()
        return definition

    def _decode_definition(
        self, encoded: str, expected_hash: str
    ) -> SavedOdooDataDefinition:
        payload = self._payload(
            encoded, {item.name for item in fields(SavedOdooDataDefinition)}
        )
        try:
            definition = SavedOdooDataDefinition(
                saved_data_id=str(payload["saved_data_id"]),
                project_id=str(payload["project_id"]),
                label=str(payload["label"]),
                revision=int(str(payload["revision"])),
                source_workspace_id=str(payload["source_workspace_id"]),
                source_target_hash=str(payload["source_target_hash"]),
                source_company_hash=str(payload["source_company_hash"]),
                odoo_major=int(str(payload["odoo_major"])),
                config_json=str(payload["config_json"]),
                latest_version=None
                if payload["latest_version"] is None
                else int(str(payload["latest_version"])),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
                updated_at=datetime.fromisoformat(str(payload["updated_at"])),
                configuration_revision=int(str(payload["configuration_revision"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise self._corruption() from error
        if definition.content_hash != expected_hash:
            raise self._corruption()
        return definition

    def _decode_version(self, encoded: str, expected_hash: str) -> SavedOdooDataVersion:
        payload = self._payload(
            encoded, {item.name for item in fields(SavedOdooDataVersion)}
        )
        try:
            version = SavedOdooDataVersion(
                saved_data_id=str(payload["saved_data_id"]),
                project_id=str(payload["project_id"]),
                version_number=int(str(payload["version_number"])),
                configuration_revision=int(str(payload["configuration_revision"])),
                workspace_id=str(payload["workspace_id"]),
                data_version_id=str(payload["data_version_id"]),
                source_selection_hash=str(payload["source_selection_hash"]),
                manifest_hashes=tuple(
                    (str(pair[0]), str(pair[1]))
                    for pair in self._pairs(payload["manifest_hashes"])
                ),
                row_counts=tuple(
                    (str(pair[0]), int(str(pair[1])))
                    for pair in self._pairs(payload["row_counts"])
                ),
                captured_at=datetime.fromisoformat(str(payload["captured_at"])),
                operation_id=str(payload["operation_id"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise self._corruption() from error
        if version.content_hash != expected_hash:
            raise self._corruption()
        return version

    def _version(self, row: tuple[object, ...]) -> SavedOdooDataVersion:
        version = self._decode_version(str(row[0]), str(row[1]))
        if tuple(row[2:]) != (
            version.project_id,
            version.saved_data_id,
            version.version_number,
            version.configuration_revision,
            version.workspace_id,
            version.data_version_id,
            version.operation_id,
        ):
            raise self._corruption()
        return version

    def _payload(self, encoded: str, expected_keys: set[str]) -> dict[str, object]:
        try:
            raw: object = json.loads(encoded)
        except (ValueError, RecursionError) as error:
            raise self._corruption() from error
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise self._corruption()
        return {str(key): value for key, value in raw.items()}

    @staticmethod
    def _pairs(value: object) -> tuple[tuple[object, object], ...]:
        if not isinstance(value, list) or any(
            not isinstance(pair, list) or len(pair) != 2 for pair in value
        ):
            raise MigrationFoundationError("Saved Odoo model values are invalid")
        return tuple((pair[0], pair[1]) for pair in value)

    @staticmethod
    def _identifiers(project_id: str, saved_data_id: str) -> None:
        require_uuid(project_id, "project_id")
        require_uuid(saved_data_id, "saved_data_id")

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 500
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
        ):
            raise MigrationFoundationError("Saved Odoo data page is outside its bounds")

    def _corruption(self) -> MigrationStorageCompatibilityError:
        return MigrationStorageCompatibilityError(str(self.database.registry_path))
