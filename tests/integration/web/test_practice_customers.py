"""Run the shared CSV exercises through real persisted application services."""

from pathlib import Path

from scripts.practice_customer_execution import (
    assert_controlled_projection,
    classifications,
    exercise_workspace,
    immediate_overrides,
)
from scripts.practice_customer_workflow import PracticeCustomerWorkflow
from scripts.qualify_practice_customers import _recipe_row_confirmation
from tests.support.practice_customer_target import PracticeCustomerTarget

ROOT = Path(__file__).resolve().parents[3] / "scenarios/impodo-practice/v1"


class PracticeCustomerTests(PracticeCustomerWorkflow):
    def target(self, major, mode, company="NORTH"):
        return PracticeCustomerTarget(
            ROOT / "target-seed-projection.json",
            major=major,
            mode=mode,
            company=company,
        )

    def test_customer_enrichment_on_each_supported_major_and_access_mode(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    target = self.target(major, mode)
                    workspace, _, _ = self.author(
                        root=ROOT,
                        target=target,
                        major=major,
                        mode=mode,
                        phase="enrichment",
                    )
                    exercise_workspace(
                        self, workspace, phase="enrichment", target=target
                    )
                    assert_controlled_projection(
                        self, target, ROOT, company="NORTH", phase="enrichment"
                    )

    def test_staged_payment_terms_on_each_supported_major_and_access_mode(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    target = self.target(major, mode)
                    for phase in ("headers", "schedule", "customers"):
                        workspace, _, _ = self.author(
                            root=ROOT,
                            target=target,
                            major=major,
                            mode=mode,
                            phase=phase,
                        )
                        exercise_workspace(self, workspace, phase=phase, target=target)
                        assert_controlled_projection(
                            self, target, ROOT, company="NORTH", phase=phase
                        )

    def test_incoming_term_dependency_survives_real_comparison_and_reopening(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    target = self.target(major, mode)
                    refs = ("MA-N-0001", "MA-N-0003")
                    workspace, _, _ = self.author(
                        root=ROOT,
                        target=target,
                        major=major,
                        mode=mode,
                        phase="incoming-immediate",
                        source_overrides=immediate_overrides(ROOT, "NORTH"),
                        customer_refs=refs,
                    )
                    exercise_workspace(
                        self, workspace, phase="incoming-immediate", target=target
                    )
                    assert_controlled_projection(
                        self,
                        target,
                        ROOT,
                        company="NORTH",
                        phase="incoming-immediate",
                        refs=refs,
                    )

    def test_recipe_reuses_enrichment_on_fresh_delivery_for_each_major_and_mode(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    target = self.target(major, mode)
                    original, _, _ = self.author(
                        root=ROOT,
                        target=target,
                        major=major,
                        mode=mode,
                        phase="enrichment",
                    )
                    exercise_workspace(
                        self, original, phase="enrichment", target=target
                    )
                    publication = self.publish_practice_recipe(
                        original, label="Morrow enrichment"
                    )
                    application, _ = self.apply_practice_recipe(
                        publication,
                        source_workspace_id=original,
                        root=ROOT,
                        target=target,
                        major=major,
                        mode=mode,
                        phase="enrichment",
                    )
                    application = _recipe_row_confirmation(
                        self, application, "enrichment"
                    )
                    self.assertEqual(application.status.value, "READY")
                    exercise_workspace(
                        self,
                        application.workspace_id,
                        phase="enrichment",
                        target=target,
                        expected=classifications(0, 1, 8),
                    )
                    records = [
                        row
                        for (model, _), row in target.records.items()
                        if model == "res.partner" and row.get("ref") == "MA-N-0001"
                    ]
                    self.assertEqual(records[0]["phone"], "+12025550199")
