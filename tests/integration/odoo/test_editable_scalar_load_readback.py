"""Simulate native writes and read-back for an approved scalar projection."""

import json
import unittest

from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.readback import Json2ReadbackReader
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.application.workspace.execution.service import execution_api_scope
from tests.integration.odoo.test_editable_scalar_transfer import _TransferScenario


class EditableScalarNativeLoadTests(unittest.TestCase):
    def test_approved_projection_reaches_native_load_and_exact_readback(self):
        for major in (19, 20):
            for mode in ("REMOTE", "LOCAL"):
                with self.subTest(major=major, mode=mode):
                    scenario = _TransferScenario(major, known_inverse=mode == "LOCAL")
                    _, _, snapshot = scenario.review_and_compile(scenario.match())
                    scope = execution_api_scope(snapshot)
                    created = next(row for row in snapshot.rows if row.source_row == 2)
                    values = {field.field: field.value for field in created.fields}
                    requests = []
                    stored = {}

                    def transport(
                        url,
                        headers,
                        body,
                        timeout,
                        method,
                        *,
                        stored=stored,
                        requests=requests,
                    ):
                        del headers, timeout
                        payload = json.loads(body)
                        self.assertEqual(method, "POST")
                        self.assertEqual(
                            payload["context"]["allowed_company_ids"], [91]
                        )
                        requests.append((url.rsplit("/", 1)[-1], payload))
                        if url.endswith("/load"):
                            self.assertEqual(
                                payload["fields"], ["id", "code", "kind", "name"]
                            )
                            self.assertEqual(
                                payload["data"][0][1:], ["001002", "asset", "New"]
                            )
                            stored.update(
                                dict(
                                    zip(
                                        payload["fields"][1:],
                                        payload["data"][0][1:],
                                        strict=True,
                                    )
                                )
                            )
                            return 200, {"ids": [901], "messages": []}
                        if url.endswith("/create"):
                            self.assertEqual(
                                payload["vals_list"],
                                [{"code": "001002", "kind": "asset", "name": "New"}],
                            )
                            stored.update(payload["vals_list"][0])
                            return 200, [901]
                        self.assertTrue(url.endswith("/search_read"))
                        self.assertEqual(payload["domain"], [["id", "in", [901]]])
                        return 200, [{"id": 901, **stored}]

                    config = Json2Config(
                        base_url=(
                            "http://127.0.0.1:8069"
                            if mode == "LOCAL"
                            else "https://destination.example.test"
                        ),
                        database="destination",
                        api_key="destination-secret",
                        connection_mode=mode,
                        context={"allowed_company_ids": [91]},
                        retries=0,
                    )
                    writer = Json2WriteExecutor(config, scope, transport=transport)
                    identifiers = (
                        writer.load_create_rows(
                            scenario.model, (values,), (created.proposed_external_id,)
                        )
                        if mode == "REMOTE"
                        else writer.create_rows(scenario.model, (values,))
                    )
                    reader = Json2ReadbackReader(config, scope, transport=transport)
                    actual = reader.read_ids(
                        scenario.model, identifiers, ("code", "kind", "name")
                    )
                    self.assertEqual(
                        actual[0].values,
                        {"code": "001002", "kind": "asset", "name": "New"},
                    )
                    self.assertEqual(
                        [method for method, _ in requests],
                        ["load" if mode == "REMOTE" else "create", "search_read"],
                    )
                    stored["code"] = "001009"
                    changed = reader.read_ids(
                        scenario.model, identifiers, ("code", "kind", "name")
                    )
                    self.assertNotEqual(changed[0].values["code"], values["code"])


if __name__ == "__main__":
    unittest.main()
