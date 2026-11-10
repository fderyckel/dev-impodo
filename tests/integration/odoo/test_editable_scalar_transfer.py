"""Verify editable scalar values through capture and exact transfer review."""

import unittest
from collections import Counter
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo_source_capture import Json2OdooSourceCapture
from impodo.application.destination_matching_service import (
    DestinationMatchingService,
    DestinationMatchKeyChoice,
)
from impodo.application.transfer_execution_service import (
    compile_transfer_execution_snapshot,
)
from impodo.application.transfer_order_service import TransferOrderService
from impodo.application.transfer_preflight_service import TransferPreflightService
from impodo.application.transfer_review_service import TransferReviewService
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.odoo_capture import OdooCaptureFilterPolicy, OdooCaptureSelection
from impodo.domain.odoo_provenance import OdooOriginBatch
from impodo.domain.odoo_source_capture import (
    OdooSourceCaptureConfigurationError,
    plan_odoo_source_capture,
)
from impodo.domain.odoo_source_policy import (
    ODOO_SOURCE_POLICIES,
    ODOO_SOURCE_POLICY_HASHES,
)
from impodo.domain.preparation.source import SourceRow
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    ProtectedOdooReadContext,
    TargetFingerprint,
    TargetRecord,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_review import TransferReviewApproval
from tests.application.workspace.test_destination_matching import (
    BINDING_HASH,
    HASH,
    PERMISSION_HASH,
    PRINCIPAL_HASH,
    _SourceValues,
    _workspace,
)
from tests.integration.odoo.test_source_capture import DatasetTransport


def _field(name, *, stored=True, kind="char", computed=None, inverse=None):
    return SchemaField(
        name,
        name,
        kind,
        True,
        False,
        None,
        None,
        (),
        stored=stored,
        computed=computed,
        has_inverse=inverse,
        related=False,
        company_dependent=False,
        searchable=True,
        exportable=True,
    )


class _TransferScenario:
    def __init__(
        self, major, *, known_inverse=False, model="account.account", key="code"
    ):
        self.now = datetime.now(UTC)
        self.model, self.key = model, key
        self.context = ProtectedOdooReadContext(7, (7,))
        self.workspace = _workspace(self.now)
        self.company = OdooCompanySelection(
            self.workspace.destination_verified_target_hash, 91, "Destination company"
        )
        self.workspace = replace(
            self.workspace,
            destination_verified_odoo_version=f"{major}.0",
            destination_company=self.company,
        )
        projection = _field(
            key,
            stored=False,
            computed=True if known_inverse else None,
            inverse=True if known_inverse else None,
        )
        self.fields = (
            projection,
            _field("name"),
            _field("kind", kind="selection", computed=True, inverse=False),
        )
        self.schema = OdooSchemaCatalog(
            workspace_id=self.workspace.workspace_id,
            policy_hash=ODOO_SOURCE_POLICY_HASHES[major],
            captured_at=self.now,
            captured_by="Manager",
            connection_mode="REMOTE",
            database="source",
            odoo_version=f"{major}.0",
            models=(
                SchemaModel(
                    model,
                    "Business record",
                    self.fields
                    + (replace(_field("write_date", kind="datetime"), readonly=True),),
                ),
            ),
            content_hash=HASH,
            origin=SchemaOrigin.LIVE_API,
            read_credential_binding_hash=HASH,
            read_principal_hash=HASH,
            read_permission_hash=HASH,
            read_context_hash=HASH,
            connection_target_hash=HASH,
        )
        self.capture = OdooCaptureSelection.create(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=str(uuid4()),
            dataset_name="business_records",
            model=model,
            field_names=tuple(sorted(field.name for field in self.fields)),
            filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
            max_rows=100,
            policy_hash=self.schema.policy_hash,
            connection_target_hash=HASH,
            schema_scope_hash=HASH,
            read_principal_hash=HASH,
            read_permission_hash=HASH,
            context_hash=HASH,
            created_at=self.now,
            created_by="Manager",
        )
        self.dataset = SourceDataset(
            dataset_id=str(uuid4()),
            name="business_records",
            source=self.capture.source_binding,
            row_count=2,
            columns=tuple(
                SourceDatasetColumn(index, field.name, field.name, "TEXT")
                for index, field in enumerate(self.fields, 1)
            ),
        )
        self.selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            data_version_id=self.capture.data_version_id,
            created_at=self.now,
            created_by="Manager",
            datasets=(self.dataset,),
            content_hash=HASH,
        )
        self.identity = OdooReadIdentity(
            target_hash=self.workspace.destination_verified_target_hash,
            principal_hash=PRINCIPAL_HASH,
            permission_hash=PERMISSION_HASH,
            context_hash=self.company.context_hash,
            readable_models=(model,),
            observed_at=self.now.isoformat(),
        )
        fingerprint = TargetFingerprint(
            target_hash=self.workspace.destination_verified_target_hash,
            connection_mode="REMOTE",
            database="destination",
            odoo_version=f"{major}.0",
            snapshot_timestamp=self.now.isoformat(),
        )
        metadata_names = {field.name for field in fields(FieldMetadata)}
        target_fields = {
            field.name: FieldMetadata(
                **{
                    name: value
                    for name, value in asdict(field).items()
                    if name in metadata_names
                }
            )
            for field in self.fields
        }
        target_fields["kind"] = replace(
            target_fields["kind"], selection=(("asset", "Asset"),)
        )
        self.metadata = MetadataSnapshot(
            fingerprint,
            {model: ModelMetadata(model, description=None, fields=target_fields)},
        )
        self.records = RecordSnapshot(
            fingerprint,
            {model: (TargetRecord(model, 900, {key: "001001"}),)},
            {model: (key,)},
        )
        self.requests = []

    def reader(self, destination, _api_key, metadata_requests, record_requests):
        self.requests.append((destination, metadata_requests, record_requests))
        return self.metadata, self.records

    def match(self, values=("001001", "001002"), *, workspace=None, identity=None):
        counts = Counter(value for value in values if value)
        source = _SourceValues(
            {
                (self.dataset.dataset_id, self.key): tuple(
                    {"value": value, "count": count} for value, count in counts.items()
                )
            }
        )
        return DestinationMatchingService(source).check(
            workspace or self.workspace,
            self.selection,
            self.schema,
            (DestinationMatchKeyChoice(self.dataset.dataset_id, self.key),),
            api_key="destination-secret",
            credential_binding_hash=BINDING_HASH,
            read_identity=identity or self.identity,
            reader=self.reader,
            recorded_by="Manager",
        )

    def review_and_compile(self, match):
        matched = replace(self.workspace, destination_match_plan=match)
        order = TransferOrderService().build(matched, match, recorded_by="Manager")
        ordered = replace(matched, transfer_order_plan=order)
        package = TransferReviewService().build(
            ordered,
            match,
            order,
            run_id=str(uuid4()),
            data_version_id=self.selection.data_version_id,
            built_by=LOCAL_ACTOR.identity,
            model_policies={self.model: "create_if_missing"},
        )
        approval = TransferReviewApproval.approve(
            package, approval_id=str(uuid4()), actor=LOCAL_ACTOR, approved_at=self.now
        )
        approved = replace(
            ordered, transfer_review_package=package, transfer_review_approval=approval
        )
        fresh = self.match()
        report = TransferPreflightService().build(
            approved, package, approval, match, fresh, recorded_by=LOCAL_ACTOR.identity
        )
        rows = (
            SourceRow(1, {self.key: "001001", "name": "Existing", "kind": "asset"}),
            SourceRow(2, {self.key: "001002", "name": "New", "kind": "asset"}),
        )
        snapshot = compile_transfer_execution_snapshot(
            replace(approved, transfer_preflight_report=report),
            self.selection,
            self.schema,
            package,
            report,
            fresh,
            self.records,
            source_rows={self.dataset.dataset_id: rows},
            source_origins={
                self.dataset.dataset_id: (
                    OdooOriginBatch(1, (101, 102), (self.now, self.now)),
                )
            },
            source_snapshots={
                self.dataset.dataset_id: SimpleNamespace(content_hash=HASH)
            },
            source_manifest_hashes={self.dataset.dataset_id: HASH},
        )
        return package, report, snapshot


class EditableScalarTransferTests(unittest.TestCase):
    def test_both_majors_keep_projection_in_exact_review_and_load_input(self):
        for major in (19, 20):
            for known_inverse in (False, True):
                with self.subTest(major=major, known_inverse=known_inverse):
                    scenario = _TransferScenario(major, known_inverse=known_inverse)
                    match = scenario.match()
                    self.assertTrue(match.ready)
                    self.assertEqual(
                        match.model_matches[0].compatible_fields,
                        ("code", "kind", "name"),
                    )
                    package, report, snapshot = scenario.review_and_compile(match)
                    self.assertTrue(report.ready)
                    self.assertEqual(
                        package.datasets[0].scalar_write_fields,
                        ("code", "kind", "name"),
                    )
                    self.assertEqual(snapshot.counts["CREATE"], 1)
                    self.assertEqual(snapshot.counts["UNCHANGED"], 1)
                    created = next(row for row in snapshot.rows if row.source_row == 2)
                    self.assertEqual(
                        {field.field: field.value for field in created.fields},
                        {
                            "code": "001002",
                            "kind": "asset",
                            "name": "New",
                        },
                    )
                    destination, _, requests = scenario.requests[0]
                    self.assertEqual(
                        destination.odoo_operation_context,
                        {"allowed_company_ids": [91]},
                    )
                    self.assertEqual(
                        requests[0].domain, (("code", "in", ("001001", "001002")),)
                    )
                    self.assertEqual(requests[0].fields, ("code",))

    def test_an_unrelated_model_uses_the_same_transfer_capability(self):
        scenario = _TransferScenario(20, model="x.equipment", key="tracking_reference")
        package, report, snapshot = scenario.review_and_compile(scenario.match())
        self.assertTrue(report.ready)
        self.assertIn("tracking_reference", package.datasets[0].scalar_write_fields)
        self.assertEqual(
            next(
                field.value
                for row in snapshot.rows
                for field in row.fields
                if field.field == "tracking_reference"
            ),
            "001002",
        )

    def test_blank_and_repeated_projection_keys_remain_unresolved(self):
        for values, reason in (
            (("001001", None), "SOURCE_KEY_BLANK"),
            (("001001", "001001"), "SOURCE_KEY_DUPLICATE"),
        ):
            with self.subTest(reason=reason):
                result = _TransferScenario(20).match(values)
                self.assertFalse(result.ready)
                self.assertIn(reason, result.model_matches[0].blocking_reasons)

    def test_projection_requires_explicit_matching_company_context(self):
        scenario = _TransferScenario(20)
        with self.assertRaisesRegex(
            WorkspaceError, "Choose the destination Odoo company"
        ):
            scenario.match(
                workspace=replace(scenario.workspace, destination_company=None)
            )
        with self.assertRaisesRegex(WorkspaceError, "company context changed"):
            scenario.match(identity=replace(scenario.identity, context_hash=HASH))
        self.assertFalse(scenario.requests)

    def test_target_projection_must_still_advertise_editable_capabilities(self):
        for changes in ({"readonly": True}, {"has_inverse": False}):
            with self.subTest(changes=changes):
                scenario = _TransferScenario(20)
                model = scenario.metadata.models[scenario.model]
                fields = dict(model.fields)
                fields["code"] = replace(fields["code"], **changes)
                scenario.metadata = replace(
                    scenario.metadata,
                    models={scenario.model: replace(model, fields=fields)},
                )
                with self.assertRaisesRegex(
                    WorkspaceError, "identity field.*not writable"
                ):
                    scenario.match()

    def test_capture_preserves_projection_strings_in_selected_company(self):
        for major in (19, 20):
            for known_inverse in (False, True):
                with self.subTest(major=major, known_inverse=known_inverse):
                    scenario = _TransferScenario(major, known_inverse=known_inverse)
                    request = plan_odoo_source_capture(
                        scenario.capture, scenario.schema
                    )
                    transport = DatasetTransport(
                        [
                            {
                                "id": 101,
                                "write_date": "2026-11-09 10:00:00",
                                "code": "001001",
                                "kind": "asset",
                                "name": "Existing",
                            },
                            {
                                "id": 102,
                                "write_date": "2026-11-09 10:00:00",
                                "code": "001002",
                                "kind": "asset",
                                "name": "New",
                            },
                        ]
                    )
                    adapter = Json2OdooSourceCapture(
                        Json2Config(
                            base_url="https://source.example.test",
                            database="source",
                            api_key="source-secret",
                            retries=0,
                        ),
                        transport=transport,
                    )
                    pages = list(
                        adapter.open_capture(request, scenario.context).pages()
                    )
                    code_column = next(
                        column
                        for column in pages[0].columns
                        if column.field_name == "code"
                    )
                    self.assertEqual(code_column.values, ("001001", "001002"))
                    self.assertTrue(
                        all(
                            call["context"]["allowed_company_ids"] == [7]
                            for call in transport.calls
                        )
                    )
                    self.assertNotIn("source-secret", scenario.capture.to_json())

    def test_capture_rejects_changed_context_and_known_managed_projection(self):
        scenario = _TransferScenario(20)
        with self.assertRaisesRegex(
            OdooSourceCaptureConfigurationError, "schema evidence"
        ):
            plan_odoo_source_capture(
                scenario.capture,
                replace(scenario.schema, read_context_hash=BINDING_HASH),
            )
        for changes in ({"readonly": True}, {"computed": True, "has_inverse": False}):
            with self.subTest(changes=changes):
                fields = (
                    replace(scenario.fields[0], **changes),
                    *scenario.schema.models[0].fields[1:],
                )
                schema = replace(
                    scenario.schema,
                    models=(replace(scenario.schema.models[0], fields=fields),),
                )
                with self.assertRaisesRegex(
                    OdooSourceCaptureConfigurationError, "not eligible"
                ):
                    plan_odoo_source_capture(scenario.capture, schema)

    def test_previous_policy_stays_readable_but_cannot_admit_new_projection_capture(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                scenario = _TransferScenario(major)
                previous_hash = content_hash(
                    replace(ODOO_SOURCE_POLICIES[major], contract_version=4).to_dict()
                )
                previous = replace(
                    scenario.capture,
                    policy_hash=previous_hash,
                    content_hash="",
                    _calculate_content_hash=True,
                )
                restored = OdooCaptureSelection.from_json(previous.to_json())
                self.assertEqual(restored.policy_hash, previous_hash)
                with self.assertRaises(OdooSourceCaptureConfigurationError):
                    plan_odoo_source_capture(
                        restored, replace(scenario.schema, policy_hash=previous_hash)
                    )


if __name__ == "__main__":
    unittest.main()
