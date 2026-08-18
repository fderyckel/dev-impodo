"""Atomic downstream invalidation while retaining immutable evidence history.

Repositories call these helpers inside their existing write transaction. The
cascade follows evidence dependencies—staging/effective dataset, quality,
normalization, then preflight—retiring lifecycle status and deleting only
``current`` pointers. Historical runs remain queryable for audit.
"""

from __future__ import annotations

from datetime import (
    datetime,
    timezone,
)

import duckdb

from ...quality import QualityRunStatus
from ...staging import StagingRunStatus








class EvidenceInvalidationMixin:
    """Retire dependent evidence in dependency order within the caller's transaction."""

    @staticmethod
    def _invalidate_normalization(
        connection: duckdb.DuckDBPyConnection,
        *,
        reason: str,
    ) -> None:
        """Retire the current prepared-data approval without deleting evidence."""

        EvidenceInvalidationMixin._invalidate_preflight(
            connection,
            reason=reason,
        )
        current = connection.execute(
            "SELECT run_id FROM normalization_current WHERE singleton_id = 1"
        ).fetchone()
        if current is None:
            return
        connection.execute(
            """
            UPDATE normalization_run
               SET status = ?, retired_at = ?, retired_reason = ?
             WHERE run_id = ?
            """,
            [
                "INVALIDATED",
                datetime.now(timezone.utc).isoformat(),
                reason,
                str(current[0]),
            ],
        )
        connection.execute("DELETE FROM normalization_current")

    @staticmethod
    def _invalidate_preflight(
        connection: duckdb.DuckDBPyConnection,
        *,
        reason: str,
    ) -> None:
        """Clear only the current pointer and retain immutable run evidence."""

        current = connection.execute(
            "SELECT run_id FROM preflight_current WHERE singleton_id = 1"
        ).fetchone()
        if current is None:
            return
        occurred_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT OR REPLACE INTO preflight_transition
            VALUES (?, ?, ?, ?, ?)
            """,
            [str(current[0]), "INVALIDATED", occurred_at, "Impodo", reason],
        )
        connection.execute("DELETE FROM preflight_current")
        connection.execute(
            """
            UPDATE project
               SET current_run_id = NULL,
                   approval_status = 'INVALIDATED'
            """
        )

    @classmethod
    def _invalidate_quality(
        cls,
        connection: duckdb.DuckDBPyConnection,
        *,
        reason: str,
    ) -> None:
        """Retire the current quality pointer without deleting evidence."""

        cls._invalidate_normalization(connection, reason=reason)

        current = connection.execute(
            "SELECT run_id FROM quality_current WHERE singleton_id = 1"
        ).fetchone()
        if current is None:
            return
        connection.execute(
            """
            UPDATE quality_run
               SET status = ?, retired_at = ?, retired_reason = ?
             WHERE run_id = ?
            """,
            [
                QualityRunStatus.INVALIDATED.value,
                datetime.now(timezone.utc).isoformat(),
                reason,
                str(current[0]),
            ],
        )
        connection.execute("DELETE FROM quality_current")
        connection.execute(
            """
            UPDATE project
               SET current_run_id = NULL,
                   approval_status = 'INVALIDATED'
            """
        )

    @classmethod
    def _invalidate_resolution(
        cls,
        connection: duckdb.DuckDBPyConnection,
        *,
        reason: str,
    ) -> None:
        """Retire the current effective dataset before quality evidence."""

        cls._invalidate_quality(connection, reason=reason)
        current = connection.execute(
            "SELECT run_id FROM resolution_current WHERE singleton_id = 1"
        ).fetchone()
        if current is None:
            connection.execute("DELETE FROM effective_dataset_current")
            return
        connection.execute(
            """
            UPDATE resolution_run
               SET status = 'INVALIDATED',
                   retired_at = ?,
                   retired_reason = ?
             WHERE run_id = ?
            """,
            [
                datetime.now(timezone.utc).isoformat(),
                reason,
                str(current[0]),
            ],
        )
        connection.execute("DELETE FROM effective_dataset_current")
        connection.execute("DELETE FROM resolution_current")

    @classmethod
    def _invalidate_canonical_staging(
        cls,
        connection: duckdb.DuckDBPyConnection,
        *,
        reason: str,
    ) -> None:
        """Retire the current staging pointer without deleting audit evidence."""

        cls._invalidate_resolution(connection, reason=reason)
        connection.execute("DELETE FROM prepared_snapshot_current")
        connection.execute("DELETE FROM derived_value_artifact_current")

        current = connection.execute(
            """
            SELECT run_id
              FROM canonical_staging_current
             WHERE singleton_id = 1
            """
        ).fetchone()
        if current is None:
            return
        connection.execute(
            """
            UPDATE canonical_staging_run
               SET status = ?, retired_at = ?, retired_reason = ?
             WHERE run_id = ?
            """,
            [
                StagingRunStatus.INVALIDATED.value,
                datetime.now(timezone.utc).isoformat(),
                reason,
                str(current[0]),
            ],
        )
        connection.execute("DELETE FROM canonical_staging_current")
        connection.execute(
            """
            UPDATE project
               SET current_run_id = NULL,
                   approval_status = 'INVALIDATED'
            """
        )
