from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest
from uuid import uuid4

from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
)
from impodo.application.transfer_execution_service import (
    compile_transfer_execution_snapshot,
)
from impodo.application.transfer_order_service import TransferOrderService
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.domain.odoo_provenance import (
    OdooOriginBatch,
    OdooRelationshipOriginColumn,
)
from impodo.domain.preparation.source import SourceRow
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import (
    BusinessReference,
    FieldMetadata,
    LogicalReference,
    TargetRecord,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.destination_matching import (
    DestinationCreateFieldDecision,
    DestinationCreateFieldEvidence,
    DestinationCreateFieldEvidenceValue,
    choose_destination_create_field_provider,
)
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from tests.application.workspace.test_destination_matching import (
    BINDING_HASH,
    _SourceValues,
    _destination_reader,
    _identity,
    _relation,
    _selection,
    _source_schema,
    _workspace,
)


HASH = "sha256:" + "7" * 64


class TransferExecutionCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(UTC)
        self.workspace = _workspace(self.now)
        self.selection = _selection(self.now)
        product_dataset, uom_dataset = self.selection.datasets
        product_model, uom_model = _source_schema(self.workspace, self.now).models
        self.schema = replace(
            _source_schema(self.workspace, self.now),
            models=(
                replace(
                    product_model,
                    fields=product_model.fields
                    + (
                        _relation(
                            "alternate_uom_ids",
                            "Alternate Units",
                            "many2many",
                            "uom.uom",
                        ),
                        _relation(
                            "uom_id",
                            "Unit of Measure",
                            "many2one",
                            "uom.uom",
                            required=True,
                        ),
                    ),
                ),
                uom_model,
            ),
        )
        self.source_values = _SourceValues(
            {
                (product_dataset.dataset_id, "product-code"): (
                    {"value": "P001", "count": 1},
                    {"value": "P002", "count": 1},
                ),
                (uom_dataset.dataset_id, "uom-name"): (
                    {"value": "Kilogram", "count": 1},
                    {"value": "Unit", "count": 1},
                ),
            },
            {
                (product_dataset.dataset_id, "product-code"): ("P001", "P002"),
                (uom_dataset.dataset_id, "uom-name"): ("Unit", "Kilogram"),
            },
        )
        self.origins = {
            product_dataset.dataset_id: (
                OdooOriginBatch(
                    first_row_ordinal=1,
                    odoo_ids=(101, 102),
                    write_dates=(self.now, self.now),
                    relationships=(
                        OdooRelationshipOriginColumn(
                            field_name="alternate_uom_ids",
                            kind="many2many",
                            relation_model="uom.uom",
                            values=((7, 8), (8,)),
                        ),
                        OdooRelationshipOriginColumn(
                            field_name="uom_id",
                            kind="many2one",
                            relation_model="uom.uom",
                            values=((7,), (8,)),
                        ),
                    ),
                ),
            ),
            uom_dataset.dataset_id: (
                OdooOriginBatch(
                    first_row_ordinal=1,
                    odoo_ids=(7, 8),
                    write_dates=(self.now, self.now),
                ),
            ),
        }
        self.reader = _destination_reader(self.workspace, with_relationships=True)
        captured = []

        def capture(*args):
            metadata, records = self.reader(*args)
            captured.append(records)
            return metadata, records

        self.match = DestinationMatchingService(self.source_values).check(
            self.workspace,
            self.selection,
            self.schema,
            (
                DestinationMatchKeyChoice(product_dataset.dataset_id, "product-code"),
                DestinationMatchKeyChoice(uom_dataset.dataset_id, "uom-name"),
            ),
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.workspace),
            reader=capture,
            recorded_by="Data manager",
            source_origins=self.origins,
        )
        self.records = captured[0]
        matched = replace(self.workspace, destination_match_plan=self.match)
        order = TransferOrderService().build(
            matched,
            self.match,
            recorded_by="Data manager",
        )
        ordered = replace(matched, transfer_order_plan=order)
        self.ordered_workspace = ordered
        self.package = TransferReviewService().build(
            ordered,
            self.match,
            order,
            run_id=str(uuid4()),
            data_version_id=self.selection.data_version_id,
            built_by=LOCAL_ACTOR.identity,
        )
        approval = TransferReviewApproval.approve(
            self.package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=datetime.now(UTC),
        )
        approved = replace(
            ordered,
            transfer_review_package=self.package,
            transfer_review_approval=approval,
        )
        self.report = TransferPreflightService().build(
            approved,
            self.package,
            approval,
            self.match,
            self.match,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.workspace = replace(approved, transfer_preflight_report=self.report)
        self.rows = {
            product_dataset.dataset_id: (
                SourceRow(1, {"default_code": "P001", "name": "Product 1"}),
                SourceRow(2, {"default_code": "P002", "name": "Product 2"}),
            ),
            uom_dataset.dataset_id: (
                SourceRow(1, {"name": "Unit"}),
                SourceRow(2, {"name": "Kilogram"}),
            ),
        }
        self.snapshots = {
            item.dataset_id: SimpleNamespace(content_hash=HASH)
            for item in self.selection.datasets
        }
        self.manifests = {item.dataset_id: HASH for item in self.selection.datasets}

    def test_compiles_mixed_relationships_in_approved_generic_order(self) -> None:
        snapshot = self._compile(self.records)

        self.assertEqual(snapshot.counts["CREATE"], 2)
        self.assertEqual(snapshot.counts["UPDATE"], 2)
        rows = {(row.dataset, row.source_row): row for row in snapshot.rows}
        product = self.selection.datasets[0]
        uom = self.selection.datasets[1]
        existing_product = rows[(product.name, 1)]
        new_product = rows[(product.name, 2)]
        new_uom = rows[(uom.name, 2)]
        existing_uom = rows[(uom.name, 1)]
        existing_relation = next(
            item for item in existing_product.fields if item.field == "uom_id"
        )
        incoming_relation = next(
            item for item in new_product.fields if item.field == "uom_id"
        )
        alternate_relation = next(
            item
            for item in existing_product.fields
            if item.field == "alternate_uom_ids"
        )

        self.assertIsInstance(existing_relation.value, BusinessReference)
        self.assertIsInstance(incoming_relation.value, LogicalReference)
        self.assertEqual(incoming_relation.value.dataset, uom.name)
        self.assertTrue(
            any(isinstance(item, BusinessReference) for item in alternate_relation.value)
        )
        self.assertTrue(
            any(isinstance(item, LogicalReference) for item in alternate_relation.value)
        )
        self.assertLess(new_uom.schedule_ordinal, new_product.schedule_ordinal)
        self.assertLessEqual(existing_uom.schedule_component, new_product.schedule_component)
        self.assertTrue(new_uom.proposed_external_id.startswith("impodo_"))
        self.assertEqual(type(snapshot).from_json(snapshot.to_json()), snapshot)

    def test_create_only_incoming_reference_uses_selected_source_record(self) -> None:
        product_dataset, uom_dataset = self.selection.datasets
        captured = []

        def required_reference_reader(*args):
            metadata, records = self.reader(*args)
            product = metadata.models["product.template"]
            metadata = replace(
                metadata,
                models={
                    **metadata.models,
                    "product.template": replace(
                        product,
                        fields={
                            **product.fields,
                            "x_source_uom_id": FieldMetadata(
                                "x_source_uom_id",
                                "many2one",
                                "Selected Source Unit",
                                required=True,
                                relation="uom.uom",
                                company_dependent=False,
                            ),
                        },
                    ),
                },
            )
            captured.append(records)
            return metadata, records

        match = DestinationMatchingService(self.source_values).check(
            self.workspace,
            self.selection,
            self.schema,
            (
                DestinationMatchKeyChoice(product_dataset.dataset_id, "product-code"),
                DestinationMatchKeyChoice(uom_dataset.dataset_id, "uom-name"),
            ),
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.workspace),
            reader=required_reference_reader,
            recorded_by="Data manager",
            source_origins=self.origins,
        )
        evidence = match.create_field_evidence
        self.assertIsNotNone(evidence)
        protected = next(
            item for item in evidence.values
            if item.key == ("product.template", "x_source_uom_id")
        )
        kilogram = next(
            item for item in protected.incoming_reference_candidates
            if item.identity == ("Kilogram",)
        )
        unit = next(
            item for item in protected.incoming_reference_candidates
            if item.identity == ("Unit",)
        )
        stored_match = replace(
            match,
            protected_create_field_artifact_hash=HASH,
            create_field_evidence=None,
        )
        existing_choice = choose_destination_create_field_provider(
            stored_match,
            evidence,
            model="product.template",
            field_name="x_source_uom_id",
            provider_kind="incoming_reference",
            incoming_reference_choice_hash=unit.choice_hash,
        )
        existing_order = TransferOrderService().build(
            replace(self.workspace, destination_match_plan=existing_choice),
            existing_choice,
            recorded_by="Data manager",
        )
        self.assertNotIn(
            "x_source_uom_id",
            {item.field_name for item in existing_order.dependencies},
        )
        chosen = choose_destination_create_field_provider(
            stored_match,
            evidence,
            model="product.template",
            field_name="x_source_uom_id",
            provider_kind="incoming_reference",
            incoming_reference_choice_hash=kilogram.choice_hash,
        )
        matched = replace(
            self.workspace,
            destination_match_plan=chosen,
            transfer_order_plan=None,
            transfer_review_package=None,
            transfer_review_approval=None,
            transfer_preflight_report=None,
        )
        order = TransferOrderService().build(
            matched,
            chosen,
            recorded_by="Data manager",
        )
        dependency = next(
            item for item in order.dependencies
            if item.field_name == "x_source_uom_id"
        )
        self.assertEqual(dependency.dependency_dataset_id, uom_dataset.dataset_id)
        ordered = replace(matched, transfer_order_plan=order)
        package = TransferReviewService().build(
            ordered,
            chosen,
            order,
            run_id=str(uuid4()),
            data_version_id=self.selection.data_version_id,
            built_by=LOCAL_ACTOR.identity,
        )
        product_review = next(
            item for item in package.datasets
            if item.dataset_id == product_dataset.dataset_id
        )
        self.assertIn(
            ("x_source_uom_id", "incoming_reference"),
            product_review.create_field_providers,
        )
        self.assertNotIn("Kilogram", package.to_json())
        approval = TransferReviewApproval.approve(
            package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=self.now,
        )
        approved = replace(
            ordered,
            transfer_review_package=package,
            transfer_review_approval=approval,
        )
        report = TransferPreflightService().build(
            approved,
            package,
            approval,
            chosen,
            chosen,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.workspace = replace(approved, transfer_preflight_report=report)
        self.match, self.package, self.report = chosen, package, report

        snapshot = self._compile(captured[0])
        rows = {(row.dataset, row.source_row): row for row in snapshot.rows}
        existing_product = rows[(product_dataset.name, 1)]
        created_product = rows[(product_dataset.name, 2)]
        created_uom = rows[(uom_dataset.name, 2)]
        self.assertNotIn(
            "x_source_uom_id",
            {item.field for item in existing_product.fields},
        )
        incoming = next(
            item for item in created_product.fields
            if item.field == "x_source_uom_id"
        )
        self.assertIsInstance(incoming.value, LogicalReference)
        self.assertEqual(incoming.value.key, ("Kilogram",))
        self.assertEqual(incoming.value.dataset, uom_dataset.name)
        self.assertLess(created_uom.schedule_ordinal, created_product.schedule_ordinal)
        self.assertNotIn("Kilogram", chosen.to_json())

    def test_rejects_changed_destination_record_identity(self) -> None:
        changed = replace(
            self.records,
            records={
                **self.records.records,
                "product.template": (
                    TargetRecord(
                        "product.template",
                        99,
                        {"default_code": "P001"},
                    ),
                ),
            },
        )

        with self.assertRaisesRegex(
            WorkspaceError,
            "Destination identities changed",
        ):
            self._compile(changed)

    def test_composite_identity_compiles_and_resolves_relations(self) -> None:
        product, uom = self.selection.datasets
        self.source_values.rows[(
            product.dataset_id, ("product-code", "product-name")
        )] = (("P001", "Product 1"), ("P002", "Product 2"))
        captured = []

        def composite_reader(*args):
            metadata, records = self.reader(*args)
            records = replace(
                records,
                records={
                    **records.records,
                    "product.template": (
                        TargetRecord(
                            "product.template", 41,
                            {"default_code": "P001", "name": "Product 1"},
                        ),
                    ),
                },
            )
            captured.append(records)
            return metadata, records

        match = DestinationMatchingService(self.source_values).check(
            self.workspace, self.selection, self.schema,
            (
                DestinationMatchKeyChoice(
                    product.dataset_id, "product-code", ("product-name",)
                ),
                DestinationMatchKeyChoice(uom.dataset_id, "uom-name"),
            ),
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.workspace),
            reader=composite_reader,
            recorded_by="Data manager",
            source_origins=self.origins,
        )
        matched = replace(self.workspace, destination_match_plan=match)
        order = TransferOrderService().build(matched, match, recorded_by="Data manager")
        ordered = replace(matched, transfer_order_plan=order)
        package = TransferReviewService().build(
            ordered, match, order,
            run_id=str(uuid4()),
            data_version_id=self.selection.data_version_id,
            built_by=LOCAL_ACTOR.identity,
        )
        approval = TransferReviewApproval.approve(
            package, approval_id=str(uuid4()), actor=LOCAL_ACTOR,
            approved_at=datetime.now(UTC),
        )
        approved = replace(
            ordered, transfer_review_package=package,
            transfer_review_approval=approval,
        )
        report = TransferPreflightService().build(
            approved, package, approval, match, match,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.workspace = replace(approved, transfer_preflight_report=report)
        self.match, self.package, self.report = match, package, report

        snapshot = self._compile(captured[0])
        product_rows = [row for row in snapshot.rows if row.dataset == product.name]
        self.assertEqual(
            [row.business_identity for row in product_rows],
            [("P001", "Product 1"), ("P002", "Product 2")],
        )
        self.assertEqual(snapshot.counts["CREATE"], 2)
        self.assertEqual(type(snapshot).from_json(snapshot.to_json()), snapshot)

    def test_reuses_existing_supporting_record_without_updating_it(self) -> None:
        product, uom = self.selection.datasets
        package = TransferReviewService().build(
            self.ordered_workspace,
            self.match,
            self.ordered_workspace.transfer_order_plan,
            run_id=str(uuid4()),
            data_version_id=self.selection.data_version_id,
            built_by=LOCAL_ACTOR.identity,
            model_policies={
                "product.template": "upsert",
                "uom.uom": "create_if_missing",
            },
        )
        approval = TransferReviewApproval.approve(
            package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=datetime.now(UTC),
        )
        approved = replace(
            self.ordered_workspace,
            transfer_review_package=package,
            transfer_review_approval=approval,
        )
        report = TransferPreflightService().build(
            approved,
            package,
            approval,
            self.match,
            self.match,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.package = package
        self.report = report
        self.workspace = replace(approved, transfer_preflight_report=report)

        snapshot = self._compile(self.records)

        self.assertEqual(snapshot.counts["CREATE"], 2)
        self.assertEqual(snapshot.counts["UPDATE"], 1)
        self.assertEqual(snapshot.counts["UNCHANGED"], 1)
        rows = {(row.dataset, row.source_row): row for row in snapshot.rows}
        existing_uom = rows[(uom.name, 1)]
        self.assertEqual(existing_uom.disposition, "UNCHANGED")
        self.assertEqual(existing_uom.fields, ())
        self.assertEqual(rows[(uom.name, 2)].disposition, "CREATE")
        product_relation = next(
            field for field in rows[(product.name, 1)].fields
            if field.field == "uom_id"
        )
        self.assertIsInstance(product_relation.value, BusinessReference)
        self.assertEqual(type(snapshot).from_json(snapshot.to_json()), snapshot)

    def test_create_only_providers_do_not_change_existing_rows_or_expose_fixed_value(self) -> None:
        product_dataset = self.selection.datasets[0]
        field_hashes = {
            name: content_hash({"field": name, "type": "char"})
            for name in ("x_default", "x_fixed", "x_source")
        }
        protected_values = {
            "x_default": "Destination default",
            "x_fixed": "Private fixed value",
        }
        decisions = tuple(
            sorted(
                (
                    DestinationCreateFieldDecision(
                        dataset_id=product_dataset.dataset_id,
                        model="product.template",
                        model_label="Products",
                        field_name=name,
                        field_label=name,
                        field_type="char",
                        provider_kind=provider,
                        decision_kind="review",
                        reason="Test create-only provider",
                        field_contract_hash=field_hashes[name],
                        value_hash=content_hash(
                            {
                                "field": name,
                                "provider": provider,
                                "value": protected_values.get(name),
                                "source": "name" if provider == "source_field" else None,
                            }
                        ),
                        reviewed=True,
                        source_field_name=("name" if provider == "source_field" else None),
                    )
                    for name, provider in (
                        ("x_default", "odoo_default"),
                        ("x_fixed", "fixed_value"),
                        ("x_source", "source_field"),
                    )
                ),
                key=lambda item: item.key,
            )
        )
        values = tuple(
            DestinationCreateFieldEvidenceValue(
                dataset_id=product_dataset.dataset_id,
                model="product.template",
                field_name=decision.field_name,
                field_type="char",
                value=protected_values.get(decision.field_name),
                display_value=(
                    protected_values.get(decision.field_name) or "Product name"
                ),
                field_contract_hash=decision.field_contract_hash,
                value_hash=decision.value_hash,
                field_label=decision.field_name,
                provider_kind=decision.provider_kind,
                source_field_name=decision.source_field_name,
            )
            for decision in decisions
        )
        evidence = DestinationCreateFieldEvidence(
            evidence_id=str(uuid4()),
            workspace_id=self.match.workspace_id,
            source_selection_hash=self.match.source_selection_hash,
            source_schema_hash=self.match.source_schema_hash,
            destination_target_hash=self.match.destination_target_hash,
            destination_read_principal_hash=self.match.destination_read_principal_hash,
            destination_read_context_hash=self.match.destination_read_context_hash,
            destination_schema_snapshot_hash=self.match.destination_schema_snapshot_hash,
            values=values,
            recorded_at=self.now,
        )
        match = replace(
            self.match,
            create_field_decisions=decisions,
            create_field_evidence_id=evidence.evidence_id,
            create_field_evidence_hash=evidence.content_hash,
            create_field_evidence=evidence,
        )
        matched = replace(self.ordered_workspace, destination_match_plan=match)
        order = TransferOrderService().build(matched, match, recorded_by="Data manager")
        ordered = replace(matched, transfer_order_plan=order)
        package = TransferReviewService().build(
            ordered,
            match,
            order,
            run_id=str(uuid4()),
            data_version_id=self.selection.data_version_id,
            built_by=LOCAL_ACTOR.identity,
        )
        approval = TransferReviewApproval.approve(
            package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=self.now,
        )
        approved = replace(
            ordered,
            transfer_review_package=package,
            transfer_review_approval=approval,
        )
        report = TransferPreflightService().build(
            approved,
            package,
            approval,
            match,
            match,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.match, self.package, self.report = match, package, report
        self.workspace = replace(approved, transfer_preflight_report=report)

        snapshot = self._compile(self.records)
        product_rows = [
            row for row in snapshot.rows if row.target_model == "product.template"
        ]
        existing = next(row for row in product_rows if row.disposition == "UPDATE")
        created = next(row for row in product_rows if row.disposition == "CREATE")
        self.assertFalse(
            {"x_default", "x_fixed", "x_source"}
            & {intent.field for intent in existing.fields}
        )
        by_field = {intent.field: intent for intent in created.fields}
        self.assertEqual(by_field["x_default"].action, "EXPECT_PROTECTED")
        self.assertEqual(by_field["x_fixed"].action, "SET_PROTECTED")
        self.assertEqual(by_field["x_source"].value, "Product 2")
        self.assertNotIn("Private fixed value", snapshot.to_json())
        self.assertEqual(type(snapshot).from_json(snapshot.to_json()), snapshot)

    def test_all_reused_records_compile_without_write_rows(self) -> None:
        product, uom = self.selection.datasets
        captured = []

        def full_reader(*args):
            metadata, records = self.reader(*args)
            records = replace(
                records,
                records={
                    "product.template": records.records["product.template"] + (
                        TargetRecord(
                            "product.template", 42, {"default_code": "P002"}
                        ),
                    ),
                    "uom.uom": records.records["uom.uom"] + (
                        TargetRecord("uom.uom", 8, {"name": "Kilogram"}),
                    ),
                },
            )
            captured.append(records)
            return metadata, records

        match = DestinationMatchingService(self.source_values).check(
            self.workspace,
            self.selection,
            self.schema,
            (
                DestinationMatchKeyChoice(product.dataset_id, "product-code"),
                DestinationMatchKeyChoice(uom.dataset_id, "uom-name"),
            ),
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.workspace),
            reader=full_reader,
            recorded_by="Data manager",
            source_origins=self.origins,
        )
        matched = replace(self.workspace, destination_match_plan=match)
        order = TransferOrderService().build(
            matched, match, recorded_by="Data manager"
        )
        ordered = replace(matched, transfer_order_plan=order)
        package = TransferReviewService().build(
            ordered, match, order,
            run_id=str(uuid4()),
            data_version_id=self.selection.data_version_id,
            built_by=LOCAL_ACTOR.identity,
            model_policies={
                "product.template": "reuse_only",
                "uom.uom": "reuse_only",
            },
        )
        approval = TransferReviewApproval.approve(
            package,
            approval_id=str(uuid4()),
            actor=LOCAL_ACTOR,
            approved_at=datetime.now(UTC),
        )
        approved = replace(
            ordered,
            transfer_review_package=package,
            transfer_review_approval=approval,
        )
        report = TransferPreflightService().build(
            approved, package, approval, match, match,
            recorded_by=LOCAL_ACTOR.identity,
        )
        self.workspace = replace(approved, transfer_preflight_report=report)
        self.package = package
        self.report = report
        self.match = match

        snapshot = self._compile(captured[0])

        self.assertEqual(snapshot.write_count, 0)
        self.assertEqual(snapshot.counts["UNCHANGED"], 4)
        self.assertEqual(snapshot.relationship_plan.components, ())
        self.assertTrue(all(not row.fields for row in snapshot.rows))

    def _compile(self, records):
        return compile_transfer_execution_snapshot(
            self.workspace,
            self.selection,
            self.schema,
            self.package,
            self.report,
            self.match,
            records,
            source_rows=self.rows,
            source_origins=self.origins,
            source_snapshots=self.snapshots,
            source_manifest_hashes=self.manifests,
        )


if __name__ == "__main__":
    unittest.main()
