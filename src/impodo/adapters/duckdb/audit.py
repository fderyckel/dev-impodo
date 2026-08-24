"""Append verified actor and workspace-transition evidence inside active writes.

Repositories call ``AuditMixin`` only within the same transaction as the
state/pointer change being described. Events retain stable actor issuer and
subject identifiers plus a display-name snapshot; display text is never used
as the actor's identity.
"""

from __future__ import annotations

from datetime import (
    datetime,
    timezone,
)

import duckdb

from ...access import Actor
from ...workspace_state import WorkspaceState








class AuditMixin:
    """Write audit rows in the caller's transaction so state and audit agree."""

    @staticmethod
    def _insert_workspace_audit(
        connection: duckdb.DuckDBPyConnection,
        *,
        revision: int,
        event_type: str,
        detail: str,
        actor: Actor,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_event (
                event_id, event_type, workspace_revision, occurred_at, detail,
                actor_issuer, actor_subject, actor_display_name
            )
            VALUES (nextval('audit_event_sequence'), ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_type,
                revision,
                datetime.now(timezone.utc).isoformat(),
                detail,
                actor.identity.issuer,
                actor.identity.subject_id,
                actor.identity.display_name,
            ],
        )

    def _insert_audit(
        self,
        connection: duckdb.DuckDBPyConnection,
        workspace: WorkspaceState,
        *,
        event_type: str,
        detail: str,
        actor: Actor,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_event (
                event_id, event_type, workspace_revision, occurred_at, detail,
                actor_issuer, actor_subject, actor_display_name
            )
            VALUES (nextval('audit_event_sequence'), ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event_type,
                workspace.revision,
                workspace.updated_at.isoformat(),
                detail,
                actor.identity.issuer,
                actor.identity.subject_id,
                actor.identity.display_name,
            ],
        )

