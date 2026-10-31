"""Keep company scope consistent through native matching, loading and read-back."""

import json
import unittest
from dataclasses import replace
from uuid import uuid4

from impodo.adapters.odoo.connectors import (
    Json2ReadConnector,
    Json2WriteIdentityConnector,
)
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import ConnectorAuthorizationError, RecordRequest
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    WorkspaceState,
    WorkspaceStateError,
)
from impodo.web.composition.target_readers import _target_json2_config
from impodo.web.composition.target_writers import _readback_reader, _write_executor


class CompanyTransportTests(unittest.TestCase):
    def test_global_model_uses_operation_company_without_adding_a_record_company(self):
        for major in ("19.0", "20.0"):
            with self.subTest(major=major):
                scope = OdooApiScope(
                    "sha256:" + "1" * 64,
                    (
                        OdooModelScope(
                            "x.global", write_fields=("code",), read_fields=("code",)
                        ),
                    ),
                )
                calls = []

                def transport(url, _headers, body, _timeout, _method):
                    if url.endswith("/web/version"):
                        return 200, {"version": major}
                    payload = json.loads(body)
                    self.assertEqual(payload["context"]["allowed_company_ids"], [105])
                    calls.append(url)
                    if url.endswith("/load"):
                        self.assertEqual(payload["fields"], ["id", "code"])
                        return 200, {"ids": [501], "messages": []}
                    if url.endswith("/search_read"):
                        return 200, [{"id": 501, "code": "GLOBAL"}]
                    self.fail(f"Unexpected endpoint: {url}")

                writer = _write_executor(self.state(), "loading-key", scope)
                reader = _readback_reader(self.state(), "loading-key", scope)
                writer.transport = reader.transport = transport
                self.assertEqual(
                    writer.load_create_rows(
                        "x.global", ({"code": "GLOBAL"},), ("impodo_test.global1",)
                    ),
                    (501,),
                )
                self.assertEqual(
                    reader.read_ids("x.global", (501,), ("code",))[0].values,
                    {"code": "GLOBAL"},
                )
                self.assertEqual(len(calls), 2)

    def state(self):
        target = target_identity_hash(
            connection_mode="REMOTE",
            base_url="https://odoo.example.test",
            database="review",
        )
        return WorkspaceState(
            str(uuid4()),
            "Routing import",
            "CSV",
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://odoo.example.test",
            odoo_database="review",
            odoo_company=OdooCompanySelection(target, 105, "Northwind Factory"),
        )

    def test_matching_load_and_readback_keep_the_same_company_and_shared_record(self):
        state = self.state()
        scope = OdooApiScope(
            "sha256:" + "1" * 64,
            (
                OdooModelScope(
                    "x.route",
                    write_fields=("code", "company_id"),
                    read_fields=("code", "company_id"),
                    lookup_fields=("code",),
                ),
            ),
        )
        calls = []
        stored = {}

        def transport(url, _headers, body, _timeout, _method):
            payload = json.loads(body) if body else None
            if url.endswith("/web/version"):
                return 200, {"version": "20.0"}
            calls.append((url, payload))
            self.assertEqual(payload["context"]["allowed_company_ids"], [105])
            if url.endswith("/x.route/load"):
                values = dict(zip(payload["fields"], payload["data"][0], strict=True))
                self.assertEqual(values["company_id"], "")
                stored.update(id=501, code=values["code"], company_id=False)
                return 200, {"ids": [501], "messages": []}
            if url.endswith("/x.route/search_read"):
                return 200, [dict(stored)] if stored else []
            self.fail(f"Unexpected endpoint: {url}")

        matching = Json2ReadConnector(
            _target_json2_config(state, "checking-key"), transport=transport
        )
        self.assertEqual(
            matching.get_records(
                (RecordRequest("x.route", ("code", "company_id")),)
            ).records["x.route"],
            (),
        )
        writer = _write_executor(state, "loading-key", scope)
        reader = _readback_reader(state, "loading-key", scope)
        writer.transport, reader.transport = transport, transport
        self.assertEqual(
            writer.load_create_rows(
                "x.route",
                ({"code": "R001", "company_id": ""},),
                ("impodo_test.route1",),
            ),
            (501,),
        )
        verified = reader.read_ids("x.route", (501,), ("code", "company_id"))
        self.assertEqual(verified[0].values, {"code": "R001", "company_id": False})
        self.assertEqual(len(calls), 3)

    def test_loading_requires_a_confirmed_company_and_rejects_wrong_target_binding(
        self,
    ):
        scope = OdooApiScope(
            "sha256:" + "1" * 64,
            (OdooModelScope("x.route", write_fields=("code",), read_fields=("code",)),),
        )
        with self.assertRaisesRegex(WorkspaceStateError, "Choose the company"):
            _write_executor(replace(self.state(), odoo_company=None), "key", scope)
        with self.assertRaisesRegex(WorkspaceStateError, "another target"):
            _target_json2_config(replace(self.state(), odoo_database="other"), "key")

    def test_loading_key_can_be_refused_create_access_even_with_read_and_write_access(
        self,
    ):
        def transport(url, _headers, body, _timeout, _method):
            payload = json.loads(body) if body else None
            if url.endswith("/web/version"):
                return 200, {"version": "20.0"}
            if url.endswith("/context_get"):
                return 200, {"uid": 27}
            if url.endswith("/res.users/search_read"):
                return 200, [
                    {
                        "id": 27,
                        "login": "loader@example.test",
                        "company_id": [80, "Group"],
                        "group_ids": [4],
                        "share": False,
                    }
                ]
            if url.endswith("/res.company/search_read"):
                return 200, [{"id": 80}, {"id": 105}]
            if url.endswith("/has_access"):
                return 200, payload["operation"] != "create"
            self.fail(f"Unexpected endpoint: {url}")

        connector = Json2WriteIdentityConnector(
            _target_json2_config(self.state(), "loading-key"), transport=transport
        )
        with self.assertRaisesRegex(ConnectorAuthorizationError, "cannot create"):
            connector.probe_write_identity(
                ("x.route",), ("x.route",), creatable_models=("x.route",)
            )

    def test_company_choices_are_for_the_authenticated_user(self):
        calls = []

        def transport(url, _headers, body, _timeout, _method):
            payload = json.loads(body) if body else None
            calls.append((url, payload))
            if url.endswith("/web/version"):
                return 200, {"version": "20.0"}
            self.assertEqual(payload["context"], {})
            if url.endswith("/context_get"):
                return 200, {"uid": 27}
            if url.endswith("/res.users/search_read"):
                self.assertEqual(payload["domain"], [["id", "=", 27]])
                return 200, [
                    {
                        "id": 27,
                        "login": "checking@example.test",
                        "company_id": [80, "Group"],
                    }
                ]
            if url.endswith("/res.company/search_read"):
                self.assertEqual(
                    payload["domain"], [["user_ids", "in", [27]], ["active", "=", True]]
                )
                return 200, [
                    {"id": 80, "name": "Group"},
                    {"id": 105, "name": "Factory"},
                ]
            self.fail(f"Unexpected endpoint: {url}")

        connector = Json2ReadConnector(
            _target_json2_config(replace(self.state(), odoo_company=None), "key"),
            transport=transport,
        )
        access = connector.get_company_access()
        self.assertEqual(access.login, "checking@example.test")
        self.assertEqual(access.default_company_name, "Group")
        self.assertEqual([item.company_id for item in access.choices], [80, 105])
        self.assertEqual(len(calls), 4)
