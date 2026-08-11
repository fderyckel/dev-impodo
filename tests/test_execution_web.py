from __future__ import annotations

import unittest

from impodo.web.routers.execution import _load_row_page


class LoadRowPaginationTests(unittest.TestCase):
    def test_defaults_to_twenty_rows_and_clamps_large_pages(self) -> None:
        rows = tuple(range(55))

        first = _load_row_page(
            rows,
            requested_page=None,
            requested_page_size=None,
        )
        last = _load_row_page(
            rows,
            requested_page="99",
            requested_page_size="20",
        )

        self.assertEqual(first.rows, tuple(range(20)))
        self.assertEqual((first.page, first.page_count), (1, 3))
        self.assertEqual((first.first_row, first.last_row), (1, 20))
        self.assertEqual(last.rows, tuple(range(40, 55)))
        self.assertEqual((last.page, last.first_row, last.last_row), (3, 41, 55))

    def test_accepts_fifty_and_rejects_unbounded_page_sizes(self) -> None:
        rows = tuple(range(55))

        fifty = _load_row_page(
            rows,
            requested_page="2",
            requested_page_size="50",
        )
        invalid = _load_row_page(
            rows,
            requested_page="invalid",
            requested_page_size="5000",
        )

        self.assertEqual(fifty.rows, tuple(range(50, 55)))
        self.assertEqual((fifty.page, fifty.page_count), (2, 2))
        self.assertEqual(invalid.page_size, 20)
        self.assertEqual((invalid.page, len(invalid.rows)), (1, 20))


if __name__ == "__main__":
    unittest.main()
