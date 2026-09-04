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
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import BusinessReference, LogicalReference, TargetRecord
from impodo.domain.workspace.errors import WorkspaceError
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
