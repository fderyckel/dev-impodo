"""Exercise the production load and recovery lookups across Odoo wire behaviors."""

import json
import unittest
from itertools import product

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.execution.odoo_readback import OdooReadbackError, ReadbackLookup
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.execution.odoo_write import OdooWriteRejected


class NestedCategoryLookupTests(unittest.TestCase):
    def test_load_and_recovery_match_the_reviewed_parent_path(self):
        for major, mode in product((19, 20), ("LOCAL", "REMOTE")):
            with self.subTest(major=major, mode=mode):
                self._check_lookups(major, mode)

    def _check_lookups(self, major, mode):
        model = "product.category"
        parents = {
            10: ("Services", "PRD-CAT / Services"),
            11: ("Services", "Other / Services"),
            12: ("Tools / Service", "Tools / Service"),
            13: ("PRD-CAT", "PRD-CAT"),
        }
        categories = (
            (41, "S0023", 10),
            (42, "S0023", 11),
            (43, "Extra", 12),
            (44, "Repeated", 10),
            (45, "Repeated", 10),
            (46, "Box", 13),
        )
        calls = []

        def transport(url, _headers, body, _timeout, method):
            self.assertTrue(url.endswith(f"/{model}/search_read"))
            self.assertEqual(method, "POST")
            payload = json.loads(body)
            calls.append(payload)

            def matches(category):
                _, name, parent_id = category
                parent_name, parent_label = parents[parent_id]
                conditions = iter(payload["domain"])

                def evaluate(condition):
                    if condition in ("&", "|"):
                        left = evaluate(next(conditions))
                        right = evaluate(next(conditions))
                        return (left and right) if condition == "&" else (left or right)
                    field, operator, value = condition
                    if field == "parent_id.name":
                        self.assertEqual(operator, "in")
                        return parent_name in value
                    self.assertEqual(operator, "=")
                    if field == "parent_id":
                        if type(value) is int:
                            return parent_id == value
                        # Odoo 20 looks up category names, while the returned
                        # many2one pair still carries the full display path.
                        return value == (parent_name if major == 20 else parent_label)
                    self.assertEqual(field, "name")
                    return name == value

                result = [evaluate(condition) for condition in conditions]
                return all(result)

            rows = [
                {"id": identifier, "name": name, "parent_id": [parent, parents[parent][1]]}
                for identifier, name, parent in categories
                if matches((identifier, name, parent))
            ]
            return 200, rows[:payload["limit"]]

        config = Json2Config(
            base_url="http://127.0.0.1:8069" if mode == "LOCAL" else "https://odoo.example.test",
            database=f"odoo{major}_fictional",
            api_key="fixture",
            connection_mode=mode,
            retries=0,
        )
        scope = OdooApiScope("sha256:" + "1" * 64, (
            OdooModelScope(model, read_fields=("name",), lookup_fields=("name", "parent_id")),
        ))
        writer = Json2WriteExecutor(config, scope, transport=transport)
        reader = Json2ReadbackReader(config, scope, transport=transport)
        domains = tuple(
            (("name", "=", name), ("parent_id", "=", parent))
            for name, parent in (
                ("S0023", "PRD-CAT / Services"),
                ("S0023", "Missing / Services"),
                ("Extra", "Tools / Service"),
                ("Repeated", "PRD-CAT / Services"),
                ("Box", "PRD-CAT"),
                ("S0023", 11),
            )
        )
        expected = ((41,), (), (43,), (44, 45), (46,), (42,))
        self.assertEqual(writer.find_ids_many(model, domains), expected)
        readback = reader.find_records_many(
            model, tuple(ReadbackLookup(domain, ("name",)) for domain in domains)
        )
        self.assertEqual(tuple(tuple(row.odoo_id for row in rows) for rows in readback), expected)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call["fields"], ["id", "name", "parent_id"])
            self.assertEqual(call["context"]["active_test"], False)

    def test_truncated_category_candidates_cannot_prove_a_unique_match(self):
        model = "product.category"
        config = Json2Config("https://odoo.example.test", "fictional", "fixture", retries=0)
        scope = OdooApiScope("sha256:" + "1" * 64, (
            OdooModelScope(model, lookup_fields=("name", "parent_id")),
        ))
        domain = (("name", "=", "S0023"), ("parent_id", "=", "PRD-CAT / Services"))

        def transport(_url, _headers, body, _timeout, _method):
            limit = json.loads(body)["limit"]
            self.assertEqual(limit, 501)
            return 200, [
                {"id": identifier, "name": "S0023", "parent_id": [10, "PRD-CAT / Services"]}
                for identifier in range(1, limit + 1)
            ]

        writer = Json2WriteExecutor(config, scope, transport=transport)
        reader = Json2ReadbackReader(config, scope, transport=transport)
        with self.assertRaises(OdooWriteRejected):
            writer.find_ids_many(model, (domain,))
        with self.assertRaises(OdooReadbackError):
            reader.find_records_many(model, (ReadbackLookup(domain),))
