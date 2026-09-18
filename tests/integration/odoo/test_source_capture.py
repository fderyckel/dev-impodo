from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import inspect
import json
import math
import unittest
from types import SimpleNamespace

from impodo.adapters.protected_odoo_capture_filters import ProtectedOdooCaptureFilterStore

from impodo.adapters.odoo_source_capture import Json2OdooSourceCapture
from impodo.application.odoo_dependency_capture import (
    discover_dependency_closure,
    require_capture_matches_discovery,
)
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.application.odoo_source_capture_service import OdooSourceCaptureService
from impodo.adapters.odoo.connectors import Json2Config
from impodo.domain.odoo.contracts import MetadataSnapshot
from impodo.domain.odoo_capture import (
    OdooCaptureConsistency,
    OdooCaptureFilterClause,
    OdooCaptureFilterOperator,
    OdooCaptureFilterPolicy,
    OdooCaptureRole,
    OdooCaptureSelection,
)
from impodo.domain.odoo_source_capture import (
    OdooCaptureAccounting,
    OdooCapturePage,
    OdooCaptureValueColumn,
    OdooCaptureFieldProjection,
    OdooCaptureRelationshipProjection,
    OdooCaptureRelationshipColumn,
    OdooSourceCaptureAccessRefreshRequired,
    OdooSourceCaptureCancelled,
    OdooSourceCaptureConfigurationError,
    OdooSourceCaptureConsistencyError,
    OdooSourceCaptureLimitError,
    OdooSourceCaptureRequest,
    plan_odoo_source_capture,
    validate_odoo_capture_selection_reference,
)
from impodo.domain.odoo_source_policy import (
    CURRENT_ODOO_SOURCE_POLICY,
    ODOO_SOURCE_POLICY_HASH,
    PREVIOUS_ODOO_SOURCE_POLICY_HASH,
    READABLE_ODOO_SOURCE_POLICY_HASHES,
    TargetInstanceAssurance,
)
from impodo.domain.odoo_provenance import OdooOriginBatch, OdooRelationshipOriginColumn
from impodo.domain.serialization import content_hash
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
from impodo.domain.workspace.errors import WorkspaceError
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
        if url.endswith("/search_count"):
            matching = self._matching(payload["domain"])
            return 200, str(min(len(matching), payload["limit"])).encode()
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
    def test_relationship_scan_reads_no_scalar_business_fields(self) -> None:
        transport = DatasetTransport([{
            **_row(1), "category_id": [7, "Fictional"],
            "category_ids": [7, 8],
        }])
        request = _request(
            schema_model_names=("res.partner", "res.partner.category"),
            relationship_projection=(OdooCaptureRelationshipProjection(
                "category_id", "many2one", "res.partner.category"
            ),),
            discovery_relationship_projection=(OdooCaptureRelationshipProjection(
                "category_ids", "one2many", "res.partner.category"
            ),),
        )
        batches = self._adapter(transport).scan_origins(request, _context())
        self.assertEqual(batches[0].odoo_ids, (1,))
        self.assertEqual(batches[0].relationships[0].values, ((7,),))
        self.assertEqual(batches[0].relationships[1].values, ((7, 8),))
        self.assertTrue(all(
            "name" not in call.get("fields", ()) for call in transport.calls
        ))

    def test_linked_request_without_members_cannot_read_all_records(self) -> None:
        request = _request(capture_role=OdooCaptureRole.LINKED_ONLY)
        transport = DatasetTransport(_rows(2))
        with self.assertRaisesRegex(
            OdooSourceCaptureConfigurationError, "protected member set"
        ):
            self._adapter(transport).open_capture(request, _context())
        self.assertEqual(transport.calls, [])

    def test_protected_root_filter_binds_selection_without_plaintext_value(self) -> None:
        workspace_id = "00000000-0000-0000-0000-000000000001"
        schema = _schema(workspace_id)
        clause = OdooCaptureFilterClause(
            "name", OdooCaptureFilterOperator.EQUALS, ("Only Example Group",)
        )

        class Evidence:
            def __init__(self):
                self.payload = b""

            def put_artifact(self, _project_id, **kwargs):
                self.payload = kwargs["payload"]
                return SimpleNamespace(artifact_hash=HASH)

            def artifact_storage_key(self, _project_id, **_kwargs):
                return "protected"

            def read(self, _project_id, **kwargs):
                if kwargs["expected_artifact_hash"] != HASH:
                    raise ValueError("artifact changed")
                return self.payload

        evidence = Evidence()
        store = ProtectedOdooCaptureFilterStore(evidence)
        original = _selection(workspace_id, schema)
        artifact_hash = store.put(
            "00000000-0000-0000-0000-000000000099",
            selection_id=original.selection_id,
            version=original.version,
            data_version_id=original.data_version_id,
            clauses=(clause,),
        )
        selection = replace(
            original,
            contract_version=5,
            filter_clauses=(),
            protected_filter_artifact_hash=artifact_hash,
            content_hash="",
            _calculate_content_hash=True,
        )
        self.assertNotIn("Only Example Group", selection.to_json())
        self.assertEqual(OdooCaptureSelection.from_json(selection.to_json()), selection)
        validate_odoo_capture_selection_reference(selection, schema)
        with self.assertRaisesRegex(
            OdooSourceCaptureConfigurationError, "protected Odoo source filter"
        ):
            plan_odoo_source_capture(selection, schema)
        clauses = store.read(
            "00000000-0000-0000-0000-000000000099", selection
        )
        self.assertEqual(clauses, (clause,))
        request = plan_odoo_source_capture(
            selection, schema, protected_filter_clauses=clauses
        )
        self.assertEqual(request.filter_clauses, (clause,))

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
                    1 + (1 if count else 0) + math.ceil(count / 500),
                )
                self.assertEqual(
                    [page.first_row_ordinal for page in pages],
                    ([1, 501] if count == 501 else ([1] if count else [])),
                )
                self.assertTrue(all(len(page.odoo_ids) <= 500 for page in pages))

    def test_relationship_ids_are_kept_in_protected_page_columns(self) -> None:
        rows = [
            {
                **_row(1),
                "category_id": [7, "Retail"],
                "category_ids": [7, 8],
            },
            {
                **_row(2),
                "category_id": False,
                "category_ids": [],
            },
        ]
        request = _request(
            schema_model_names=("res.partner", "res.partner.category"),
            relationship_projection=(
                OdooCaptureRelationshipProjection(
                    name="category_id",
                    kind="many2one",
                    relation_model="res.partner.category",
                ),
                OdooCaptureRelationshipProjection(
                    name="category_ids",
                    kind="many2many",
                    relation_model="res.partner.category",
                ),
            ),
        )

        page = next(
            iter(self._adapter(DatasetTransport(rows)).open_capture(
                request,
                _context(),
            ).pages())
        )

        relationships = {item.field_name: item for item in page.relationships}
        self.assertEqual(relationships["category_id"].values, ((7,), ()))
        self.assertEqual(relationships["category_ids"].values, ((7, 8), ()))
        protected = {item.field_name: item for item in page.origin_batch.relationships}
        self.assertEqual(protected["category_id"].values, ((7,), ()))

    def test_float_values_are_captured_and_nonfinite_values_are_rejected(self) -> None:
        request = _request(
            projection=(
                OdooCaptureFieldProjection("name", "char"),
                OdooCaptureFieldProjection("product_qty", "float"),
            ),
        )
        rows = [{**_row(1), "product_qty": 0.45}]
        page = next(iter(self._adapter(DatasetTransport(rows)).open_capture(
            request, _context(),
        ).pages()))
        self.assertEqual(page.columns[1].values, (0.45,))

        for invalid in (float("nan"), float("inf"), "0.45", True):
            with self.subTest(invalid=invalid):
                rows = [{**_row(1), "product_qty": invalid}]
                with self.assertRaisesRegex(
                    OdooSourceCaptureConsistencyError, "invalid float value",
                ):
                    list(self._adapter(DatasetTransport(rows)).open_capture(
                        request, _context(),
                    ).pages())

    def test_float_quantity_is_eligible_in_a_saved_source_selection(self) -> None:
        workspace_id = "00000000-0000-0000-0000-000000000001"
        schema = _schema(workspace_id)
        model = schema.models[0]
        quantity = replace(
            model.fields[0],
            name="product_qty",
            label="Quantity",
            type="float",
        )
        schema = replace(schema, models=(replace(
            model,
            fields=(model.fields[0], quantity, model.fields[1]),
        ),))
        selection = replace(
            _selection(workspace_id, schema),
            field_names=("name", "product_qty"),
            content_hash="",
            _calculate_content_hash=True,
        )

        request = plan_odoo_source_capture(selection, schema)

        self.assertEqual(
            tuple((field.name, field.field_type) for field in request.projection),
            (("name", "char"), ("product_qty", "float")),
        )
        self.assertNotIn("float", CURRENT_ODOO_SOURCE_POLICY.writable_field_types)

    def test_previous_policy_selection_remains_readable_but_cannot_capture(self) -> None:
        workspace_id = "00000000-0000-0000-0000-000000000001"
        schema = _schema(workspace_id)
        previous_hash = PREVIOUS_ODOO_SOURCE_POLICY_HASH
        self.assertIn(previous_hash, READABLE_ODOO_SOURCE_POLICY_HASHES)
        prior_policy = replace(
            CURRENT_ODOO_SOURCE_POLICY,
            contract_version=3,
            capture_field_types=tuple(
                field for field in CURRENT_ODOO_SOURCE_POLICY.capture_field_types
                if field != "float"
            ),
        )
        self.assertEqual(content_hash(prior_policy.to_dict()), previous_hash)
        previous = replace(
            _selection(workspace_id, schema),
            policy_hash=previous_hash,
            content_hash="",
            _calculate_content_hash=True,
        )

        self.assertEqual(
            OdooCaptureSelection.from_json(previous.to_json()), previous,
        )
        with self.assertRaisesRegex(
            OdooSourceCaptureConfigurationError, "current schema evidence",
        ):
            plan_odoo_source_capture(previous, schema)

    def test_maximum_plus_one_fails_closed(self) -> None:
        with self.assertRaisesRegex(OdooSourceCaptureLimitError, "More than 500"):
            self._adapter(DatasetTransport(_rows(501))).open_capture(
                _request(maximum_rows=500),
                _context(),
            )

    def test_count_and_capture_use_the_selected_batch_size(self) -> None:
        for page_size in (10, 100, 500):
            with self.subTest(page_size=page_size):
                transport = DatasetTransport(_rows(205))
                request = _request(maximum_rows=1_000, page_size=page_size)
                adapter = self._adapter(transport)

                self.assertEqual(
                    adapter.count_matching(request, _context(), limit=1_001),
                    205,
                )
                session = adapter.open_capture(request, _context())
                pages = list(session.pages())

                self.assertEqual(session.matching_rows, 205)
                self.assertEqual(len(pages), math.ceil(205 / page_size))
                self.assertTrue(
                    all(page.row_count <= page_size for page in pages)
                )

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
        self.assertEqual(transport.calls[0]["context"]["lang"], "en_US")
        self.assertEqual(transport.calls[0]["context"]["tz"], "UTC")
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
            {
                "count_matching",
                "open_capture",
                "probe_identity",
                "probe_schema",
                "scan_origins",
                "sample",
            },
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

        with self.assertRaises(OdooSourceCaptureCancelled):
            self._adapter(DatasetTransport(_rows(501))).open_capture(
                _request(),
                _context(),
                cancellation=cancellation,
            )

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


class OdooDependencyClosureTests(unittest.TestCase):
    def test_planner_reads_one2many_only_for_linked_supporting_models(self) -> None:
        workspace_id = "00000000-0000-0000-0000-000000000001"
        schema = _schema(workspace_id)
        root = schema.models[0]
        category = replace(root, name="res.partner.category", label="Category")
        child_field = replace(
            root.fields[0], name="category_ids", label="Categories",
            type="one2many", relation=category.name,
            relation_field="partner_id",
        )
        schema = replace(schema, models=(
            replace(root, fields=(*root.fields, child_field)), category,
        ))
        selection = _selection(workspace_id, schema)
        self.assertEqual(
            plan_odoo_source_capture(selection, schema).discovery_relationship_projection,
            (),
        )
        planned = plan_odoo_source_capture(
            selection, schema, linked_models=frozenset({category.name}),
        )
        self.assertEqual(
            tuple(item.name for item in planned.discovery_relationship_projection),
            ("category_ids",),
        )

    def test_parent_one2many_discovers_children_without_duplicate_portable_link(self) -> None:
        models = ("mrp.bom", "mrp.bom.line")
        root = _request(
            model="mrp.bom", schema_model_names=models,
            discovery_relationship_projection=(OdooCaptureRelationshipProjection(
                "bom_line_ids", "one2many", "mrp.bom.line", "bom_id"
            ),),
        )
        child = _request(
            model="mrp.bom.line", schema_model_names=models,
            capture_role=OdooCaptureRole.LINKED_ONLY,
            relationship_projection=(OdooCaptureRelationshipProjection(
                "bom_id", "many2one", "mrp.bom"
            ),),
        )

        def scan(request):
            if request.model == "mrp.bom":
                return (OdooOriginBatch(1, (10,), (None,), (
                    OdooRelationshipOriginColumn(
                        "bom_line_ids", "one2many", "mrp.bom.line", ((71,),)
                    ),
                )),)
            self.assertEqual(request.member_ids, (71,))
            return (OdooOriginBatch(1, (71,), (None,), (
                OdooRelationshipOriginColumn(
                    "bom_id", "many2one", "mrp.bom", ((10,),)
                ),
            )),)

        closure = discover_dependency_closure((root, child), scan)
        self.assertEqual(closure.ids_by_model["mrp.bom.line"], (71,))
        self.assertEqual(
            closure.discovery_facts_by_model["mrp.bom"][10][1],
            (("bom_line_ids", (71,)),),
        )
        require_capture_matches_discovery(
            root, (OdooOriginBatch(1, (10,), (None,), ()),), closure
        )

    def test_changed_one2many_edges_are_detected_even_when_membership_is_same(self) -> None:
        models = ("mrp.bom", "mrp.bom.line")
        root = _request(
            model="mrp.bom", schema_model_names=models,
            discovery_relationship_projection=(
                OdooCaptureRelationshipProjection(
                    "a_ids", "one2many", "mrp.bom.line", "bom_id"
                ),
                OdooCaptureRelationshipProjection(
                    "b_ids", "one2many", "mrp.bom.line", "bom_id"
                ),
            ),
        )
        child = _request(
            model="mrp.bom.line", schema_model_names=models,
            capture_role=OdooCaptureRole.LINKED_ONLY,
        )

        def scan(swapped):
            def read(request):
                if request.model == "mrp.bom.line":
                    return (OdooOriginBatch(
                        1, request.member_ids,
                        (None,) * len(request.member_ids), (),
                    ),)
                return (OdooOriginBatch(1, (10,), (None,), (
                    OdooRelationshipOriginColumn(
                        "a_ids", "one2many", "mrp.bom.line",
                        ((72 if swapped else 71,),),
                    ),
                    OdooRelationshipOriginColumn(
                        "b_ids", "one2many", "mrp.bom.line",
                        ((71 if swapped else 72,),),
                    ),
                )),)
            return read

        before = discover_dependency_closure((root, child), scan(False))
        after = discover_dependency_closure((root, child), scan(True))
        self.assertEqual(before.ids_by_model, after.ids_by_model)
        self.assertNotEqual(before, after)

    def test_follows_generic_chain_and_deduplicates_cycle(self) -> None:
        models = ("mrp.bom", "product.template", "uom.uom")
        requests = (
            _request(
                model="mrp.bom", schema_model_names=models,
                relationship_projection=(OdooCaptureRelationshipProjection(
                    "product_tmpl_id", "many2one", "product.template"
                ),),
            ),
            _request(
                model="product.template", schema_model_names=models,
                capture_role=OdooCaptureRole.LINKED_ONLY,
                relationship_projection=(OdooCaptureRelationshipProjection(
                    "uom_id", "many2one", "uom.uom"
                ),),
            ),
            _request(
                model="uom.uom", schema_model_names=models,
                capture_role=OdooCaptureRole.LINKED_ONLY,
                relationship_projection=(OdooCaptureRelationshipProjection(
                    "product_id", "many2one", "product.template"
                ),),
            ),
        )
        calls = []

        def scan(request):
            calls.append((request.model, request.member_ids))
            identifier, field, target = {
                "mrp.bom": (10, "product_tmpl_id", 20),
                "product.template": (20, "uom_id", 30),
                "uom.uom": (30, "product_id", 20),
            }[request.model]
            return (OdooOriginBatch(
                1, (identifier,), (None,),
                (OdooRelationshipOriginColumn(
                    field, "many2one",
                    request.relationship_projection[0].relation_model,
                    ((target,),),
                ),),
            ),)

        closure = discover_dependency_closure(requests, scan)
        self.assertEqual(closure.ids_by_model, {
            "mrp.bom": (10,), "product.template": (20,), "uom.uom": (30,)
        })
        self.assertEqual(calls, [
            ("mrp.bom", ()), ("product.template", (20,)), ("uom.uom", (30,))
        ])
        require_capture_matches_discovery(requests[0], scan(requests[0]), closure)
        with self.assertRaisesRegex(
            OdooSourceCaptureConsistencyError, "missing or inaccessible"
        ):
            discover_dependency_closure(
                requests,
                lambda request: () if request.model == "uom.uom" else scan(request),
            )

    def test_depth_limit_blocks_runaway_related_graph(self) -> None:
        models = tuple(f"related.m{index}" for index in range(6))
        requests = tuple(_request(
            model=model,
            schema_model_names=models,
            capture_role=(
                OdooCaptureRole.ROOT if index == 0 else OdooCaptureRole.LINKED_ONLY
            ),
            relationship_projection=(
                (OdooCaptureRelationshipProjection(
                    "next_id", "many2one", models[index + 1]
                ),) if index < 5 else ()
            ),
        ) for index, model in enumerate(models))

        def scan(request):
            index = models.index(request.model)
            relation = (
                (OdooRelationshipOriginColumn(
                    "next_id", "many2one", models[index + 1],
                    ((index + 2,),),
                ),) if index < 5 else ()
            )
            return (OdooOriginBatch(1, (index + 1,), (None,), relation),)

        with self.assertRaisesRegex(OdooSourceCaptureLimitError, "depth limit"):
            discover_dependency_closure(requests, scan)


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

    def test_planner_uses_read_evidence_without_inventing_compute_metadata(
        self,
    ) -> None:
        request = plan_odoo_source_capture(self.selection, self.schema)
        self.assertEqual(request.field_names, ("name",))

        live_model = replace(
            self.schema.models[0],
            fields=tuple(
                replace(
                    field,
                    computed=None,
                    related=None,
                    translated=None,
                )
                for field in self.schema.models[0].fields
            ),
        )
        live_request = plan_odoo_source_capture(
            self.selection,
            replace(self.schema, models=(live_model,)),
        )
        self.assertEqual(live_request.field_names, ("name",))

        name = live_model.fields[0]
        changed_model = replace(
            live_model,
            fields=(replace(name, stored=None), live_model.fields[1]),
        )
        with self.assertRaisesRegex(
            OdooSourceCaptureConfigurationError,
            "not eligible",
        ):
            plan_odoo_source_capture(
                self.selection,
                replace(self.schema, models=(changed_model,)),
            )

        related_model = replace(
            live_model,
            fields=(replace(name, related=True), live_model.fields[1]),
        )
        with self.assertRaisesRegex(
            OdooSourceCaptureConfigurationError,
            "not eligible",
        ):
            plan_odoo_source_capture(
                self.selection,
                replace(self.schema, models=(related_model,)),
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

    def test_assessment_counts_once_without_opening_the_value_stream(self) -> None:
        gateway = _Gateway(self.schema, matching_rows=205)

        assessment = self.service.assess(
            self.workspace_id,
            gateway,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(assessment.matching_rows, 205)
        self.assertEqual(assessment.batch_count, 1)
        self.assertEqual(gateway.identity_calls, 1)
        self.assertEqual(gateway.schema_calls, 1)
        self.assertEqual(gateway.count_calls, 1)
        self.assertEqual(gateway.open_calls, 0)

    def test_set_assessment_shares_identity_and_schema_verification(self) -> None:
        service, schema = self._multi_model_service()
        gateway = _Gateway(schema, matching_rows=205)

        assessment = service.assess_all(
            self.workspace_id,
            gateway,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(len(assessment.items), 2)
        self.assertEqual(assessment.matching_rows, 410)
        self.assertEqual(gateway.identity_calls, 1)
        self.assertEqual(gateway.schema_calls, 1)
        self.assertEqual(gateway.count_calls, 2)
        self.assertEqual(gateway.open_calls, 0)

    def test_set_capture_shares_start_and_end_verification(self) -> None:
        service, schema = self._multi_model_service()
        gateway = _Gateway(schema)

        results = service.capture_all(
            self.workspace_id,
            gateway,
            consume_page_factory=lambda request, selection: lambda page: None,
            actor=LOCAL_ACTOR,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(gateway.identity_calls, 2)
        self.assertEqual(gateway.schema_calls, 2)
        self.assertEqual(gateway.open_calls, 2)

    def _multi_model_service(
        self,
    ) -> tuple[OdooSourceCaptureService, OdooSchemaCatalog]:
        product = replace(
            self.schema.models[0],
            name="product.template",
            label="Product",
        )
        schema = replace(
            self.schema,
            models=tuple(
                sorted(
                    (*self.schema.models, product),
                    key=lambda item: item.name,
                )
            ),
        )
        selections = _SelectionReader(
            _selection(self.workspace_id, schema),
            _selection(
                self.workspace_id,
                schema,
                selection_id="00000000-0000-0000-0000-000000000003",
                dataset_name="products",
                model="product.template",
            ),
        )
        service = OdooSourceCaptureService(
            _WorkspaceStateReader(
                replace(
                    self.workspace_state,
                    intended_models=("product.template", "res.partner"),
                )
            ),
            selections,
            _SchemaReader(schema),
            workspace_access_service(),
        )
        return service, schema

    def test_service_reads_protected_filter_before_any_odoo_call(self) -> None:
        clause = OdooCaptureFilterClause(
            "name", OdooCaptureFilterOperator.EQUALS, ("Fictional Group",)
        )
        selection = replace(
            self.selection,
            contract_version=5,
            filter_clauses=(),
            protected_filter_artifact_hash=HASH,
            content_hash="",
            _calculate_content_hash=True,
        )
        self.selections = _SelectionReader(selection)
        gateway = _Gateway(self.schema)
        service = OdooSourceCaptureService(
            self.workspace_states,
            self.selections,
            self.schemas,
            workspace_access_service(),
        )
        with self.assertRaisesRegex(
            WorkspaceError, "Protected Odoo source filters are not configured"
        ):
            service.assess(self.workspace_id, gateway, actor=LOCAL_ACTOR)
        self.assertEqual(gateway.identity_calls, 0)

        class Filters:
            def read(self, _project_id, current):
                self_seen.append(current.content_hash)
                return (clause,)

        self_seen: list[str] = []
        service = OdooSourceCaptureService(
            self.workspace_states,
            self.selections,
            self.schemas,
            workspace_access_service(),
            capture_filters=Filters(),
        )
        request = service.validate_current_plans(
            self.workspace_id, actor=LOCAL_ACTOR
        )[0]
        self.assertEqual(request.filter_clauses, (clause,))
        self.assertEqual(self_seen, [selection.content_hash])

    def test_linked_only_capture_resolves_members_before_freezing_values(self) -> None:
        related_name = "res.partner.category"
        partner = self.schema.models[0]
        category = replace(partner, name=related_name, label="Category")
        category_id = replace(
            partner.fields[0],
            name="category_id", label="Category", type="many2one",
            relation=related_name,
        )
        partner = replace(partner, fields=(category_id, *partner.fields))
        schema = replace(self.schema, models=(partner, category))
        root = _selection(self.workspace_id, schema)
        related = replace(
            _selection(
                self.workspace_id, schema,
                selection_id="00000000-0000-0000-0000-000000000003",
                dataset_name="categories", model=related_name,
            ),
            contract_version=6,
            capture_role=OdooCaptureRole.LINKED_ONLY,
            content_hash="",
            _calculate_content_hash=True,
        )
        service = OdooSourceCaptureService(
            _WorkspaceStateReader(replace(
                self.workspace_state,
                intended_models=("res.partner", related_name),
            )),
            _SelectionReader(root, related),
            _SchemaReader(schema),
            workspace_access_service(),
        )
        now = datetime.now(timezone.utc)

        class Gateway(_Gateway):
            def __init__(self):
                super().__init__(schema)
                self.scanned = []
                self.opened = []
                self.missing = False

            def scan_origins(self, request, context, *, cancellation=None):
                self.scanned.append((request.model, request.member_ids))
                if request.model == related_name and self.missing:
                    return ()
                identifier = 41 if request.model == "res.partner" else 7
                relations = (
                    (OdooRelationshipOriginColumn(
                        "category_id", "many2one", related_name, ((7,),)
                    ),) if request.model == "res.partner" else ()
                )
                return (OdooOriginBatch(1, (identifier,), (now,), relations),)

            def open_capture(self, request, context, *, cancellation=None):
                self.opened.append((request.model, request.member_ids))
                identifier = 41 if request.model == "res.partner" else 7
                name = "Demo contact" if identifier == 41 else "Demo category"
                relations = (
                    (OdooCaptureRelationshipColumn(
                        "category_id", "many2one", related_name, ((7,),)
                    ),) if identifier == 41 else ()
                )
                page = OdooCapturePage(
                    1, (identifier,), (now,),
                    (OdooCaptureValueColumn("name", "char", (name,)),),
                    100, 30, relations,
                )
                accounting = OdooCaptureAccounting(
                    high_water_id=identifier, row_count=1, page_count=1,
                    record_request_count=3, response_bytes=200,
                    normalized_bytes=30, capture_started_at=now,
                    capture_finished_at=now,
                    consistency=request.consistency,
                    target_instance_assurance=request.target_instance_assurance,
                    consistency_limitation="Bounded native reads",
                )

                class Session:
                    matching_rows = 1

                    def pages(self):
                        return iter((page,))

                    @property
                    def accounting(self):
                        return accounting

                return Session()

        gateway = Gateway()
        assessment = service.assess_all(
            self.workspace_id, gateway, actor=LOCAL_ACTOR
        )
        self.assertEqual(assessment.matching_rows, 2)
        self.assertEqual(assessment.items[1][1].page_size, 100)
        self.assertEqual(gateway.scanned, [
            ("res.partner", ()), (related_name, (7,))
        ])
        pages = []
        results = service.capture_all(
            self.workspace_id, gateway,
            consume_page_factory=lambda request, selection: pages.append,
            actor=LOCAL_ACTOR,
        )
        self.assertEqual([item.accounting.row_count for item in results], [1, 1])
        self.assertEqual([page.odoo_ids for page in pages], [(41,), (7,)])
        self.assertEqual(gateway.opened, [
            ("res.partner", ()), (related_name, (7,))
        ])
        gateway.missing = True
        with self.assertRaisesRegex(
            OdooSourceCaptureConsistencyError, "missing or inaccessible"
        ):
            service.assess_all(self.workspace_id, gateway, actor=LOCAL_ACTOR)

    def test_service_rejects_end_identity_drift(self) -> None:
        gateway = _Gateway(self.schema, drift_identity=True)

        with self.assertRaisesRegex(
            OdooSourceCaptureConsistencyError,
            "API user",
        ):
            self.service.capture(
                self.workspace_id,
                gateway,
                consume_page_factory=lambda request, selection: lambda page: None,
                actor=LOCAL_ACTOR,
            )

    def test_assessment_requests_one_time_refresh_for_legacy_access_hash(self) -> None:
        gateway = _Gateway(
            self.schema,
            context_hash="sha256:" + "8" * 64,
        )

        with self.assertRaisesRegex(
            OdooSourceCaptureAccessRefreshRequired,
            "earlier verification format",
        ):
            self.service.assess(
                self.workspace_id,
                gateway,
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(gateway.schema_calls, 0)
        self.assertEqual(gateway.count_calls, 0)


class _WorkspaceStateReader:
    def __init__(self, workspace_state):
        self.workspace_state = workspace_state

    def get(self, workspace_id):
        return self.workspace_state


class _SelectionReader:
    def __init__(self, *selections):
        self.selections = tuple(selections)

    def get_current_odoo_capture_selection(self, workspace_id):
        return self.selections[0] if self.selections else None

    def get_current_odoo_capture_selections(self, workspace_id):
        return self.selections


class _SchemaReader:
    def __init__(self, schema):
        self.schema = schema

    def get_odoo_schema_catalog(self, workspace_id):
        return self.schema


class _Gateway:
    def __init__(
        self,
        schema,
        *,
        drift_identity=False,
        matching_rows=0,
        context_hash=None,
    ):
        self.schema = schema
        self.drift_identity = drift_identity
        self.matching_rows = matching_rows
        self.identity_calls = 0
        self.schema_calls = 0
        self.count_calls = 0
        self.open_calls = 0
        self.context = _context()
        self.context_hash = context_hash or schema.read_context_hash

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
                context_hash=self.context_hash,
                readable_models=tuple(
                    sorted(item.name for item in self.schema.models)
                ),
                observed_at="2026-08-12T10:00:00Z",
            ),
            self.context,
        )

    def probe_schema(self, request, context, *, cancellation=None):
        self.schema_calls += 1
        return MetadataSnapshot(
            fingerprint=TargetFingerprint(
                target_hash=self.schema.connection_target_hash,
                connection_mode="REMOTE",
                database="production",
                odoo_version="19.0",
                snapshot_timestamp="2026-08-12T10:00:00Z",
            ),
            models={
                model.name: ModelMetadata(
                    model=model.name,
                    description=model.label,
                    fields={
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
                        for field in model.fields
                    },
                )
                for model in self.schema.models
            },
        )

    def open_capture(self, request, context, *, cancellation=None):
        self.open_calls += 1
        return _EmptySession(request)

    def count_matching(self, request, context, *, limit, cancellation=None):
        self.count_calls += 1
        return min(self.matching_rows, limit)

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
    def matching_rows(self):
        return 0

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
    *,
    selection_id: str = "00000000-0000-0000-0000-000000000002",
    dataset_name: str = "contacts",
    model: str = "res.partner",
) -> OdooCaptureSelection:
    return OdooCaptureSelection.create(
        selection_id=selection_id,
        version=1,
        data_version_id=data_version_id(workspace_id),
        dataset_name=dataset_name,
        model=model,
        field_names=("name",),
        filter_policy=OdooCaptureFilterPolicy.ALL_MATCHING_RECORDS,
        max_rows=10_000,
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
