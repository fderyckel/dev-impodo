from __future__ import annotations

from types import SimpleNamespace
import unittest

from impodo.application.fallout_workbook_service import _source_column_number
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.errors import WorkspaceError


HASH = "sha256:" + "1" * 64


def _binding(table_key: str) -> FileSourceBinding:
    return FileSourceBinding(
        file_id="file-1",
        table_key=table_key,
        source_sha256=HASH,
        catalog_hash=HASH,
        encoding=None,
        delimiter=None,
        header_row=3,
    )


class FalloutWorkbookServiceTests(unittest.TestCase):
    def test_named_table_column_ordinal_is_relative_to_table_start(self):
        binding = _binding("table:Products:ProductTable")
        catalog = SimpleNamespace(
            tables=(
                SimpleNamespace(
                    table_key=binding.table_key,
                    named_tables=(SimpleNamespace(cell_range="D3:G20"),),
                ),
            )
        )

        self.assertEqual(_source_column_number(binding, 2, catalog), 5)

    def test_missing_named_table_bounds_fail_closed(self):
        binding = _binding("table:Products:ProductTable")

        with self.assertRaisesRegex(WorkspaceError, "bounds are unavailable"):
            _source_column_number(binding, 2, SimpleNamespace(tables=()))


if __name__ == "__main__":
    unittest.main()
