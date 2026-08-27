from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import inspect
import json
import math
import unittest

from impodo.adapters.odoo_source_capture import Json2OdooSourceCapture
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.application.odoo_source_capture_service import OdooSourceCaptureService
from impodo.adapters.odoo.connectors import Json2Config
from impodo.domain.odoo.contracts import MetadataSnapshot
from impodo.domain.odoo_capture import (
    OdooCaptureConsistency,
    OdooCaptureFilterClause,
    OdooCaptureFilterOperator,
    OdooCaptureFilterPolicy,
    OdooCaptureSelection,
)
from impodo.domain.odoo_source_capture import (
    OdooCaptureAccounting,
    OdooCaptureFieldProjection,
    OdooSourceCaptureCancelled,
    OdooSourceCaptureConfigurationError,
    OdooSourceCaptureConsistencyError,
    OdooSourceCaptureLimitError,
    OdooSourceCaptureRequest,
    plan_odoo_source_capture,
)
from impodo.domain.odoo_source_policy import (
    CURRENT_ODOO_SOURCE_POLICY,
    ODOO_SOURCE_POLICY_HASH,
    TargetInstanceAssurance,
)
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    OdooReadIdentity,
    ProtectedOdooReadContext,
    TargetFingerprint,
)
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    OdooConnectionMode,
    WorkspaceStatus,
    SourceMode,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from tests.support.workspace_access import data_version_id, workspace_access_service


HASH = "sha256:" + "1" * 64


class DatasetTransport:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, object]] = []
        self.mutate_after_high_water = None
        self.override_page = None

    def __call__(self, url, headers, body, timeout, method, maximum_bytes):
        payload = json.loads(body)
        self.calls.append(payload)
        if payload["order"] == "id desc":
            matching = self._matching(payload["domain"])
            response = [{"id": max(row["id"] for row in matching)}] if matching else []
            if self.mutate_after_high_water is not None:
                self.mutate_after_high_water(self.rows)
            return 200, json.dumps(response, separators=(",", ":")).encode()
        matching = self._matching(payload["domain"])
        response = matching[: payload["limit"]]
        fields = payload["fields"]
        projected = [{name: row[name] for name in fields} for row in response]
        if self.override_page is not None:
            projected = self.override_page(projected, len(self.calls))
        return 200, json.dumps(projected, separators=(",", ":")).encode()

    def _matching(self, domain):
        result = list(self.rows)
        for field, operator, operand in domain:
            if operator == "=":
                result = [row for row in result if row[field] == operand]
            elif operator == "in":
                result = [row for row in result if row[field] in operand]
            elif operator == ">":
                result = [row for row in result if row[field] > operand]
            elif operator == ">=":
                result = [row for row in result if row[field] >= operand]
            elif operator == "<=":
                result = [row for row in result if row[field] <= operand]
            elif operator == "<":
                result = [row for row in result if row[field] < operand]
            else:
                raise AssertionError(operator)
        return sorted(result, key=lambda row: row["id"])


class OdooSourceCaptureAdapterTests(unittest.TestCase):
    def test_page_boundaries_and_calls_scale_by_page(self) -> None:
        for count in (0, 1, 499, 500, 501):
            with self.subTest(count=count):
                transport = DatasetTransport(_rows(count))
                session = self._adapter(transport).open_capture(
                    _request(maximum_rows=1_000),
                    _context(),
                )
                pages = list(session.pages())

                self.assertEqual(sum(page.row_count for page in pages), count)
                self.assertEqual(
                    session.accounting.record_request_count,
                    1 + math.ceil(count / 500),
                )
                self.assertEqual(
                    [page.first_row_ordinal for page in pages],
                    ([1, 501] if count == 501 else ([1] if count else [])),
                )
                self.assertTrue(all(len(page.odoo_ids) <= 500 for page in pages))

    def test_maximum_plus_one_fails_closed(self) -> None:
        session = self._adapter(DatasetTransport(_rows(501))).open_capture(
            _request(maximum_rows=500),
            _context(),
        )

        with self.assertRaisesRegex(OdooSourceCaptureLimitError, "row limit"):
            list(session.pages())

    def test_high_water_excludes_later_insert_and_keyset_survives_delete(self) -> None:
        transport = DatasetTransport(_rows(501))

        def mutate(rows):
            rows.append(_row(900))
            rows[:] = [row for row in rows if row["id"] != 250]

        transport.mutate_after_high_water = mutate
        session = self._adapter(transport).open_capture(
            _request(maximum_rows=1_000),
            _context(),
        )
        pages = list(session.pages())
        ids = [identifier for page in pages for identifier in page.odoo_ids]

        self.assertNotIn(250, ids)
        self.assertNotIn(900, ids)
        self.assertEqual(ids[-1], 501)
        self.assertEqual(session.accounting.row_count, 500)
        self.assertIn("deletes", session.accounting.consistency_limitation)

    def test_projection_order_and_bounds_are_strict(self) -> None:
        cases = {
            "duplicate": lambda rows, call: [rows[0], rows[0]],
            "reordered": lambda rows, call: list(reversed(rows)),
            "extra": lambda rows, call: [{**rows[0], "secret": "x"}],
            "missing": lambda rows, call: [
                {key: value for key, value in rows[0].items() if key != "name"}
            ],
            "out-of-range": lambda rows, call: [{**rows[0], "id": 3}],
        }
        for label, override in cases.items():
            with self.subTest(label=label):
                transport = DatasetTransport(_rows(2))
                transport.override_page = override
                session = self._adapter(transport).open_capture(
                    _request(maximum_rows=10),
                    _context(),
                )
                with self.assertRaises(OdooSourceCaptureConsistencyError):
                    list(session.pages())

    def test_malformed_and_oversized_transport_responses_are_rejected(self) -> None:
        def malformed(url, headers, body, timeout, method, maximum_bytes):
            return 200, b"{"

        with self.assertRaisesRegex(OdooSourceCaptureConsistencyError, "malformed"):
            self._adapter(malformed).open_capture(_request(), _context())

        def oversized(url, headers, body, timeout, method, maximum_bytes):
            return 200, b"x" * (maximum_bytes + 1)

        with self.assertRaisesRegex(OdooSourceCaptureLimitError, "response"):
            self._adapter(oversized).open_capture(
                _request(max_response_bytes=100),
                _context(),
            )

    def test_value_row_and_snapshot_limits_apply_during_page_adaptation(self) -> None:
        transport = DatasetTransport([{**_row(1), "name": "four"}])
        session = self._adapter(transport).open_capture(
            _request(max_value_bytes=3),
            _context(),
        )
        with self.assertRaisesRegex(OdooSourceCaptureLimitError, "value"):
            list(session.pages())

        session = self._adapter(DatasetTransport(_rows(2))).open_capture(
            _request(max_snapshot_bytes=10),
            _context(),
        )
        with self.assertRaisesRegex(OdooSourceCaptureLimitError, "snapshot"):
            list(session.pages())

    def test_filters_and_context_are_service_shaped(self) -> None:
        transport = DatasetTransport(_rows(3))
        request = replace(
            _request(),
            filter_policy=OdooCaptureFilterPolicy.ACTIVE_AND_ARCHIVED_RECORDS,
            filter_clauses=(
                OdooCaptureFilterClause(
                    "name",
                    OdooCaptureFilterOperator.IN_SET,
                    ("Name 1", "Name 3"),
                ),
            ),
        )
        session = self._adapter(transport).open_capture(request, _context())
        pages = list(session.pages())

        self.assertEqual(pages[0].odoo_ids, (1, 3))
        self.assertFalse(transport.calls[0]["context"]["active_test"])
        self.assertEqual(
            set(transport.calls[0]["context"]),
            {"active_test", "allowed_company_ids", "lang", "tz"},
        )
        self.assertNotIn("offset", transport.calls[-1])

    def test_sample_is_one_non_authoritative_call_and_type_decoding_is_exact(
        self,
    ) -> None:
        transport = DatasetTransport(_rows(3))
        sample = self._adapter(transport).sample(
            _request(),
            _context(),
            limit=2,
        )

        self.assertTrue(sample.non_authoritative)
        self.assertEqual(sample.page.row_count, 2)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sample.page.columns[0].values, ("Name 1", "Name 2"))

    def test_type_aware_false_null_and_empty_text_are_distinct(self) -> None:
        rows = [
            {
                "id": 1,
                "write_date": False,
                "active": False,
                "name": "",
                "count": False,
            }
        ]
        request = replace(
            _request(),
            projection=(
                OdooCaptureFieldProjection("active", "boolean"),
                OdooCaptureFieldProjection("count", "integer"),
                OdooCaptureFieldProjection("name", "char"),
            ),
        )
        session = self._adapter(DatasetTransport(rows)).open_capture(
            request,
            _context(),
        )
        page = list(session.pages())[0]

        self.assertEqual(page.write_dates, (None,))
        self.assertEqual(page.columns[0].values, (False,))
        self.assertEqual(page.columns[1].values, (None,))
        self.assertEqual(page.columns[2].values, ("",))

    def test_http_acl_and_timeout_fail_without_response_body(self) -> None:
        def denied(url, headers, body, timeout, method, maximum_bytes):
            return 403, b"sensitive business value"

        with self.assertRaisesRegex(
            OdooSourceCaptureConsistencyError,
            "authorization failed",
        ) as denied_error:
            self._adapter(denied).open_capture(_request(), _context())
        self.assertNotIn("sensitive", str(denied_error.exception))

        def timed_out(url, headers, body, timeout, method, maximum_bytes):
            raise TimeoutError("internal host detail")

        with self.assertRaisesRegex(
            OdooSourceCaptureConsistencyError,
            "timed out or was unreachable",
        ) as timeout_error:
            self._adapter(timed_out).open_capture(_request(), _context())
        self.assertNotIn("internal host", str(timeout_error.exception))

    def test_capture_surface_exposes_no_raw_domain_method_or_context(self) -> None:
        request_fields = set(OdooSourceCaptureRequest.__dataclass_fields__)
        public = {
            name
            for name, value in inspect.getmembers(Json2OdooSourceCapture)
            if callable(value) and not name.startswith("_")
        }

        self.assertFalse({"domain", "method", "context"} & request_fields)
        self.assertEqual(
            public,
            {"open_capture", "probe_identity", "probe_schema", "sample"},
        )
        with self.assertRaises(ValueError):
            OdooCaptureFilterClause(
                "company_id.name",
                OdooCaptureFilterOperator.EQUALS,
                ("x",),
            )
        with self.assertRaisesRegex(
            OdooSourceCaptureConfigurationError,
            "binding",
        ):
            replace(_request(), model="res.partner;drop")

    def test_reader_hot_path_contains_no_hashing(self) -> None:
        from impodo.adapters import odoo_source_capture

        source = inspect.getsource(odoo_source_capture)
        self.assertNotIn("hashlib", source)
        self.assertNotIn("content_hash", source)

    def test_cancellation_is_checked_between_requests(self) -> None:
        checks = 0

        def cancellation():
            nonlocal checks
            checks += 1
            return checks >= 3

        session = self._adapter(DatasetTransport(_rows(501))).open_capture(
            _request(),
            _context(),
            cancellation=cancellation,
        )
        with self.assertRaises(OdooSourceCaptureCancelled):
            list(session.pages())

    @staticmethod
    def _adapter(transport) -> Json2OdooSourceCapture:
        return Json2OdooSourceCapture(
            Json2Config(
                base_url="https://odoo.example.test",
                database="production",
                api_key="secret",
                retries=0,
            ),
            transport=transport,
        )


class OdooSourceCaptureServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_id = "00000000-0000-0000-0000-000000000001"
        self.schema = _schema(self.workspace_id)
        self.selection = _selection(self.workspace_id, self.schema)
        self.workspace_state = WorkspaceState(
            workspace_id=self.workspace_id,
            name="Odoo source",
            source_system="Odoo",
            source_mode=SourceMode.ODOO,
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://odoo.example.test",
            odoo_database="production",
            intended_models=("res.partner",),
            status=WorkspaceStatus.REGISTERED,
        )
        self.workspace_states = _WorkspaceStateReader(self.workspace_state)
        self.selections = _SelectionReader(self.selection)
        self.schemas = _SchemaReader(self.schema)
        self.service = OdooSourceCaptureService(
            self.workspace_states,
            self.selections,
            self.schemas,
            workspace_access_service(),
        )

    def test_planner_requires_explicit_tier_one_metadata(self) -> None:
        request = plan_odoo_source_capture(self.selection, self.schema)
        self.assertEqual(request.field_names, ("name",))

        name = self.schema.models[0].fields[0]
        changed_model = replace(
            self.schema.models[0],
            fields=(replace(name, stored=None), self.schema.models[0].fields[1]),
        )
        with self.assertRaisesRegex(
            OdooSourceCaptureConfigurationError,
            "not eligible",
        ):
            plan_odoo_source_capture(
                self.selection,
                replace(self.schema, models=(changed_model,)),
            )

    def test_service_checks_identity_and_schema_at_both_ends(self) -> None:
        gateway = _Gateway(self.schema)
        pages = []

        result = self.service.capture(
            self.workspace_id,
            gateway,
            consume_page_factory=lambda request, selection: pages.append,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(result.accounting.row_count, 0)
        self.assertEqual(gateway.identity_calls, 2)
        self.assertEqual(gateway.schema_calls, 2)

    def test_service_rejects_end_identity_drift(self) -> None:
        gateway = _Gateway(self.schema, drift_identity=True)

        with self.assertRaisesRegex(
            OdooSourceCaptureConsistencyError,
            "principal",
        ):
            self.service.capture(
                self.workspace_id,
                gateway,
                consume_page_factory=lambda request, selection: lambda page: None,
                actor=LOCAL_ACTOR,
            )


class _WorkspaceStateReader:
    def __init__(self, workspace_state):
        self.workspace_state = workspace_state

    def get(self, workspace_id):
        return self.workspace_state


class _SelectionReader:
    def __init__(self, selection):
        self.selection = selection

    def get_current_odoo_capture_selection(self, workspace_id):
        return self.selection


class _SchemaReader:
    def __init__(self, schema):
        self.schema = schema

    def get_odoo_schema_catalog(self, workspace_id):
        return self.schema


class _Gateway:
    def __init__(self, schema, *, drift_identity=False):
        self.schema = schema
        self.drift_identity = drift_identity
        self.identity_calls = 0
        self.schema_calls = 0
        self.context = _context()

    def probe_identity(self, request, *, cancellation=None):
        self.identity_calls += 1
        principal_hash = (
            "sha256:" + "9" * 64
            if self.drift_identity and self.identity_calls == 2
            else self.schema.read_principal_hash
        )
        return (
            OdooReadIdentity(
                target_hash=self.schema.connection_target_hash,
                principal_hash=principal_hash,
                permission_hash=self.schema.read_permission_hash,
                context_hash=self.schema.read_context_hash,
                readable_models=("res.partner",),
                observed_at="2026-08-12T10:00:00Z",
            ),
            self.context,
        )

    def probe_schema(self, request, context, *, cancellation=None):
        self.schema_calls += 1
        fields = {
            field.name: FieldMetadata(
                name=field.name,
                type=field.type,
                label=field.label,
                required=field.required,
                readonly=field.readonly,
                relation=field.relation,
                relation_field=field.relation_field,
                selection=field.selection,
                stored=field.stored,
                computed=field.computed,
                has_inverse=field.has_inverse,
                related=field.related,
                translated=field.translated,
                company_dependent=field.company_dependent,
                searchable=field.searchable,
                sortable=field.sortable,
                exportable=field.exportable,
                digits=field.digits,
                currency_field=field.currency_field,
            )
            for field in self.schema.models[0].fields
        }
        return MetadataSnapshot(
            fingerprint=TargetFingerprint(
                target_hash=self.schema.connection_target_hash,
                connection_mode="REMOTE",
                database="production",
                odoo_version="19.0",
                snapshot_timestamp="2026-08-12T10:00:00Z",
            ),
            models={
                "res.partner": ModelMetadata(
                    model="res.partner",
                    description="Contact",
                    fields=fields,
                )
            },
        )

    def open_capture(self, request, context, *, cancellation=None):
        return _EmptySession(request)

    def sample(self, request, context, *, limit, cancellation=None):
        raise AssertionError("sample is not used by capture")


class _EmptySession:
    def __init__(self, request):
        now = datetime.now(timezone.utc)
        self._accounting = OdooCaptureAccounting(
            high_water_id=0,
            row_count=0,
            page_count=0,
            record_request_count=1,
            response_bytes=2,
            normalized_bytes=0,
            capture_started_at=now,
            capture_finished_at=now,
            consistency=request.consistency,
            target_instance_assurance=request.target_instance_assurance,
            consistency_limitation="Native pages are not one database snapshot.",
        )

    def pages(self):
        return iter(())

    @property
    def accounting(self):
        return self._accounting


def _schema(workspace_id: str) -> OdooSchemaCatalog:
    eligibility = dict(
        relation=None,
        relation_field=None,
        selection=(),
        stored=True,
        computed=False,
        has_inverse=False,
        related=False,
        translated=False,
        company_dependent=False,
        searchable=True,
        sortable=True,
        exportable=True,
    )
    return OdooSchemaCatalog(
        workspace_id=workspace_id,
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        captured_at=datetime.now(timezone.utc),
        captured_by="Manager",
        connection_mode="REMOTE",
        database="production",
        odoo_version="19.0",
        models=(
            SchemaModel(
                name="res.partner",
                label="Contact",
                fields=(
                    SchemaField(
                        name="name",
                        label="Name",
                        type="char",
                        required=False,
                        readonly=False,
                        **eligibility,
                    ),
                    SchemaField(
                        name="write_date",
                        label="Last Updated on",
                        type="datetime",
                        required=False,
                        readonly=True,
                        **eligibility,
                    ),
                ),
            ),
        ),
        content_hash="sha256:" + "2" * 64,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:" + "6" * 64,
        read_principal_hash="sha256:" + "3" * 64,
        read_permission_hash="sha256:" + "4" * 64,
        read_context_hash="sha256:" + "5" * 64,
        connection_target_hash=HASH,
    )


def _selection(
    workspace_id: str,
    schema: OdooSchemaCatalog,
) -> OdooCaptureSelection:
    return OdooCaptureSelection.create(
        selection_id="00000000-0000-0000-0000-000000000002",
        version=1,
        data_version_id=data_version_id(workspace_id),
        dataset_name="contacts",
        model="res.partner",
        field_names=("name",),
        filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
        max_rows=1_000,
        connection_target_hash=schema.connection_target_hash,
        schema_scope_hash=schema.content_hash,
        read_principal_hash=schema.read_principal_hash,
        read_permission_hash=schema.read_permission_hash,
        context_hash=schema.read_context_hash,
        created_at=datetime.now(timezone.utc),
        created_by="Manager",
    )


def _request(**changes) -> OdooSourceCaptureRequest:
    policy = CURRENT_ODOO_SOURCE_POLICY
    values = dict(
        data_version_id=data_version_id(
            "00000000-0000-0000-0000-000000000001"
        ),
        selection_id="00000000-0000-0000-0000-000000000002",
        selection_version=1,
        selection_hash=HASH,
        policy_hash=ODOO_SOURCE_POLICY_HASH,
        model="res.partner",
        projection=(OdooCaptureFieldProjection("name", "char"),),
        filter_clauses=(),
        filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
        schema_model_names=("res.partner",),
        maximum_rows=1_000,
        page_size=500,
        max_sample_rows=50,
        max_request_bytes=policy.max_request_bytes,
        max_response_bytes=policy.max_response_bytes,
        max_value_bytes=policy.max_value_bytes,
        max_row_bytes=policy.max_row_bytes,
        max_snapshot_bytes=policy.max_snapshot_bytes,
        expected_connection_target_hash=HASH,
        expected_schema_scope_hash=HASH,
        expected_read_principal_hash=HASH,
        expected_read_permission_hash=HASH,
        expected_context_hash=HASH,
        consistency=OdooCaptureConsistency.KEYSET_HIGH_WATER_INTERVAL,
        target_instance_assurance=TargetInstanceAssurance.CONNECTION_ONLY,
    )
    values.update(changes)
    return OdooSourceCaptureRequest(**values)


def _context() -> ProtectedOdooReadContext:
    return ProtectedOdooReadContext(
        language="en_US",
        timezone="UTC",
        primary_company_id=1,
        allowed_company_ids=(1, 2),
    )


def _row(identifier: int) -> dict[str, object]:
    return {
        "id": identifier,
        "write_date": "2026-08-12 10:11:12",
        "name": f"Name {identifier}",
        "active": identifier % 2 == 1,
    }


def _rows(count: int) -> list[dict[str, object]]:
    return [_row(identifier) for identifier in range(1, count + 1)]


if __name__ == "__main__":
    unittest.main()

