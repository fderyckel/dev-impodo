"""Parent rules belong to source tables even when their Odoo model is shared."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.workspace.supporting_closure import (
    close_supporting_plan,
    supporting_mapping_conflicts,
)
from impodo.domain.workspace.supporting_errors import SupportingPlanIncompleteError
from impodo.domain.workspace.supporting_plan import (
    SupportingPlanGap,
    SupportingRelationshipIntent,
    plan_supporting_data,
)
from impodo.domain.workspace.supporting_review import SupportingChoice
from tests.domain.workspace.test_supporting_plan import dataset, model, relation


class HierarchicalSupportingPlanTests(TestCase):
    def _schema(self, name, parent, version, collection=None):
        fields = [relation(parent, name, required=False)]
        if collection:
            fields.append(
                replace(
                    relation(collection, name, required=False, kind="one2many"),
                    relation_field=parent,
                )
            )
        return SimpleNamespace(
            odoo_version=version,
            content_hash="schema",
            models=(model(name, *fields),),
        )

    def _mapping(self, owner, name, parent, incoming):
        return DatasetMapping(
            owner,
            name,
            source_identity_column_keys=("row_key",),
            target_identity=(IdentityComponentMapping(("label",), ("name",)),),
            target_scope=(
                IdentityComponentMapping(
                    ("parent_key",),
                    (parent,),
                    resolver=RelationshipResolver(
                        ResolverOrigin.DATASET
                        if incoming
                        else ResolverOrigin.TARGET_CATALOG,
                        dataset_id=incoming,
                        model=name,
                    ),
                ),
            ),
        )

    def _close(self, schema, mappings):
        return close_supporting_plan(
            schema=schema,
            datasets=tuple(dataset(item.dataset_id) for item in mappings),
            mappings=mappings,
            choices=(),
            source_hash="source",
            preparation_hash="hierarchy",
        )

    def test_shared_model_keeps_each_tables_explicit_parent_provider(self):
        for version in ("19.0", "20.0"):
            for name, parent in (
                ("stock.location", "location_id"),
                ("product.category", "parent_id"),
                ("account.group", "parent_id"),
                ("x.tree_node", "x_parent_id"),
            ):
                for second in ("hierarchy", "original", None):
                    with self.subTest(version=version, model=name, second=second):
                        mappings = (
                            self._mapping("hierarchy", name, parent, "hierarchy"),
                            self._mapping("original", name, parent, second),
                        )
                        closure = self._close(
                            self._schema(name, parent, version), mappings
                        )
                        self.assertTrue(
                            closure.proposal.complete, closure.proposal.issues
                        )
                        edge = closure.proposal.edges[0]
                        expected = {"hierarchy", second} - {None}
                        self.assertEqual(
                            {item.dataset_id for item in edge.candidates}, expected
                        )

    def test_conflicting_providers_within_one_source_table_stay_blocked(self):
        schema = self._schema("x.tree_node", "x_parent_id", "20.0")
        first = self._mapping("hierarchy", "x.tree_node", "x_parent_id", "hierarchy")
        conflicting = replace(
            first,
            target_scope=(
                *first.target_scope,
                replace(
                    first.target_scope[0],
                    resolver=RelationshipResolver(
                        ResolverOrigin.TARGET_CATALOG, model="x.tree_node"
                    ),
                ),
            ),
        )
        closure = self._close(schema, (conflicting,))
        self.assertFalse(closure.proposal.complete)
        self.assertEqual(
            closure.proposal.issues[0].reason, SupportingPlanGap.INVALID_INTENT
        )
        error = SupportingPlanIncompleteError(
            closure.proposal, schema, (conflicting,), ()
        )
        self.assertIn("in Match data, then check matches again", str(error))

    def test_parent_table_can_receive_children_from_multiple_incoming_tables(self):
        for version in ("19.0", "20.0"):
            for name, parent, collection in (
                ("stock.location", "location_id", "child_ids"),
                ("product.category", "parent_id", "child_id"),
                ("account.group", "parent_id", "child_ids"),
                ("x.tree_node", "x_parent_id", "x_child_ids"),
            ):
                with self.subTest(version=version, model=name):
                    mappings = (
                        self._mapping("hierarchy", name, parent, "hierarchy"),
                        self._mapping("original", name, parent, "hierarchy"),
                    )
                    schema = self._schema(name, parent, version, collection)
                    closure = self._close(schema, mappings)
                    self.assertTrue(closure.proposal.complete, closure.proposal.issues)
                    children = next(
                        edge
                        for edge in closure.proposal.edges
                        if edge.field_name == collection
                    )
                    self.assertEqual(
                        {item.dataset_id for item in children.candidates},
                        {"hierarchy", "original"},
                    )
                    skipped = (SupportingChoice(name, collection, "SKIP_OPTIONAL"),)
                    self.assertTrue(
                        supporting_mapping_conflicts(skipped, mappings, schema)
                    )

    def test_missing_parent_table_stays_blocked(self):
        schema = self._schema("x.tree_node", "x_parent_id", "20.0")
        mapping = self._mapping("original", "x.tree_node", "x_parent_id", "missing")
        closure = self._close(schema, (mapping,))
        self.assertFalse(closure.proposal.complete)
        self.assertEqual(
            closure.proposal.issues[0].reason, SupportingPlanGap.INVALID_INTENT
        )

    def test_reassigning_parent_owners_changes_evidence_without_changing_candidates(
        self,
    ):
        schema = self._schema("x.tree_node", "x_parent_id", "20.0")
        mappings = (
            self._mapping("a", "x.tree_node", "x_parent_id", "a"),
            self._mapping("b", "x.tree_node", "x_parent_id", "b"),
        )
        original = self._close(schema, mappings)
        reordered = self._close(schema, tuple(reversed(mappings)))
        swapped = self._close(
            schema,
            tuple(
                replace(
                    item,
                    target_scope=(
                        replace(
                            item.target_scope[0],
                            resolver=replace(
                                item.target_scope[0].resolver,
                                dataset_id="b" if item.dataset_id == "a" else "a",
                            ),
                        ),
                    ),
                )
                for item in mappings
            ),
        )
        self.assertTrue(original.proposal.complete)
        self.assertTrue(swapped.proposal.complete)
        self.assertEqual(original.content_hash, reordered.content_hash)
        self.assertNotEqual(original.proposal.intent_hash, swapped.proposal.intent_hash)

    def test_unknown_intent_owner_is_rejected(self):
        proposal = plan_supporting_data(
            selected_models=("x.tree_node",),
            models=self._schema("x.tree_node", "x_parent_id", "20.0").models,
            datasets=(dataset("known", "x.tree_node"),),
            source_selection_hash="source",
            schema_hash="schema",
            preparation_hash=None,
            relationship_intents=(
                SupportingRelationshipIntent(
                    "x.tree_node", "x_parent_id", "known", source_dataset_id="missing"
                ),
            ),
        )
        self.assertFalse(proposal.complete)
        self.assertEqual(proposal.issues[0].reason, SupportingPlanGap.INVALID_INTENT)
