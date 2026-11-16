"""Project a generated hierarchy path before every kind of consumer mapping."""

from datetime import UTC, datetime
from dataclasses import replace
from pathlib import PurePath
from unittest import TestCase
from uuid import uuid4

from impodo.domain.hierarchy import HierarchyParentPolicy
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    IdentityNullPolicy,
    MappingDefinition,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.preparation.quality import quality_identity_key
from impodo.domain.preparation.source import SourceRow, SourceTable
from impodo.domain.preparation.staging_contracts import CanonicalStagingRun
from impodo.domain.staging.evaluator import evaluate_browser_mapping
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    HierarchicalLookupRule,
    HierarchyValuePolicy,
    derived_dataset_links,
    mapping_source_selection,
)
from tests.application.workspace.test_derived_entities import _source_evidence


class HierarchyMatchScopeTests(TestCase):
    def evaluate(
        self, *, scope=True, model="x.structure.node", parent_field="x_parent_id"
    ):
        rows = tuple((f"Child {i}", f"P{i:03d}") for i in range(22)) + (
            ("Same name", "P000"),
            ("Same name", "P021"),
        )
        selection, catalog = _source_evidence(rows=rows)
        physical = selection.datasets[0]
        child_key, parent_key = (column.stable_key for column in physical.columns)
        rule = HierarchicalLookupRule(
            str(uuid4()),
            "parent_nodes",
            physical.dataset_id,
            (parent_key,),
            model,
            "name",
            "fictional",
            HierarchyValuePolicy("block"),
            existing_parent=HierarchyParentPolicy(parent_field, ("Existing", "Root")),
        )
        plan = DerivedEntityPlan(
            str(uuid4()),
            1,
            str(uuid4()),
            selection.content_hash,
            (rule,),
            datetime.now(UTC),
            "Fictional operator",
        )
        effective = mapping_source_selection(selection, plan, (catalog,))
        link = derived_dataset_links(plan)[0]
        incoming = RelationshipResolver(
            origin=ResolverOrigin.DATASET, dataset_id=link.derived_dataset_id
        )
        parent_component = IdentityComponentMapping(
            (link.source_column_key,),
            (parent_field,),
            resolver=incoming,
        )
        mapping = DatasetMapping(
            physical.dataset_id,
            model,
            source_identity_column_keys=(parent_key, child_key),
            target_identity=(IdentityComponentMapping((child_key,), ("name",)),),
            target_scope=(parent_component,) if scope else (),
            relationships=()
            if scope
            else (
                RelationshipMapping(
                    target_field=parent_field,
                    source_column_keys=(link.source_column_key,),
                    kind="many2one",
                    resolver=incoming,
                    required=True,
                ),
            ),
            fields=(ScalarFieldMapping("x_path", link.source_column_key),),
        )
        parent_mapping = DatasetMapping(
            link.derived_dataset_id,
            model,
            source_identity_column_keys=(link.canonical_key_column_key,),
            target_identity=(
                IdentityComponentMapping((link.name_column_key,), ("name",)),
            ),
            target_scope=(
                IdentityComponentMapping(
                    (link.parent_key_column_key,),
                    (parent_field,),
                    resolver=incoming,
                    null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                ),
            ),
        )
        definition = MappingDefinition(
            str(uuid4()),
            effective.content_hash,
            "sha256:" + "a" * 64,
            (mapping, parent_mapping),
        )
        headers = tuple(column.source_name for column in physical.columns)
        table = SourceTable(
            physical.name,
            PurePath("fictional.csv"),
            headers,
            tuple(
                SourceRow(number, dict(zip(headers, values, strict=True)))
                for number, values in enumerate(rows, start=2)
            ),
            physical.source_evidence_hash,
        )
        return evaluate_browser_mapping(
            workspace_id=plan.workspace_id,
            definition=definition,
            physical_selection=selection,
            effective_selection=effective,
            plan=plan,
            loaded_tables={physical.dataset_id: table},
        ).canonical_run

    def test_parent_scope_and_scalar_use_the_complete_canonical_path(self):
        for model, field in (
            ("x.structure.node", "x_parent_id"),
            ("product.category", "parent_id"),
        ):
            with self.subTest(model=model):
                staged = self.evaluate(model=model, parent_field=field)
                children = [row for row in staged.rows if row.dataset == "products"]
                parents = {
                    row.source_identity[0]
                    for row in staged.rows
                    if row.dataset == "parent_nodes"
                }
                self.assertEqual(len(children), 24)
                for index, row in enumerate(children):
                    parent = index if index < 22 else (0 if index == 22 else 21)
                    expected = f'["existing","root","p{parent:03d}"]'
                    self.assertEqual(row.target_scope[0].key, (expected,))
                    self.assertEqual(row.proposed_values["x_path"], expected)
                    self.assertIn(expected, parents)
                    self.assertEqual(row.issues, ())
                self.assertNotEqual(
                    quality_identity_key(children[-2]),
                    quality_identity_key(children[-1]),
                )

    def test_explicit_parent_relationship_keeps_the_same_projection(self):
        children = [
            row for row in self.evaluate(scope=False).rows if row.dataset == "products"
        ]
        self.assertEqual(
            children[0].references["x_parent_id"].key, ('["existing","root","p000"]',)
        )
        self.assertEqual(
            children[-1].references["x_parent_id"].key, ('["existing","root","p021"]',)
        )
        self.assertTrue(all(not row.issues for row in children))

    def test_prior_staging_stays_readable_without_claiming_current_evaluation(self):
        current = self.evaluate()
        self.assertEqual(current.evaluator_version, 12)
        previous = replace(current, evaluator_version=11)
        restored = CanonicalStagingRun.from_json(previous.to_json())
        self.assertEqual(restored.content_hash, previous.content_hash)
        self.assertEqual(restored.evaluator_version, 11)
        self.assertNotEqual(restored.content_hash, current.content_hash)
