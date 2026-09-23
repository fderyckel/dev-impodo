from __future__ import annotations

import unittest

from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.supporting_models import (
    SupportingModelRole,
    derive_supporting_model_dependencies,
    unavailable_required_supporting_dependencies,
)


class SupportingModelDependencyTests(unittest.TestCase):
    def test_classifies_direct_relationships_without_model_specific_rules(
        self,
    ) -> None:
        dependencies = derive_supporting_model_dependencies(
            (
                SchemaModel(
                    name="x.asset",
                    label="Assets",
                    fields=(
                        _field(
                            "category_id",
                            "Category",
                            "many2one",
                            relation="x.category",
                            required=True,
                        ),
                        _field(
                            "company_id",
                            "Company",
                            "many2one",
                            relation="res.company",
                            required=True,
                            create_default_present=True,
                        ),
                        _field(
                            "line_ids",
                            "Lines",
                            "one2many",
                            relation="x.asset.line",
                            relation_field="asset_id",
                        ),
                        _field(
                            "generated_id",
                            "Generated record",
                            "many2one",
                            relation="x.generated",
                            required=True,
                            readonly=True,
                            computed=True,
                        ),
                    ),
                ),
            )
        )

        roles = {
            item.relation_model: item.role
            for item in dependencies
        }
        self.assertEqual(
            roles,
            {
                "x.category": SupportingModelRole.REUSE_EXISTING,
                "res.company": SupportingModelRole.CHECKED_DEFAULT,
                "x.generated": SupportingModelRole.ODOO_MANAGED,
                "x.asset.line": SupportingModelRole.REVIEW_INCOMING,
            },
        )

    def test_selected_and_ineligible_relations_do_not_reenter_the_plan(
        self,
    ) -> None:
        dependencies = derive_supporting_model_dependencies(
            (
                SchemaModel(
                    name="x.parent",
                    label="Parents",
                    fields=(
                        _field(
                            "child_ids",
                            "Children",
                            "one2many",
                            relation="x.child",
                        ),
                        _field(
                            "hidden_id",
                            "Hidden",
                            "many2one",
                            relation="x.hidden",
                            exportable=False,
                        ),
                    ),
                ),
                SchemaModel(
                    name="x.child",
                    label="Children",
                    fields=(),
                ),
            )
        )

        self.assertEqual(dependencies, ())

    def test_optional_relationship_waits_for_source_or_recipe_intent(
        self,
    ) -> None:
        dependencies = derive_supporting_model_dependencies(
            (
                SchemaModel(
                    name="x.asset",
                    label="Assets",
                    fields=(
                        _field(
                            "owner_id",
                            "Owner",
                            "many2one",
                            relation="x.owner",
                        ),
                        _field(
                            "tag_ids",
                            "Tags",
                            "many2many",
                            relation="x.tag",
                        ),
                    ),
                ),
            )
        )

        self.assertEqual(dependencies, ())

    def test_only_unavailable_required_reuse_is_a_completion_blocker(
        self,
    ) -> None:
        model = SchemaModel(
            name="x.asset",
            label="Assets",
            fields=(
                _field(
                    "category_id",
                    "Category",
                    "many2one",
                    relation="x.category",
                    required=True,
                ),
                _field(
                    "tag_ids",
                    "Tags",
                    "many2many",
                    relation="x.tag",
                ),
                _field(
                    "company_id",
                    "Company",
                    "many2one",
                    relation="res.company",
                    required=True,
                    create_default_present=True,
                ),
            ),
        )

        unavailable = unavailable_required_supporting_dependencies(
            (model,),
            available_model_names=("x.tag", "res.company"),
        )

        self.assertEqual(
            tuple(item.relation_model for item in unavailable),
            ("x.category",),
        )


def _field(
    name: str,
    label: str,
    field_type: str,
    *,
    relation: str,
    relation_field: str | None = None,
    required: bool = False,
    readonly: bool = False,
    computed: bool | None = False,
    exportable: bool | None = True,
    create_default_present: bool = False,
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
        computed=computed,
        has_inverse=False,
        related=False,
        company_dependent=False,
        exportable=exportable,
        create_default_present=create_default_present,
    )


if __name__ == "__main__":
    unittest.main()
