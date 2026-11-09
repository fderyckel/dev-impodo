"""Exercise shared manufacturing through actual persisted application services."""

import json
from functools import partial

from scripts.practice_customer_execution import classifications, exercise_workspace
from scripts.practice_manufacturing_live import ControlledManufacturingWorkflow
from scripts.practice_manufacturing_projection import ManufacturingProjection
from scripts.practice_manufacturing_recipe_review import confirm_manufacturing_recipe
from scripts.practice_manufacturing_workflow import MANUFACTURING_COUNTS
from scripts.qualify_practice_manufacturing import (
    POPULATIONS,
    ROOT,
    STAGES,
    check_snapshot,
    controlled_records,
)
from tests.support.morrow_uncertain_write import recover_south_bom
from tests.support.practice_manufacturing_target import PracticeManufacturingTarget


class ManufacturingWorkflowTests(ControlledManufacturingWorkflow):
    def test_odoo19_local(self):
        self.complete(19, "LOCAL")

    def test_odoo19_remote(self):
        self.complete(19, "REMOTE")

    def test_odoo20_local(self):
        self.complete(20, "LOCAL")

    def test_odoo20_remote(self):
        self.complete(20, "REMOTE")

    def test_lost_bom_reply_odoo19_local(self):
        self.complete(19, "LOCAL", lose_bom_reply=True)

    def test_lost_bom_reply_odoo19_remote(self):
        self.complete(19, "REMOTE", lose_bom_reply=True)

    def test_lost_bom_reply_odoo20_local(self):
        self.complete(20, "LOCAL", lose_bom_reply=True)

    def test_lost_bom_reply_odoo20_remote(self):
        self.complete(20, "REMOTE", lose_bom_reply=True)

    def complete(self, major, mode, *, lose_bom_reply=False):
        target = PracticeManufacturingTarget(
            ROOT / "target-seed-projection.json", major=major, mode=mode
        )
        projection = ManufacturingProjection(
            controlled_records(target),
            major,
            target.company_ids,
            json.loads((ROOT / "expected-business-results.json").read_text()),
        )
        for phase, company in STAGES:
            other = "SOUTH" if company == "NORTH" else "NORTH"
            preserved = projection.snapshot_company(other) if phase == "boms" else None
            workspace, _, _revision = self.author(
                root=ROOT,
                target=target,
                major=major,
                mode=mode,
                phase=phase,
                company=company,
            )
            if lose_bom_reply and (phase, company) == ("boms", "SOUTH"):
                self.prepare(workspace)
                self.approve(workspace)
                publication = self.publish_practice_recipe(
                    workspace,
                    label="South Morrow BoMs",
                    business_purpose="Reuse the reviewed South manufacturing rules",
                )
                application, _schema = self.apply_practice_recipe(
                    publication,
                    source_workspace_id=workspace,
                    root=ROOT,
                    target=target,
                    major=major,
                    mode=mode,
                    phase=phase,
                    company=company,
                    run_label="Fresh South Morrow BoMs",
                )
                application = confirm_manufacturing_recipe(
                    self, application, phase, expected_population=POPULATIONS[phase]
                )
                recover_south_bom(self, application.workspace_id, target)
            else:
                receipt = exercise_workspace(
                    self,
                    workspace,
                    phase=phase,
                    target=target,
                    expected=classifications(*MANUFACTURING_COUNTS[phase, company]),
                    inspect_snapshot=partial(
                        check_snapshot, self, phase=phase, company=company
                    ),
                )
                self.assertTrue(receipt["persisted_reopen_verified"])
            if phase == "categories":
                projection.verify_categories()
                self.assertEqual(len(target.category_paths()), 12)
                target.seed_existing_products()
            elif phase == "products":
                projection.verify_products(company, initial=True)
            elif phase == "product-status":
                projection.verify_products(company)
            else:
                projection.verify_boms(company)
                self.assertEqual(projection.snapshot_company(other), preserved)
        self.assertEqual(projection.verify_products(), {"products": 32, "variants": 32})
        self.assertEqual(
            sum(model == "product.template" for model, _ in target.records), 32
        )
        self.assertEqual(
            sum(model == "product.product" for model, _ in target.records), 32
        )
        self.assertEqual(sum(model == "mrp.bom" for model, _ in target.records), 6)
        self.assertEqual(
            sum(model == "mrp.bom.line" for model, _ in target.records), 24
        )
        self.assertEqual(
            sum(model == "mrp.routing.workcenter" for model, _ in target.records), 12
        )
