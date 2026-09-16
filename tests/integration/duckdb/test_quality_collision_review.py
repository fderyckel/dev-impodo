"""Read-only collision-group projection for the set-aside review."""

from __future__ import annotations

import json
from contextlib import nullcontext
import unittest
from uuid import uuid4

import duckdb

from impodo.adapters.duckdb.quality_repository import QualityRepository


class _DatabasePath:
    def is_file(self) -> bool:
        return True


class _DatabaseDirectory:
    def __truediv__(self, _name: str) -> _DatabasePath:
        return _DatabasePath()


class _Database:
    def __init__(self, connection) -> None:
        self.connection = connection

    def workspace_directory(self, workspace_id: str) -> _DatabaseDirectory:
        return _DatabaseDirectory()

    def _connect(self, _path: _DatabasePath):
        return nullcontext(self.connection)


class QualityCollisionReviewTests(unittest.TestCase):
    def test_each_row_points_to_its_own_complete_collision_pair(self) -> None:
        with duckdb.connect(":memory:") as connection:
            quality_run_id = str(uuid4())
            staging_run_id = str(uuid4())
            rows = (
                ("row-1856", 1856, "rpe-key", "RPEHD02", "5637257099", "0"),
                ("row-1857", 1857, "bu-key", "BU221G", "5637257101", "0"),
                ("row-1861", 1861, "rpe-key", "RPEHD02", "5637257102", "1000"),
                ("row-1862", 1862, "bu-key", "BU221G", "5637257103", "1000"),
            )
            connection.execute(
                "CREATE TABLE quality_run (run_id VARCHAR, staging_run_id VARCHAR)"
            )
            connection.execute(
                """CREATE TABLE canonical_staging_row (
                        run_id VARCHAR, row_id VARCHAR, source_row BIGINT,
                        quality_identity_key VARCHAR, row_json VARCHAR
                    )"""
            )
            connection.execute(
                "INSERT INTO quality_run VALUES (?, ?)",
                [quality_run_id, staging_run_id],
            )
            for row_id, source_row, key, product, recid, quantity in rows:
                connection.execute(
                    "INSERT INTO canonical_staging_row VALUES (?, ?, ?, ?, ?)",
                    [
                        staging_run_id, row_id, source_row, key,
                        json.dumps({
                            "dataset": "plw_bom_line",
                            "source_identity": [recid],
                            "target_identity": [{"key": [product]}],
                            "target_scope": [{"key": ["BI0290DBU003I04-1"]}],
                            "proposed_values": {"product_qty": {
                                "type": "decimal", "value": quantity,
                            }},
                            "references": {},
                        }),
                    ],
                )
            repository = QualityRepository(_Database(connection), None)

            groups = repository.get_quality_collision_groups(
                "workspace", quality_run_id, ("row-1856", "row-1857")
            )

            self.assertEqual(
                tuple(member.source_row for member in groups["row-1856"].members),
                (1856, 1861),
            )
            self.assertEqual(
                tuple(member.source_row for member in groups["row-1857"].members),
                (1857, 1862),
            )
            self.assertEqual(groups["row-1856"].target_identity, "RPEHD02")
            self.assertEqual(groups["row-1856"].target_scope, "BI0290DBU003I04-1")
            self.assertEqual(
                groups["row-1856"].members[1].differing_values,
                (("product_qty", "1000"),),
            )
            self.assertEqual(
                groups["row-1856"].members[0].source_identity_value,
                "5637257099",
            )


if __name__ == "__main__":
    unittest.main()
