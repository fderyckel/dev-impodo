"""Preserve the Recipe target-major boundary before enabling a second major."""

from types import SimpleNamespace
import unittest

from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.domain.workspace.reference_keys import REFERENCE_POLICY_HASHES


class RecipeOdooVersionBaselineTests(unittest.TestCase):
    @staticmethod
    def assessment(recipe_major, target_version):
        # The same empty field projection isolates version compatibility from
        # model differences. It grants no Recipe publication or write authority.
        definition = {
            "contract_versions": {"odoo_target_contract": 2},
            "odoo_target_contract": {
                "odoo_major_version": recipe_major,
                "reference_policy_hash": REFERENCE_POLICY_HASHES.get(
                    recipe_major,
                    REFERENCE_POLICY_HASHES[19],
                ),
                "models": [],
            },
            "mapping": {"datasets": []},
        }
        return RecipeApplicationCompiler()._target_assessment(
            definition, SimpleNamespace(odoo_version=target_version, models=())
        )[1]

    def test_odoo19_recipe_keeps_same_major_application_behavior(self):
        for target_version in ("19.0", "19.4"):
            with self.subTest(target_version=target_version):
                self.assertEqual(self.assessment(19, target_version), [])

    def test_cross_major_application_is_blocked_even_with_identical_fields(self):
        for recipe_major, target_version in ((19, "20.0"), (20, "19.0")):
            with self.subTest(recipe_major=recipe_major, target_version=target_version):
                issues = self.assessment(recipe_major, target_version)
                self.assertIn("RECIPE_TARGET_VERSION_INCOMPATIBLE", {issue.code for issue in issues})

    def test_odoo20_recipe_keeps_same_major_read_assessment_behavior(self):
        self.assertEqual(self.assessment(20, "20.0"), [])

    def test_malformed_recipe_targets_remain_blocked(self):
        for recipe_major, target_version in ((19, "19.garbage"), (19, "unknown")):
            with self.subTest(recipe_major=recipe_major, target_version=target_version):
                issues = self.assessment(recipe_major, target_version)
                self.assertIn("RECIPE_TARGET_VERSION_UNSUPPORTED", {issue.code for issue in issues})


if __name__ == "__main__":
    unittest.main()
