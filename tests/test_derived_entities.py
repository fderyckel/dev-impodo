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
    DerivedEntityRule,
    DerivedEntityWorkspaceService,
    preview_derived_entities,
)
from impodo.inspection import (
    CATALOG_CONTRACT_VERSION,
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.project_store import DuckDbProjectRepository
from impodo.projects import MigrationProject, ProjectStatus
from impodo.workspace import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
    WorkspaceError,
)


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


class DerivedEntityWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.repository = DuckDbProjectRepository(self.temporary.name)
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


if __name__ == "__main__":
    unittest.main()
