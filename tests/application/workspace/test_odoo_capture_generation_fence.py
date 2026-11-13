"""Reject stale source sessions before another page or receipt can escape."""

import unittest
from unittest.mock import Mock

from impodo.application.odoo_capture_generation_fence import (
    GenerationFencedOdooSourceCaptureGateway,
    GenerationFencedOdooSourceCaptureSession,
)
from impodo.application.odoo_source_capture_service import (
    OdooSourceCapturePort,
    OdooSourceCaptureSession,
)
from impodo.domain.odoo_source_capture import OdooSourceCaptureCancelled
from impodo.domain.workspace.errors import WorkspaceError


class CaptureGenerationFenceTests(unittest.TestCase):
    def _calls(self):
        request, context = object(), object()
        cancellation = Mock(return_value=False)
        return {
            "probe_identity": ((request,), {"cancellation": cancellation}),
            "probe_schema": ((request, context), {"cancellation": cancellation}),
            "open_capture": ((request, context), {"cancellation": cancellation}),
            "count_matching": (
                (request, context),
                {"limit": 10, "cancellation": cancellation},
            ),
            "scan_origins": ((request, context), {"cancellation": cancellation}),
            "sample": ((request, context), {"limit": 10, "cancellation": cancellation}),
        }

    def test_every_gateway_method_refuses_missing_binding_without_remote_calls(self):
        for name, (args, kwargs) in self._calls().items():
            with self.subTest(method=name):
                delegate = Mock(spec=OdooSourceCapturePort)
                guard = Mock(side_effect=ValueError("private source binding"))
                with self.assertRaises(WorkspaceError) as raised:
                    getattr(
                        GenerationFencedOdooSourceCaptureGateway(delegate, guard), name
                    )(*args, **kwargs)
                self.assertIn("fresh source workspace", str(raised.exception))
                self.assertNotIn("private source binding", str(raised.exception))
                self.assertEqual(delegate.mock_calls, [])

    def test_every_gateway_method_discards_a_result_if_generation_changes(self):
        for name, (args, kwargs) in self._calls().items():
            with self.subTest(method=name):
                delegate = Mock(spec=OdooSourceCapturePort)
                guard = Mock(side_effect=(None, ValueError("changed generation")))
                with self.assertRaises(WorkspaceError):
                    getattr(
                        GenerationFencedOdooSourceCaptureGateway(delegate, guard), name
                    )(*args, **kwargs)
                self.assertEqual(getattr(delegate, name).call_count, 1)
                self.assertEqual(guard.call_count, 2)

    def test_gateway_returns_original_results_and_wraps_the_session(self):
        for name, (args, kwargs) in self._calls().items():
            with self.subTest(method=name):
                delegate = Mock(spec=OdooSourceCapturePort)
                result = (
                    Mock(spec=OdooSourceCaptureSession)
                    if name == "open_capture"
                    else object()
                )
                getattr(delegate, name).return_value = result
                guard = Mock()
                actual = getattr(
                    GenerationFencedOdooSourceCaptureGateway(delegate, guard), name
                )(*args, **kwargs)
                if name == "open_capture":
                    self.assertIsInstance(
                        actual, GenerationFencedOdooSourceCaptureSession
                    )
                    self.assertIs(actual.delegate, result)
                else:
                    self.assertIs(actual, result)
                forwarded = getattr(delegate, name).call_args
                self.assertEqual(forwarded.args, args)
                self.assertIs(forwarded.kwargs["cancellation"], kwargs["cancellation"])
                self.assertEqual(guard.call_count, 2)

    def test_session_rechecks_before_next_without_reading_or_mutating_more_pages(self):
        state = {"current": True, "page_reads": 0}
        first, second = object(), object()

        def pages():
            state["page_reads"] += 1
            yield first
            state["page_reads"] += 1
            yield second

        def require_current():
            if not state["current"]:
                raise ValueError("changed")

        delegate = Mock(spec=OdooSourceCaptureSession)
        delegate.pages.side_effect = pages
        iterator = iter(
            GenerationFencedOdooSourceCaptureSession(delegate, require_current).pages()
        )
        self.assertIs(next(iterator), first)
        state["current"] = False
        with self.assertRaises(WorkspaceError):
            next(iterator)
        self.assertEqual(state["page_reads"], 1)

    def test_session_discards_page_when_reset_happens_inside_next(self):
        current = [True]

        def require_current():
            if not current[0]:
                raise ValueError("changed")

        def pages():
            current[0] = False
            yield object()

        delegate = Mock(spec=OdooSourceCaptureSession)
        delegate.pages.side_effect = pages
        with self.assertRaises(WorkspaceError):
            list(
                GenerationFencedOdooSourceCaptureSession(
                    delegate, require_current
                ).pages()
            )

    def test_session_metadata_and_completion_do_not_authorize_after_a_reset(self):
        for name in ("matching_rows", "accounting"):
            with self.subTest(property=name):
                delegate = Mock(spec=OdooSourceCaptureSession)
                guard = Mock(side_effect=ValueError("changed"))
                with self.assertRaises(WorkspaceError):
                    getattr(
                        GenerationFencedOdooSourceCaptureSession(delegate, guard), name
                    )
                self.assertEqual(delegate.mock_calls, [])

        def pages():
            yield object()
            current[0] = False

        current = [True]

        def require_current():
            if not current[0]:
                raise ValueError("changed on completion")

        delegate = Mock(spec=OdooSourceCaptureSession)
        delegate.pages.side_effect = pages
        iterator = iter(
            GenerationFencedOdooSourceCaptureSession(delegate, require_current).pages()
        )
        next(iterator)
        with self.assertRaises(WorkspaceError):
            next(iterator)

    def test_cancellation_and_native_failures_propagate_unchanged(self):
        for error in (
            OdooSourceCaptureCancelled("cancelled"),
            RuntimeError("native failure"),
        ):
            with self.subTest(error=type(error)):
                delegate = Mock(spec=OdooSourceCapturePort)
                delegate.open_capture.side_effect = error
                with self.assertRaises(type(error)) as raised:
                    GenerationFencedOdooSourceCaptureGateway(
                        delegate, Mock()
                    ).open_capture(object(), object())
                self.assertIs(raised.exception, error)
                session = Mock(spec=OdooSourceCaptureSession)
                session.pages.side_effect = error
                with self.assertRaises(type(error)) as raised:
                    list(
                        GenerationFencedOdooSourceCaptureSession(
                            session, Mock()
                        ).pages()
                    )
                self.assertIs(raised.exception, error)

    def test_relationship_scan_checks_each_page_before_observer_or_next_read(self):
        current = [True]
        observed = []

        def guard():
            if not current[0]:
                raise ValueError("changed")

        def scan(*_args, observe_page, **_kwargs):
            current[0] = False
            observe_page(500, 501)
            self.fail("A stale scan must stop before another page read")

        delegate = Mock(spec=OdooSourceCapturePort)
        delegate.scan_origins.side_effect = scan
        with self.assertRaises(WorkspaceError):
            GenerationFencedOdooSourceCaptureGateway(delegate, guard).scan_origins(
                object(), object(), observe_page=lambda *page: observed.append(page)
            )
        self.assertEqual(observed, [])


if __name__ == "__main__":
    unittest.main()
