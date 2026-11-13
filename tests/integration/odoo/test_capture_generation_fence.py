"""Native source streams refuse later pages from a recreated database."""

import unittest

from impodo.adapters.duckdb.unit_of_work import (
    _read_databases,
    retain_databases_for_operation,
)
from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo_source_capture import Json2OdooSourceCapture
from impodo.application.odoo_capture_generation_fence import (
    GenerationFencedOdooSourceCaptureGateway,
)
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASHES
from impodo.domain.workspace.errors import WorkspaceError
from tests.integration.odoo import test_source_capture as capture_fixtures


class NativeCaptureGenerationFenceTests(unittest.TestCase):
    def test_reset_between_pages_stops_before_another_remote_read(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._assert_native_stream_fenced(major, mode, during_page=False)

    def test_reset_during_page_discards_the_page_and_metadata_receipts(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    self._assert_native_stream_fenced(major, mode, during_page=True)

    def _assert_native_stream_fenced(self, major, mode, *, during_page):
        state = {"current": True, "checks": 0}
        dataset = capture_fixtures.DatasetTransport(capture_fixtures._rows(101))

        def require_current():
            with retain_databases_for_operation():
                state["checks"] += 1
                if not state["current"]:
                    raise ValueError("Declared database recreation")

        def transport(*args, **kwargs):
            self.assertIsNone(getattr(_read_databases, "connections", None))
            return dataset(*args, **kwargs)

        if during_page:

            def reset_during_page(rows, _request_count):
                state["current"] = False
                return rows

            dataset.override_page = reset_during_page

        config = Json2Config(
            "http://127.0.0.1:8069"
            if mode == "LOCAL"
            else "https://source.example.test",
            "source_database",
            "secret",
            connection_mode=mode,
            retries=0,
        )
        gateway = GenerationFencedOdooSourceCaptureGateway(
            Json2OdooSourceCapture(config, transport=transport),
            require_current,
        )
        request = capture_fixtures._request(
            page_size=100, policy_hash=ODOO_SOURCE_POLICY_HASHES[major]
        )
        session = gateway.open_capture(request, capture_fixtures._context())
        self.assertEqual(session.matching_rows, 101)
        self.assertEqual(len(dataset.calls), 2)
        pages = iter(session.pages())
        published = []
        if not during_page:
            published.append(next(pages))
            self.assertIsNone(getattr(_read_databases, "connections", None))
            self.assertEqual(published[0].odoo_ids, tuple(range(1, 101)))
            state["current"] = False
        with self.assertRaises(WorkspaceError):
            published.append(next(pages))
        self.assertEqual(len(dataset.calls), 3)
        self.assertEqual(len(published), 0 if during_page else 1)
        for name in ("matching_rows", "accounting"):
            with self.subTest(receipt=name), self.assertRaises(WorkspaceError):
                getattr(session, name)
        with self.assertRaises(WorkspaceError):
            gateway.open_capture(request, capture_fixtures._context())
        self.assertEqual(len(dataset.calls), 3)
        self.assertIsNone(getattr(_read_databases, "connections", None))


if __name__ == "__main__":
    unittest.main()
