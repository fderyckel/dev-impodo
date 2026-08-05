from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from impodo.access import (
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    DerivedEntityWorkspaceService,
    RelatedDatasetRule,
    derived_dataset_links,
    derived_mapping_samples,
    mapping_source_selection,
    preview_derived_entities,
    preview_related_datasets,
    related_dataset_links,
)
from impodo.inspection import (
    CATALOG_CONTRACT_VERSION,
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.adapters.duckdb import DuckDbRepositories
from impodo.projects import MigrationProject, ProjectStatus
from impodo.workspace_contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.workspace_errors import WorkspaceError
from impodo.web.presenters.mapping_view import _mapping_dataset_views


ROOT = Path(__file__).resolve().parents[1]


class DerivedEntityPreviewTests(unittest.TestCase):
    def test_target_display_field_must_be_one_odoo_field(self) -> None:
        selection, _catalog = _source_evidence()

        with self.assertRaisesRegex(ValueError, "valid Odoo field name"):
            replace(_rule(selection), target_name_field="parent_id.name")

    def test_homonymous_children_receive_distinct_category_owned_ids(self) -> None:
        selection, catalog = _source_evidence()
        rule = _rule(selection)

        preview = preview_derived_entities(rule, selection, (catalog,))
        repeated = preview_derived_entities(
            replace(rule, rule_id=str(uuid4())),
            selection,
            (catalog,),
        )

        by_key = {item.canonical_key: item for item in preview.candidates}
        self.assertEqual(
            set(by_key),
            {
                "computers",
                "furniture",
                "computers / accessories",
                "furniture / accessories",
            },
        )
        furniture_accessories = by_key["furniture / accessories"]
        computer_accessories = by_key["computers / accessories"]
        self.assertEqual(furniture_accessories.name, "Accessories")
        self.assertEqual(computer_accessories.name, "Accessories")
        self.assertNotEqual(
            furniture_accessories.entity_id,
            computer_accessories.entity_id,
        )
        self.assertEqual(
            furniture_accessories.parent_entity_id,
            by_key["furniture"].entity_id,
        )
        self.assertTrue(furniture_accessories.requires_alias_review)
        self.assertEqual(furniture_accessories.sampled_source_row_count, 2)
        self.assertNotIn("P001", furniture_accessories.entity_id)
        self.assertNotIn("P001", furniture_accessories.odoo_external_id)
        self.assertEqual(
            tuple(item.entity_id for item in preview.candidates),
            tuple(item.entity_id for item in repeated.candidates),
        )
        self.assertEqual(
            tuple(item.odoo_external_id for item in preview.candidates),
            tuple(item.odoo_external_id for item in repeated.candidates),
        )

    def test_blank_and_malformed_hierarchy_values_are_visible(self) -> None:
        selection, catalog = _source_evidence(
            rows=(
                ("P001", None),
                ("P002", "Furniture // Chairs"),
            )
        )

        preview = preview_derived_entities(
            _rule(selection),
            selection,
            (catalog,),
        )

        self.assertEqual(preview.blank_sample_rows, 1)
        self.assertEqual(preview.invalid_path_sample_rows, 1)
        self.assertEqual(preview.candidates, ())

    def test_parent_child_preview_keeps_every_line_and_groups_parents(self) -> None:
        selection, catalog = _source_evidence(
            rows=(
                ("1", "BOM-A"),
                ("2", "BOM-A"),
                ("1", "BOM-B"),
            )
        )
        dataset = selection.datasets[0]
        rule = RelatedDatasetRule(
            rule_id=str(uuid4()),
            source_dataset_id=dataset.dataset_id,
            parent_dataset_name="boms",
            child_dataset_name="bom_components",
            parent_key_column_key=dataset.columns[1].stable_key,
            child_key_column_key=dataset.columns[0].stable_key,
        )

        preview = preview_related_datasets(rule, selection, (catalog,))

        self.assertEqual(preview.source_rows, 3)
        self.assertEqual(preview.parent_candidate_count, 2)
        self.assertTrue(preview.parent_candidate_count_is_exact)
        self.assertEqual(preview.child_rows, 3)
        self.assertEqual(preview.sampled_parent_groups, 2)
        self.assertEqual(preview.duplicate_child_key_sample_rows, 0)
        self.assertEqual(
            preview.parent_samples[0].sampled_child_keys,
            ("1", "2"),
        )

    def test_parent_child_preview_exposes_dirty_and_duplicate_keys(self) -> None:
        selection, catalog = _source_evidence(
            rows=(
                ("1", " BOM-A "),
                ("1", "BOM-A"),
                ("", "BOM-B"),
            )
        )
        dataset = selection.datasets[0]
        rule = RelatedDatasetRule(
            rule_id=str(uuid4()),
            source_dataset_id=dataset.dataset_id,
            parent_dataset_name="boms",
            child_dataset_name="bom_components",
            parent_key_column_key=dataset.columns[1].stable_key,
            child_key_column_key=dataset.columns[0].stable_key,
        )

        preview = preview_related_datasets(rule, selection, (catalog,))

        self.assertEqual(preview.normalized_key_sample_rows, 1)
        self.assertEqual(preview.duplicate_child_key_sample_rows, 1)
        self.assertEqual(preview.blank_child_key_sample_rows, 1)

    def test_contract_one_lookup_plan_remains_readable(self) -> None:
        selection, _catalog = _source_evidence()
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            project_id=selection.project_id,
            source_selection_hash=selection.content_hash,
            rules=(_rule(selection),),
            updated_at=datetime.now(timezone.utc),
            updated_by="Test operator",
            contract_version=1,
        )

        repeated = DerivedEntityPlan.from_json(plan.to_json())

        self.assertEqual(repeated, plan)

    def test_company_scope_is_part_of_parent_and_child_guidance(self) -> None:
        rule = RelatedDatasetRule(
            rule_id=str(uuid4()),
            source_dataset_id="dataset:bom_lines",
            parent_dataset_name="boms",
            child_dataset_name="bom_components",
            parent_key_column_key="column:bom_id",
            scope_column_key="column:company",
            child_key_column_key="column:line_number",
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            project_id=str(uuid4()),
            source_selection_hash="sha256:" + "a" * 64,
            rules=(rule,),
            updated_at=datetime.now(timezone.utc),
            updated_by="Test operator",
        )

        link = related_dataset_links(plan)[0]

        self.assertEqual(
            link.reference_column_keys,
            ("column:bom_id", "column:company"),
        )
        self.assertEqual(
            link.child_identity_column_keys,
            ("column:bom_id", "column:company", "column:line_number"),
        )

    def test_mapping_view_guides_child_to_generated_parent(self) -> None:
        selection, catalog = _source_evidence(
            rows=(("1", "BOM-A"), ("2", "BOM-A"))
        )
        dataset = selection.datasets[0]
        rule = RelatedDatasetRule(
            rule_id=str(uuid4()),
            source_dataset_id=dataset.dataset_id,
            parent_dataset_name="boms",
            child_dataset_name="bom_components",
            parent_key_column_key=dataset.columns[1].stable_key,
            child_key_column_key=dataset.columns[0].stable_key,
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            project_id=selection.project_id,
            source_selection_hash=selection.content_hash,
            rules=(rule,),
            updated_at=datetime.now(timezone.utc),
            updated_by="Test operator",
        )
        effective = mapping_source_selection(selection, plan, (catalog,))
        schema = OdooSchemaCatalog(
            project_id=selection.project_id,
            target_hash="sha256:" + "1" * 64,
            captured_at=datetime.now(timezone.utc),
            captured_by="Test operator",
            connection_mode="LOCAL",
            database="test",
            odoo_version="19.0",
            models=(
                SchemaModel(
                    name="mrp.bom",
                    label="Bill of Materials",
                    fields=(
                        _schema_field("code", "Reference", "char"),
                    ),
                ),
                SchemaModel(
                    name="mrp.bom.line",
                    label="BOM Component",
                    fields=(
                        _schema_field(
                            "bom_id",
                            "Bill of Materials",
                            "many2one",
                            required=True,
                            relation="mrp.bom",
                        ),
                        _schema_field("sequence", "Sequence", "integer"),
                    ),
                ),
            ),
            content_hash="sha256:" + "2" * 64,
            origin=SchemaOrigin.LIVE_API,
        )
        governance = SchemaGovernance(
            governance_id=str(uuid4()),
            version=1,
            project_id=selection.project_id,
            catalog_hash=schema.content_hash,
            permitted_models=("mrp.bom", "mrp.bom.line"),
            business_keys=(
                BusinessKeyDefinition(
                    key_id="mrp.bom::code",
                    model="mrp.bom",
                    key_fields=("code",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    key_id="mrp.bom.line::sequence",
                    model="mrp.bom.line",
                    key_fields=("sequence",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
            recorded_at=datetime.now(timezone.utc),
            recorded_by="Test operator",
        )

        views = _mapping_dataset_views(
            effective,
            schema,
            governance,
            (),
            (catalog,),
            {0: "mrp.bom", 1: "mrp.bom.line"},
            related_dataset_links(plan),
        )

        self.assertEqual(views[0]["related_role"], "parent")
        self.assertEqual(views[1]["related_role"], "child")
        self.assertEqual(
            views[1]["recommended_source_identity"],
            (dataset.columns[1].stable_key, dataset.columns[0].stable_key),
        )
        bom_relation = next(
            item
            for item in views[1]["relation_rows"]
            if item["metadata"].name == "bom_id"
        )
        self.assertEqual(
            bom_relation["recommended_dataset_id"],
            effective.datasets[0].dataset_id,
        )
        self.assertEqual(
            bom_relation["recommended_source_columns"],
            (dataset.columns[1].stable_key,),
        )

    def test_lookup_extraction_becomes_mapping_ready_with_product_link(self) -> None:
        selection, catalog = _source_evidence(
            rows=(("P001", "Article"), ("P002", "Service"), ("P003", "Article"))
        )
        rule = replace(_rule(selection), parent_separator=None)
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            project_id=selection.project_id,
            source_selection_hash=selection.content_hash,
            rules=(rule,),
            updated_at=datetime.now(timezone.utc),
            updated_by="Test operator",
        )

        effective = mapping_source_selection(selection, plan, (catalog,))
        link = derived_dataset_links(plan)[0]
        preview = preview_derived_entities(rule, selection, (catalog,))
        samples = {
            link.derived_dataset_id: derived_mapping_samples(link, preview)
        }

        self.assertEqual(
            tuple(item.name for item in effective.datasets),
            ("product_categories", "products"),
        )
        self.assertEqual(effective.datasets[0].row_count, 2)
        self.assertEqual(
            tuple(item.stable_key for item in effective.datasets[0].columns),
            (link.canonical_key_column_key, link.name_column_key),
        )
        self.assertEqual(link.consumer_dataset_id, selection.datasets[0].dataset_id)

        schema = OdooSchemaCatalog(
            project_id=selection.project_id,
            target_hash="sha256:" + "1" * 64,
            captured_at=datetime.now(timezone.utc),
            captured_by="Test operator",
            connection_mode="LOCAL",
            database="test",
            odoo_version="19.0",
            models=(
                SchemaModel(
                    name="product.category",
                    label="Product Category",
                    fields=(_schema_field("name", "Name", "char"),),
                ),
                SchemaModel(
                    name="product.template",
                    label="Product",
                    fields=(
                        _schema_field("default_code", "Reference", "char"),
                        _schema_field(
                            "categ_id",
                            "Product Category",
                            "many2one",
                            relation="product.category",
                        ),
                    ),
                ),
            ),
            content_hash="sha256:" + "2" * 64,
            origin=SchemaOrigin.LIVE_API,
        )
        governance = SchemaGovernance(
            governance_id=str(uuid4()),
            version=1,
            project_id=selection.project_id,
            catalog_hash=schema.content_hash,
            permitted_models=("product.category", "product.template"),
            business_keys=(
                BusinessKeyDefinition(
                    key_id="product.category::name",
                    model="product.category",
                    key_fields=("name",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    key_id="product.template::default_code",
                    model="product.template",
                    key_fields=("default_code",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
            recorded_at=datetime.now(timezone.utc),
            recorded_by="Test operator",
        )

        views = _mapping_dataset_views(
            effective,
            schema,
            governance,
            (),
            (catalog,),
            {1: "product.template"},
            (),
            (link,),
            samples,
        )

        self.assertEqual(views[0]["selected_model"], "product.category")
        self.assertEqual(views[0]["related_role"], "lookup")
        self.assertEqual(
            views[0]["recommended_source_identity"],
            (link.canonical_key_column_key,),
        )
        self.assertEqual(
            views[0]["identity_rows"][0]["selected_sources"],
            (link.name_column_key,),
        )
        category_relation = next(
            item
            for item in views[1]["relation_rows"]
            if item["metadata"].name == "categ_id"
        )
        self.assertEqual(
            category_relation["recommended_dataset_id"],
            link.derived_dataset_id,
        )
        self.assertEqual(
            category_relation["recommended_source_columns"],
            (selection.datasets[0].columns[1].stable_key,),
        )


class DerivedEntityWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.repository = DuckDbRepositories(self.temporary.name)
        now = datetime.now(timezone.utc)
        self.project = MigrationProject(
            project_id=str(uuid4()),
            name="Product migration",
            source_system="Legacy ERP",
            data_manager="Data Manager",
            functional_owner="Functional Owner",
            business_unit="Example Business Unit",
            status=ProjectStatus.REGISTERED,
            registered_at=now,
        )
        self.repository.create(self.project, actor=LOCAL_ACTOR)
        self.selection, self.catalog = _source_evidence(
            project_id=self.project.project_id
        )
        self.repository.save_source_selection(
            self.project.project_id,
            self.selection,
            actor=LOCAL_ACTOR,
        )
        self.service = DerivedEntityWorkspaceService(
            self.repository,
            CapabilityAuthorizationPolicy(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_plan_is_versioned_persisted_and_invalidated_by_source_change(
        self,
    ) -> None:
        dataset = self.selection.datasets[0]
        category = dataset.columns[1]
        plan, rule = self.service.save_rule(
            self.project.project_id,
            output_dataset_name="product_categories",
            source_dataset_id=dataset.dataset_id,
            source_column_key=category.stable_key,
            target_model="product.category",
            target_name_field="name",
            external_id_namespace="legacy_erp",
            parent_separator="/",
            blank_policy="block",
            expected_parent_version=None,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(plan.version, 1)
        self.assertEqual(
            self.repository.get_derived_entity_plan(self.project.project_id),
            plan,
        )
        self.assertEqual(rule.source_column_key, category.stable_key)
        with self.assertRaisesRegex(
            WorkspaceError,
            "Derived dataset names must be unique",
        ):
            self.service.save_rule(
                self.project.project_id,
                output_dataset_name="product_categories",
                source_dataset_id=dataset.dataset_id,
                source_column_key=category.stable_key,
                target_model="product.category",
                target_name_field="name",
                external_id_namespace="legacy_erp",
                parent_separator="/",
                blank_policy="block",
                expected_parent_version=1,
                actor=LOCAL_ACTOR,
            )

        replacement = replace(
            self.selection,
            selection_id=str(uuid4()),
            version=2,
            content_hash="sha256:" + "f" * 64,
        )
        self.repository.save_source_selection(
            self.project.project_id,
            replacement,
            actor=LOCAL_ACTOR,
        )
        self.assertIsNone(
            self.repository.get_derived_entity_plan(self.project.project_id)
        )

    def test_related_split_replaces_physical_source_for_mapping(self) -> None:
        dataset = self.selection.datasets[0]
        plan, rule = self.service.save_related_split(
            self.project.project_id,
            source_dataset_id=dataset.dataset_id,
            parent_dataset_name="product_categories",
            child_dataset_name="product_rows",
            parent_key_column_key=dataset.columns[1].stable_key,
            child_key_column_key=dataset.columns[0].stable_key,
            scope_column_key=None,
            blank_policy="block",
            expected_parent_version=None,
            actor=LOCAL_ACTOR,
        )

        effective = mapping_source_selection(self.selection, plan, (self.catalog,))
        links = related_dataset_links(plan)

        self.assertEqual(
            tuple(item.name for item in effective.datasets),
            ("product_categories", "product_rows"),
        )
        self.assertEqual(len(effective.datasets[0].columns), 1)
        self.assertEqual(effective.datasets[1].row_count, dataset.row_count)
        self.assertNotEqual(effective.content_hash, self.selection.content_hash)
        self.assertEqual(links[0].parent_dataset_id, effective.datasets[0].dataset_id)
        self.assertEqual(links[0].child_dataset_id, effective.datasets[1].dataset_id)
        self.assertEqual(
            links[0].child_identity_column_keys,
            (dataset.columns[1].stable_key, dataset.columns[0].stable_key),
        )
        self.assertEqual(rule.parent_dataset_name, "product_categories")


def _rule(selection: SourceSelection) -> DerivedEntityRule:
    dataset = selection.datasets[0]
    return DerivedEntityRule(
        rule_id=str(uuid4()),
        output_dataset_name="product_categories",
        source_dataset_id=dataset.dataset_id,
        source_column_key=dataset.columns[1].stable_key,
        target_model="product.category",
        target_name_field="name",
        external_id_namespace="legacy_erp",
        parent_separator="/",
        blank_policy="block",
    )


def _source_evidence(
    *,
    project_id: str | None = None,
    rows: tuple[tuple[str | None, ...], ...] = (
        ("P001", "Furniture / Accessories"),
        ("P002", "Computers / Accessories"),
        ("P003", "FURNITURE / Accessories"),
    ),
) -> tuple[SourceSelection, SourceFileCatalog]:
    now = datetime.now(timezone.utc)
    product_column = SourceColumnProfile(
        ordinal=1,
        name="Product ID",
        candidate_type="string",
        null_count=0,
        non_null_count=len(rows),
        distinct_count=len(rows),
        distinct_count_is_exact=True,
        duplicate_count=0,
        minimum="P001",
        maximum="P003",
        minimum_length=4,
        maximum_length=4,
    )
    category_values = tuple(row[1] for row in rows if row[1] is not None)
    category_column = SourceColumnProfile(
        ordinal=2,
        name="Product Category",
        candidate_type="string",
        null_count=len(rows) - len(category_values),
        non_null_count=len(category_values),
        distinct_count=len(set(category_values)),
        distinct_count_is_exact=True,
        duplicate_count=len(category_values) - len(set(category_values)),
        minimum=min(category_values, default=None),
        maximum=max(category_values, default=None),
        minimum_length=min((len(item) for item in category_values), default=None),
        maximum_length=max((len(item) for item in category_values), default=None),
    )
    table = SourceTableCatalog(
        table_key="csv",
        name="products",
        kind="csv",
        hidden=False,
        header_row=1,
        row_count=len(rows),
        column_count=2,
        columns=(product_column, category_column),
        preview_rows=rows,
    )
    catalog = SourceFileCatalog(
        contract_version=CATALOG_CONTRACT_VERSION,
        file_id=str(uuid4()),
        display_name="products.csv",
        source_sha256="a" * 64,
        source_size_bytes=128,
        format="csv",
        inspected_at=now,
        encoding="utf-8",
        delimiter=",",
        tables=(table,),
    )
    dataset = SourceDataset(
        dataset_id="dataset:products",
        name="products",
        file_id=catalog.file_id,
        table_key="csv",
        source_sha256=catalog.source_sha256,
        catalog_hash=catalog.content_hash,
        encoding="utf-8",
        delimiter=",",
        header_row=1,
        row_count=len(rows),
        columns=(
            SourceDatasetColumn(
                ordinal=1,
                source_name="Product ID",
                stable_key="column:1:product",
                candidate_type="string",
            ),
            SourceDatasetColumn(
                ordinal=2,
                source_name="Product Category",
                stable_key="column:2:category",
                candidate_type="string",
            ),
        ),
    )
    selection = SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        project_id=project_id or str(uuid4()),
        created_at=now,
        created_by="Test operator",
        datasets=(dataset,),
        content_hash="sha256:" + "b" * 64,
    )
    return selection, catalog


def _schema_field(
    name: str,
    label: str,
    field_type: str,
    *,
    required: bool = False,
    relation: str | None = None,
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
