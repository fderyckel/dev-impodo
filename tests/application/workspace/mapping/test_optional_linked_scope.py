"""Check exact empty scope values without broadening Stage 3 Odoo queries."""

from dataclasses import replace
from unittest import TestCase

from impodo.application.workspace.mapping.order_service import MatchingOrderService
from impodo.domain.mapping.contracts import ReferenceKeyMapping
from impodo.domain.mapping.reference_preparation import ReferencePreparation
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.access import LOCAL_ACTOR, CapabilityAuthorizationPolicy
from impodo.domain.shared.models import FieldMetadata, ModelMetadata, TargetRecord
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from tests.application.workspace.mapping import (
    test_matching_order_live_check as fixtures,
)
from tests.support.optional_scope import optional_scope_fixture


def schema_models(*, required=False):
    def field(name, kind="char", relation=None):
        return SchemaField(
            name,
            name,
            kind,
            required if name == "batch_id" else False,
            False,
            relation,
            None,
            (),
        )

    return (
        SchemaModel(
            "x.stock",
            "Stock",
            (
                field("code"),
                field("location"),
                field("batch_id", "many2one", "x.batch"),
            ),
        ),
        SchemaModel("x.batch", "Batch", (field("name"), field("company"))),
    )


def business_keys():
    return (
        BusinessKeyDefinition(
            "stock",
            "x.stock",
            ("code",),
            ("location", "batch_id"),
            status=BusinessKeyStatus.CONFIRMED,
        ),
        BusinessKeyDefinition(
            "batch",
            "x.batch",
            ("name",),
            ("company",),
            status=BusinessKeyStatus.CONFIRMED,
        ),
    )


class OptionalScopeLiveCheckTests(TestCase):
    def check(
        self,
        major=20,
        mode="REMOTE",
        *,
        duplicate=False,
        missing=False,
        prepared_formula=False,
        reused_source=False,
    ):
        preparation = (
            ReferencePreparation(
                formula='concat(value, "-", column_4)',
                bindings=(("column_4", "company"),),
            )
            if prepared_formula
            else None
        )
        selection, definition, rows = optional_scope_fixture(preparation=preparation)
        if reused_source:
            mapping = definition.datasets[0]
            scope = mapping.target_scope[-1]
            scope = replace(
                scope,
                source_column_keys=("lot", "product"),
                resolver=replace(
                    scope.resolver,
                    scope_mappings=(ReferenceKeyMapping("product", "company"),),
                ),
            )
            definition = replace(
                definition,
                datasets=(
                    replace(mapping, target_scope=(*mapping.target_scope[:-1], scope)),
                ),
            )
        evidence = fixtures._evidence()
        evidence.fingerprint = replace(
            evidence.fingerprint, odoo_version=f"{major}.0", connection_mode=mode
        )
        evidence.schema = replace(
            evidence.schema,
            models=schema_models(),
            odoo_version=f"{major}.0",
            connection_mode=mode,
        )
        evidence.selection = selection
        evidence.governance = replace(
            evidence.governance,
            permitted_models=("x.stock", "x.batch"),
            business_keys=business_keys(),
        )
        definition = replace(
            definition,
            mapping_id=evidence.draft.mapping_id,
            schema_hash=evidence.governance.content_hash,
        )
        evidence.draft = replace(evidence.draft, definition=definition)
        columns = (
            ("product", "location", "lot")
            if reused_source
            else ("product", "location", "lot", "company")
        )
        keys = fixtures._Keys(
            {
                (selection.datasets[0].dataset_id, columns): tuple(
                    tuple(row.values[key] for key in columns) for row in rows
                )
            }
        )
        repository = fixtures._Repository()
        service = MatchingOrderService(
            repository, CapabilityAuthorizationPolicy(), keys
        )
        local = service.recommend(selection, evidence.schema, definition)
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )
        stock_requests = [
            request
            for request in prepared.requirements.record_requests
            if request.model == "x.stock"
        ]
        self.assertEqual(len(stock_requests), 2)
        self.assertIn(
            "['batch_id', '=', False]",
            str([request.domain for request in stock_requests]),
        )
        self.assertNotIn(
            "['batch_id.name', '=', False]",
            str([request.domain for request in stock_requests]),
        )
        self.assertEqual(len(keys.calls), 1)
        self.assertEqual(len(prepared.requirements.record_requests), 3)
        fingerprint = evidence.fingerprint
        metadata = MetadataSnapshot(
            fingerprint,
            {
                model.name: ModelMetadata(
                    model.name,
                    model.label,
                    {
                        field.name: FieldMetadata(
                            field.name, field.type, relation=field.relation
                        )
                        for field in model.fields
                    },
                )
                for model in evidence.schema.models
            },
        )
        stocks = tuple(
            TargetRecord(
                "x.stock", index, {"code": "P", "location": "L", "batch_id": batch}
            )
            for index, batch in enumerate((False, 21, 22), 11)
        )
        if duplicate:
            stocks += (
                TargetRecord(
                    "x.stock", 14, {"code": "P", "location": "L", "batch_id": False}
                ),
            )
        batches = tuple(
            TargetRecord(
                "x.batch",
                index,
                {
                    "name": name + ("-C" if prepared_formula else ""),
                    "company": "P" if reused_source else "C",
                },
            )
            for index, name in ((21, "LOT-001"), (22, "LOT-002"))
            if not missing or index != 21
        )
        snapshot = RecordSnapshot(
            fingerprint,
            {"x.stock": stocks, "x.batch": batches},
            requested_fields={
                request.model: request.fields
                for request in prepared.requirements.record_requests
            },
        )
        service._run_live_check(prepared, lambda _requirements: (metadata, snapshot))
        self.assertIsNotNone(repository.check)
        return repository.check.identity_results[0]

    def test_blank_and_populated_scopes_are_three_distinct_existing_records(self):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    result = self.check(major, mode)
                    self.assertTrue(result.checked)
                    self.assertEqual(result.source_blank_row_count, 0)
                    self.assertEqual(result.expected_existing_count, 3)
                    self.assertEqual(result.blocked_count, 0)

    def test_ambiguous_empty_scope_still_blocks(self):
        result = self.check(duplicate=True)
        self.assertEqual(result.target_ambiguous_key_count, 1)
        self.assertEqual(result.blocked_count, 1)

    def test_optional_scope_skips_its_formula_only_when_absence_input_is_blank(self):
        result = self.check(prepared_formula=True)
        self.assertEqual(result.expected_existing_count, 3)
        self.assertEqual(result.blocked_count, 0)

    def test_reused_source_column_keeps_primary_identity_populated(self):
        result = self.check(reused_source=True)
        self.assertEqual(result.expected_existing_count, 3)
        self.assertEqual(result.source_blank_row_count, 0)

    def test_missing_populated_reference_never_becomes_empty_scope(self):
        result = self.check(missing=True)
        self.assertFalse(result.checked)
