"""Hardened DuckDB persistence for local migration projects."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Iterable, Iterator
from uuid import UUID

import duckdb

from .access import Actor
from .inspection import SourceFileCatalog, SourceInspectionError
from .projects import (
    ApprovalStatus,
    DataClassification,
    ExportStatus,
    MigrationProject,
    OdooConnectionMode,
    ProjectConflictError,
    ProjectNotFoundError,
    ProjectStatus,
    ProjectSummary,
    SourceFile,
    TargetEnvironment,
)


SCHEMA_VERSION = 4


class DuckDbProjectRepository:
    """Store a minimal registry plus one isolated database per project."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "registry.duckdb"
        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS project_registry (
                    project_id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at VARCHAR NOT NULL
                )
                """
            )

    def create(self, project: MigrationProject, *, actor: Actor) -> None:
        project_dir = self.project_directory(project.project_id)
        project_dir.mkdir(parents=False, exist_ok=False)
        for child in ("inbox", "staging", "snapshots", "reports", "audit"):
            (project_dir / child).mkdir()
        database_path = project_dir / "project.duckdb"
        with self._connect(database_path) as connection:
            self._initialize_project_database(connection)
            self._insert_project(connection, project)
            self._insert_audit(
                connection,
                project,
                event_type="PROJECT_CREATED",
                detail="",
                actor=actor,
            )
        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                INSERT INTO project_registry VALUES (?, ?, ?, ?, ?)
                """,
                [
                    project.project_id,
                    project.name,
                    project.status.value,
                    project.revision,
                    project.updated_at.isoformat(),
                ],
            )
    def get(self, project_id: str) -> MigrationProject:
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            row = connection.execute("SELECT * FROM project").fetchone()
            if row is None:
                raise ProjectNotFoundError("Project not found")
            columns = [item[0] for item in connection.description]
            data = dict(zip(columns, row, strict=True))
            source_rows = connection.execute(
                """
                SELECT file_id, display_name, stored_name, size_bytes, sha256,
                       received_at
                  FROM source_file
                 ORDER BY received_at, file_id
                """
            ).fetchall()
        return _project_from_rows(data, source_rows)

    def list(self) -> tuple[ProjectSummary, ...]:
        with self._connect(self.registry_path) as connection:
            rows = connection.execute(
                """
                SELECT project_id, name, status, revision, updated_at
                  FROM project_registry
                 ORDER BY updated_at DESC, project_id
                """
            ).fetchall()
        return tuple(
            ProjectSummary(
                project_id=row[0],
                name=row[1],
                status=ProjectStatus(row[2]),
                revision=row[3],
                updated_at=datetime.fromisoformat(row[4]),
            )
            for row in rows
        )

    def get_source_catalogs(
        self,
        project_id: str,
    ) -> tuple[SourceFileCatalog, ...]:
        """Load Phase B catalogs in the same order as registered source files."""

        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            rows = connection.execute(
                """
                SELECT catalog.catalog_json
                  FROM source_file AS source
                  JOIN source_catalog AS catalog
                    ON catalog.file_id = source.file_id
                 ORDER BY source.received_at, source.file_id
                """
            ).fetchall()
        return tuple(SourceFileCatalog.from_json(str(row[0])) for row in rows)

    def save_source_catalogs(
        self,
        project_id: str,
        catalogs: Iterable[SourceFileCatalog],
        *,
        actor: Actor,
    ) -> None:
        """Atomically replace the complete hash-bound catalog set."""

        catalog_set = tuple(catalogs)
        database_path = self.project_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            source_rows = connection.execute(
                "SELECT file_id, sha256 FROM source_file"
            ).fetchall()
            registered = {str(row[0]): str(row[1]) for row in source_rows}
            supplied = {
                catalog.file_id: catalog.source_sha256
                for catalog in catalog_set
            }
            if supplied != registered or len(supplied) != len(catalog_set):
                raise SourceInspectionError(
                    "Source catalogs do not match the registered project files"
                )
            revision_row = connection.execute(
                "SELECT revision FROM project"
            ).fetchone()
            if revision_row is None:
                raise ProjectNotFoundError("Project not found")

            connection.begin()
            try:
                connection.execute("DELETE FROM source_catalog")
                for catalog in catalog_set:
                    connection.execute(
                        """
                        INSERT INTO source_catalog
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            catalog.file_id,
                            catalog.source_sha256,
                            catalog.contract_version,
                            catalog.inspected_at.isoformat(),
                            catalog.to_json(),
                        ],
                    )
                connection.execute(
                    """
                    INSERT INTO audit_event (
                        event_id, event_type, project_revision, occurred_at,
                        detail, actor_issuer, actor_subject, actor_display_name
                    )
                    VALUES (nextval('audit_event_sequence'), ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        "SOURCE_FILES_INSPECTED",
                        int(revision_row[0]),
                        datetime.now(timezone.utc).isoformat(),
                        f"{len(catalog_set)} source file(s)",
                        actor.identity.issuer,
                        actor.identity.subject_id,
                        actor.identity.display_name,
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save(
        self,
        project: MigrationProject,
        *,
        expected_revision: int,
        event_type: str,
        event_detail: str,
        actor: Actor,
    ) -> None:
        database_path = self.project_directory(project.project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision FROM project"
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                self._update_project(connection, project)
                self._insert_audit(
                    connection,
                    project,
                    event_type=event_type,
                    detail=event_detail,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._update_registry(project)
        if project.status is ProjectStatus.REGISTERED:
            self._write_registration_manifest(project)

    def add_source_file(
        self,
        project: MigrationProject,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Insert one immutable source row without rewriting existing evidence."""

        database_path = self.project_directory(project.project_id) / "project.duckdb"
        if not database_path.is_file():
            raise ProjectNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._migrate_project_database(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT revision FROM project"
                ).fetchone()
                if current is None:
                    raise ProjectNotFoundError("Project not found")
                if current[0] != expected_revision:
                    raise ProjectConflictError(
                        "The project was modified by another request"
                    )
                self._update_project(connection, project)
                connection.execute(
                    """
                    INSERT INTO source_file VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        source_file.file_id,
                        source_file.display_name,
                        source_file.stored_name,
                        source_file.size_bytes,
                        source_file.sha256,
                        source_file.received_at.isoformat(),
                    ],
                )
                self._insert_audit(
                    connection,
                    project,
                    event_type="SOURCE_FILE_ADDED",
                    detail=source_file.display_name,
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self._update_registry(project)

    def project_directory(self, project_id: str) -> Path:
        try:
            canonical = str(UUID(project_id))
        except (ValueError, AttributeError) as error:
            raise ProjectNotFoundError("Invalid project identifier") from error
        target = (self.root / canonical).resolve()
        if target.parent != self.root:
            raise ProjectNotFoundError("Invalid project identifier")
        return target

    def _update_registry(self, project: MigrationProject) -> None:
        with self._connect(self.registry_path) as connection:
            connection.execute(
                """
                UPDATE project_registry
                   SET name = ?, status = ?, revision = ?, updated_at = ?
                 WHERE project_id = ?
                """,
                [
                    project.name,
                    project.status.value,
                    project.revision,
                    project.updated_at.isoformat(),
                    project.project_id,
                ],
            )

    @contextmanager
    def _connect(self, path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
        connection = duckdb.connect(
            str(path),
            config={
                "allow_community_extensions": "false",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
                "enable_external_access": "false",
                "lock_configuration": "true",
                "memory_limit": "256MB",
                "threads": "2",
            },
        )
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_project_database(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        connection.execute(
            f"""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES ({SCHEMA_VERSION});

            CREATE TABLE project (
                project_id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                source_system VARCHAR NOT NULL,
                export_status VARCHAR NOT NULL,
                export_date VARCHAR,
                description VARCHAR NOT NULL,
                data_manager VARCHAR NOT NULL,
                functional_owner VARCHAR NOT NULL,
                business_unit VARCHAR NOT NULL,
                data_classification VARCHAR NOT NULL,
                retention_days INTEGER NOT NULL,
                support_access BOOLEAN NOT NULL,
                odoo_connection_mode VARCHAR,
                target_environment VARCHAR,
                odoo_base_url VARCHAR NOT NULL,
                odoo_database VARCHAR NOT NULL,
                intended_applications VARCHAR NOT NULL,
                intended_models VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                revision INTEGER NOT NULL,
                created_at VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL,
                registered_at VARCHAR,
                mapping_version VARCHAR,
                current_run_id VARCHAR,
                approval_status VARCHAR NOT NULL
            );

            CREATE TABLE source_file (
                file_id VARCHAR PRIMARY KEY,
                display_name VARCHAR NOT NULL,
                stored_name VARCHAR NOT NULL,
                size_bytes BIGINT NOT NULL,
                sha256 VARCHAR NOT NULL,
                received_at VARCHAR NOT NULL
            );

            CREATE TABLE source_catalog (
                file_id VARCHAR PRIMARY KEY,
                source_sha256 VARCHAR NOT NULL,
                contract_version INTEGER NOT NULL,
                inspected_at VARCHAR NOT NULL,
                catalog_json VARCHAR NOT NULL
            );

            CREATE TABLE audit_event (
                event_id BIGINT PRIMARY KEY,
                event_type VARCHAR NOT NULL,
                project_revision INTEGER NOT NULL,
                occurred_at VARCHAR NOT NULL,
                detail VARCHAR NOT NULL,
                actor_issuer VARCHAR NOT NULL,
                actor_subject VARCHAR NOT NULL,
                actor_display_name VARCHAR NOT NULL
            );

            CREATE SEQUENCE audit_event_sequence START 1;
            """
        )

    def _insert_project(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: MigrationProject,
    ) -> None:
        connection.execute(
            f"INSERT INTO project VALUES ({', '.join('?' for _ in range(26))})",
            _project_values(project),
        )

    def _update_project(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: MigrationProject,
    ) -> None:
        connection.execute(
            """
            UPDATE project SET
                name = ?,
                source_system = ?,
                export_status = ?,
                export_date = ?,
                description = ?,
                data_manager = ?,
                functional_owner = ?,
                business_unit = ?,
                data_classification = ?,
                retention_days = ?,
                support_access = ?,
                odoo_connection_mode = ?,
                target_environment = ?,
                odoo_base_url = ?,
                odoo_database = ?,
                intended_applications = ?,
                intended_models = ?,
                status = ?,
                revision = ?,
                created_at = ?,
                updated_at = ?,
                registered_at = ?,
                mapping_version = ?,
                current_run_id = ?,
                approval_status = ?
            WHERE project_id = ?
            """,
            _project_values(project)[1:] + [project.project_id],
        )

    def _migrate_project_database(
        self,
        connection: duckdb.DuckDBPyConnection,
    ) -> None:
        row = connection.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            raise RuntimeError("Project database has no schema version")
        version = int(row[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                "Project database was created by a newer Impodo version"
            )
        if version < SCHEMA_VERSION:
            connection.begin()
            try:
                if version == 1:
                    connection.execute(
                        """
                        ALTER TABLE project
                        ADD COLUMN odoo_connection_mode VARCHAR DEFAULT 'REMOTE'
                        """
                    )
                    version = 2
                if version == 2:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_catalog (
                            file_id VARCHAR PRIMARY KEY,
                            source_sha256 VARCHAR NOT NULL,
                            contract_version INTEGER NOT NULL,
                            inspected_at VARCHAR NOT NULL,
                            catalog_json VARCHAR NOT NULL
                        )
                        """
                    )
                    version = 3
                if version == 3:
                    connection.execute(
                        """
                        ALTER TABLE audit_event
                        ADD COLUMN IF NOT EXISTS actor_issuer
                        VARCHAR DEFAULT 'urn:impodo:legacy'
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE audit_event
                        ADD COLUMN IF NOT EXISTS actor_subject
                        VARCHAR DEFAULT 'unknown'
                        """
                    )
                    connection.execute(
                        """
                        ALTER TABLE audit_event
                        ADD COLUMN IF NOT EXISTS actor_display_name
                        VARCHAR DEFAULT 'Legacy operator'
                        """
                    )
                    version = 4
                connection.execute(
                    "UPDATE schema_version SET version = ?",
                    [version],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _insert_audit(
        self,
        connection: duckdb.DuckDBPyConnection,
        project: MigrationProject,
        *,
        event_type: str,
        detail: str,
        actor: Actor,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_event (
                event_id, event_type, project_revision, occurred_at, detail,
                actor_issuer, actor_subject, actor_display_name
            )
            VALUES (nextval('audit_event_sequence'), ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_type,
                project.revision,
                project.updated_at.isoformat(),
                detail,
                actor.identity.issuer,
                actor.identity.subject_id,
                actor.identity.display_name,
            ],
        )

    def _write_registration_manifest(self, project: MigrationProject) -> Path:
        payload = {
            "contract_version": 2,
            "project": {
                "project_id": project.project_id,
                "name": project.name,
                "source_system": project.source_system,
                "export_status": project.export_status.value,
                "export_date": (
                    project.export_date.isoformat() if project.export_date else None
                ),
                "data_manager": project.data_manager,
                "functional_owner": project.functional_owner,
                "business_unit": project.business_unit,
                "data_classification": project.data_classification.value,
                "retention_days": project.retention_days,
                "support_access": project.support_access,
                "odoo_connection_mode": (
                    project.odoo_connection_mode.value
                    if project.odoo_connection_mode
                    else None
                ),
                "target_environment": (
                    project.target_environment.value
                    if project.target_environment
                    else None
                ),
                "odoo_base_url": project.odoo_base_url,
                "odoo_database": project.odoo_database,
                "intended_applications": list(project.intended_applications),
                "intended_models": list(project.intended_models),
                "status": project.status.value,
                "revision": project.revision,
                "created_at": project.created_at.isoformat(),
                "registered_at": (
                    project.registered_at.isoformat()
                    if project.registered_at
                    else None
                ),
                "mapping_version": project.mapping_version,
                "current_run_id": project.current_run_id,
                "approval_status": project.approval_status.value,
            },
            "source_files": [
                {
                    "file_id": source_file.file_id,
                    "display_name": source_file.display_name,
                    "stored_name": source_file.stored_name,
                    "size_bytes": source_file.size_bytes,
                    "sha256": source_file.sha256,
                    "received_at": source_file.received_at.isoformat(),
                }
                for source_file in project.source_files
            ],
        }
        audit_dir = self.project_directory(project.project_id) / "audit"
        target = audit_dir / (
            f"project-registration-r{project.revision}.json"
        )
        partial = target.with_suffix(".json.partial")
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        partial.write_bytes(encoded)
        partial.replace(target)
        return target


def _project_values(project: MigrationProject) -> list[object]:
    return [
        project.project_id,
        project.name,
        project.source_system,
        project.export_status.value,
        project.export_date.isoformat() if project.export_date else None,
        project.description,
        project.data_manager,
        project.functional_owner,
        project.business_unit,
        project.data_classification.value,
        project.retention_days,
        project.support_access,
        (
            project.odoo_connection_mode.value
            if project.odoo_connection_mode
            else None
        ),
        project.target_environment.value if project.target_environment else None,
        project.odoo_base_url,
        project.odoo_database,
        json.dumps(project.intended_applications),
        json.dumps(project.intended_models),
        project.status.value,
        project.revision,
        project.created_at.isoformat(),
        project.updated_at.isoformat(),
        project.registered_at.isoformat() if project.registered_at else None,
        project.mapping_version,
        project.current_run_id,
        project.approval_status.value,
    ]


def _project_from_rows(
    data: dict[str, object],
    source_rows: list[tuple[object, ...]],
) -> MigrationProject:
    export_date = str(data["export_date"]) if data["export_date"] else None
    registered_at = (
        str(data["registered_at"]) if data["registered_at"] else None
    )
    target_environment = (
        TargetEnvironment(str(data["target_environment"]))
        if data["target_environment"]
        else None
    )
    connection_mode = (
        OdooConnectionMode(str(data["odoo_connection_mode"]))
        if data.get("odoo_connection_mode")
        else None
    )
    return MigrationProject(
        project_id=str(data["project_id"]),
        name=str(data["name"]),
        source_system=str(data["source_system"]),
        export_status=ExportStatus(str(data["export_status"])),
        export_date=date.fromisoformat(export_date) if export_date else None,
        description=str(data["description"]),
        data_manager=str(data["data_manager"]),
        functional_owner=str(data["functional_owner"]),
        business_unit=str(data["business_unit"]),
        data_classification=DataClassification(
            str(data["data_classification"])
        ),
        retention_days=int(data["retention_days"]),
        support_access=bool(data["support_access"]),
        odoo_connection_mode=connection_mode,
        target_environment=target_environment,
        odoo_base_url=str(data["odoo_base_url"]),
        odoo_database=str(data["odoo_database"]),
        intended_applications=tuple(json.loads(str(data["intended_applications"]))),
        intended_models=tuple(json.loads(str(data["intended_models"]))),
        source_files=tuple(
            SourceFile(
                file_id=str(row[0]),
                display_name=str(row[1]),
                stored_name=str(row[2]),
                size_bytes=int(row[3]),
                sha256=str(row[4]),
                received_at=datetime.fromisoformat(str(row[5])),
            )
            for row in source_rows
        ),
        status=ProjectStatus(str(data["status"])),
        revision=int(data["revision"]),
        created_at=datetime.fromisoformat(str(data["created_at"])),
        updated_at=datetime.fromisoformat(str(data["updated_at"])),
        registered_at=(
            datetime.fromisoformat(registered_at) if registered_at else None
        ),
        mapping_version=(
            str(data["mapping_version"]) if data["mapping_version"] else None
        ),
        current_run_id=(
            str(data["current_run_id"]) if data["current_run_id"] else None
        ),
        approval_status=ApprovalStatus(str(data["approval_status"])),
    )
