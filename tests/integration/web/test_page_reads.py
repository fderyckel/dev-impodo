"""Page-read workers keep blocking database work off the event loop."""

import asyncio
from contextlib import contextmanager
from threading import Event, get_ident
import unittest
from unittest.mock import patch

from impodo.web.composition.page_reads import run_local_operation, run_page_read


class PageReadWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_scope_is_opened_and_closed_inside_one_worker(self) -> None:
        loop_thread = get_ident()
        entered = Event()
        release = Event()
        events = []

        @contextmanager
        def scope():
            events.append(("open", get_ident()))
            try:
                yield
            finally:
                events.append(("close", get_ident()))

        def read(*, value):
            events.append(("read", get_ident()))
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("The event loop could not release the read")
            return value

        with patch("impodo.web.composition.page_reads.retain_databases_for_read", scope):
            task = asyncio.create_task(run_page_read(read, value="rendered"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            finally:
                release.set()
            self.assertEqual(await task, "rendered")

        self.assertEqual([name for name, _ in events], ["open", "read", "close"])
        self.assertEqual(len({thread for _, thread in events}), 1)
        self.assertNotEqual(events[0][1], loop_thread)

    async def test_failed_read_closes_scope_before_propagating_error(self) -> None:
        closed = []

        @contextmanager
        def scope():
            try:
                yield
            finally:
                closed.append(True)

        def fail():
            raise ValueError("invalid saved evidence")

        with patch("impodo.web.composition.page_reads.retain_databases_for_read", scope):
            with self.assertRaisesRegex(ValueError, "invalid saved evidence"):
                await run_page_read(fail)
        self.assertEqual(closed, [True])

    async def test_local_operation_uses_bounded_worker_scope(self) -> None:
        loop_thread = get_ident()
        events = []

        @contextmanager
        def scope():
            events.append(("open", get_ident()))
            try:
                yield
            finally:
                events.append(("close", get_ident()))

        def operate():
            events.append(("operate", get_ident()))
            return "saved"

        with patch(
            "impodo.web.composition.page_reads.retain_databases_for_operation",
            scope,
        ):
            self.assertEqual(await run_local_operation(operate), "saved")

        self.assertEqual([name for name, _ in events], ["open", "operate", "close"])
        self.assertEqual(len({thread for _, thread in events}), 1)
        self.assertNotEqual(events[0][1], loop_thread)

    async def test_async_callable_is_rejected_before_it_escapes_scope(self) -> None:
        events = []

        @contextmanager
        def scope():
            events.append("open")
            try:
                yield
            finally:
                events.append("close")

        async def unsupported():
            return "outside the database scope"

        with patch(
            "impodo.web.composition.page_reads.retain_databases_for_operation",
            scope,
        ):
            with self.assertRaisesRegex(
                TypeError,
                "must use a synchronous callable",
            ):
                await run_local_operation(unsupported)

        self.assertEqual(events, ["open", "close"])
