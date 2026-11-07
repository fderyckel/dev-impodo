"""Pin the audited native archived-create difference and staged alternative."""

from unittest import TestCase, main

from tests.support.paths import REPOSITORY_ROOT
from tests.support.practice_manufacturing_target import PracticeManufacturingTarget

SEED = (
    REPOSITORY_ROOT
    / "scenarios/impodo-practice/manufacturing/v1/target-seed-projection.json"
)


class NativeArchivedVariantTests(TestCase):
    def test_bounded_namespace_domains_use_native_wildcards(self):
        target = PracticeManufacturingTarget(SEED, major=20)
        template, _variant = self.create(target, active=True)
        for operator, pattern, expected in (
            ("like", "MA-P-%", True),
            ("=like", "MA-P-____", True),
            ("=like", "MA-P-", False),
            ("ilike", "ma-p-%", True),
            ("like", "OTHER-%", False),
        ):
            with self.subTest(operator=operator, pattern=pattern):
                self.assertEqual(
                    target.matches(
                        "product.template",
                        template,
                        (("default_code", operator, pattern),),
                    ),
                    expected,
                )

    def create(self, target, *, active):
        identifier = target.create_rows(
            "product.template",
            (
                {
                    "name": "Fictional archive regression",
                    "default_code": "MA-P-0031",
                    "uom_id": 1,
                    "type": "service",
                    "active": active,
                },
            ),
        )[0]
        variant = target.value("product.template", identifier, "product_variant_id")
        return identifier, variant

    def test_direct_inactive_create_distinguishes_actual_native_majors(self):
        for major, expected_code in ((19, "MA-P-0031"), (20, False)):
            with self.subTest(major=major):
                target = PracticeManufacturingTarget(SEED, major=major)
                template, variant = self.create(target, active=False)
                self.assertEqual(
                    target.value("product.template", template, "default_code"),
                    "MA-P-0031",
                )
                self.assertEqual(
                    target.value("product.product", variant, "default_code"),
                    expected_code,
                )
                self.assertIs(target.value("product.product", variant, "active"), False)

    def test_create_active_then_explicit_status_phase_preserves_business_reference(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                target = PracticeManufacturingTarget(SEED, major=major)
                template, variant = self.create(target, active=True)
                target.update_row("product.template", template, {"active": False})
                self.assertEqual(
                    target.value("product.product", variant, "default_code"),
                    "MA-P-0031",
                )
                self.assertIs(target.value("product.product", variant, "active"), False)
                self.assertEqual(
                    target.value("product.template", template, "product_variant_ids"),
                    [variant],
                )
                self.assertEqual(
                    [model for model, _, _ in target.writes],
                    ["product.template", "product.template"],
                )

    def test_unrelated_update_does_not_heal_failed_native20_variant_code(self):
        target = PracticeManufacturingTarget(SEED, major=20)
        template, variant = self.create(target, active=False)
        target.update_row(
            "product.template", template, {"name": "Another reviewed name"}
        )
        self.assertIs(target.value("product.product", variant, "default_code"), False)


if __name__ == "__main__":
    main()
