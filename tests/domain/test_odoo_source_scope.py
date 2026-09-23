from __future__ import annotations

import unittest

from impodo.domain.odoo_source_scope import (
    RelatedDataHandling,
    propose_related_odoo_data,
)
from impodo.domain.workspace.contracts import SchemaField, SchemaModel


class OdooSourceScopeTests(unittest.TestCase):
    def test_product_relationships_are_grouped_by_business_handling(self) -> None:
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="product.template",
                    label="Product",
                    fields=(
                        _relationship("categ_id", "Product Category", "product.category"),
                        _relationship("uom_id", "Unit of Measure", "uom.uom", required=True),
                        _relationship(
                            "attribute_line_ids",
                            "Product Attributes",
                            "product.template.attribute.line",
                            field_type="one2many",
                        ),
                        _relationship(
                            "product_variant_ids",
                            "Product Variants",
                            "product.product",
                            field_type="one2many",
                            readonly=True,
                        ),
                        _relationship(
                            "bom_ids",
                            "Bills of Materials",
                            "mrp.bom",
                            field_type="one2many",
                        ),
                        _relationship("company_id", "Company", "res.company"),
                        _relationship(
                            "activity_ids",
                            "Activities",
                            "mail.activity",
                            field_type="one2many",
                        ),
                        _relationship("x_owner_id", "Owner", "x.owner"),
                    ),
                ),
            )
        )

        by_field = {item.field_name: item.handling for item in suggestions}
        self.assertEqual(
            by_field,
            {
                "activity_ids": RelatedDataHandling.EXCLUDE_HISTORY,
                "attribute_line_ids": RelatedDataHandling.OPTIONAL_BUSINESS_DATA,
                "bom_ids": RelatedDataHandling.SEPARATE_PROCESS,
                "categ_id": RelatedDataHandling.INCLUDE_SUPPORTING,
                "company_id": RelatedDataHandling.REUSE_DESTINATION,
                "product_variant_ids": RelatedDataHandling.ODOO_MANAGED,
                "uom_id": RelatedDataHandling.INCLUDE_SUPPORTING,
                "x_owner_id": RelatedDataHandling.NEEDS_DECISION,
            },
        )

    def test_selected_or_ineligible_relationships_are_not_proposed(self) -> None:
        product = SchemaModel(
            name="product.template",
            label="Product",
            fields=(
                _relationship("uom_id", "Unit of Measure", "uom.uom"),
                _relationship(
                    "categ_id",
                    "Product Category",
                    "product.category",
                    exportable=False,
                ),
                _relationship(
                    "x_related_id",
                    "Related value",
                    "x.value",
                    related=True,
                ),
                _relationship(
                    "x_company_value_id",
                    "Company value",
                    "x.value",
                    company_dependent=True,
                ),
            ),
        )
        uom = SchemaModel(name="uom.uom", label="Unit of Measure", fields=())

        self.assertEqual(propose_related_odoo_data((product, uom)), ())

    def test_unit_category_is_supporting_data_when_discovered(self) -> None:
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="uom.uom",
                    label="Unit of Measure",
                    fields=(
                        _relationship(
                            "category_id",
                            "Unit of Measure Category",
                            "uom.category",
                            required=True,
                        ),
                    ),
                ),
            )
        )

        self.assertEqual(len(suggestions), 1)
        self.assertIs(
            suggestions[0].handling,
            RelatedDataHandling.INCLUDE_SUPPORTING,
        )

    def test_unknown_custom_relationships_fail_closed(self) -> None:
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="x.document",
                    label="Document",
                    fields=(
                        _relationship(
                            "parent_id",
                            "Parent",
                            "x.parent",
                            required=True,
                        ),
                        _relationship("reviewer_id", "Reviewer", "x.reviewer"),
                    ),
                ),
            )
        )

        by_field = {item.field_name: item.handling for item in suggestions}
        self.assertIs(
            by_field["parent_id"],
            RelatedDataHandling.NEEDS_DECISION,
        )
        self.assertIs(
            by_field["reviewer_id"],
            RelatedDataHandling.NEEDS_DECISION,
        )


def _relationship(
    name: str,
    label: str,
    relation: str,
    *,
    field_type: str = "many2one",
    required: bool = False,
    readonly: bool = False,
    exportable: bool = True,
    related: bool = False,
    company_dependent: bool = False,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=label,
        type=field_type,
        required=required,
        readonly=readonly,
        relation=relation,
        relation_field=None,
        selection=(),
        related=related,
        company_dependent=company_dependent,
        exportable=exportable,
    )


if __name__ == "__main__":
    unittest.main()
