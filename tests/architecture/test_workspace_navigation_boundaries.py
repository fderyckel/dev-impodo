"""Keep shared navigation independent of row-scale workflow artifacts."""

from __future__ import annotations

import ast
import asyncio
import unittest
from contextlib import contextmanager
from threading import get_ident
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import PlainTextResponse

from impodo.web.security import _WorkspacePageRead, run_workspace_page_read
from tests.support.paths import REPOSITORY_ROOT


class WorkspaceNavigationBoundaryTests(unittest.TestCase):
    def test_navigation_adapter_never_selects_large_evidence_documents(self) -> None:
        source = (
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "adapters"
            / "duckdb"
            / "navigation_repository.py"
        ).read_text(encoding="utf-8")
        lowered = source.split("def _read_facts", 1)[1].casefold()

        for forbidden in (
            "report_json",
            "snapshot_json",
            "decision_json",
            "dry_run_json",
            "select *",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_presenter_is_a_pure_mapper_over_navigation_facts(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "impodo" / "web" / "presenters" / "navigation.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("current_preview", source)
        self.assertNotIn("current_execution_snapshot", source)
        self.assertNotIn("materialize_report", source)
        self.assertIn(
            "def build_workspace_navigation(\n    facts: WorkspaceNavigationFacts,",
            source,
        )

    def test_overview_authorizes_and_renders_in_one_local_scope(self) -> None:
        source = (
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "web"
            / "routers"
            / "workspace_setup.py"
        ).read_text(encoding="utf-8")

        overview = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "workspace_overview"
        )
        render = next(
            node
            for node in overview.body
            if isinstance(node, ast.FunctionDef) and node.name == "render_overview"
        )
        callback_calls = {
            ast.unparse(node.func)
            for node in ast.walk(render)
            if isinstance(node, ast.Call)
        }
        self.assertIn("context.navigation.get_for_workspace", callback_calls)
        self.assertIn("_render", callback_calls)
        outer_calls = {
            ast.unparse(node.func)
            for statement in overview.body
            if statement is not render
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
        }
        self.assertEqual(
            outer_calls,
            {
                "require_session",
                "request_identity",
                "run_workspace_page_read",
            },
        )
        self.assertEqual(
            ast.unparse(overview.body[-1]),
            "return await run_workspace_page_read(request, render_overview)",
        )

    def test_workspace_page_helper_runs_admission_and_render_inside_one_worker_scope(
        self,
    ) -> None:
        loop_thread = get_ident()
        events = []

        @contextmanager
        def read_scope():
            events.append(("open", get_ident()))
            try:
                yield
            finally:
                events.append(("close", get_ident()))

        def render():
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            events.append(("render", get_ident()))
            return PlainTextResponse("overview")

        def admit(function):
            events.append(("authorize", get_ident()))
            return function()

        request = Request({"type": "http"})
        page = _WorkspacePageRead(admit)
        request.state.workspace_page_read = page
        with patch(
            "impodo.web.composition.page_reads.retain_databases_for_read", read_scope
        ):
            response = asyncio.run(run_workspace_page_read(request, render))

        self.assertEqual(response.body, b"overview")
        self.assertEqual(
            [name for name, _thread in events], ["open", "authorize", "render", "close"]
        )
        self.assertEqual(len({thread for _name, thread in events}), 1)
        self.assertNotEqual(events[0][1], loop_thread)
        self.assertTrue(page.consumed)

    def test_owner_view_uses_the_batched_foundation_read(self) -> None:
        source = (
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "application"
            / "workspace"
            / "views.py"
        ).read_text(encoding="utf-8")

        self.assertIn("get_workspace_owner_records(context)", source)
        self.assertNotIn("get_source_package", source)
        self.assertNotIn("get_migration_run_target_setup", source)

        adapter_source = (
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "adapters"
            / "duckdb"
            / "foundation_workspace_records.py"
        ).read_text(encoding="utf-8")
        batched_read = adapter_source.split(
            "def get_workspace_owner_records",
            1,
        )[1].split("def _get_workspace_registry", 1)[0]
        self.assertNotIn("ensure_workspace_store", batched_read)
        self.assertNotIn("ensure_data_version_store", batched_read)


if __name__ == "__main__":
    unittest.main()
