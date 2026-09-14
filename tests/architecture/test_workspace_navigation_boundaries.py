"""Keep shared navigation independent of row-scale workflow artifacts."""

from __future__ import annotations

import unittest

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
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "web"
            / "presenters"
            / "navigation.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("current_preview", source)
        self.assertNotIn("current_execution_snapshot", source)
        self.assertNotIn("materialize_report", source)
        self.assertIn(
            "def build_workspace_navigation(\n    facts: WorkspaceNavigationFacts,",
            source,
        )

    def test_overview_contains_complete_rendering_in_the_thread_pool(self) -> None:
        source = (
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "web"
            / "routers"
            / "workspace_setup.py"
        ).read_text(encoding="utf-8")

        self.assertIn("return await run_in_threadpool(render_overview)", source)

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
