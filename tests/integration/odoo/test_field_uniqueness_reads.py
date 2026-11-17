"""Both Odoo transports preserve the independent bounded uniqueness lookups."""

from contextlib import redirect_stdout
from io import StringIO
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    target_record_read_config,
)
from impodo.adapters.odoo.local_reader import _preflight_script
from impodo.domain.execution.planner import plan_preflight_requirements
from impodo.domain.preparation.preflight import PreflightEngine
from impodo.domain.preparation.source import PreparedBundle
from impodo.domain.shared.models import Classification
from tests.domain.preparation.test_field_uniqueness import plan, record


class FieldUniquenessReadTests(unittest.TestCase):
    def test_remote_value_queries_include_inactive_records_and_preserve_company_context(
        self,
    ):
        for major in (19, 20):
            with self.subTest(major=major):
                calls = []

                def transport(url, headers, body, timeout, method):
                    payload = json.loads(body) if body else None
                    if url.endswith("/web/version"):
                        return 200, {"version": f"{major}.0"}
                    if url.endswith("/fields_get"):
                        return 200, {"ref": {"type": "char"}, "vat": {"type": "char"}}
                    calls.append(payload)
                    if payload["domain"][0][0] == "vat":
                        return 200, [{"id": 7, "ref": "OTHER", "vat": "VAT-1"}]
                    return 200, []

                compiled, prepared = plan(), (record("NEW", "VAT-1"),)
                requirements = plan_preflight_requirements(compiled, prepared)
                connector = Json2ReadConnector(
                    target_record_read_config(
                        Json2Config(
                            "https://example.test",
                            "fictional",
                            "fictional-key",
                            context={"allowed_company_ids": [105]},
                        )
                    ),
                    transport=transport,
                )
                metadata = connector.get_model_metadata(requirements.metadata_requests)
                captured = connector.get_records(requirements.record_requests)
                result = PreflightEngine().run(
                    compiled, PreparedBundle(prepared, {}, ()), metadata, captured
                )
                self.assertEqual(
                    result.decisions[0].classification, Classification.BLOCKED
                )
                self.assertIn(
                    "TARGET_FIELD_NOT_UNIQUE",
                    {issue.code for issue in result.decisions[0].issues},
                )
                self.assertEqual(len(calls), 2)
                self.assertTrue(
                    all(call["context"]["active_test"] is False for call in calls)
                )
                self.assertTrue(
                    all(
                        call["context"]["allowed_company_ids"] == [105]
                        for call in calls
                    )
                )
                self.assertTrue(all(call["limit"] == 500 for call in calls))
                self.assertEqual(
                    next(
                        call["domain"]
                        for call in calls
                        if call["domain"][0][0] == "vat"
                    ),
                    [["vat", "in", ["VAT-1"]]],
                )

    def test_local_shell_uses_the_same_selective_queries_for_both_majors(self):
        requirements = plan_preflight_requirements(plan(), (record("NEW", "VAT-1"),))
        for major in (19, 20):
            with self.subTest(major=major):
                env, model = MagicMock(), MagicMock()
                env.cr.dbname = "fictional"
                env.__getitem__.return_value = model
                model.sudo.return_value = model
                model.with_context.return_value = model
                model._description = "Customer"
                model.fields_get.return_value = {
                    "ref": {"type": "char"},
                    "vat": {"type": "char"},
                }
                model.default_get.return_value = {}
                model._fields = {
                    name: SimpleNamespace(type="char") for name in ("ref", "vat")
                }
                model.search_read.return_value = []
                output = StringIO()
                script = _preflight_script(
                    requirements.metadata_requests, requirements.record_requests
                )
                with (
                    patch.dict(
                        sys.modules,
                        {
                            "odoo": SimpleNamespace(
                                release=SimpleNamespace(
                                    version=f"{major}.0",
                                    version_info=(major, 0, 0, "final", 0, ""),
                                )
                            )
                        },
                    ),
                    redirect_stdout(output),
                ):
                    exec(script, {"env": env})  # noqa: S102 -- Fixed application shell code runs against a fake Odoo environment.
                domains = [call.args[0] for call in model.search_read.call_args_list]
                self.assertIn([["vat", "in", ["VAT-1"]]], domains)
                self.assertEqual(len(domains), 2)
                model.with_context.assert_called_with(active_test=False)
                env.cr.rollback.assert_called_once()
