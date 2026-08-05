"""Atomic invalidation of downstream evidence pointers."""

from __future__ import annotations

from datetime import (
    datetime,
    timezone,
)

import duckdb

from ...quality import QualityRunStatus
from ...staging import StagingRunStatus








class EvidenceInvalidationMixin:
    """Retire dependent evidence without deleting its audit history."""

    @staticmethod
    def _invalidate_normalization(
        connection: duckdb.DuckDBPyConnection,
        *,
        reason: str,
    ) -> None:
        """Retire the current prepared-data approval without deleting evidence."""

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
    def _invalidate_canonical_staging(
        cls,
        connection: duckdb.DuckDBPyConnection,
        *,
        reason: str,
    ) -> None:
        """Retire the current staging pointer without deleting audit evidence."""

        cls._invalidate_quality(connection, reason=reason)

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
