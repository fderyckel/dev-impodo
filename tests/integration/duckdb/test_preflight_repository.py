from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import unittest
from uuid import UUID

import duckdb

from impodo.adapters.duckdb.preflight_repository import PreflightRepository


RUN_ID = "00000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "00000000-0000-4000-8000-000000000002"


class _ExistingFileDirectory:
    def __truediv__(self, _name: str) -> Path:
        return Path(__file__)


class _InMemoryDatabase:
    """Provide the repository boundary needed by the bounded row reader."""

    root = Path(__file__).parent

    def workspace_directory(self, workspace_id: str) -> _ExistingFileDirectory:
        UUID(workspace_id)
        return _ExistingFileDirectory()

    @contextmanager
    def _connect(self, _path: Path):
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                """
                CREATE TABLE preflight_decision (
                    run_id VARCHAR,
                    ordinal BIGINT,
                    dataset VARCHAR,
                    status VARCHAR,
                    decision_json VARCHAR
                )
                """
            )
            decisions = (
                ("ready", "CREATE"),
                ("ready", "UPDATE"),
                ("ready", "UNCHANGED"),
                ("needs_review", "AMBIGUOUS"),
                ("blocked", "BLOCKED"),
            )
            rows = []
            for ordinal, (status, classification) in enumerate(
                decisions,
                start=1,
            ):
                rows.append(
                    [
                        RUN_ID,
                        ordinal,
                        "contacts",
                        status,
                        json.dumps(
                            {
                                "dataset": "contacts",
                                "dataset_label": "Contacts",
                                "source_row": ordinal,
                                "status": status,
                                "classification": classification,
                                "identity": f"ROW-{ordinal}",
                                "reason": "Test row",
                                "field": "",
                                "recommended_action": "Review it",
                                "technical_code": "TEST",
                            }
                        ),
                    ]
                )
            connection.executemany(
                "INSERT INTO preflight_decision VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _ensure_workspace_database_schema(_connection) -> None:
        return None


class PreflightRepositoryReadinessRowTests(unittest.TestCase):
    def test_card_filters_select_their_matching_rows(self) -> None:
        repository = PreflightRepository(
            _InMemoryDatabase(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
        page = repository.get_readiness_rows(
            WORKSPACE_ID,
            RUN_ID,
            status="attention",
            page_size=20,
        )

        self.assertEqual(page.matching_count, 2)
        self.assertEqual(
            tuple(item.status for item in page.items),
            ("needs_review", "blocked"),
        )

        for expected_ordinal, status in enumerate(
            ("create", "update", "unchanged"),
            start=1,
        ):
            with self.subTest(status=status):
                outcome_page = repository.get_readiness_rows(
                    WORKSPACE_ID,
                    RUN_ID,
                    status=status,
                    page_size=20,
                )
                self.assertEqual(outcome_page.matching_count, 1)
                self.assertEqual(
                    outcome_page.items[0].identity,
                    f"ROW-{expected_ordinal}",
                )


if __name__ == "__main__":
    unittest.main()
