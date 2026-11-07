"""Author the fictional manufacturing pack through normal app services.

No target calls, handcrafted snapshots, or generated replacement CSVs occur
here. The owner supplies a real schema-capture/controlled-schema hook and uses
the existing exercise_workspace lifecycle for comparison, durable reopen,
execution, reconciliation, independent business projection, and repeat.

BOM headers, component lines and operations use separate physical exports.
The line export retains repeated header cells for independent agreement checks;
no header aggregation or split-provider support is implied by this workflow.

Product phases include all twelve category UPSERT rows. After the category
phase these are twelve UNCHANGED rows, so total product-phase counts are 28,
not 16. Native name/parent identity is deliberately retained: complete_name
is stored in Odoo19 but nonstored in Odoo20 and is unsafe as a common LOCAL
lookup key. Product-only counts can still be reported per model.

Initial product phases omit active so Odoo creates visible variants before
assigning references. Explicit product-status phases reuse the full mappings
and then finalize source status; only active may differ from loaded values.

Baseline QuantitySeries=1 is an independently checked corpus precondition.
Quantity is parsed directly as French decimal; no undocumented x1000 rule,
zero-divisor fallback, date field, or Weight write is introduced.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from impodo.application.workspace.derived_entities import (
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    ConstantBusinessReference,
    ConstantReferenceComponent,
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    ScalarFieldMapping,
    ValueMapping,
)
from impodo.domain.recipe.value_rules import ScalarTransformPolicy
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.web.composition.page_reads import run_local_operation_sync
from scripts.practice_customer_workflow import (
    COMPANY_NAMES,
    PracticeCustomerWorkflow,
    _company_rows,
    _constant,
    _key,
)

MANUFACTURING_PHASE_FILES = {
    "categories": ("categories.csv",),
    "products": ("categories.csv", "products_core.csv", "product_details.csv"),
    "product-status": ("categories.csv", "products_core.csv", "product_details.csv"),
    "boms": ("bom_headers.csv", "bom_rows.csv", "bom_operations.csv"),
}

# Total rows include the deliberately retained category dependencies.
MANUFACTURING_COUNTS = {
    ("categories", "NORTH"): (9, 0, 3),
    ("categories", "SOUTH"): (9, 0, 3),
    ("products", "NORTH"): (12, 0, 16),
    ("products", "SOUTH"): (12, 1, 15),
    ("product-status", "NORTH"): (0, 1, 27),
    ("product-status", "SOUTH"): (0, 1, 27),
    ("boms", "NORTH"): (21, 0, 0),
    ("boms", "SOUTH"): (21, 0, 0),
}


def manufacturing_business_keys():
    """Exact reviewed native business keys for the captured practice schema.

    Work-center codes and BOM codes are globally prefixed in this corpus.
    Source CompanyCode selects a load; it is never substituted for a native
    res.company.name. Header company is assigned by a constant business key.
    These are fixture governance choices, not Odoo uniqueness constraints.
    """
    definitions = (
        ("company-name", "res.company", ("name",), ()),
        ("category-name-parent", "product.category", ("name",), ("parent_id",)),
        ("template-reference", "product.template", ("default_code",), ()),
        ("variant-reference", "product.product", ("default_code",), ()),
        ("unit-name", "uom.uom", ("name",), ()),
        ("tag-name", "product.tag", ("name",), ()),
        ("workcenter-code", "mrp.workcenter", ("code",), ()),
        ("bom-code", "mrp.bom", ("code",), ()),
        ("line-sequence-parent", "mrp.bom.line", ("sequence",), ("bom_id",)),
        (
            "operation-name-parent",
            "mrp.routing.workcenter",
            ("name",),
            ("bom_id",),
        ),
    )
    return tuple(
        BusinessKeyDefinition(
            f"practice:{key}",
            model,
            fields,
            scope_fields=scope,
            status=BusinessKeyStatus.CONFIRMED,
        )
        for key, model, fields, scope in definitions
    )


def _target_relation(
    dataset, target_field, source_name, model, key_field, *, many=False
):
    key = _key(dataset, source_name)
    return RelationshipMapping(
        target_field,
        "many2many" if many else "many2one",
        (key,),
        RelationshipResolver(
            ResolverOrigin.TARGET_CATALOG,
            model=model,
            key_mappings=(ReferenceKeyMapping(key, key_field),),
        ),
        categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
    )


def _unit_relation(dataset, target_field):
    relationship = _target_relation(
        dataset, target_field, "UnitLabel", "uom.uom", "name"
    )
    return replace(
        relationship,
        categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
        resolver=replace(
            relationship.resolver, value_mappings=(ValueMapping("PCE", "Units"),)
        ),
        required=True,
        required_on_create=True,
    )


def _incoming_parent(dataset, headers):
    return IdentityComponentMapping(
        (_key(dataset, "CompanyCode"), _key(dataset, "BOMRef")),
        ("bom_id",),
        resolver=RelationshipResolver(
            ResolverOrigin.DATASET, dataset_id=headers.dataset_id
        ),
    )


class PracticeManufacturingWorkflow(PracticeCustomerWorkflow):
    """Reuse normal source freeze, mapping submission, and Recipe activation.

    The owner must implement install_manufacturing_schema. A live fixture
    should reuse the normal capture path in LivePracticeCustomerWorkflow with
    this model/key set. A controlled fixture should publish independent pinned
    stock metadata from its target. Never mark fabricated metadata as live
    evidence or call the customer-specific schema installer unchanged.
    """

    def practice_files(
        self, root, *, phase, delivery="2026-04-15", source_overrides=None
    ):
        if phase not in MANUFACTURING_PHASE_FILES:
            raise ValueError("Unsupported manufacturing phase")
        overrides = dict(source_overrides or {})
        unknown = set(overrides) - set(MANUFACTURING_PHASE_FILES[phase])
        if unknown:
            raise ValueError("A manufacturing source override is outside its phase")
        directory = Path(root) / "deliveries" / delivery
        return {
            name.removesuffix(".csv"): overrides.get(
                name, (directory / name).read_bytes()
            )
            for name in MANUFACTURING_PHASE_FILES[phase]
        }

    def install_practice_schema(self, workspace_id, *, target, major, mode, company):
        if major not in (19, 20) or mode not in ("LOCAL", "REMOTE"):
            raise ValueError(
                "Manufacturing requires verified Odoo19/20 and LOCAL/REMOTE"
            )
        return self.install_manufacturing_schema(
            workspace_id,
            target=target,
            major=major,
            mode=mode,
            company=company,
            business_keys=manufacturing_business_keys(),
        )

    def install_manufacturing_schema(self, workspace_id, **kwargs):
        raise NotImplementedError(
            "Owner supplies normal live capture or controlled schema"
        )

    def category_mapping(self, dataset):
        return DatasetMapping(
            dataset.dataset_id,
            "product.category",
            source_identity_column_keys=(_key(dataset, "CategoryKey"),),
            target_identity=(
                IdentityComponentMapping((_key(dataset, "Name"),), ("name",)),
            ),
            target_scope=(
                IdentityComponentMapping(
                    (_key(dataset, "ParentCategoryKey"),),
                    ("parent_id",),
                    resolver=RelationshipResolver(
                        ResolverOrigin.DATASET, dataset_id=dataset.dataset_id
                    ),
                    null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                ),
            ),
        )

    def save_product_combination(self, workspace_id):
        selection = self.context.queries.get_source_selection(workspace_id)
        by_name = {dataset.name: dataset for dataset in selection.datasets}
        core, details = (by_name[name] for name in ("products_core", "product_details"))
        projected = (
            *(
                (core, name, name)
                for name in (
                    "CompanyCode",
                    "ProductRef",
                    "Name",
                    "ProductTypeLabel",
                    "CategoryKey",
                    "UnitLabel",
                    "SalesPrice",
                    "StatusCode",
                    "RecId",
                )
            ),
            (details, "Name", "DetailName"),
            (details, "Status", "DetailStatus"),
            (details, "Weight", "Weight"),
            (details, "TagCodes", "TagCodes"),
            (details, "ModifiedAtUS", "ModifiedAtUS"),
            (details, "ModifiedAtISO", "ModifiedAtISO"),
        )
        request = ExactJoinGroupRequest(
            output_dataset_name="products_complete",
            primary_dataset_id=core.dataset_id,
            primary_key_column_keys=(
                _key(core, "CompanyCode"),
                _key(core, "ProductRef"),
            ),
            key_value_types=("string", "string"),
            additions=(
                ExactJoinGroupAddition(
                    details.dataset_id,
                    (_key(details, "CompanyCode"), _key(details, "ProductRef")),
                ),
            ),
            projections=tuple(
                ExactJoinGroupProjection(
                    dataset.dataset_id, _key(dataset, name), output
                )
                for dataset, name, output in projected
            ),
        )
        current = self.context.queries.get_derived_entity_plan(workspace_id)
        plan, preview = run_local_operation_sync(
            self.context.derived_entities.save_join_group,
            workspace_id,
            request,
            expected_parent_version=current.version if current else None,
            actor=self.actor,
        )
        self.assertTrue(preview.can_save)
        self.assertEqual(preview.reviews[-1].actual_output_rows, 32)
        effective = self.context.queries.get_mapping_source_selection(workspace_id)
        output = next(
            item for item in effective.datasets if item.name == "products_complete"
        )
        self.assertEqual(output.row_count, 32)
        return output, plan

    def product_mapping(self, dataset, categories, company, *, finalize_status=False):
        text = ScalarTransformPolicy(
            trim=True, collapse_whitespace=True, empty_as_null=True
        )
        return DatasetMapping(
            dataset.dataset_id,
            "product.template",
            row_inclusion=_company_rows(dataset, company),
            source_identity_column_keys=(
                _key(dataset, "CompanyCode"),
                _key(dataset, "ProductRef"),
            ),
            target_identity=(
                IdentityComponentMapping(
                    (_key(dataset, "ProductRef"),), ("default_code",)
                ),
            ),
            fields=(
                ScalarFieldMapping(
                    "name",
                    _key(dataset, "Name"),
                    transform=text,
                    required_on_create=True,
                ),
                ScalarFieldMapping(
                    "type",
                    _key(dataset, "ProductTypeLabel"),
                    value_mappings=(
                        ValueMapping("Article", "consu"),
                        ValueMapping("Service", "service"),
                    ),
                    categorical_policy=CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH,
                ),
                ScalarFieldMapping(
                    "list_price",
                    _key(dataset, "SalesPrice"),
                    value_type="decimal",
                    transform=ScalarTransformPolicy(decimal_locale="fr_FR"),
                ),
                *(
                    (
                        ScalarFieldMapping(
                            "active",
                            _key(dataset, "StatusCode"),
                            value_type="boolean",
                            transform=ScalarTransformPolicy(
                                formula="value == 10 or value == '10'"
                            ),
                        ),
                    )
                    if finalize_status
                    else ()
                ),
            ),
            relationships=(
                RelationshipMapping(
                    "categ_id",
                    "many2one",
                    (_key(dataset, "CategoryKey"),),
                    RelationshipResolver(
                        ResolverOrigin.DATASET, dataset_id=categories.dataset_id
                    ),
                    required=True,
                    required_on_create=True,
                    categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                ),
                _unit_relation(dataset, "uom_id"),
                _target_relation(
                    dataset,
                    "product_tag_ids",
                    "TagCodes",
                    "product.tag",
                    "name",
                    many=True,
                ),
            ),
        )

    def header_mapping(self, dataset, company, *, unit_field):
        rows = _company_rows(dataset, company)
        approved = next(
            column.ordinal
            for column in dataset.columns
            if column.source_name == "Approved"
        )
        return DatasetMapping(
            dataset.dataset_id,
            "mrp.bom",
            row_inclusion=rows,
            source_identity_column_keys=(
                _key(dataset, "CompanyCode"),
                _key(dataset, "BOMRef"),
            ),
            target_identity=(
                IdentityComponentMapping((_key(dataset, "BOMRef"),), ("code",)),
            ),
            fields=(
                replace(
                    _constant("type", "normal"),
                    categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
                ),
                ScalarFieldMapping(
                    "product_qty",
                    _key(dataset, "BaseQty"),
                    value_type="decimal",
                    required=True,
                ),
                ScalarFieldMapping(
                    "active",
                    _key(dataset, "Active"),
                    value_type="boolean",
                    transform=ScalarTransformPolicy(
                        formula=f"value == 'Oui' and column_{approved} == 'Oui'"
                    ),
                ),
            ),
            relationships=(
                replace(
                    _target_relation(
                        dataset,
                        "product_tmpl_id",
                        "FinishedProductRef",
                        "product.template",
                        "default_code",
                    ),
                    required=True,
                    required_on_create=True,
                ),
                _unit_relation(dataset, unit_field),
                RelationshipMapping(
                    "company_id",
                    "many2one",
                    (),
                    RelationshipResolver(
                        ResolverOrigin.TARGET_CATALOG, model="res.company"
                    ),
                    value_source=RelationshipValueSource.CONSTANT_EXISTING,
                    constant_reference=ConstantBusinessReference(
                        (ConstantReferenceComponent("name", COMPANY_NAMES[company]),)
                    ),
                    categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                ),
            ),
        )

    def line_mapping(self, dataset, headers, company, *, unit_field):
        return DatasetMapping(
            dataset.dataset_id,
            "mrp.bom.line",
            row_inclusion=_company_rows(dataset, company),
            source_identity_column_keys=(
                _key(dataset, "CompanyCode"),
                _key(dataset, "BOMRef"),
                _key(dataset, "LineRef"),
            ),
            target_identity=(
                IdentityComponentMapping(
                    (_key(dataset, "Sequence"),), ("sequence",), "integer"
                ),
            ),
            target_scope=(_incoming_parent(dataset, headers),),
            fields=(
                ScalarFieldMapping(
                    "product_qty",
                    _key(dataset, "Quantity"),
                    value_type="decimal",
                    required=True,
                    transform=ScalarTransformPolicy(decimal_locale="fr_FR"),
                ),
            ),
            relationships=(
                replace(
                    _target_relation(
                        dataset,
                        "product_id",
                        "ComponentRef",
                        "product.product",
                        "default_code",
                    ),
                    required=True,
                    required_on_create=True,
                ),
                _unit_relation(dataset, unit_field),
            ),
        )

    def operation_mapping(self, dataset, headers, company):
        return DatasetMapping(
            dataset.dataset_id,
            "mrp.routing.workcenter",
            row_inclusion=_company_rows(dataset, company),
            source_identity_column_keys=(
                _key(dataset, "CompanyCode"),
                _key(dataset, "BOMRef"),
                _key(dataset, "OperationRef"),
            ),
            target_identity=(
                IdentityComponentMapping((_key(dataset, "Name"),), ("name",)),
            ),
            target_scope=(_incoming_parent(dataset, headers),),
            fields=(
                ScalarFieldMapping(
                    "sequence", _key(dataset, "Sequence"), value_type="integer"
                ),
                replace(
                    _constant("time_mode", "manual"),
                    categorical_policy=CategoricalCoveragePolicy.EXACT_TARGET_VALUE,
                ),
                ScalarFieldMapping(
                    "time_cycle_manual",
                    _key(dataset, "Minutes"),
                    value_type="decimal",
                    required=True,
                    transform=ScalarTransformPolicy(decimal_locale="fr_FR"),
                ),
            ),
            relationships=(
                replace(
                    _target_relation(
                        dataset,
                        "workcenter_id",
                        "WorkCentreCode",
                        "mrp.workcenter",
                        "code",
                    ),
                    required=True,
                    required_on_create=True,
                ),
            ),
        )

    def author(
        self,
        *,
        root,
        target,
        major,
        mode,
        company="NORTH",
        phase="products",
        delivery="2026-04-15",
        source_overrides=None,
    ):
        workspace_id = self.freeze_files(
            self.practice_files(
                root, phase=phase, delivery=delivery, source_overrides=source_overrides
            )
        )
        schema = self.install_practice_schema(
            workspace_id, target=target, major=major, mode=mode, company=company
        )
        physical = self.context.queries.get_source_selection(workspace_id)
        by_name = {item.name: item for item in physical.datasets}
        if phase == "categories":
            mappings = (self.category_mapping(by_name["categories"]),)
        elif phase in ("products", "product-status"):
            products, _plan = self.save_product_combination(workspace_id)
            mappings = (
                self.category_mapping(by_name["categories"]),
                self.product_mapping(
                    products,
                    by_name["categories"],
                    company,
                    finalize_status=phase == "product-status",
                ),
            )
        elif phase == "boms":
            headers, lines, operations = (
                by_name[name] for name in ("bom_headers", "bom_rows", "bom_operations")
            )
            unit_field = "product_uom_id" if major == 19 else "uom_id"
            mappings = (
                self.header_mapping(headers, company, unit_field=unit_field),
                self.line_mapping(lines, headers, company, unit_field=unit_field),
                self.operation_mapping(operations, headers, company),
            )
        else:
            raise ValueError("Unsupported manufacturing phase")
        revision = self.submit(
            workspace_id, mappings, confirm_rows=phase != "categories"
        )
        return workspace_id, schema, revision
