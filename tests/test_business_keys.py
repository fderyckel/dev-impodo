from __future__ import annotations

import unittest

from impodo.domain.workspace.business_keys import recommend_business_key
from impodo.domain.shared.models import UniqueConstraintMetadata
from impodo.domain.workspace.reference_keys import standard_reference_key
from impodo.domain.workspace.contracts import SchemaField, SchemaModel


class BusinessKeyRecommendationTests(unittest.TestCase):
    def test_only_reviewed_standard_reference_keys_are_available_without_capture(
        self,
    ) -> None:
        country = standard_reference_key("res.country")

        self.assertIsNotNone(country)
        self.assertEqual(country.key_fields, ("code",))
        self.assertEqual(country.display_field, "name")
        self.assertEqual(country.field_contract("code").field_type, "char")
        self.assertTrue(country.field_contract("code").required)
        self.assertFalse(country.field_contract("code").readonly)
        language = standard_reference_key("res.lang")
        currency = standard_reference_key("res.currency")
        self.assertIsNotNone(language)
        self.assertIsNotNone(currency)
        self.assertEqual(language.key_fields, ("code",))
        self.assertEqual(currency.key_fields, ("name",))
        self.assertIsNone(standard_reference_key("product.product"))

    def test_custom_unique_constraint_becomes_simple_labelled_recommendation(self) -> None:
        model = _model(
            "x.asset",
            (
                _field("code", "Asset Code", required=True),
                _field(
                    "company_id",
                    "Company",
                    field_type="many2one",
                    relation="res.company",
                    required=True,
                ),
            ),
            constraints=(
                UniqueConstraintMetadata(
                    name="x_asset_code_company_uniq",
                    definition="UNIQUE(code, company_id)",
                ),
            ),
        )

        recommendation = recommend_business_key(model)

        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation.key_fields, ("code",))
        self.assertEqual(recommendation.scope_fields, ("company_id",))
        self.assertEqual(
            recommendation.technical_summary,
            "Asset Code (code), within Company (company_id)",
        )
        self.assertEqual(recommendation.evidence, "Enforced by Odoo")

    def test_ambiguous_constraints_do_not_guess_for_custom_model(self) -> None:
        model = _model(
            "x.asset",
            (
                _field("code", "Asset Code", required=True),
                _field("serial", "Serial Number", required=True),
            ),
            constraints=(
                UniqueConstraintMetadata("code_uniq", "unique(code)"),
                UniqueConstraintMetadata("serial_uniq", "unique(serial)"),
            ),
        )

        self.assertIsNone(recommend_business_key(model))

    def test_nullable_unique_constraint_carries_plain_warning(self) -> None:
        model = _model(
            "x.asset",
            (_field("serial", "Serial Number", required=False),),
            constraints=(
                UniqueConstraintMetadata("serial_uniq", "unique(serial)"),
            ),
        )

        recommendation = recommend_business_key(model)

        self.assertEqual(
            recommendation.evidence,
            "Enforced by Odoo when populated",
        )
        self.assertIn("blank values", recommendation.warning)

    def test_expression_constraint_is_not_treated_as_a_field_rule(self) -> None:
        model = _model(
            "x.asset",
            (_field("code", "Asset Code", required=True),),
            constraints=(
                UniqueConstraintMetadata("lower_code_uniq", "unique(lower(code))"),
            ),
        )

        self.assertIsNone(recommend_business_key(model))

    def test_product_convention_is_explicitly_not_uniqueness_proof(self) -> None:
        model = _model(
            "product.template",
            (_field("default_code", "Internal Reference"),),
        )

        recommendation = recommend_business_key(model)

        self.assertEqual(recommendation.evidence, "Common Odoo convention")
        self.assertIn("duplicate", recommendation.warning)
        self.assertIn("multi-variant", recommendation.warning)

    def test_partner_has_no_name_based_guess(self) -> None:
        model = _model(
            "res.partner",
            (
                _field("ref", "Reference"),
                _field("name", "Name", required=True),
            ),
        )

        self.assertIsNone(recommend_business_key(model))


def _model(
    name: str,
    fields: tuple[SchemaField, ...],
    *,
    constraints: tuple[UniqueConstraintMetadata, ...] = (),
) -> SchemaModel:
    return SchemaModel(
        name=name,
        label=name,
        fields=fields,
        unique_constraints=constraints,
    )


def _field(
    name: str,
    label: str,
    *,
    field_type: str = "char",
    relation: str | None = None,
    required: bool = False,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=label,
        type=field_type,
        required=required,
        readonly=False,
        relation=relation,
        relation_field=None,
        selection=(),
    )


if __name__ == "__main__":
    unittest.main()
