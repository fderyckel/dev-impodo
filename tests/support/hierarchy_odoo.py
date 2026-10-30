"""Independent stateful Odoo boundary for fictional hierarchy qualification."""

import json
from dataclasses import replace

from impodo.domain.execution.odoo_readback import ExternalIdBinding, ReadbackRecord
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.shared.models import TargetRecord
from tests.support.hierarchy_workflow import EXISTING_CATEGORY_PATHS
from tests.support.recipe_lifecycle import metadata_for_schema


class HierarchyOdoo:
    """Resolve actual domains and store writes without consulting expected output."""

    def __init__(self, schema):
        self.schema = schema
        self.target_hash = schema.connection_target_hash
        self.scope_hash = ""
        self.imports_external_ids = schema.connection_mode == "REMOTE"
        self.records = {}
        self.external_ids = {}
        self.requests = []
        self.writes = []
        self.lookups = []
        self.reads = []
        self.after_create = None
        self.before_create = None
        self.wire_calls = []
        self.relations = {
            (model.name, field.name): field.relation
            for model in schema.models
            for field in model.fields
            if field.relation
        }
        path_ids = {}
        for identifier, path in enumerate(EXISTING_CATEGORY_PATHS, 101):
            path_ids[path] = identifier
            self.records[("product.category", identifier)] = {
                "name": path[-1],
                "parent_id": path_ids.get(path[:-1], False),
            }

    def value(self, model, identifier, field):
        first, _, remaining = field.partition(".")
        value = (
            identifier
            if first == "id"
            else self.records[(model, identifier)].get(first, False)
        )
        if remaining:
            if value is False or value is None:
                return False
            return self.value(self.relations[(model, first)], int(value), remaining)
        return value

    def matches(self, model, identifier, domain):
        tokens = iter(domain)

        def expression(token):
            if token == "|":
                left, right = expression(next(tokens)), expression(next(tokens))
                return left or right
            if token == "&":
                left, right = expression(next(tokens)), expression(next(tokens))
                return left and right
            if token == "!":
                return not expression(next(tokens))
            field, operator, expected = token
            actual = self.value(model, identifier, field)
            if operator == "=":
                return actual == expected or (
                    actual in (False, None) and expected in (False, None)
                )
            if operator == "in":
                return actual in expected
            raise AssertionError(f"Unsupported fictional-domain operator: {operator}")

        results = [expression(token) for token in tokens]
        return all(results)

    def compare(self, requirements):
        metadata = metadata_for_schema(
            self.schema,
            {
                request.model: request.fields
                for request in requirements.metadata_requests
            },
        )
        metadata = replace(
            metadata,
            fingerprint=replace(
                metadata.fingerprint,
                odoo_version=self.schema.odoo_version,
                snapshot_timestamp="2026-10-30T00:00:00Z",
                module_versions={"base": f"{self.schema.odoo_version}.1"},
            ),
        )
        selected, requested = {}, {}
        for request in requirements.record_requests:
            assert request.domain, (
                "A fictional comparison attempted an unrestricted read"
            )
            self.requests.append(request)
            requested.setdefault(request.model, set()).update(request.fields)
            for model, identifier in self.records:
                if model == request.model and self.matches(
                    model, identifier, request.domain
                ):
                    selected.setdefault(model, {})[identifier] = TargetRecord(
                        model,
                        identifier,
                        {
                            field: self.value(model, identifier, field)
                            for field in request.fields
                        },
                    )
        return metadata, RecordSnapshot(
            metadata.fingerprint,
            {model: tuple(selected.get(model, {}).values()) for model in requested},
            {model: tuple(sorted(fields)) for model, fields in requested.items()},
        )

    def find_ids_many(self, model, domains):
        assert all("." not in field for domain in domains for field, _, _ in domain), (
            "Native grouped lookups must receive resolved parent IDs"
        )
        self.lookups.append((model, tuple(domains)))
        return tuple(self.find_ids(model, domain) for domain in domains)

    def find_ids(self, model, domain):
        assert domain
        return tuple(
            identifier
            for record_model, identifier in self.records
            if record_model == model and self.matches(model, identifier, domain)
        )

    def _create(self, model, values, external_ids=()):
        if self.before_create:
            self.before_create()
        identifiers = []
        for index, raw in enumerate(values):
            row = {}
            for field, value in raw.items():
                if field.endswith("/.id"):
                    field, value = field[:-4], int(value) if value else False
                elif field.endswith("/id"):
                    field, value = (
                        field[:-3],
                        self.external_ids[value].odoo_id if value else False,
                    )
                if (model, field) in self.relations and value == "":
                    value = False
                if (model, field) in self.relations and value not in (False, None):
                    assert (
                        self.relations[(model, field)],
                        int(value),
                    ) in self.records, "A child was written before its parent existed"
                row[field] = value
            identifier = 1000 + len(self.records)
            self.records[(model, identifier)] = row
            identifiers.append(identifier)
            if external_ids:
                external_id = external_ids[index]
                assert external_id not in self.external_ids, (
                    "A parent create was duplicated"
                )
                self.external_ids[external_id] = ExternalIdBinding(
                    external_id, model, identifier
                )
                module, _, name = external_id.partition(".")
                self.records[("ir.model.data", len(self.external_ids))] = {
                    "module": module, "name": name, "model": model, "res_id": identifier
                }
        self.writes.append(
            (model, tuple(dict(row) for row in values), tuple(identifiers))
        )
        if self.after_create:
            self.after_create()
        return tuple(identifiers)

    def create_rows(self, model, values):
        return self._create(model, values)

    def load_create_rows(self, model, values, external_ids):
        return self._create(model, values, external_ids)

    def update_row(self, model, identifier, values):
        raise AssertionError("The unchanged hierarchy fixture must not need an update")

    def read_ids(self, model, identifiers, fields):
        self.reads.append((model, tuple(identifiers), tuple(fields)))
        return tuple(
            ReadbackRecord(
                identifier,
                {field: self.value(model, identifier, field) for field in fields},
            )
            for identifier in identifiers
            if (model, identifier) in self.records
        )

    def find_records_many(self, model, lookups):
        assert all(
            "." not in field for lookup in lookups for field, _, _ in lookup.domain
        )
        return tuple(
            self.find_records(model, lookup.domain, lookup.fields) for lookup in lookups
        )

    def find_records(self, model, domain, fields):
        return self.read_ids(model, self.find_ids(model, domain), fields)

    def read_external_ids(self, external_ids):
        return tuple(
            self.external_ids[value]
            for value in external_ids
            if value in self.external_ids
        )

    def category_paths(self):
        def path(identifier):
            row = self.records[("product.category", identifier)]
            return (
                (*path(row["parent_id"]), row["name"])
                if row.get("parent_id")
                else (row["name"],)
            )

        return {
            identifier: path(identifier)
            for model, identifier in self.records
            if model == "product.category"
        }

    def transport(self, url, headers, body, timeout, method):
        """Exercise real JSON-2 adapters against an independent in-memory server."""

        del headers, timeout
        assert method == "POST"
        model, operation = url.split("/json/2/", 1)[1].rsplit("/", 1)
        payload = json.loads(body)
        self.wire_calls.append((model, operation))
        if operation == "search_read":
            fields = payload["fields"]
            assert all("." not in field for field in fields), (
                "Odoo cannot return dotted fields"
            )
            identifiers = sorted(
                identifier
                for record_model, identifier in self.records
                if record_model == model
                and self.matches(model, identifier, payload["domain"])
            )
            return 200, [
                {field: self.value(model, identifier, field) for field in fields}
                for identifier in identifiers[: payload["limit"]]
            ]
        if operation == "create":
            return 200, list(self.create_rows(model, payload["vals_list"]))
        if operation == "load":
            fields = payload["fields"][1:]
            rows = tuple(
                dict(zip(fields, row[1:], strict=True)) for row in payload["data"]
            )
            identifiers = self.load_create_rows(
                model, rows, tuple(row[0] for row in payload["data"])
            )
            return 200, {"ids": list(identifiers), "messages": []}
        raise AssertionError(f"Unexpected JSON-2 operation {operation}")
