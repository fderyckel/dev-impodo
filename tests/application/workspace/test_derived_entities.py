from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from impodo.domain.shared.access import (
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    HierarchicalLookupRule,
    HierarchyValuePolicy,
    RelatedDatasetRule,
    derived_dataset_links,
    derived_mapping_samples,
    evaluate_hierarchy_path,
    mapping_source_selection,
    preview_derived_entities,
    preview_related_datasets,
    related_dataset_links,
)
from impodo.application.workspace.derived_entities import DerivedEntityWorkspaceService
from impodo.application.data_version.inspection import (
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
from impodo.domain.source_binding import FileSourceBinding
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus
from impodo.domain.workspace.contracts import (
    canonical_mapping_source_selection,
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.odoo_source_policy import ODOO_SOURCE_POLICY_HASH
from impodo.domain.workspace.errors import WorkspaceError
from tests.support.workspace_access import data_version_id
from impodo.web.presenters.mapping_view import _mapping_dataset_views


ROOT = REPOSITORY_ROOT
WORKSPACE_ID = str(uuid4())
DATA_VERSION_ID = str(uuid4())


class DerivedEntityPreviewTests(unittest.TestCase):
    def test_multi_column_hierarchy_applies_reviewed_blank_decisions(self) -> None:
        selection, catalog = _hierarchy_source_evidence()
        dataset = selection.datasets[0]
        rule = HierarchicalLookupRule(
            rule_id=str(uuid4()),
            output_dataset_name="product_categories",
            source_dataset_id=dataset.dataset_id,
            source_level_column_keys=(
                dataset.columns[1].stable_key,
                dataset.columns[2].stable_key,
            ),
            target_model="product.category",
            target_name_field="name",
            external_id_namespace="legacy_erp",
            missing_parent=HierarchyValuePolicy(mode="fixed", value="Default"),
            missing_leaf="use_deepest",
            all_blank=HierarchyValuePolicy(mode="emit_null_reference"),
        )

        preview = preview_derived_entities(rule, selection, (catalog,))
        by_key = {item.canonical_key: item for item in preview.candidates}

        self.assertEqual(
            set(by_key),
            {
                "components",
                "default",
                "default / m-200",
                "finished",
                "finished / m-100",
            },
        )
        self.assertEqual(preview.fixed_parent_sample_rows, 1)
        self.assertEqual(preview.deepest_level_sample_rows, 1)
        self.assertEqual(preview.blank_reference_sample_rows, 1)
        self.assertEqual(
            by_key["default / m-200"].parent_entity_id,
            by_key["default"].entity_id,
        )

        promoted = replace(
            rule,
            missing_parent=HierarchyValuePolicy(mode="promote"),
        )
        evaluation = evaluate_hierarchy_path(
            promoted,
            {
                dataset.columns[1].stable_key: None,
                dataset.columns[2].stable_key: "M-200",
            },
        )
        self.assertEqual(evaluation.canonical_parts, ("m-200",))
        self.assertEqual(evaluation.outcomes, ("promote",))

    def test_multi_column_hierarchy_supports_arbitrary_models(self) -> None:
        selection, catalog = _hierarchy_source_evidence()
        dataset = selection.datasets[0]
        rule = HierarchicalLookupRule(
            rule_id=str(uuid4()),
            output_dataset_name="organizational_units",
            source_dataset_id=dataset.dataset_id,
            source_level_column_keys=(
                dataset.columns[1].stable_key,
                dataset.columns[2].stable_key,
            ),
            target_model="x_org_unit",
            target_name_field="title",
            external_id_namespace="legacy_erp",
            missing_parent=HierarchyValuePolicy(mode="fixed", value="Default"),
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id=WORKSPACE_ID,
            source_selection_hash=selection.content_hash,
            rules=(rule,),
            updated_at=datetime.now(timezone.utc),
            updated_by="Test operator",
        )
        effective = mapping_source_selection(selection, plan, (catalog,))
        link = derived_dataset_links(plan)[0]

        self.assertEqual(
            tuple(item.name for item in effective.datasets),
            ("organizational_units", "products"),
        )
        self.assertTrue(link.hierarchical)
        self.assertIn(
            link.source_column_key,
            {item.stable_key for item in effective.datasets[1].columns},
        )
        self.assertEqual(
            tuple(item.stable_key for item in effective.datasets[0].columns),
            (
                link.canonical_key_column_key,
                link.name_column_key,
                link.parent_key_column_key,
            ),
        )

        schema = OdooSchemaCatalog(
            workspace_id=WORKSPACE_ID,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
            captured_at=datetime.now(timezone.utc),
            captured_by="Test operator",
            connection_mode="LOCAL",
            database="test",
            odoo_version="19.0",
            models=(
                SchemaModel(
                    name="x_org_unit",
                    label="Organizational Unit",
                    fields=(
                        _schema_field("title", "Title", "char"),
                        _schema_field(
                            "superior_unit_id",
                            "Superior Unit",
                            "many2one",
                            relation="x_org_unit",
                        ),
                    ),
                ),
                SchemaModel(
                    name="x_worker",
                    label="Worker",
                    fields=(
                        _schema_field("employee_code", "Employee Number", "char"),
                        _schema_field(
                            "assigned_unit_id",
                            "Assigned Unit",
                            "many2one",
                            relation="x_org_unit",
                        ),
                    ),
                ),
            ),
            content_hash="sha256:" + "2" * 64,
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash="sha256:" + "3" * 64,
            read_principal_hash="sha256:" + "4" * 64,
            read_permission_hash="sha256:" + "5" * 64,
            read_context_hash="sha256:" + "6" * 64,
            connection_target_hash="sha256:" + "7" * 64,
        )
        governance = SchemaGovernance(
            governance_id=str(uuid4()),
            version=1,
            workspace_id=WORKSPACE_ID,
            catalog_hash=schema.content_hash,
            permitted_models=("x_org_unit", "x_worker"),
            business_keys=(
                BusinessKeyDefinition(
                    key_id="x_org_unit::title",
                    model="x_org_unit",
                    key_fields=("title",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    key_id="x_worker::employee_code",
                    model="x_worker",
                    key_fields=("employee_code",),
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
            {0: "x_org_unit", 1: "x_worker"},
            (),
            (link,),
            {
                link.derived_dataset_id: derived_mapping_samples(
                    link,
                    preview_derived_entities(rule, selection, (catalog,)),
                )
            },
        )
        parent = next(
            item
            for item in views[0]["relation_rows"]
            if item["metadata"].name == "superior_unit_id"
        )
        worker_unit = next(
            item
            for item in views[1]["relation_rows"]
            if item["metadata"].name == "assigned_unit_id"
        )
        self.assertEqual(parent["recommended_dataset_id"], link.derived_dataset_id)
        self.assertEqual(
            parent["recommended_source_columns"],
            (link.parent_key_column_key,),
        )
        self.assertEqual(parent["recommended_origin"], "dataset")
        self.assertEqual(
            worker_unit["recommended_source_columns"],
            (link.source_column_key,),
        )
        self.assertEqual(worker_unit["recommended_origin"], "dataset")

        scoped_governance = replace(
            governance,
            governance_id=str(uuid4()),
            version=2,
            business_keys=(
                BusinessKeyDefinition(
                    key_id="x_org_unit::title,superior_unit_id",
                    model="x_org_unit",
                    key_fields=("title",),
                    scope_fields=("superior_unit_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                governance.business_keys[1],
            ),
        )
        scoped_views = _mapping_dataset_views(
            effective,
            schema,
            scoped_governance,
            (),
            (catalog,),
            {0: "x_org_unit", 1: "x_worker"},
            (),
            (link,),
            {
                link.derived_dataset_id: derived_mapping_samples(
                    link,
                    preview_derived_entities(rule, selection, (catalog,)),
                )
            },
        )
        parent_identity = next(
            item
            for item in scoped_views[0]["identity_rows"]
            if item["target_field"] == "superior_unit_id"
        )
        self.assertEqual(
            parent_identity["selected_sources"],
            (link.parent_key_column_key,),
        )
        self.assertEqual(parent_identity["selected_origin"], "dataset")
        self.assertEqual(
            parent_identity["selected_dataset_id"],
            link.derived_dataset_id,
        )
        self.assertIn(
            link.derived_dataset_id,
            {
                item.dataset_id
                for item in parent_identity["related_datasets"]
            },
        )

        restored = DerivedEntityPlan.from_json(plan.to_json())
        restored_rule = restored.rules[0]
        self.assertIsInstance(restored_rule, HierarchicalLookupRule)
        self.assertEqual(
            restored_rule.source_level_column_keys,
            rule.source_level_column_keys,
        )
        self.assertEqual(restored_rule.missing_parent.value, "Default")

    def test_hierarchy_requires_explicit_valid_missing_value_policies(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a value"):
            HierarchicalLookupRule(
                rule_id=str(uuid4()),
                output_dataset_name="categories",
                source_dataset_id="dataset:products",
                source_level_column_keys=("column:group", "column:code"),
                target_model="product.category",
                target_name_field="name",
                external_id_namespace="legacy",
                missing_parent=HierarchyValuePolicy(mode="fixed"),
            )
        with self.assertRaisesRegex(ValueError, "different field"):
            HierarchicalLookupRule(
                rule_id=str(uuid4()),
                output_dataset_name="categories",
                source_dataset_id="dataset:products",
                source_level_column_keys=("column:group", "column:group"),
                target_model="product.category",
                target_name_field="name",
                external_id_namespace="legacy",
                missing_parent=HierarchyValuePolicy(mode="block"),
            )

    def test_effective_selection_is_stable_across_physical_dataset_order(
        self,
    ) -> None:
        selection, catalog = _source_evidence()
        product = selection.datasets[0]
        account = replace(
            product,
            dataset_id="dataset:accounts",
            name="accounts",
        )
        plan = DerivedEntityPlan(
            plan_id=str(uuid4()),
            version=1,
            workspace_id=WORKSPACE_ID,
            source_selection_hash=selection.content_hash,
            rules=(_rule(selection),),
            updated_at=datetime.now(timezone.utc),
            updated_by="Test operator",
        )
        authored = canonical_mapping_source_selection(
            replace(
                selection,
                datasets=(product, account),
                content_hash="sha256:" + "a" * 64,
            )
        )
        canonical = canonical_mapping_source_selection(
            replace(
                selection,
                datasets=(account, product),
                content_hash="sha256:" + "b" * 64,
            )
        )

        authored_effective = mapping_source_selection(authored, plan, (catalog,))
        canonical_effective = mapping_source_selection(canonical, plan, (catalog,))

        self.assertEqual(
            tuple(item.dataset_id for item in authored_effective.datasets),
            tuple(item.dataset_id for item in canonical_effective.datasets),
        )
        self.assertEqual(
            authored_effective.content_hash,
            canonical_effective.content_hash,
        )

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

    def test_noncurrent_plan_contract_is_rejected(self) -> None:
        selection, _catalog = _source_evidence()
        with self.assertRaisesRegex(ValueError, "contract version"):
            DerivedEntityPlan(
                plan_id=str(uuid4()),
                version=1,
                workspace_id=WORKSPACE_ID,
                source_selection_hash=selection.content_hash,
                rules=(_rule(selection),),
                updated_at=datetime.now(timezone.utc),
                updated_by="Test operator",
                contract_version=1,
            )

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
            workspace_id=str(uuid4()),
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
            workspace_id=WORKSPACE_ID,
            source_selection_hash=selection.content_hash,
            rules=(rule,),
            updated_at=datetime.now(timezone.utc),
            updated_by="Test operator",
        )
        effective = mapping_source_selection(selection, plan, (catalog,))
        schema = OdooSchemaCatalog(
            workspace_id=WORKSPACE_ID,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
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
            read_credential_binding_hash="sha256:" + "3" * 64,
            read_principal_hash="sha256:" + "4" * 64,
            read_permission_hash="sha256:" + "5" * 64,
            read_context_hash="sha256:" + "6" * 64,
            connection_target_hash="sha256:" + "7" * 64,
        )
        governance = SchemaGovernance(
            governance_id=str(uuid4()),
            version=1,
            workspace_id=WORKSPACE_ID,
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
            workspace_id=WORKSPACE_ID,
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
            workspace_id=WORKSPACE_ID,
            policy_hash=ODOO_SOURCE_POLICY_HASH,
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
            read_credential_binding_hash="sha256:" + "3" * 64,
            read_principal_hash="sha256:" + "4" * 64,
            read_permission_hash="sha256:" + "5" * 64,
            read_context_hash="sha256:" + "6" * 64,
            connection_target_hash="sha256:" + "7" * 64,
        )
        governance = SchemaGovernance(
            governance_id=str(uuid4()),
            version=1,
            workspace_id=WORKSPACE_ID,
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
        database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspace_states = WorkspaceStateRepository(database)
        self.derived_entities = DerivedEntityRepository(database)
        self.sources = SourceRepository(database, self.derived_entities)
        now = datetime.now(timezone.utc)
        self.workspace_state = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Product migration",
            source_system="Legacy ERP",
            status=WorkspaceStatus.REGISTERED,
            registered_at=now,
        )
        self.workspace_states.initialize_workbench(self.workspace_state, actor=LOCAL_ACTOR)
        self.selection, self.catalog = _source_evidence(
            workspace_id=self.workspace_state.workspace_id
        )
        self.sources.save_source_selection(
            self.workspace_state.workspace_id,
            self.selection,
            actor=LOCAL_ACTOR,
        )
        self.service = DerivedEntityWorkspaceService(
            self.sources,
            self.derived_entities,
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
            self.workspace_state.workspace_id,
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
            self.derived_entities.get_derived_entity_plan(self.workspace_state.workspace_id),
            plan,
        )
        self.assertEqual(rule.source_column_key, category.stable_key)
        with self.assertRaisesRegex(
            WorkspaceError,
            "Derived dataset names must be unique",
        ):
            self.service.save_rule(
                self.workspace_state.workspace_id,
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
        self.sources.save_source_selection(
            self.workspace_state.workspace_id,
            replacement,
            actor=LOCAL_ACTOR,
        )
        self.assertIsNone(
            self.derived_entities.get_derived_entity_plan(self.workspace_state.workspace_id)
        )

    def test_multi_column_hierarchy_is_saved_as_one_versioned_rule(self) -> None:
        dataset = self.selection.datasets[0]

        plan, rule = self.service.save_hierarchy(
            self.workspace_state.workspace_id,
            output_dataset_name="product_categories",
            source_dataset_id=dataset.dataset_id,
            source_level_column_keys=tuple(
                item.stable_key for item in dataset.columns
            ),
            target_model="product.category",
            target_name_field="name",
            external_id_namespace="legacy_erp",
            missing_parent_mode="fixed",
            missing_parent_value="Default",
            missing_leaf="use_deepest",
            all_blank_mode="emit_null_reference",
            all_blank_value=None,
            expected_parent_version=None,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(plan.version, 1)
        self.assertEqual(rule.missing_parent.value, "Default")
        self.assertEqual(
            self.derived_entities.get_derived_entity_plan(
                self.workspace_state.workspace_id
            ),
            plan,
        )

    def test_related_split_replaces_physical_source_for_mapping(self) -> None:
        dataset = self.selection.datasets[0]
        plan, rule = self.service.save_related_split(
            self.workspace_state.workspace_id,
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
    workspace_id: str | None = None,
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
        source=FileSourceBinding(
            file_id=catalog.file_id,
            table_key="csv",
            source_sha256=catalog.source_sha256,
            catalog_hash=catalog.content_hash,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
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
        data_version_id=(
            data_version_id(workspace_id) if workspace_id else DATA_VERSION_ID
        ),
        created_at=now,
        created_by="Test operator",
        datasets=(dataset,),
        content_hash="sha256:" + "b" * 64,
    )
    return selection, catalog


def _hierarchy_source_evidence() -> tuple[SourceSelection, SourceFileCatalog]:
    rows = (
        ("P-001", "Finished", "M-100"),
        ("P-002", None, "M-200"),
        ("P-003", "Components", None),
        ("P-004", None, None),
    )
    now = datetime.now(timezone.utc)
    profiles = tuple(
        SourceColumnProfile(
            ordinal=ordinal,
            name=name,
            candidate_type="string",
            null_count=sum(row[ordinal - 1] is None for row in rows),
            non_null_count=sum(row[ordinal - 1] is not None for row in rows),
            distinct_count=len(
                {row[ordinal - 1] for row in rows if row[ordinal - 1] is not None}
            ),
            distinct_count_is_exact=True,
            duplicate_count=0,
            minimum=None,
            maximum=None,
            minimum_length=None,
            maximum_length=None,
        )
        for ordinal, name in enumerate(
            ("Product ID", "Model group", "Model code"),
            start=1,
        )
    )
    table = SourceTableCatalog(
        table_key="csv",
        name="products",
        kind="csv",
        hidden=False,
        header_row=1,
        row_count=len(rows),
        column_count=3,
        columns=profiles,
        preview_rows=rows,
    )
    catalog = SourceFileCatalog(
        contract_version=CATALOG_CONTRACT_VERSION,
        file_id=str(uuid4()),
        display_name="products.csv",
        source_sha256="c" * 64,
        source_size_bytes=256,
        format="csv",
        inspected_at=now,
        encoding="utf-8",
        delimiter=",",
        tables=(table,),
    )
    columns = tuple(
        SourceDatasetColumn(
            ordinal=ordinal,
            source_name=name,
            stable_key=f"column:{ordinal}:{name.casefold().replace(' ', '_')}",
            candidate_type="string",
        )
        for ordinal, name in enumerate(
            ("Product ID", "Model group", "Model code"),
            start=1,
        )
    )
    dataset = SourceDataset(
        dataset_id="dataset:products",
        name="products",
        source=FileSourceBinding(
            file_id=catalog.file_id,
            table_key="csv",
            source_sha256=catalog.source_sha256,
            catalog_hash=catalog.content_hash,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=len(rows),
        columns=columns,
    )
    selection = SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=DATA_VERSION_ID,
        created_at=now,
        created_by="Test operator",
        datasets=(dataset,),
        content_hash="sha256:" + "d" * 64,
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
