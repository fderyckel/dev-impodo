"""Persist fixed-size, display-only transfer progress in the engine transaction."""

from __future__ import annotations

import json
from dataclasses import astuple, fields

import duckdb

from impodo.application.workspace.navigation import TransferNavigationProgress
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState

from .serialization import decode_workspace_state

PROGRESS_COLUMNS = tuple(field.name for field in fields(TransferNavigationProgress))


def create_transfer_navigation(connection: duckdb.DuckDBPyConnection) -> None:
    """Create one display row independently of the size of the matching evidence."""

    connection.execute("""
        CREATE TABLE transfer_navigation (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            workspace_id VARCHAR NOT NULL, selection_hash VARCHAR NOT NULL, schema_hash VARCHAR NOT NULL,
            match_present BOOLEAN NOT NULL, order_present BOOLEAN NOT NULL, review_present BOOLEAN NOT NULL,
            match_ready BOOLEAN NOT NULL, order_ready BOOLEAN NOT NULL, review_current BOOLEAN NOT NULL,
            review_approved BOOLEAN NOT NULL, preflight_current BOOLEAN NOT NULL, preflight_ready BOOLEAN NOT NULL,
            expires_at TIMESTAMPTZ
        )
    """)


def save_transfer_navigation(connection: duckdb.DuckDBPyConnection, workspace: WorkspaceState) -> None:
    """Replace the summary together with its authoritative transfer state."""

    if workspace.source_mode is not SourceMode.ODOO:
        return
    progress = TransferNavigationProgress.from_workspace(workspace)
    connection.execute(
        f"INSERT OR REPLACE INTO transfer_navigation VALUES (1, {', '.join('?' for _ in PROGRESS_COLUMNS)})",
        astuple(progress),
    )


def upgrade_transfer_navigation(connection: duckdb.DuckDBPyConnection) -> None:
    """Backfill existing Odoo workspaces once while upgrading their engine."""

    create_transfer_navigation(connection)
    row = connection.execute(
        "SELECT * FROM workspace_projection_cache WHERE source_mode = 'ODOO'"
    ).fetchone()
    if row is None:
        return
    data = dict(zip((item[0] for item in connection.description), row, strict=True))
    match = data.get("destination_match_plan_json")
    # No match can advance a later stage. Retain presence of any stale later evidence.
    workspace_id = str(json.loads(match).get("workspace_id", "")) if match else ""
    workspace = decode_workspace_state(data, [], workspace_id=workspace_id)
    save_transfer_navigation(connection, workspace)
