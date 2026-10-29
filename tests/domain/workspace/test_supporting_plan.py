"""Pure dependency coverage and bounded planning over accepted descriptors."""

from dataclasses import replace
import unittest

from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.domain.workspace.supporting_plan import (
    MAX_SUPPORTING_DATASETS,
    MAX_SUPPORTING_DEPTH,
    MAX_SUPPORTING_EDGES,
    MAX_SUPPORTING_MODELS,
    SupportingDatasetIntent,
    SupportingPlanGap,
    SupportingPlanRole,
    SupportingRelationshipIntent,
    SupportingSourceDescriptor,
    plan_supporting_data,
)

HASH = "sha256:" + "a" * 64


def model(name, *fields):
    return SchemaModel(name, name, tuple(fields))


def relation(name, related, *, required=True, kind="many2one", **flags):
    return SchemaField(name, name, kind, required, False, related, None, (), **flags)


def dataset(name, target=None):
    return SupportingSourceDescriptor(name, name, "PHYSICAL", HASH, target)


def proposal(models, datasets=(), bindings=(), intents=(), roots=("x.product",)):
    return plan_supporting_data(
        selected_models=roots,
        models=tuple(models),
        datasets=tuple(datasets),
        source_selection_hash=HASH,
        schema_hash=HASH,
        preparation_hash=None,
        dataset_intents=tuple(bindings),
        relationship_intents=tuple(intents),
    )


class SupportingDataPlanTests(unittest.TestCase):
    def test_descriptor_and_intent_limits_precede_dependency_expansion(self):
        models = (model("x.product", relation("category_id", "x.category")),)
        result = proposal(
            models, tuple(dataset(f"d{i}") for i in range(MAX_SUPPORTING_DATASETS + 1))
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.edges, ())
        self.assertEqual(result.issues[0].reason, SupportingPlanGap.INPUT_LIMIT)
        result = proposal(
            models,
            intents=tuple(
                SupportingRelationshipIntent("x.product", f"f{i}")
                for i in range(MAX_SUPPORTING_EDGES + 1)
            ),
        )
        self.assertEqual(result.edges, ())
        self.assertEqual(result.issues[0].reason, SupportingPlanGap.INPUT_LIMIT)

    def test_explicit_physical_binding_traverses_incoming_metadata_only(self):
        models = (
            model("x.product", relation("category_id", "x.category")),
            model("x.category", relation("company_id", "res.company")),
            model("x.unrelated", relation("unrelated_id", "x.other")),
        )
        result = proposal(
            models,
            (dataset("categories"),),
            (SupportingDatasetIntent("categories", "x.category"),),
        )
        self.assertTrue(result.complete)
        self.assertEqual(len(result.edges), 2)
        category, product = result.edges
        self.assertEqual(product.role, SupportingPlanRole.INCOMING_CANDIDATE)
        self.assertEqual(category.role, SupportingPlanRole.REUSE_EXISTING)
        self.assertEqual(result.selected_models, ("x.product",))

    def test_labels_and_unused_optional_fields_do_not_infer_business_intent(self):
        models = (
            model(
                "x.product",
                relation("category_id", "x.category"),
                relation("tag_ids", "x.tag", required=False, kind="many2many"),
            ),
        )
        result = proposal(models, (dataset("x.category"), dataset("x.tag", "x.tag")))
        self.assertTrue(result.complete)
        self.assertEqual(len(result.edges), 1)
        self.assertEqual(result.edges[0].role, SupportingPlanRole.REUSE_EXISTING)

    def test_explicit_optional_intent_retains_separate_field_paths(self):
        models = (
            model(
                "x.product",
                relation("first_id", "x.category", required=False),
                relation("second_ids", "x.category", required=False, kind="many2many"),
            ),
            model("x.category"),
        )
        result = proposal(
            models,
            (dataset("categories", "x.category"),),
            intents=(
                SupportingRelationshipIntent("x.product", "first_id"),
                SupportingRelationshipIntent("x.product", "second_ids"),
            ),
        )
        self.assertTrue(result.complete)
        self.assertEqual(
            {item.field_name for item in result.edges}, {"first_id", "second_ids"}
        )
        self.assertTrue(
            all(item.reason == "EXPLICIT_RELATIONSHIP" for item in result.edges)
        )

    def test_ambiguous_tables_wait_for_explicit_choice(self):
        models = (
            model("x.product", relation("category_id", "x.category")),
            model("x.category"),
        )
        tables = (dataset("a", "x.category"), dataset("b", "x.category"))
        result = proposal(models, tables)
        self.assertFalse(result.complete)
        self.assertEqual(result.issues[0].reason, SupportingPlanGap.AMBIGUOUS_INCOMING)
        self.assertEqual(result.edges[0].role, SupportingPlanRole.UNRESOLVED)
        resolved = proposal(
            models,
            tables,
            intents=(SupportingRelationshipIntent("x.product", "category_id", "b"),),
        )
        self.assertTrue(resolved.complete)
        self.assertEqual(resolved.edges[0].candidates, (tables[1],))

    def test_conflicting_and_unknown_bindings_and_intents_remain_incomplete(self):
        models = (model("x.product", relation("category_id", "x.category")),)
        for bindings, intents in (
            ((SupportingDatasetIntent("missing", "x.category"),), ()),
            ((SupportingDatasetIntent("a", "x.different"),), ()),
            ((), (SupportingRelationshipIntent("x.product", "missing"),)),
            (
                (),
                (SupportingRelationshipIntent("x.product", "category_id", "missing"),),
            ),
            (
                (),
                (
                    SupportingRelationshipIntent("x.product", "category_id", "a"),
                    SupportingRelationshipIntent("x.product", "category_id", "b"),
                ),
            ),
        ):
            with self.subTest(bindings=bindings, intents=intents):
                result = proposal(
                    models, (dataset("a", "x.category"),), bindings, intents
                )
                self.assertFalse(result.complete)
                self.assertIn(
                    SupportingPlanGap.INVALID_INTENT,
                    {item.reason for item in result.issues},
                )

    def test_default_and_managed_fields_do_not_force_incoming_tables(self):
        models = (
            model(
                "x.product",
                relation("company_id", "res.company", create_default_present=True),
                relation("computed_id", "x.computed", computed=True),
            ),
        )
        result = proposal(
            models,
            (dataset("companies", "res.company"), dataset("computed", "x.computed")),
        )
        self.assertTrue(result.complete)
        self.assertEqual(
            {item.role for item in result.edges},
            {SupportingPlanRole.CHECKED_DEFAULT, SupportingPlanRole.ODOO_MANAGED},
        )

    def test_inverse_child_has_a_candidate_and_retains_its_owner_field(self):
        inverse = replace(
            relation("line_ids", "x.line", required=False, kind="one2many"),
            relation_field="product_id",
        )
        models = (
            model("x.product", inverse),
            model("x.line", relation("product_id", "x.product")),
        )
        missing = proposal(models)
        self.assertIn(
            SupportingPlanGap.INCOMING_UNAVAILABLE,
            {item.reason for item in missing.issues},
        )
        result = proposal(
            models, (dataset("lines", "x.line"), dataset("products", "x.product"))
        )
        self.assertTrue(result.complete)
        self.assertEqual(len(result.edges), 2)
        self.assertEqual(result.edges[1].inverse_field, "product_id")

    def test_cycles_and_repeated_paths_are_visited_once(self):
        models = (
            model(
                "x.product",
                relation("a_id", "x.category"),
                relation("b_id", "x.category"),
            ),
            model("x.category", relation("product_id", "x.product")),
        )
        result = proposal(
            models,
            (dataset("products", "x.product"), dataset("categories", "x.category")),
        )
        self.assertTrue(result.complete)
        self.assertEqual(len(result.edges), 3)

    def test_missing_incoming_metadata_is_not_claimed_complete(self):
        result = proposal(
            (model("x.product", relation("category_id", "x.category")),),
            (dataset("categories", "x.category"),),
        )
        self.assertFalse(result.complete)
        self.assertEqual(
            result.issues[0].reason, SupportingPlanGap.METADATA_UNAVAILABLE
        )

    def test_depth_model_and_edge_limits_are_explicit(self):
        chain = tuple(
            model(f"x.m{i}", relation("next_id", f"x.m{i + 1}"))
            for i in range(MAX_SUPPORTING_DEPTH + 2)
        )
        tables = tuple(dataset(f"d{i}", f"x.m{i}") for i in range(len(chain) + 1))
        result = proposal(chain, tables, roots=("x.m0",))
        self.assertIn(
            SupportingPlanGap.DEPTH_LIMIT, {item.reason for item in result.issues}
        )
        self.assertEqual(len(result.edges), MAX_SUPPORTING_DEPTH + 1)
        roots = tuple(f"x.m{i}" for i in range(MAX_SUPPORTING_MODELS + 1))
        result = proposal(tuple(model(name) for name in roots), roots=roots)
        self.assertIn(
            SupportingPlanGap.MODEL_LIMIT, {item.reason for item in result.issues}
        )
        result = proposal(
            (
                model(
                    "x.product",
                    *(
                        relation(f"r{i}", "x.reference")
                        for i in range(MAX_SUPPORTING_EDGES + 1)
                    ),
                ),
            )
        )
        self.assertEqual(len(result.edges), MAX_SUPPORTING_EDGES)
        self.assertIn(
            SupportingPlanGap.EDGE_LIMIT, {item.reason for item in result.issues}
        )

    def test_order_and_hash_are_deterministic_and_bind_evidence(self):
        models = (
            model(
                "x.product", relation("b", "x.category"), relation("a", "x.category")
            ),
            model("x.category"),
        )
        tables = (dataset("z", "x.category"), dataset("a", "x.category"))
        first = proposal(models, tables)
        second = proposal(tuple(reversed(models)), tuple(reversed(tables)))
        self.assertEqual(first, second)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(
            first.content_hash,
            replace(first, schema_hash="sha256:" + "b" * 64).content_hash,
        )
