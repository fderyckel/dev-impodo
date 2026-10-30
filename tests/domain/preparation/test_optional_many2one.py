"""Use existing requiredness for blank links across every resolver origin."""

from dataclasses import replace
from unittest import TestCase

from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import prepare_source_tables
from impodo.domain.recipe.profile import ResolveSpec
from impodo.domain.shared.models import Classification, TargetRecord
from tests.domain.preparation import test_target_first_relationships as fixtures


class OptionalMany2oneTests(TestCase):
    def prepare(
        self,
        *,
        origin="hybrid",
        value=None,
        required=False,
        create=False,
        null_policy="distinct",
        existing=False,
    ):
        plan = fixtures._plan()
        parent, child = plan.datasets
        resolver = {
            "target": ResolveSpec(target_model="uom.uom", target_fields=("name",)),
            "incoming": ResolveSpec(
                dataset="sales_uoms", target_source_fields=("uom_code",)
            ),
            "hybrid": child.relations["uom_id"].resolve,
        }[origin]
        relation = child.relations["uom_id"].model_copy(
            update={
                "resolve": resolver,
                "required": required,
                "required_on_create": create,
                "null_policy": null_policy,
            }
        )
        child = child.model_copy(update={"relations": {"uom_id": relation}})
        plan = plan.model_copy(update={"datasets": (parent, child)})
        tables = fixtures._tables(uom_codes=("PCE",))
        child_table = tables[1]
        row = child_table.rows[0]
        child_table = replace(
            child_table, rows=(replace(row, values={**row.values, "uom_code": value}),)
        )
        prepared = prepare_source_tables(
            plan,
            (tables[0], child_table),
            source_hashes={"sales_uoms": fixtures._HASH, "products": fixtures._HASH},
        )
        metadata, records = fixtures._snapshots()
        model = metadata.models["product.template"]
        metadata = replace(
            metadata,
            models={
                **metadata.models,
                "product.template": replace(
                    model,
                    fields={
                        **model.fields,
                        "uom_id": replace(model.fields["uom_id"], required=False),
                    },
                ),
            },
        )
        if existing:
            records = replace(
                records,
                records={
                    **records.records,
                    "product.template": (
                        TargetRecord(
                            "product.template",
                            99,
                            {
                                "default_code": "P-PCE",
                                "name": "Product in PCE",
                                "uom_id": False,
                            },
                        ),
                    ),
                },
            )
        result = PreflightEngine().run(plan, prepared, metadata, records)
        row = next(row for row in prepared.records if row.dataset == "products")
        decision = next(row for row in result.decisions if row.dataset == "products")
        return row, decision

    def test_optional_blanks_are_allowed_for_target_incoming_and_hybrid(self):
        for origin in ("target", "incoming", "hybrid"):
            for value in (None, "", " \t\u3000 "):
                for policy in ("distinct", "equivalent", "ignore_source_null"):
                    with self.subTest(origin=origin, value=value, policy=policy):
                        row, decision = self.prepare(
                            origin=origin, value=value, null_policy=policy
                        )
                        self.assertIsNone(row.references["uom_id"])
                        self.assertEqual(row.issues, ())
                        self.assertEqual(
                            decision.classification,
                            Classification.CREATE,
                            decision.issues,
                        )

    def test_required_blanks_still_block_every_resolver_origin(self):
        for origin in ("target", "incoming", "hybrid"):
            with self.subTest(origin=origin):
                row, decision = self.prepare(origin=origin, required=True)
                self.assertIn(
                    "SOURCE_REQUIRED_VALUE_MISSING",
                    {issue.code for issue in row.issues},
                )
                self.assertEqual(decision.classification, Classification.BLOCKED)

    def test_required_for_new_records_does_not_require_a_value_on_updates(self):
        for origin in ("target", "incoming", "hybrid"):
            for existing in (False, True):
                with self.subTest(origin=origin, existing=existing):
                    row, decision = self.prepare(
                        origin=origin, create=True, existing=existing
                    )
                    self.assertEqual(row.issues, ())
                    if existing:
                        self.assertNotEqual(
                            decision.classification,
                            Classification.BLOCKED,
                            decision.issues,
                        )
                    else:
                        self.assertEqual(
                            decision.classification, Classification.BLOCKED
                        )
                        self.assertIn(
                            "REQUIRED_ON_CREATE_MISSING",
                            {issue.code for issue in decision.issues},
                        )

    def test_populated_missing_reference_still_blocks_optional_field(self):
        for origin in ("target", "incoming", "hybrid"):
            with self.subTest(origin=origin):
                row, decision = self.prepare(origin=origin, value="NOT_FOUND")
                self.assertIsNotNone(row.references["uom_id"])
                self.assertEqual(decision.classification, Classification.BLOCKED)
