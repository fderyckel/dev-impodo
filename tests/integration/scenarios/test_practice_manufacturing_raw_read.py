"""Regressions for archived inverse proof, including independent links."""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock

from impodo.adapters.odoo.connectors import Json2ReadConnector
from impodo.domain.shared.models import TargetRecord
from scripts import practice_manufacturing_raw_read as raw
from scripts.practice_manufacturing_projection import ManufacturingProjection


class ArchivedVariantProofTests(TestCase):
    def setup_case(self, *, inverse=707, raw_code="MA-P-0031", raw_active=False):
        expected = {
            "product_ref": "MA-P-0031",
            "name": "Archived fictional product",
            "price": "0.00",
            "target_type": "service",
            "active": False,
            "category_path": ["Services", "Design"],
            "tags": [],
        }
        template = TargetRecord(
            "product.template",
            501,
            {
                "default_code": "MA-P-0031",
                "name": expected["name"],
                "list_price": 0.0,
                "type": "service",
                "active": False,
                "company_id": False,
                "categ_id": 9,
                "uom_id": 1,
                "product_tag_ids": [],
                "product_variant_ids": [],
            },
        )
        variant = TargetRecord(
            "product.product",
            707,
            {
                "default_code": "MA-P-0031",
                "product_tmpl_id": 501,
                "active": False,
                "company_id": False,
            },
        )
        connector = SimpleNamespace(
            _config=SimpleNamespace(
                base_url="http://127.0.0.1:8069",
                context={"allowed_company_ids": [9], "active_test": False},
            ),
            _request_url=Mock(
                return_value=(
                    200,
                    [
                        {
                            "id": 501,
                            "default_code": raw_code,
                            "active": raw_active,
                            "product_variant_ids": [inverse],
                        }
                    ],
                )
            ),
        )
        return expected, template, variant, connector

    def projection(self, expected, template, variant, connector, *, corrected, major):
        def records(model, fields, domain, **kwargs):
            self.assertTrue(domain)
            if model == "product.template":
                return (
                    raw.read_template_variant_links(connector, (template,), major=major)
                    if corrected
                    else (template,)
                )
            self.assertEqual(model, "product.product")
            return (variant,)

        projection = ManufacturingProjection(
            records, major, {"NORTH": 9, "SOUTH": 19}, {"products": [expected]}
        )
        projection._category_index = Mock(
            return_value={
                tuple(expected["category_path"]): TargetRecord(
                    "product.category", 9, {}
                )
            }
        )
        projection._setup_relations = Mock(return_value=(1, {}))
        return projection

    def test_archived_inverse_requires_actual_read_and_preserves_zero_false(self):
        before = Json2ReadConnector._READ_METHODS
        for major in (19, 20):
            with self.subTest(major=major):
                expected, template, variant, connector = self.setup_case()
                standard = self.projection(
                    expected, template, variant, connector, corrected=False, major=major
                )
                with self.assertRaisesRegex(
                    RuntimeError, "^TEMPLATE_VARIANT_LINK_DIFFERED$"
                ):
                    standard._products([expected], company="NORTH", exact=True)
                connector._request_url.assert_not_called()
                corrected = self.projection(
                    expected, template, variant, connector, corrected=True, major=major
                )
                templates, variants = corrected._products(
                    [expected], company="NORTH", exact=True
                )
                self.assertEqual(variants["MA-P-0031"].odoo_id, 707)
                self.assertIs(templates["MA-P-0031"].values["active"], False)
                self.assertEqual(
                    Decimal(str(templates["MA-P-0031"].values["list_price"])),
                    Decimal(0),
                )
                call = connector._request_url.call_args
                self.assertEqual(
                    call.args, ("http://127.0.0.1:8069/json/2/product.template/read",)
                )
                self.assertEqual(call.kwargs["method"], "POST")
                self.assertEqual(
                    json.loads(call.kwargs["body"]),
                    {
                        "ids": [501],
                        "fields": ["default_code", "active", "product_variant_ids"],
                        "context": {"allowed_company_ids": [9], "active_test": False},
                    },
                )
        self.assertEqual(Json2ReadConnector._READ_METHODS, before)
        self.assertNotIn("read", Json2ReadConnector._READ_METHODS)

    def test_wrong_native_inverse_still_fails_independent_forward_ownership(self):
        expected, template, variant, connector = self.setup_case(inverse=708)
        projection = self.projection(
            expected, template, variant, connector, corrected=True, major=20
        )
        with self.assertRaisesRegex(RuntimeError, "^TEMPLATE_VARIANT_LINK_DIFFERED$"):
            projection._products([expected], company="NORTH", exact=True)
        connector._request_url.assert_called_once()

    def test_unreviewed_template_never_crosses_private_transport(self):
        _, template, _, connector = self.setup_case()
        template = TargetRecord(
            template.model,
            template.odoo_id,
            {**template.values, "default_code": "UNRELATED"},
        )
        with self.assertRaisesRegex(
            RuntimeError, "^EXACT_VARIANT_READ_SCOPE_REQUIRED$"
        ):
            raw.read_template_variant_links(connector, (template,), major=19)
        connector._request_url.assert_not_called()

    def test_changed_identity_or_archived_flag_never_replaces_values(self):
        for change in ({"raw_code": "MA-P-0032"}, {"raw_active": True}):
            with self.subTest(change=change):
                _, template, _, connector = self.setup_case(**change)
                with self.assertRaisesRegex(
                    RuntimeError, "^EXACT_VARIANT_READ_BINDING_DIFFERED$"
                ):
                    raw.read_template_variant_links(connector, (template,), major=19)
                self.assertEqual(template.values["product_variant_ids"], [])

    def test_incomplete_duplicate_or_unrequested_read_fails_closed(self):
        for change in ("missing", "foreign", "extra_field", "empty_inverse"):
            with self.subTest(change=change):
                _, template, _, connector = self.setup_case()
                payload = connector._request_url.return_value[1]
                if change == "missing":
                    payload.clear()
                elif change == "foreign":
                    payload[0]["id"] = 502
                elif change == "extra_field":
                    payload[0]["name"] = "not requested"
                else:
                    payload[0]["product_variant_ids"] = []
                with self.assertRaises(RuntimeError):
                    raw.read_template_variant_links(connector, (template,), major=20)


if __name__ == "__main__":
    main()
