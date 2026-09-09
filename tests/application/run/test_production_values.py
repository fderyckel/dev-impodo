"""Validate Production answers before credential or application work."""

from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock

from impodo.application.run.production_values import ProductionRunValuesUseCase
from impodo.domain.recipe.models import RecipeError
from impodo.domain.recipe_parameters import EXPORT_AS_OF_PARAMETER_ID
from impodo.domain.run.production import ProductionRunError


class ProductionRunValuesTests(TestCase):
    def setUp(self):
        self.definition = {
            "source_shape": {"datasets": []},
            "parameter_definitions": {"parameters": [
                {"logical_parameter_id": "parameter:batch", "label": "Batch", "type": "integer", "required": True, "constraints": {}},
                {"logical_parameter_id": EXPORT_AS_OF_PARAMETER_ID, "label": "Export date", "type": "date", "required": True, "constraints": {}},
            ]},
            "control_definitions": {"controls": [
                {"logical_control_id": "control:amount", "name": "Opening total", "dataset_id": "dataset:balances", "unit": "EUR", "tolerance": "0.01", "invariant_expectation": False},
                {"logical_control_id": "control:fixed", "name": "Fixed total", "dataset_id": "dataset:balances", "unit": "EUR", "tolerance": "0", "invariant_expectation": True, "invariant_expected_total": "0"},
            ]},
        }
        self.recipes = Mock()
        self.recipes.read_revisions.return_value = {
            (recipe_id, 1): NS(recipe=NS(display_name=recipe_id, business_purpose="Opening balances"),
                              envelope={"semantic_hash": "semantic", "recipe": self.definition})
            for recipe_id in ("one", "two")
        }
        self.service = ProductionRunValuesUseCase(self.recipes)
        self.binding = NS(project_id="project", plan_content_hash="plan", data_version_id="data", content_hash="binding")
        self.plan = NS(project_id="project", content_hash="plan", selected_revisions=tuple(
            NS(recipe_id=recipe_id, recipe_revision=1, semantic_hash="semantic") for recipe_id in ("one", "two")))
        self.data = NS(project_id="project", data_version_id="data", source_package_hash="fresh", export_as_of="2026-09-09T00:00:00Z")
        self.review = self.service.review(self.binding, self.plan, self.data, actor=NS())

    def submitted(self, *, parameters=None, controls=None, evidence_hash=None):
        return self.service.submitted_values(
            self.review, {"parameter:batch": "0042"} if parameters is None else parameters,
            {"one": {"control:amount": "200.50"}, "two": {"control:amount": "-5"}} if controls is None else controls,
            expected_evidence_hash=evidence_hash or self.review.evidence_hash,
        )

    def test_shared_typed_values_and_recipe_specific_totals_use_one_bulk_read(self):
        values = self.submitted()
        self.assertEqual(len(self.review.values.editable_values), 1)
        self.assertEqual(self.review.values.editable_values[0].input_type, "number")
        self.assertEqual(values.parameters["one"]["parameter:batch"], 42)
        self.assertEqual(values.parameters["two"][EXPORT_AS_OF_PARAMETER_ID], "2026-09-09")
        self.assertEqual(values.controls["one"], {"control:amount": "200.50", "control:fixed": "0"})
        self.assertEqual(values.controls["two"]["control:amount"], "-5")
        self.recipes.read_revisions.assert_called_once()
        self.recipes.get.assert_not_called()
        self.recipes.read_revision.assert_not_called()

    def test_missing_invalid_and_nonfinite_answers_are_rejected(self):
        for parameters, controls in (({}, None), ({"parameter:batch": "4.2"}, None),
                                     (None, {}), (None, {"one": {"control:amount": "NaN"}})):
            with self.subTest(parameters=parameters, controls=controls), self.assertRaises(ValueError):
                self.submitted(parameters=parameters, controls=controls)

    def test_form_cannot_replace_fixed_values_or_supply_unknown_fields(self):
        for parameters, controls in (({EXPORT_AS_OF_PARAMETER_ID: "2020-01-01"}, None),
                                     (None, {"one": {"control:fixed": "7"}}),
                                     ({"unexpected": "value"}, None), (None, {"other": {}})):
            with self.subTest(parameters=parameters, controls=controls), self.assertRaises(ProductionRunError):
                self.submitted(parameters=parameters, controls=controls)

    def test_direct_activation_also_rejects_fixed_value_changes(self):
        with self.assertRaisesRegex(ProductionRunError, "export date"):
            self.service.normalize(self.review, {"one": {EXPORT_AS_OF_PARAMETER_ID: "2020-01-01"}}, {})

    def test_changed_delivery_or_recipe_does_not_accept_an_old_form(self):
        with self.assertRaisesRegex(ProductionRunError, "setup changed"):
            self.submitted(evidence_hash="old")
        self.plan.selected_revisions[0].semantic_hash = "changed"
        with self.assertRaises(RecipeError):
            self.service.review(self.binding, self.plan, self.data, actor=NS())

    def test_failed_request_retains_editable_answers_without_changing_fixed_values(self):
        plan = self.review.with_answers({"parameter:batch": "0042", EXPORT_AS_OF_PARAMETER_ID: "old"},
                                        {"one": {"control:amount": "17", "control:fixed": "99"}})
        self.assertEqual(plan.editable_values[0].supplied_value, "0042")
        self.assertEqual(plan.automatic_values[0].supplied_value, "2026-09-09")
        self.assertEqual(plan.editable_controls[0].supplied_value, "17")
        self.assertEqual(next(item for item in plan.controls if item.automatic).supplied_value, "0")
