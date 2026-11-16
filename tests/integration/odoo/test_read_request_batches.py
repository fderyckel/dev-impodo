"""Exercise byte-bounded comparison requests through the real JSON-2 reader."""

import json
from dataclasses import replace
from unittest import TestCase

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.adapters.odoo.read_request_batches import bounded_record_requests
from impodo.application.odoo_read_failures import classify_odoo_read_failure
from impodo.domain.odoo.contracts import (
    ConnectorIncompleteResultError,
    ConnectorRequestSizeError,
    RecordRequest,
)
from impodo.domain.shared.models import canonical_json_bytes


def expression_matches(values, domain):
    """Evaluate Odoo prefix expressions independently of the batching helper."""

    tokens = iter(domain)

    def consume(token):
        if token in ("&", "|"):
            left, right = consume(next(tokens)), consume(next(tokens))
            return left and right if token == "&" else left or right
        if token == "!":
            return not consume(next(tokens))
        field, operator, expected = token
        actual = values.get(field, False)
        if operator == "=":
            return actual == expected
        if operator == "in":
            return actual in expected
        raise AssertionError(operator)

    results = [consume(token) for token in tokens]
    return all(results)


def hierarchy_search():
    records, expressions = [], []
    for number in range(201):
        values = {
            "id": number + 1,
            "name": f"Child {number:03d}",
            "company_id.name": "Example",
            "parent_id.name": f"Parent {number // 10:03d}",
            "parent_id.company_id.name": "Example",
            "parent_id.parent_id.name": "Storage " + "é" * 120,
            "parent_id.parent_id.company_id.name": "Example",
            "parent_id.parent_id.parent_id.name": "Root",
            "parent_id.parent_id.parent_id.company_id.name": "Example",
            "parent_id.parent_id.parent_id.parent_id": False,
        }
        records.append(values)
        leaves = [
            [field, "=", value] for field, value in values.items() if field != "id"
        ]
        expressions.append(["&"] * (len(leaves) - 1) + leaves)
    domain = tuple(
        ["|"] * (len(expressions) - 1) + [item for part in expressions for item in part]
    )
    return records, domain


class ReadRequestBatchTests(TestCase):
    def read(
        self, major, mode, request, records, *, maximum_bytes=65536, conflict=False
    ):
        calls = []
        requests_seen = set()

        def transport(url, _headers, body, _timeout, method):
            if url.endswith("/web/version"):
                return 200, {"version": f"{major}.0"}
            self.assertEqual(method, "POST")
            self.assertTrue(url.endswith("/search_read"))
            self.assertLessEqual(len(body), maximum_bytes)
            payload = json.loads(body)
            calls.append(payload)
            self.assertEqual(payload["context"], {"allowed_company_ids": [7]})
            matching = [
                row for row in records if expression_matches(row, payload["domain"])
            ]
            rows = matching[payload["offset"] : payload["offset"] + payload["limit"]]
            if conflict and rows:
                rows = [
                    dict(row, name="Changed") if row["id"] in requests_seen else row
                    for row in rows
                ]
            requests_seen.update(row["id"] for row in rows)
            return 200, [
                {field: row[field] for field in payload["fields"]} for row in rows
            ]

        config = Json2Config(
            base_url="https://odoo.example.test"
            if mode == "REMOTE"
            else "http://127.0.0.1:8069",
            database="fictional",
            api_key="fictional-read-key",
            connection_mode=mode,
            page_size=37,
            max_request_bytes=maximum_bytes,
            context={"allowed_company_ids": [7]},
        )
        result = Json2ReadConnector(config, transport=transport).get_records((request,))
        return result, calls

    def test_201_hierarchical_records_complete_for_both_majors_and_modes(self):
        records, domain = hierarchy_search()
        self.assertGreater(len(canonical_json_bytes(domain)), 65536)
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    request = RecordRequest("x.tree.node", ("name",), domain)
                    snapshot, calls = self.read(major, mode, request, records)
                    self.assertEqual(
                        [row.odoo_id for row in snapshot.records[request.model]],
                        list(range(1, 202)),
                    )
                    self.assertGreater(len(calls), 1)
                    self.assertLessEqual(len(calls), 12)
                    self.assertTrue(all(call["domain"] for call in calls))

    def test_conjunction_negation_and_implicit_scope_keep_the_same_results(self):
        records, domain = hierarchy_search()
        records = [
            dict(row, active=row["id"] % 2 == 0, private=row["id"] % 3 == 0)
            for row in records
        ]
        scoped = (["active", "=", True], "&", "!", ["private", "=", True], *domain)
        snapshot, _calls = self.read(
            20, "REMOTE", RecordRequest("product.category", ("name",), scoped), records
        )
        expected = [row["id"] for row in records if expression_matches(row, scoped)]
        self.assertEqual(
            [row.odoo_id for row in snapshot.records["product.category"]], expected
        )

    def test_large_in_values_are_split_without_changing_a_request_limit(self):
        records = [{"id": i + 1, "name": f"{i:03d}" + "漢" * 150} for i in range(201)]
        domain = (["name", "in", [row["name"] for row in records]],)
        request = RecordRequest("account.group", ("name",), domain, limit=11)
        result, calls = self.read(19, "LOCAL", request, records)
        self.assertEqual(
            [row.odoo_id for row in result.records[request.model]], list(range(1, 12))
        )
        self.assertGreater(len(calls), 1)

    def test_overlapping_partitions_merge_identical_records_and_reject_conflicts(self):
        records, domain = hierarchy_search()
        overlap = ("|", *domain, *domain)
        request = RecordRequest("x.tree.node", ("name",), overlap)
        result, _calls = self.read(20, "REMOTE", request, records)
        self.assertEqual(len(result.records[request.model]), 201)
        with self.assertRaises(ConnectorIncompleteResultError):
            self.read(20, "REMOTE", request, records, conflict=True)

    def test_small_requests_and_external_identity_contract_are_preserved(self):
        request = RecordRequest("x.tree.node", ("name",), (["name", "=", "One"],))
        self.assertEqual(
            bounded_record_requests(
                request, context={}, page_size=500, maximum_bytes=65536
            ),
            (request,),
        )
        external = replace(request, external_ids=("fictional.one",))
        self.assertEqual(
            bounded_record_requests(
                external, context={}, page_size=500, maximum_bytes=1
            ),
            (external,),
        )

    def test_an_indivisible_or_negated_request_has_a_size_error_not_connection_error(
        self,
    ):
        _records, domain = hierarchy_search()
        for selected in ((["name", "=", "x" * 70000],), ("!", *domain)):
            with self.subTest(negated=selected[0] == "!"):
                with self.assertRaises(ConnectorRequestSizeError) as caught:
                    self.read(
                        20,
                        "REMOTE",
                        RecordRequest("x.tree.node", ("name",), selected),
                        [],
                    )
                failure = classify_odoo_read_failure(caught.exception)
                self.assertEqual(failure.code.value, "ODOO_READ_REQUEST_TOO_LARGE")
                self.assertEqual(failure.owner.value, "SUPPORT")
                self.assertNotIn("70000", str(caught.exception))
