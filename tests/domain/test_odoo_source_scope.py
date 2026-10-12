from __future__ import annotations

import unittest

from impodo.domain.odoo_relationship_profiles import (
    OdooRelationshipProfile,
    OdooRelationshipRecommendation,
    STANDARD_ODOO_RELATIONSHIP_PROFILE,
)
from impodo.domain.odoo_source_scope import (
    RelatedDataDefaultAction,
    RelatedDataHandling,
    is_writable_related_many2one,
    propose_related_odoo_data,
    recommended_related_data_action,
    related_model_can_be_selected,
    related_model_should_default_linked_only,
)
from impodo.domain.odoo_source_capture import (
    is_odoo_capture_relationship_field,
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
            ),
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

    def test_selected_and_unsafe_relationships_are_not_selectable(self) -> None:
        product = SchemaModel(
            name="product.template",
            label="Product",
            fields=(
                _relationship("uom_id", "Unit of Measure", "uom.uom"),
                _relationship("company_id", "Company", "res.company"),
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
                    readonly=True,
                ),
                _relationship(
                    "x_company_value_id",
                    "Company value",
                    "x.value",
                    company_dependent=True,
                ),
                _relationship(
                    "parent_id",
                    "Parent Product",
                    "product.template",
                ),
            ),
        )
        uom = SchemaModel(name="uom.uom", label="Unit of Measure", fields=())
        company = SchemaModel(name="res.company", label="Company", fields=())

        visible = propose_related_odoo_data((product, uom, company))
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].field_name, "x_related_id")
        self.assertIs(
            visible[0].handling,
            RelatedDataHandling.ODOO_MANAGED,
        )
        self.assertFalse(related_model_can_be_selected(visible[0].handling))
        included = propose_related_odoo_data(
            (product, uom, company),
            include_selected=True,
        )
        self.assertEqual(
            {item.field_name: item.handling for item in included},
            {
                "company_id": RelatedDataHandling.REUSE_DESTINATION,
                "uom_id": RelatedDataHandling.INCLUDE_SUPPORTING,
                "x_related_id": RelatedDataHandling.ODOO_MANAGED,
            },
        )

    def test_readonly_nonstored_unowned_collection_is_odoo_managed(self) -> None:
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="x.operation",
                    label="Operation",
                    fields=(
                        _relationship(
                            "default_option_ids",
                            "Default Options",
                            "x.option",
                            field_type="one2many",
                            readonly=True,
                            stored=False,
                        ),
                        _relationship(
                            "owned_line_ids",
                            "Owned Lines",
                            "x.line",
                            field_type="one2many",
                            relation_field="operation_id",
                        ),
                    ),
                ),
            )
        )

        by_field = {item.field_name: item for item in suggestions}
        self.assertIs(
            by_field["default_option_ids"].handling,
            RelatedDataHandling.ODOO_MANAGED,
        )
        self.assertFalse(
            related_model_can_be_selected(
                by_field["default_option_ids"].handling
            )
        )
        self.assertIs(
            by_field["owned_line_ids"].handling,
            RelatedDataHandling.NEEDS_DECISION,
        )
        self.assertTrue(
            related_model_can_be_selected(
                by_field["owned_line_ids"].handling
            )
        )

    def test_unit_category_is_supporting_data_when_discovered(self) -> None:
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="uom.uom",
                    label="Unit of Measure",
                    fields=(
                        SchemaField(
                            name="name",
                            label="Unit of Measure",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                        ),
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
        self.assertEqual(
            suggestions[0].recommendation_profile_id,
            "impodo.standard.odoo.relationships",
        )
        self.assertEqual(suggestions[0].recommendation_profile_version, 5)
        self.assertTrue(suggestions[0].identity_scope)

    def test_generated_alias_stops_before_odoo_registry_metadata(self) -> None:
        alias_field = _relationship(
            "alias_id",
            "Email Alias",
            "mail.alias",
            required=True,
        )
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="maintenance.team",
                    label="Maintenance Team",
                    fields=(alias_field,),
                ),
                SchemaModel(
                    name="mail.alias",
                    label="Email Alias",
                    fields=(
                        _relationship(
                            "alias_model_id",
                            "Aliased Model",
                            "ir.model",
                            required=True,
                        ),
                    ),
                ),
            ),
            include_selected=True,
        )

        by_identity = {
            (item.source_model, item.field_name): item
            for item in suggestions
        }
        registry = by_identity[("mail.alias", "alias_model_id")]
        self.assertIs(
            STANDARD_ODOO_RELATIONSHIP_PROFILE.recommend(
                "maintenance.team",
                alias_field,
            ),
            OdooRelationshipRecommendation.ODOO_MANAGED,
        )
        self.assertNotIn(("maintenance.team", "alias_id"), by_identity)
        self.assertIs(
            registry.handling,
            RelatedDataHandling.REUSE_DESTINATION,
        )

    def test_manufacturing_profile_transfers_owned_rows_and_workcenters(self) -> None:
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="mrp.bom",
                    label="Bill of Materials",
                    fields=(
                        _relationship(
                            "bom_line_ids",
                            "Components",
                            "mrp.bom.line",
                            field_type="one2many",
                        ),
                        _relationship(
                            "operation_ids",
                            "Operations",
                            "mrp.routing.workcenter",
                            field_type="one2many",
                        ),
                    ),
                ),
                SchemaModel(
                    name="mrp.routing.workcenter",
                    label="Operation",
                    fields=(
                        _relationship(
                            "workcenter_id",
                            "Work Center",
                            "mrp.workcenter",
                        ),
                    ),
                ),
            ),
            include_selected=True,
        )

        by_field = {item.field_name: item.handling for item in suggestions}
        self.assertEqual(
            by_field,
            {
                "bom_line_ids": RelatedDataHandling.INCLUDE_SUPPORTING,
                "operation_ids": RelatedDataHandling.INCLUDE_SUPPORTING,
                "workcenter_id": RelatedDataHandling.TRANSFER_RELATED,
            },
        )

    def test_unknown_custom_relationships_require_explicit_linked_inclusion(self) -> None:
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
        self.assertTrue(
            related_model_can_be_selected(RelatedDataHandling.NEEDS_DECISION)
        )
        self.assertTrue(
            related_model_should_default_linked_only(
                RelatedDataHandling.NEEDS_DECISION
            )
        )
        self.assertTrue(
            all(item.recommendation_profile_id is None for item in suggestions)
        )

    def test_caller_profile_can_recommend_a_custom_relationship(self) -> None:
        organization_profile = OdooRelationshipProfile(
            profile_id="example.organization.relationships",
            version=3,
            supporting_relationships=frozenset(
                {("x.document", "reviewer_id")}
            ),
        )

        suggestion = propose_related_odoo_data(
            (
                SchemaModel(
                    name="x.document",
                    label="Document",
                    fields=(
                        _relationship("reviewer_id", "Reviewer", "x.reviewer"),
                    ),
                ),
            ),
            profiles=(organization_profile,),
        )[0]

        self.assertIs(
            suggestion.handling,
            RelatedDataHandling.INCLUDE_SUPPORTING,
        )
        self.assertEqual(
            suggestion.recommendation_profile_id,
            organization_profile.profile_id,
        )
        self.assertEqual(
            suggestion.recommendation_profile_version,
            organization_profile.version,
        )

    def test_direction_owns_the_generic_default_action(self) -> None:
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="x.document",
                    label="Document",
                    fields=(
                        _relationship("owner_id", "Owner", "x.owner"),
                        _relationship(
                            "line_ids",
                            "Lines",
                            "x.line",
                            field_type="one2many",
                        ),
                        _relationship(
                            "tag_ids",
                            "Tags",
                            "x.tag",
                            field_type="many2many",
                        ),
                        _relationship(
                            "bom_id",
                            "Bill of Material",
                            "mrp.bom",
                        ),
                    ),
                ),
                SchemaModel(
                    name="resource.calendar",
                    label="Working Hours",
                    fields=(
                        _relationship(
                            "attendance_ids",
                            "Working Time",
                            "resource.calendar.attendance",
                            field_type="one2many",
                            relation_field="calendar_id",
                            computed=True,
                        ),
                    ),
                ),
            ),
        )

        actions = {
            item.field_name: recommended_related_data_action(item)
            for item in suggestions
        }
        self.assertEqual(
            actions,
            {
                "attendance_ids": RelatedDataDefaultAction.CAPTURE_LINKED,
                "bom_id": RelatedDataDefaultAction.MATCH_EXISTING,
                "line_ids": RelatedDataDefaultAction.DO_NOT_CAPTURE,
                "owner_id": RelatedDataDefaultAction.PRESERVE_LINKED,
                "tag_ids": RelatedDataDefaultAction.DO_NOT_CAPTURE,
            },
        )

    def test_stored_writable_related_many2one_is_capturable(self) -> None:
        working_time = _relationship(
            "resource_calendar_id",
            "Working Hours",
            "resource.calendar",
            related=True,
            stored=True,
            computed=True,
        )
        next_activity = _relationship(
            "activity_type_id",
            "Next Activity Type",
            "mail.activity.type",
            related=True,
            stored=False,
            computed=True,
        )
        readonly_related = _relationship(
            "currency_id",
            "Currency",
            "res.currency",
            related=True,
            readonly=True,
            stored=True,
            computed=True,
        )
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="mrp.workcenter",
                    label="Work Center",
                    fields=(working_time, next_activity, readonly_related),
                ),
            ),
        )
        by_field = {item.field_name: item for item in suggestions}

        self.assertTrue(is_writable_related_many2one(working_time))
        self.assertIs(
            by_field["resource_calendar_id"].handling,
            RelatedDataHandling.TRANSFER_RELATED,
        )
        self.assertIs(
            by_field["activity_type_id"].handling,
            RelatedDataHandling.ODOO_MANAGED,
        )
        self.assertIs(
            by_field["currency_id"].handling,
            RelatedDataHandling.ODOO_MANAGED,
        )
        self.assertTrue(
            is_odoo_capture_relationship_field(
                working_time,
                selected_models={"mrp.workcenter", "resource.calendar"},
            )
        )
        self.assertFalse(
            is_odoo_capture_relationship_field(
                next_activity,
                selected_models={"mrp.workcenter", "mail.activity.type"},
            )
        )
        self.assertFalse(
            is_odoo_capture_relationship_field(
                readonly_related,
                selected_models={"mrp.workcenter", "res.currency"},
            )
        )

    def test_standard_profile_owns_workcenter_resource_and_calendar_lines(
        self,
    ) -> None:
        suggestions = propose_related_odoo_data(
            (
                SchemaModel(
                    name="mrp.workcenter",
                    label="Work Center",
                    fields=(
                        _relationship(
                            "resource_id",
                            "Resource",
                            "resource.resource",
                            required=True,
                        ),
                        _relationship(
                            "resource_calendar_id",
                            "Working Hours",
                            "resource.calendar",
                            related=True,
                            stored=True,
                            computed=True,
                        ),
                    ),
                ),
                SchemaModel(
                    name="resource.calendar",
                    label="Working Hours",
                    fields=(
                        _relationship(
                            "attendance_ids",
                            "Working Time",
                            "resource.calendar.attendance",
                            field_type="one2many",
                            relation_field="calendar_id",
                            computed=True,
                        ),
                        _relationship(
                            "attendance_ids_1st_week",
                            "Working Time 1st Week",
                            "resource.calendar.attendance",
                            field_type="one2many",
                            relation_field="calendar_id",
                            computed=True,
                            has_inverse=True,
                        ),
                        _relationship(
                            "attendance_ids_2nd_week",
                            "Working Time 2nd Week",
                            "resource.calendar.attendance",
                            field_type="one2many",
                            relation_field="calendar_id",
                            computed=True,
                            has_inverse=True,
                        ),
                    ),
                ),
            ),
            include_selected=True,
        )

        by_field = {item.field_name: item for item in suggestions}
        self.assertIs(
            by_field["resource_id"].handling,
            RelatedDataHandling.ODOO_MANAGED,
        )
        self.assertIs(
            by_field["resource_calendar_id"].handling,
            RelatedDataHandling.TRANSFER_RELATED,
        )
        self.assertIs(
            by_field["attendance_ids"].handling,
            RelatedDataHandling.INCLUDE_SUPPORTING,
        )
        self.assertIs(
            by_field["attendance_ids_1st_week"].handling,
            RelatedDataHandling.ODOO_MANAGED,
        )
        self.assertIs(
            by_field["attendance_ids_2nd_week"].handling,
            RelatedDataHandling.ODOO_MANAGED,
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
    relation_field: str | None = None,
    stored: bool | None = None,
    computed: bool | None = None,
    has_inverse: bool | None = None,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=label,
        type=field_type,
        required=required,
        readonly=readonly,
        relation=relation,
        relation_field=relation_field,
        selection=(),
        stored=stored,
        computed=computed,
        has_inverse=has_inverse,
        related=related,
        company_dependent=company_dependent,
        exportable=exportable,
    )


if __name__ == "__main__":
    unittest.main()
