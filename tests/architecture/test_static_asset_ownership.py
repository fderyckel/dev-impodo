"""Protect the Phase 4 browser-asset ownership boundaries."""

from __future__ import annotations

import unittest

from tests.support.paths import REPOSITORY_ROOT


STATIC_ROOT = REPOSITORY_ROOT / "src" / "impodo" / "web" / "static"
TEMPLATE_ROOT = REPOSITORY_ROOT / "src" / "impodo" / "web" / "templates"


class StaticAssetOwnershipTests(unittest.TestCase):
    def test_base_template_loads_only_bounded_shared_assets(self) -> None:
        template = (TEMPLATE_ROOT / "base.html").read_text(encoding="utf-8")
        styles = (
            "/tokens.css",
            "/layout.css",
            "/components.css",
            "/workflow-pages.css",
        )

        positions = [template.index(style) for style in styles]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("/app.js", template)
        self.assertIn("/server-recovery.js", template)
        self.assertLess(
            template.index("/app.js"),
            template.index("/server-recovery.js"),
        )
        self.assertFalse((STATIC_ROOT / "app.css").exists())

    def test_shared_recovery_ui_uses_bounded_authenticated_heartbeat(self) -> None:
        template = (TEMPLATE_ROOT / "base.html").read_text(encoding="utf-8")
        script = (STATIC_ROOT / "server-recovery.js").read_text(
            encoding="utf-8"
        )
        shared_script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        mapping_editor = (STATIC_ROOT / "mapping-editor.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("Impodo is not responding", template)
        self.assertIn('data-health-url="/health"', template)
        self.assertIn('data-failure-limit="3"', template)
        self.assertIn('data-heartbeat-interval-ms="4000"', template)
        self.assertIn('data-heartbeat-timeout-ms="2000"', template)
        self.assertIn('action="/diagnostics/bundle"', template)
        self.assertIn("Create diagnostic bundle", template)
        self.assertIn("consecutiveFailures >= failureLimit", script)
        self.assertIn('credentials: "same-origin"', script)
        self.assertIn('cache: "no-store"', script)
        self.assertIn('new CustomEvent("impodo:server-disconnected")', script)
        self.assertIn('"impodo:server-disconnected"', shared_script)
        self.assertIn('"impodo:server-disconnected"', mapping_editor)
        self.assertIn("check the save outcome before retrying", mapping_editor)
        self.assertLessEqual(len(script.splitlines()), 220)

    def test_shared_script_does_not_reclaim_page_workflows(self) -> None:
        script = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

        self.assertLessEqual(len(script.splitlines()), 400)
        for page_selector in (
            "[data-mapping-form]",
            "[data-model-choice]",
            "[data-preparation-job]",
            "[data-transformation-impact-prepare]",
        ):
            with self.subTest(page_selector=page_selector):
                self.assertNotIn(page_selector, script)

    def test_page_templates_name_their_owned_scripts(self) -> None:
        expected_assets = {
            "mapping/page.html": (
                "/mapping-save-recovery.js",
                "/mapping-editor.js",
                "/mapping-formula-validation.js",
                "/mapping-value-rules.js",
                "/mapping-catalogs.js",
                "/mapping.js",
            ),
            "workspace_schema.html": ("/schema.js",),
            "workspace_normalization.html": ("/normalization.js", "/review.js"),
            "workspace_summary.html": ("/review.js",),
            "workspace_load.html": ("/execution.js",),
            "workspace_transformation_impact.html": (
                "/transformation-impact.js",
            ),
            "workspace_preparation_progress.html": ("/job-polling.js",),
            "workspace_odoo_capture_progress.html": ("/job-polling.js",),
            "workspace_load_progress.html": ("/job-polling.js",),
            "project_integrated_run.html": ("/job-polling.js",),
        }

        for relative_template, assets in expected_assets.items():
            with self.subTest(template=relative_template):
                template = (TEMPLATE_ROOT / relative_template).read_text(
                    encoding="utf-8"
                )
                positions = [template.index(asset) for asset in assets]
                self.assertEqual(positions, sorted(positions))

    def test_mapping_template_remains_split_by_server_contract(self) -> None:
        page = (TEMPLATE_ROOT / "mapping" / "page.html").read_text(
            encoding="utf-8"
        )
        expected_partials = (
            "_dataset.html",
            "_scalar_catalog.html",
            "_relationship_catalog.html",
            "_control_totals.html",
            "_validation.html",
            "_next_step.html",
            "_form_actions.html",
            "_matching_review_workbook.html",
            "_quality.html",
            "_value_match_dialog.html",
        )
        template_graph = "".join(
            template.read_text(encoding="utf-8")
            for template in sorted((TEMPLATE_ROOT / "mapping").glob("*.html"))
        )

        self.assertLessEqual(len(page.splitlines()), 100)
        for partial in expected_partials:
            with self.subTest(partial=partial):
                self.assertIn(partial, template_graph)
                partial_path = TEMPLATE_ROOT / "mapping" / partial
                self.assertTrue(partial_path.exists())
                self.assertLessEqual(
                    len(partial_path.read_text(encoding="utf-8").splitlines()),
                    600,
                )

    def test_mapping_editor_uses_the_position_module_public_contract(self) -> None:
        editor = (STATIC_ROOT / "mapping-editor.js").read_text(encoding="utf-8")
        position = (STATIC_ROOT / "mapping.js").read_text(encoding="utf-8")

        self.assertIn(
            "window.impodoMappingPosition = { rememberInteraction, remember };",
            position,
        )
        self.assertIn(
            "window.impodoMappingPosition?.rememberInteraction(event.target);",
            editor,
        )
        self.assertNotIn("rememberMappingInteraction", editor)
        dirty_handler = editor.split("const markMappingDirty", maxsplit=1)[1].split(
            'mappingForm.addEventListener("input"',
            maxsplit=1,
        )[0]
        self.assertLess(
            dirty_handler.index('saveStatus.classList.add("unsaved")'),
            dirty_handler.index(
                "window.impodoMappingPosition?.rememberInteraction(event.target);"
            ),
        )

    def test_static_modules_remain_reviewable(self) -> None:
        oversized_scripts = {
            script.name: len(script.read_text(encoding="utf-8").splitlines())
            for script in sorted(STATIC_ROOT.glob("*.js"))
            if len(script.read_text(encoding="utf-8").splitlines()) > 1_200
        }
        oversized_styles = {
            style.name: len(style.read_text(encoding="utf-8").splitlines())
            for style in sorted(STATIC_ROOT.glob("*.css"))
            if len(style.read_text(encoding="utf-8").splitlines()) > 2_500
        }

        self.assertEqual(oversized_scripts, {})
        self.assertEqual(oversized_styles, {})


if __name__ == "__main__":
    unittest.main()
