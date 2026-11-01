"""Offline Odoo wire contract, including discovery, defaults, and exact cleanup.

Only the HTTP boundary is replaced. The qualification runner still captures
schema and identities, plans comparison, writes, and reads back through the
real application composition and JSON-2 adapters.
"""

import json
from types import SimpleNamespace

from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.domain.odoo.contracts import MetadataRequest
from impodo.web.composition.target_readers import _model_catalog_requests
from tests.support.hierarchy_odoo import HierarchyOdoo


class QualificationWire(HierarchyOdoo):
    def __init__(self, major=19, mode="REMOTE"):
        super().__init__(
            SimpleNamespace(models=(), connection_target_hash="", connection_mode=mode)
        )
        self.records.clear()
        self.major = major
        self.denied = None
        self.module_names = ("base", "contacts", "mrp", "product")
        self.calls = []
        self.unlinks = []
        self.fields = {
            "product.category": {
                "name": {"type": "char", "required": True},
                "parent_id": {"type": "many2one", "relation": "product.category"},
            },
            "product.template": {
                "name": {"type": "char", "required": True},
                "default_code": {"type": "char"},
                "categ_id": {
                    "type": "many2one",
                    "relation": "product.category",
                    "required": True,
                },
                "sale_ok": {"type": "boolean", "required": True},
            },
        }
        self.relations = {
            ("product.category", "parent_id"): "product.category",
            ("product.template", "categ_id"): "product.category",
        }
        self.records[("product.category", 50)] = {"name": "All", "parent_id": False}

    def _create(self, model, values, external_ids=()):
        identifiers = super()._create(model, values, external_ids)
        if model == "product.template":
            for identifier in identifiers:
                variant_id = 50000 + identifier
                self.records[("product.product", variant_id)] = {
                    "product_tmpl_id": identifier
                }
        return identifiers

    def open(self, request, timeout):
        status, payload = self.transport(
            request.full_url, request.headers, request.data, timeout, request.method
        )
        raw = json.dumps(payload).encode()
        return SimpleNamespaceContext(status, raw)

    def transport(self, url, headers, body, timeout, method):
        assert getattr(_read_databases, "connections", None) is None, (
            "Odoo called inside a retained DuckDB scope"
        )
        if url.endswith("/web/version"):
            self.calls.append(("web", "version"))
            return 200, {"version": f"{self.major}.0"}
        model, operation = url.split("/json/2/", 1)[1].rsplit("/", 1)
        payload = json.loads(body)
        self.calls.append((model, operation))
        if operation == "has_access":
            return 200, (model, payload["operation"]) != self.denied
        if operation == "context_get":
            return 200, {
                "uid": 7,
                "allowed_company_ids": [9],
                "lang": "en_US",
                "tz": "UTC",
            }
        if operation == "fields_get":
            fields = self.fields[model]
            return 200, {
                name: {"string": name, "readonly": False, "store": True, **value}
                for name, value in fields.items()
                if not payload["allfields"] or name in payload["allfields"]
            }
        if operation == "default_get":
            defaults = {"sale_ok": True} if model == "product.template" else {}
            return 200, {
                name: value
                for name, value in defaults.items()
                if name in payload["fields"]
            }
        if operation == "search_read":
            if model == "ir.module.module":
                return 200, [
                    {
                        "id": index,
                        "name": name,
                        "state": "installed",
                        "installed_version": f"{self.major}.0.1.0",
                    }
                    for index, name in enumerate(self.module_names, 1)
                ]
            if model == "res.users":
                return 200, [
                    {
                        "id": 7,
                        "login": "fixture-user",
                        "company_id": [9, "Fixture company"],
                        "group_ids": [3],
                        "share": False,
                    }
                ]
            if model == "res.company":
                return 200, [{"id": 9, "name": "Fixture company"}]
            if model == "ir.model":
                return 200, [
                    {
                        "id": index,
                        "model": name,
                        "name": name,
                        "abstract": False,
                        "transient": False,
                        "modules": "product",
                        "state": "base",
                    }
                    for index, name in enumerate(self.fields, 1)
                ]
            if model == "ir.model.constraint":
                return 200, []
        if operation == "unlink":
            identifiers = set(payload["ids"])
            self.unlinks.append((model, identifiers))
            for identifier in identifiers:
                self.records.pop((model, identifier))
            if model == "product.template":
                self.records = {
                    key: value
                    for key, value in self.records.items()
                    if key[0] != "product.product"
                    or value["product_tmpl_id"] not in identifiers
                }
            if model == "ir.model.data":
                self.external_ids = {
                    name: binding
                    for name, binding in self.external_ids.items()
                    if any(
                        row.get("module", "") + "." + row.get("name", "") == name
                        for (record_model, _), row in self.records.items()
                        if record_model == model
                    )
                }
            return 200, True
        status, result = super().transport(url, headers, body, timeout, method)
        if (
            operation == "search_read"
            and payload.get("context", {}).get("active_test") is not False
        ):
            result = [
                row
                for row in result
                if self.records[(model, row["id"])].get("active", True)
            ]
        return status, result


class SimpleNamespaceContext:
    def __init__(self, status, raw):
        self.status, self.raw = status, raw

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        return self.raw[:size]


class OfflineLocalReader:
    """Replace host execution, retaining the normal LOCAL reader selection seam."""

    def __init__(self, wire, database):
        self.wire, self.database, self.calls = wire, database, []

    def connector(self, state):
        assert getattr(_read_databases, "connections", None) is None
        return Json2ReadConnector(
            Json2Config(
                state.odoo_base_url,
                self.database,
                "offline-local",
                connection_mode="LOCAL",
                retries=0,
                context=state.odoo_operation_context,
            ),
            transport=self.wire.transport,
        )

    def get_target_fingerprint(self, state, _profile):
        self.calls.append("fingerprint")
        return self.connector(state).get_target_fingerprint()

    def get_company_access(self, state, _profile):
        self.calls.append("company")
        return self.connector(state).get_company_access()

    def get_model_catalog(self, state, _profile):
        self.calls.append("catalog")
        return self.connector(state).get_records(_model_catalog_requests())

    def get_model_metadata(self, state, _profile, models):
        self.calls.append("schema")
        return self.connector(state).get_model_metadata(
            tuple(
                MetadataRequest(
                    model, (), all_fields=True, include_unique_constraints=True
                )
                for model in models
            )
        )

    def get_preflight_snapshots(
        self, state, _profile, metadata_requests, record_requests, **_kwargs
    ):
        self.calls.append("comparison")
        connector = self.connector(state)
        return connector.get_model_metadata(metadata_requests), connector.get_records(
            record_requests
        )
