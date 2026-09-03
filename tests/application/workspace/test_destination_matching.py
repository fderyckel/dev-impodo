from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import unittest
from uuid import uuid4

from impodo.application.destination_matching_service import (
    DestinationMatchKeyChoice,
    DestinationMatchingService,
)
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.odoo_provenance import (
    OdooOriginBatch,
    OdooRelationshipOriginColumn,
)
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    TargetFingerprint,
    TargetRecord,
    target_identity_hash,
)
from impodo.domain.source_binding import OdooSourceBinding
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    SourceMode,
    WorkspaceState,
    WorkspaceStatus,
)


HASH = "sha256:" + "a" * 64
BINDING_HASH = "sha256:" + "b" * 64
PRINCIPAL_HASH = "sha256:" + "c" * 64
PERMISSION_HASH = "sha256:" + "d" * 64
CONTEXT_HASH = "sha256:" + "e" * 64


class _SourceValues:
    def __init__(self, values, rows=None):
        self.values = values
        self.rows = rows or {}

    def source_value_choices(self, _workspace_id, dataset_id, source_column_key):
        return self.values[(dataset_id, source_column_key)]

    def source_key_rows(self, _workspace_id, dataset_id, source_column_key):
        return self.rows[(dataset_id, source_column_key)]


class DestinationMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(UTC)
        self.workspace = _workspace(self.now)
        self.selection = _selection(self.now)
        self.schema = _source_schema(self.workspace, self.now)
        self.product = self.selection.datasets[0]
        self.uom = self.selection.datasets[1]
        self.source_values = _SourceValues(
            {
                (self.product.dataset_id, "product-code"): (
                    {"value": "P001", "count": 1},
                    {"value": "P002", "count": 1},
                ),
                (self.uom.dataset_id, "uom-name"): (
                    {"value": "Kilogram", "count": 1},
                    {"value": "Unit", "count": 1},
                ),
            }
        )

    def test_product_and_uom_matches_are_bounded_and_portable(self) -> None:
        service = DestinationMatchingService(self.source_values)

        plan = service.check(
            self.workspace,
            self.selection,
            self.schema,
            (
                DestinationMatchKeyChoice(self.product.dataset_id, "product-code"),
                DestinationMatchKeyChoice(self.uom.dataset_id, "uom-name"),
            ),
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.workspace),
            reader=_destination_reader(self.workspace),
            recorded_by="Data manager",
        )

        self.assertTrue(plan.ready)
        by_model = {item.model: item for item in plan.model_matches}
        self.assertEqual(by_model["product.template"].destination_existing_key_count, 1)
        self.assertEqual(by_model["product.template"].destination_create_key_count, 1)
        self.assertEqual(by_model["uom.uom"].destination_existing_key_count, 1)
        self.assertEqual(by_model["uom.uom"].destination_create_key_count, 1)
        self.assertEqual(by_model["product.template"].compatible_fields, ("default_code", "name"))
        self.assertEqual(DestinationMatchPlan.from_json(plan.to_json()), plan)
        self.assertNotIn("P001", plan.to_json())
        self.assertNotIn("Kilogram", plan.to_json())
        matched_workspace = replace(self.workspace, destination_match_plan=plan)
        self.assertTrue(
            matched_workspace.destination_match_ready(
                source_selection_hash=self.selection.content_hash,
                source_schema_hash=self.schema.content_hash,
            )
        )
        self.assertFalse(
            replace(
                matched_workspace,
                destination_verified_credential_binding_hash=(
                    "sha256:" + "f" * 64
                ),
            ).destination_match_current(
                source_selection_hash=self.selection.content_hash,
                source_schema_hash=self.schema.content_hash,
            )
        )
        legacy = replace(plan, contract_version=1, relationship_matches=())
        self.assertFalse(legacy.ready)
        self.assertEqual(
            DestinationMatchPlan.from_json(legacy.to_json()).to_json(),
            legacy.to_json(),
        )

    def test_duplicate_source_keys_block_the_plan(self) -> None:
        self.source_values.values[(self.product.dataset_id, "product-code")] = (
            {"value": "P001", "count": 2},
        )
        service = DestinationMatchingService(self.source_values)

        plan = service.check(
            self.workspace,
            self.selection,
            self.schema,
            (
                DestinationMatchKeyChoice(self.product.dataset_id, "product-code"),
                DestinationMatchKeyChoice(self.uom.dataset_id, "uom-name"),
            ),
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.workspace),
            reader=_destination_reader(self.workspace),
            recorded_by="Data manager",
        )

        product = next(
            item for item in plan.model_matches if item.model == "product.template"
        )
        self.assertFalse(plan.ready)
        self.assertEqual(product.source_duplicate_key_count, 1)
        self.assertEqual(product.source_blank_row_count, 0)
        self.assertIn("SOURCE_KEY_DUPLICATE", product.blocking_reasons)

    def test_equal_counts_still_bind_each_key_to_its_exact_destination_record(self) -> None:
        service = DestinationMatchingService(self.source_values)
        choices = (
            DestinationMatchKeyChoice(self.product.dataset_id, "product-code"),
            DestinationMatchKeyChoice(self.uom.dataset_id, "uom-name"),
        )
        base_reader = _destination_reader(self.workspace)
        first = service.check(
            self.workspace,
            self.selection,
            self.schema,
            choices,
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.workspace),
            reader=base_reader,
            recorded_by="Data manager",
        )

        def swapped_reader(*args):
            metadata, records = base_reader(*args)
            return metadata, replace(
                records,
                records={
                    **records.records,
                    "product.template": (
                        TargetRecord(
                            "product.template",
                            52,
                            {"default_code": "P002"},
                        ),
                    ),
                },
            )

        second = service.check(
            self.workspace,
            self.selection,
            self.schema,
            choices,
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.workspace),
            reader=swapped_reader,
            recorded_by="Data manager",
        )

        first_product = next(
            item for item in first.model_matches if item.model == "product.template"
        )
        second_product = next(
            item for item in second.model_matches if item.model == "product.template"
        )
        self.assertEqual(
            (
                first_product.destination_existing_key_count,
                first_product.destination_create_key_count,
            ),
            (
                second_product.destination_existing_key_count,
                second_product.destination_create_key_count,
            ),
        )
        self.assertNotEqual(
            first_product.destination_key_binding_hash,
            second_product.destination_key_binding_hash,
        )

    def test_relations_resolve_generically_and_one2many_uses_its_inverse(self) -> None:
        product_model, uom_model = self.schema.models
        many2one = _relation(
            "uom_id",
            "Unit of Measure",
            "many2one",
            "uom.uom",
            required=True,
        )
        many2many = _relation(
            "alternate_uom_ids",
            "Alternate Units",
            "many2many",
            "uom.uom",
        )
        inverse_one2many = _relation(
            "product_ids",
            "Products",
            "one2many",
            "product.template",
            relation_field="uom_id",
        )
        schema = replace(
            self.schema,
            models=(
                replace(
                    product_model,
                    fields=product_model.fields + (many2many, many2one),
                ),
                replace(
                    uom_model,
                    fields=uom_model.fields + (inverse_one2many,),
                ),
            ),
        )
        source_values = _SourceValues(
            self.source_values.values,
            {
                (self.product.dataset_id, "product-code"): ("P001", "P002"),
                (self.uom.dataset_id, "uom-name"): ("Unit", "Kilogram"),
            },
        )
        origins = {
            self.product.dataset_id: (
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
            self.uom.dataset_id: (
                OdooOriginBatch(
                    first_row_ordinal=1,
                    odoo_ids=(7, 8),
                    write_dates=(self.now, self.now),
                ),
            ),
        }

        plan = DestinationMatchingService(source_values).check(
            self.workspace,
            self.selection,
            schema,
            (
                DestinationMatchKeyChoice(self.product.dataset_id, "product-code"),
                DestinationMatchKeyChoice(self.uom.dataset_id, "uom-name"),
            ),
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=_identity(self.workspace),
            reader=_destination_reader(self.workspace, with_relationships=True),
            recorded_by="Data manager",
            source_origins=origins,
        )

        self.assertTrue(plan.ready)
        by_field = {item.field_name: item for item in plan.relationship_matches}
        self.assertEqual(set(by_field), {"alternate_uom_ids", "uom_id"})
        self.assertEqual(by_field["uom_id"].kind, "many2one")
        self.assertEqual(by_field["uom_id"].operation, "set")
        self.assertEqual(by_field["uom_id"].inverse_field, "product_ids")
        self.assertEqual(by_field["uom_id"].destination_reused_link_count, 1)
        self.assertEqual(by_field["uom_id"].incoming_link_count, 1)
        self.assertEqual(by_field["alternate_uom_ids"].kind, "many2many")
        self.assertEqual(by_field["alternate_uom_ids"].operation, "replace")
        self.assertEqual(by_field["alternate_uom_ids"].source_link_count, 3)
        self.assertNotIn("odoo_ids", plan.to_json())
        self.assertNotIn("Kilogram", plan.to_json())
        self.assertEqual(DestinationMatchPlan.from_json(plan.to_json()), plan)


def _workspace(now: datetime) -> WorkspaceState:
    destination_hash = target_identity_hash(
        connection_mode="REMOTE",
        base_url="https://destination.example.test",
        database="destination",
    )
    return WorkspaceState(
        workspace_id=str(uuid4()),
        name="Product and UoM transfer",
        source_system="Odoo",
        source_mode=SourceMode.ODOO,
        status=WorkspaceStatus.REGISTERED,
        odoo_connection_mode=OdooConnectionMode.REMOTE,
        odoo_base_url="https://source.example.test",
        odoo_database="source",
        destination_odoo_connection_mode=OdooConnectionMode.REMOTE,
        destination_odoo_base_url="https://destination.example.test",
        destination_odoo_database="destination",
        destination_verified_target_hash=destination_hash,
        destination_verified_credential_binding_hash=BINDING_HASH,
        destination_verified_read_principal_hash=PRINCIPAL_HASH,
        destination_verified_odoo_version="19.0",
        destination_verified_at=now,
    )


def _selection(now: datetime) -> SourceSelection:
    product = SourceDataset(
        dataset_id=str(uuid4()),
        name="Products",
        source=_binding("product.template"),
        row_count=2,
        columns=(
            SourceDatasetColumn(1, "default_code", "product-code", "TEXT"),
            SourceDatasetColumn(2, "name", "product-name", "TEXT"),
        ),
    )
    uom = SourceDataset(
        dataset_id=str(uuid4()),
        name="Units of Measure",
        source=_binding("uom.uom"),
        row_count=2,
        columns=(SourceDatasetColumn(1, "name", "uom-name", "TEXT"),),
    )
    return SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        created_at=now,
        created_by="Data manager",
        datasets=(product, uom),
        content_hash="sha256:" + "1" * 64,
    )


def _binding(model: str) -> OdooSourceBinding:
    return OdooSourceBinding(
        capture_selection_hash=HASH,
        model=model,
        policy_hash=HASH,
        connection_target_hash=HASH,
        schema_scope_hash=HASH,
        read_principal_hash=HASH,
        read_permission_hash=HASH,
        context_hash=HASH,
    )


def _source_schema(workspace: WorkspaceState, now: datetime) -> OdooSchemaCatalog:
    text = lambda name, label: SchemaField(
        name=name,
        label=label,
        type="char",
        required=False,
        readonly=False,
        relation=None,
        relation_field=None,
        selection=(),
    )
    return OdooSchemaCatalog(
        workspace_id=workspace.workspace_id,
        policy_hash=HASH,
        captured_at=now,
        captured_by="Data manager",
        connection_mode="REMOTE",
        database="source",
        odoo_version="19.0",
        models=(
            SchemaModel(
                "product.template",
                "Product",
                (text("default_code", "Internal Reference"), text("name", "Name")),
            ),
            SchemaModel("uom.uom", "Unit of Measure", (text("name", "Name"),)),
        ),
        content_hash="sha256:" + "2" * 64,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash=HASH,
        read_principal_hash=HASH,
        read_permission_hash=HASH,
        read_context_hash=HASH,
        connection_target_hash=HASH,
    )


def _relation(
    name: str,
    label: str,
    kind: str,
    relation: str,
    *,
    relation_field: str | None = None,
    required: bool = False,
) -> SchemaField:
    return SchemaField(
        name=name,
        label=label,
        type=kind,
        required=required,
        readonly=False,
        relation=relation,
        relation_field=relation_field,
        selection=(),
        related=False,
        company_dependent=False,
        exportable=True,
    )


def _identity(workspace: WorkspaceState) -> OdooReadIdentity:
    return OdooReadIdentity(
        target_hash=workspace.destination_verified_target_hash,
        principal_hash=PRINCIPAL_HASH,
        permission_hash=PERMISSION_HASH,
        context_hash=CONTEXT_HASH,
        readable_models=("product.template", "uom.uom"),
        observed_at=datetime.now(UTC).isoformat(),
    )


def _destination_reader(workspace: WorkspaceState, *, with_relationships=False):
    fingerprint = TargetFingerprint(
        target_hash=workspace.destination_verified_target_hash,
        connection_mode="REMOTE",
        database="destination",
        odoo_version="19.0",
        snapshot_timestamp=datetime.now(UTC).isoformat(),
    )

    def reader(_destination, _api_key, metadata_requests, record_requests):
        self_fields = {
            "default_code": FieldMetadata("default_code", "char", "Internal Reference"),
            "name": FieldMetadata("name", "char", "Name"),
        }
        if with_relationships:
            self_fields.update(
                {
                    "alternate_uom_ids": FieldMetadata(
                        "alternate_uom_ids",
                        "many2many",
                        "Alternate Units",
                        relation="uom.uom",
                    ),
                    "uom_id": FieldMetadata(
                        "uom_id",
                        "many2one",
                        "Unit of Measure",
                        required=True,
                        relation="uom.uom",
                    ),
                }
            )
        metadata = MetadataSnapshot(
            fingerprint=fingerprint,
            models={
                "product.template": ModelMetadata(
                    "product.template",
                    "Product",
                    self_fields,
                ),
                "uom.uom": ModelMetadata(
                    "uom.uom",
                    "Unit of Measure",
                    {"name": self_fields["name"]},
                ),
            },
        )
        records = RecordSnapshot(
            fingerprint=fingerprint,
            records={
                "product.template": (
                    TargetRecord("product.template", 41, {"default_code": "P001"}),
                ),
                "uom.uom": (TargetRecord("uom.uom", 7, {"name": "Unit"}),),
            },
            requested_fields={item.model: item.fields for item in record_requests},
        )
        self_models = tuple(item.model for item in metadata_requests)
        if self_models != ("product.template", "uom.uom"):
            raise AssertionError(self_models)
        return metadata, records

    return reader


if __name__ == "__main__":
    unittest.main()
