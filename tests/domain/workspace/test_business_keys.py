from __future__ import annotations

from datetime import datetime, timezone
import unittest

from impodo.domain.workspace.business_keys import (
    BusinessKeyRecommendationBasis,
    BusinessKeyRecommendationOutcome,
    assess_business_key_recommendation,
    recommend_business_key,
)
from impodo.domain.shared.models import UniqueConstraintMetadata
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
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
        assessment = assess_business_key_recommendation(model)
        self.assertEqual(
            assessment.outcome,
            BusinessKeyRecommendationOutcome.MULTIPLE_CANDIDATES,
        )
        self.assertEqual(
            tuple(item.key_fields for item in assessment.alternatives),
            (("code",), ("serial",)),
        )

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

    def test_partner_uses_reviewed_reference_instead_of_name_guess(self) -> None:
        model = _model(
            "res.partner",
            (
                _field("ref", "Reference"),
                _field("name", "Name", required=True),
            ),
        )

        recommendation = recommend_business_key(model)

        self.assertEqual(recommendation.key_fields, ("ref",))
        self.assertEqual(recommendation.scope_fields, ())
        self.assertEqual(
            recommendation.basis,
            BusinessKeyRecommendationBasis.CURATED_CONVENTION,
        )
        self.assertIn("duplicate", recommendation.warning)

    def test_bom_line_is_suggested_within_its_parent_bom(self) -> None:
        model = _model(
            "mrp.bom.line",
            (
                _field("sequence", "Sequence", field_type="integer"),
                _field(
                    "bom_id",
                    "Parent BoM",
                    field_type="many2one",
                    relation="mrp.bom",
                ),
                _field(
                    "product_id",
                    "Component",
                    field_type="many2one",
                    relation="product.product",
                ),
            ),
        )

        recommendation = recommend_business_key(model)

        self.assertEqual(recommendation.key_fields, ("sequence",))
        self.assertEqual(recommendation.scope_fields, ("bom_id",))
        self.assertIn("parent bill of materials", recommendation.reason)

    def test_work_center_usage_is_suggested_within_its_parent_bom(self) -> None:
        model = _model(
            "mrp.routing.workcenter",
            (
                _field("name", "Operation"),
                _field(
                    "bom_id",
                    "Bill of Material",
                    field_type="many2one",
                    relation="mrp.bom",
                ),
            ),
        )

        recommendation = recommend_business_key(model)

        self.assertEqual(recommendation.key_fields, ("name",))
        self.assertEqual(recommendation.scope_fields, ("bom_id",))
        self.assertEqual(
            recommendation.technical_summary,
            "Operation (name), within Bill of Material (bom_id)",
        )

    def test_parent_scoped_suggestion_requires_the_expected_relation(self) -> None:
        model = _model(
            "mrp.routing.workcenter",
            (
                _field("name", "Operation"),
                _field(
                    "bom_id",
                    "Wrong parent",
                    field_type="many2one",
                    relation="x.other",
                ),
            ),
        )

        self.assertIsNone(recommend_business_key(model))

    def test_representative_odoo_models_have_reviewed_compatible_rules(self) -> None:
        cases = (
            ("res.country", (_field("code", "Code"),), ("code",), ()),
            ("res.lang", (_field("code", "Code"),), ("code",), ()),
            ("res.currency", (_field("name", "Code"),), ("name",), ()),
            ("res.partner", (_field("ref", "Reference"),), ("ref",), ()),
            ("res.company", (_field("name", "Name"),), ("name",), ()),
            (
                "product.template",
                (_field("default_code", "Internal Reference"),),
                ("default_code",),
                (),
            ),
            (
                "product.product",
                (_field("default_code", "Internal Reference"),),
                ("default_code",),
                (),
            ),
            (
                "product.category",
                (
                    _field("name", "Name"),
                    _field(
                        "parent_id",
                        "Parent Category",
                        field_type="many2one",
                        relation="product.category",
                    ),
                ),
                ("name",),
                ("parent_id",),
            ),
            (
                "uom.uom",
                (
                    _field("name", "Name"),
                    _field(
                        "category_id",
                        "Category",
                        field_type="many2one",
                        relation="uom.category",
                    ),
                ),
                ("name",),
                ("category_id",),
            ),
            ("mrp.bom", (_field("code", "Reference"),), ("code",), ()),
            (
                "mrp.bom.line",
                (
                    _field("sequence", "Sequence", field_type="integer"),
                    _field(
                        "bom_id",
                        "Parent BoM",
                        field_type="many2one",
                        relation="mrp.bom",
                    ),
                ),
                ("sequence",),
                ("bom_id",),
            ),
            (
                "mrp.routing.workcenter",
                (
                    _field("name", "Operation"),
                    _field(
                        "bom_id",
                        "Bill of Material",
                        field_type="many2one",
                        relation="mrp.bom",
                    ),
                ),
                ("name",),
                ("bom_id",),
            ),
        )

        for model_name, fields, key_fields, scope_fields in cases:
            with self.subTest(model=model_name):
                recommendation = recommend_business_key(
                    _model(model_name, fields)
                )
                self.assertIsNotNone(recommendation)
                self.assertEqual(recommendation.key_fields, key_fields)
                self.assertEqual(recommendation.scope_fields, scope_fields)

    def test_governance_round_trip_retains_suggestion_provenance(self) -> None:
        governance = SchemaGovernance(
            governance_id="governance",
            version=1,
            workspace_id="workspace",
            catalog_hash="sha256:" + "a" * 64,
            permitted_models=("mrp.routing.workcenter",),
            business_keys=(
                BusinessKeyDefinition(
                    key_id="operation-within-bom",
                    model="mrp.routing.workcenter",
                    key_fields=("name",),
                    scope_fields=("bom_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                    recommendation_basis="CURATED_CONVENTION",
                    recommendation_policy_version=1,
                ),
            ),
            recorded_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )

        restored = SchemaGovernance.from_json(governance.to_json())

        self.assertEqual(restored, governance)

    def test_governance_without_provenance_keeps_legacy_payload_shape(self) -> None:
        governance = SchemaGovernance(
            governance_id="governance",
            version=1,
            workspace_id="workspace",
            catalog_hash="sha256:" + "a" * 64,
            permitted_models=("res.partner",),
            business_keys=(
                BusinessKeyDefinition(
                    key_id="partner-ref",
                    model="res.partner",
                    key_fields=("ref",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
            recorded_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
            recorded_by="Data Manager",
        )

        key_payload = governance.to_dict()["business_keys"][0]

        self.assertNotIn("recommendation_basis", key_payload)
        self.assertNotIn("recommendation_policy_version", key_payload)
        self.assertEqual(SchemaGovernance.from_json(governance.to_json()), governance)


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
