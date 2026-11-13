"""Persist declared database generations and immutable reviewed workspace proof.

Each public method owns one bounded registry operation. No Odoo call or open
connection survives the method; the shared local-operation scope may retain
its owner only while synchronous application work continues.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import duckdb

from impodo.domain.odoo_database_binding import (
    OdooDatabaseBinding,
    OdooDatabaseBindingError,
    OdooDatabaseBindingRole,
    OdooDatabaseGeneration,
    OdooDatabaseGenerationConflict,
)
from impodo.domain.project.foundation import require_hash, require_uuid

from .migration_foundation_database import MigrationFoundationDatabase
from .schema.migration_registry import ensure_migration_registry_schema


class DuckDbOdooDatabaseBindingRepository:
    """Keep one Organization ledger across Projects, Companies and source/destination roles."""

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
                    raise OdooDatabaseGenerationConflict(
                        "The database authority changed; reload and review the current generation"
                    ) from error
                raise

    def get(
        self, workspace_id: str, role: OdooDatabaseBindingRole, scope_hash: str
    ) -> OdooDatabaseBinding | None:
        """Read the original proof even when its generation is no longer current."""

        require_uuid(workspace_id, "workspace_id")
        require_hash(scope_hash, "scope_hash")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT binding_json, content_hash FROM odoo_workspace_database_binding WHERE workspace_id = ? AND role = ? AND scope_hash = ?",
                [workspace_id, OdooDatabaseBindingRole(role).value, scope_hash],
            ).fetchone()
        result = self._binding(row) if row is not None else None
        if result is not None and (
            result.workspace_id != workspace_id
            or result.role != role
            or result.scope_hash != scope_hash
        ):
            raise OdooDatabaseBindingError(
                "The stored database binding identity is inconsistent"
            )
        return result

    def current(self, organization_id: str, scope_hash: str) -> OdooDatabaseGeneration:
        """Return initial generation zero without creating a ledger row."""

        require_hash(scope_hash, "scope_hash")
        with self._connect() as connection:
            return self._current(connection, organization_id, scope_hash)

    def bind(
        self, binding: OdooDatabaseBinding, *, expected_generation_hash: str
    ) -> OdooDatabaseBinding:
        """Admit one proof atomically without replacing an earlier generation."""

        require_hash(expected_generation_hash, "expected_generation_hash")
        if binding.generation_hash != expected_generation_hash:
            raise OdooDatabaseGenerationConflict(
                "The reviewed database generation does not match this binding"
            )
        with self._connect(write=True) as connection:
            workspace = connection.execute(
                "SELECT project_id FROM migration_workspace WHERE workspace_id = ?",
                [binding.workspace_id],
            ).fetchone()
            if workspace is None or str(workspace[0]) != binding.project_id:
                raise OdooDatabaseBindingError(
                    "The database review belongs to another workspace or Project"
                )
            current = self._current(
                connection, binding.organization_id, binding.scope_hash
            )
            if current.generation_hash != expected_generation_hash:
                raise OdooDatabaseGenerationConflict(
                    "The database was declared recreated; start a fresh review workspace"
                )
            row = connection.execute(
                "SELECT binding_json, content_hash FROM odoo_workspace_database_binding WHERE workspace_id = ? AND role = ? AND scope_hash = ?",
                [binding.workspace_id, binding.role.value, binding.scope_hash],
            ).fetchone()
            if row is not None:
                existing = self._binding(row)
                if (
                    existing.project_id != binding.project_id
                    or existing.organization_id != binding.organization_id
                    or existing.generation_hash != binding.generation_hash
                ):
                    raise OdooDatabaseGenerationConflict(
                        "This workspace retains an earlier database authority; start a fresh review workspace"
                    )
                return existing
            self._seed(connection, current)
            connection.execute(
                "INSERT INTO odoo_workspace_database_binding VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    binding.workspace_id,
                    binding.project_id,
                    binding.organization_id,
                    binding.role.value,
                    binding.scope_hash,
                    binding.generation_hash,
                    binding.operation_id,
                    binding.to_json(),
                    binding.content_hash,
                ],
            )
        return binding

    def declare(
        self, generation: OdooDatabaseGeneration, *, expected_generation_hash: str
    ) -> OdooDatabaseGeneration:
        """Append one reset declaration using a durable operation UUID and CAS."""

        require_hash(expected_generation_hash, "expected_generation_hash")
        if (
            generation.generation == 0
            or generation.previous_generation_hash != expected_generation_hash
        ):
            raise OdooDatabaseBindingError(
                "A reset declaration must name the preceding generation"
            )
        with self._connect(write=True) as connection:
            replay = connection.execute(
                "SELECT generation_json, content_hash FROM odoo_database_generation WHERE organization_id = ? AND operation_id = ?",
                [generation.organization_id, generation.operation_id],
            ).fetchone()
            if replay is not None:
                previous = self._generation(replay)
                if (
                    previous.scope_hash != generation.scope_hash
                    or previous.previous_generation_hash != expected_generation_hash
                    or previous.reason != generation.reason
                    or previous.actor != generation.actor
                ):
                    raise OdooDatabaseGenerationConflict(
                        "This reset request was already used for a different declaration"
                    )
                return previous
            current = self._current(
                connection, generation.organization_id, generation.scope_hash
            )
            if (
                current.generation_hash != expected_generation_hash
                or generation.generation != current.generation + 1
            ):
                raise OdooDatabaseGenerationConflict(
                    "The declared database generation changed; reload before declaring another reset"
                )
            self._seed(connection, current)
            self._insert_generation(connection, generation)
            changed = connection.execute(
                """UPDATE odoo_database_generation_current SET generation = ?, generation_hash = ?
                   WHERE organization_id = ? AND scope_hash = ? AND generation_hash = ? RETURNING generation_hash""",
                [
                    generation.generation,
                    generation.generation_hash,
                    generation.organization_id,
                    generation.scope_hash,
                    expected_generation_hash,
                ],
            ).fetchone()
            if changed is None:
                raise OdooDatabaseGenerationConflict(
                    "The declared database generation changed concurrently; reload before retrying"
                )
        return generation

    def workspace_binding_status(
        self,
        workspace_id: str,
        project_id: str,
        role: OdooDatabaseBindingRole,
        scope_hash: str,
    ) -> tuple[OdooDatabaseBinding | None, str | None]:
        """Project proof and current authority through one bounded registry join."""

        require_uuid(workspace_id, "workspace_id")
        require_uuid(project_id, "project_id")
        require_hash(scope_hash, "scope_hash")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT b.binding_json, b.content_hash, c.generation_hash, c.generation,
                          g.generation_json, g.content_hash,
                          (SELECT MAX(newer.generation) FROM odoo_database_generation newer
                           WHERE newer.organization_id = b.organization_id AND newer.scope_hash = b.scope_hash)
                   FROM odoo_workspace_database_binding b
                   LEFT JOIN odoo_database_generation_current c
                     ON c.organization_id = b.organization_id AND c.scope_hash = b.scope_hash
                   LEFT JOIN odoo_database_generation g
                     ON g.organization_id = c.organization_id AND g.scope_hash = c.scope_hash AND g.generation = c.generation
                   WHERE b.workspace_id = ? AND b.project_id = ? AND b.role = ? AND b.scope_hash = ?""",
                [
                    workspace_id,
                    project_id,
                    OdooDatabaseBindingRole(role).value,
                    scope_hash,
                ],
            ).fetchone()
        if row is None:
            return None, None
        binding = self._binding((row[0], row[1]))
        if (
            binding.workspace_id != workspace_id
            or binding.project_id != project_id
            or binding.role != role
            or binding.scope_hash != scope_hash
        ):
            raise OdooDatabaseBindingError(
                "The stored database binding identity is inconsistent"
            )
        if any(value is None for value in row[2:]) or row[3] != row[6]:
            raise OdooDatabaseBindingError(
                "The current database generation authority is incomplete"
            )
        current = self._generation((row[4], row[5]))
        if (
            current.organization_id != binding.organization_id
            or current.scope_hash != scope_hash
            or current.generation_hash != str(row[2])
            or current.generation != row[3]
        ):
            raise OdooDatabaseBindingError(
                "The current database generation authority is inconsistent"
            )
        return binding, current.generation_hash

    def assert_current(
        self,
        workspace_id: str,
        role: OdooDatabaseBindingRole,
        scope_hash: str,
        *,
        project_id: str,
    ) -> OdooDatabaseBinding:
        """Reject an unbound or superseded workspace before any external access."""

        binding, current = self.workspace_binding_status(
            workspace_id, project_id, role, scope_hash
        )
        if binding is None:
            raise OdooDatabaseBindingError(
                "Review this database connection in a fresh workspace before continuing"
            )
        if binding.generation_hash != current:
            raise OdooDatabaseGenerationConflict(
                "This workspace belongs to a database declared recreated; start a fresh review workspace"
            )
        return binding

    @staticmethod
    def _current(
        connection: duckdb.DuckDBPyConnection, organization_id: str, scope_hash: str
    ) -> OdooDatabaseGeneration:
        row = connection.execute(
            """SELECT g.generation_json, g.content_hash, c.generation, c.generation_hash
               FROM odoo_database_generation g LEFT JOIN odoo_database_generation_current c
                 ON c.organization_id = g.organization_id AND c.scope_hash = g.scope_hash
               WHERE g.organization_id = ? AND g.scope_hash = ? ORDER BY g.generation DESC LIMIT 1""",
            [organization_id, scope_hash],
        ).fetchone()
        if row is None:
            orphan = connection.execute(
                "SELECT 1 FROM odoo_database_generation_current WHERE organization_id = ? AND scope_hash = ?",
                [organization_id, scope_hash],
            ).fetchone()
            if orphan is not None:
                raise OdooDatabaseBindingError(
                    "The current database generation authority is incomplete"
                )
            return OdooDatabaseGeneration(organization_id, scope_hash, 0)
        generation = DuckDbOdooDatabaseBindingRepository._generation((row[0], row[1]))
        if (
            generation.organization_id != organization_id
            or generation.scope_hash != scope_hash
            or row[2] != generation.generation
            or row[3] != generation.generation_hash
        ):
            raise OdooDatabaseBindingError(
                "The current database generation authority is inconsistent"
            )
        return generation

    @staticmethod
    def _seed(
        connection: duckdb.DuckDBPyConnection, generation: OdooDatabaseGeneration
    ) -> None:
        if generation.generation != 0:
            return
        present = connection.execute(
            "SELECT 1 FROM odoo_database_generation_current WHERE organization_id = ? AND scope_hash = ?",
            [generation.organization_id, generation.scope_hash],
        ).fetchone()
        if present is None:
            DuckDbOdooDatabaseBindingRepository._insert_generation(
                connection, generation
            )
            connection.execute(
                "INSERT INTO odoo_database_generation_current VALUES (?, ?, ?, ?)",
                [
                    generation.organization_id,
                    generation.scope_hash,
                    0,
                    generation.generation_hash,
                ],
            )

    @staticmethod
    def _insert_generation(
        connection: duckdb.DuckDBPyConnection, generation: OdooDatabaseGeneration
    ) -> None:
        connection.execute(
            "INSERT INTO odoo_database_generation VALUES (?, ?, ?, ?, ?, ?)",
            [
                generation.organization_id,
                generation.scope_hash,
                generation.generation,
                generation.operation_id,
                generation.to_json(),
                generation.content_hash,
            ],
        )

    @staticmethod
    def _binding(row: tuple[object, ...]) -> OdooDatabaseBinding:
        result = OdooDatabaseBinding.from_json(str(row[0]))
        if result.content_hash != str(row[1]):
            raise OdooDatabaseBindingError(
                "The stored database binding integrity check failed"
            )
        return result

    @staticmethod
    def _generation(row: tuple[object, ...]) -> OdooDatabaseGeneration:
        result = OdooDatabaseGeneration.from_json(str(row[0]))
        if result.content_hash != str(row[1]):
            raise OdooDatabaseBindingError(
                "The stored database generation integrity check failed"
            )
        return result
