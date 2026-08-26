"""Verify that the recorded unittest shuffle is reproducible."""

from __future__ import annotations

import unittest

from scripts.run_seeded_unittest import shuffled_ids


class SeededTestOrderTests(unittest.TestCase):
    def test_order_starts_from_sorted_ids_and_is_reproducible(self):
        test_ids = ("test.delta", "test.alpha", "test.charlie", "test.bravo")

        first = shuffled_ids(test_ids, 1729)

        self.assertEqual(first, shuffled_ids(reversed(test_ids), 1729))
        self.assertNotEqual(first, shuffled_ids(test_ids, 20260826))


if __name__ == "__main__":
    unittest.main()
