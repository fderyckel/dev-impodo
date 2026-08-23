"""Persist immutable supporting lookups inside each project workspace."""

from __future__ import annotations

from ...access import Actor
from ...workspace_state import WorkspaceStateNotFoundError
from ...supporting_lookups import SupportingLookupSnapshot
from ...workspace_errors import WorkspaceError
from .repository import DuckDbRepository


class SupportingLookupRepository(DuckDbRepository):
    """Own supporting-lookup revisions and their current pointers."""

    def get_current(
        self,
        project_id: str,
        lookup_key: str,
    ) -> SupportingLookupSnapshot | None:
        database_path = self.workspace_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            row = connection.execute(
                """
                SELECT revision.snapshot_json
                  FROM supporting_lookup_current AS current
                  JOIN supporting_lookup_revision AS revision
                    ON revision.snapshot_id = current.snapshot_id
                 WHERE current.lookup_key = ?
                """,
                [lookup_key],
            ).fetchone()
        if row is None:
            return None
        try:
            snapshot = SupportingLookupSnapshot.from_json(str(row[0]))
        except (KeyError, TypeError, ValueError) as error:
            raise WorkspaceError(
                "The saved Odoo choices are invalid; refresh them"
            ) from error
        if snapshot.project_id != project_id or snapshot.lookup_key != lookup_key:
            raise WorkspaceError(
                "The saved Odoo choices belong to another lookup; refresh them"
            )
        return snapshot

    def save(
        self,
        project_id: str,
        snapshot: SupportingLookupSnapshot,
        *,
        actor: Actor,
    ) -> None:
        if snapshot.project_id != project_id:
            raise WorkspaceError("Supporting lookup belongs to another project")
        database_path = self.workspace_directory(project_id) / "project.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Project not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    INSERT INTO supporting_lookup_revision (
                        snapshot_id, lookup_key, content_hash, relation_model,
                        captured_at, snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        snapshot.snapshot_id,
                        snapshot.lookup_key,
                        snapshot.content_hash,
                        snapshot.relation_model,
                        snapshot.captured_at.isoformat(),
                        snapshot.to_json(),
                    ],
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO supporting_lookup_current (
                        lookup_key, snapshot_id
                    ) VALUES (?, ?)
                    """,
                    [snapshot.lookup_key, snapshot.snapshot_id],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="SUPPORTING_LOOKUP_CAPTURED",
                    detail=(
                        f"Saved {len(snapshot.choices)} portable choices for "
                        f"{snapshot.relation_model}; content "
                        f"{snapshot.content_hash}"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

