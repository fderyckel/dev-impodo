"""Exercise the staged Recipe-run Odoo check without a live Odoo server."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from impodo.application.run.recipe_run_jobs import RecipeRunJobPhase
from impodo.web.routers.schema import _perform_run_odoo_check
from impodo.web.run_urls import RunSetupKind


class RecipeRunOdooProgressTests(TestCase):
    def test_odoo_check_reports_each_bounded_stage_before_continuing(self) -> None:
        workspace = SimpleNamespace(
            workspace_id="workspace-1",
            intended_models=("res.partner",),
        )
        binding = SimpleNamespace(
            setup_workspace_id=workspace.workspace_id,
            state=SimpleNamespace(value="SETUP"),
        )
        plan = SimpleNamespace(
            model_names=("res.partner",),
            models=(SimpleNamespace(field_names=("name", "lang")),),
            supporting_values=(SimpleNamespace(),),
        )
        schema = SimpleNamespace(pending_refresh=None)
        result = SimpleNamespace(applications=())
        context = SimpleNamespace(
            actor=object(),
            queries=SimpleNamespace(get_odoo_schema_catalog=lambda _workspace_id: None),
            run_planning=SimpleNamespace(
                target_evidence_from_workspace=lambda *_args, **_kwargs: (
                    "target schema",
                    None,
                ),
                repository=SimpleNamespace(
                    list_applications=lambda _run_id: (),
                    list_run_issues=lambda _run_id: {},
                ),
            ),
            test_runs=SimpleNamespace(
                resume_activation_if_needed=lambda *_args, **_kwargs: None,
                activate=MagicMock(return_value=result),
            ),
            preparation_jobs=None,
            recipe_target_matches=SimpleNamespace(prepare_review=MagicMock()),
            secret_store=object(),
        )
        reports = []

        with (
            patch(
                "impodo.web.routers.schema._run_odoo_check_scope",
                return_value=(binding, SimpleNamespace(), workspace, plan),
            ),
            patch(
                "impodo.web.routers.schema._capture_selected_schema_sync",
                return_value=schema,
            ),
            patch("impodo.web.routers.schema._capture_recipe_supporting_values"),
            patch(
                "impodo.web.routers.schema.get_target_credential",
                return_value=SimpleNamespace(binding_hash="credential-generation"),
            ),
        ):
            completed = _perform_run_odoo_check(
                context,
                "project-1",
                "run-1",
                RunSetupKind.TEST,
                expected_workspace_revision=7,
                operation_id="operation-1",
                report=reports.append,
            )

        self.assertEqual(completed.redirect_url, "/projects/project-1/runs/run-1")
        self.assertEqual(completed.message, "Odoo is ready for Review and load")
        phases = [item.phase for item in reports]
        self.assertEqual(
            phases,
            [
                RecipeRunJobPhase.VALIDATING,
                RecipeRunJobPhase.READING_ODOO,
                RecipeRunJobPhase.CHECKING_FIELDS,
                RecipeRunJobPhase.SUPPORTING_VALUES,
                RecipeRunJobPhase.SUPPORTING_VALUES,
                RecipeRunJobPhase.MATERIALIZING,
                RecipeRunJobPhase.TARGET_MATCHES,
            ],
        )
        self.assertEqual(reports[1].total_units, 2)
        self.assertEqual(reports[2].completed_units, 2)
        self.assertEqual(reports[3].total_units, 1)
        context.test_runs.activate.assert_called_once()
        self.assertEqual(
            context.test_runs.activate.call_args.kwargs["progress"].__name__,
            "<lambda>",
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
