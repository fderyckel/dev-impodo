"""Apply live field eligibility before record reads on both Odoo transports."""

import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from impodo.adapters.odoo.connectors import Json2ReadConnector
from impodo.adapters.odoo.local_reader import _preflight_script
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.company_domains import linked_value_domain
from impodo.domain.odoo.contracts import ConnectorError, MetadataRequest, RecordRequest
from impodo.domain.shared.models import OdooReadIdentity, target_identity_hash
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode, WorkspaceState
from impodo.web.composition.target_readers import _read_supporting_lookup_snapshots

DOMAIN = "[('company_ids', 'parent_of', [allowed_company_ids[0]])] + [('account_type', '=', 'liability_payable')]"


class LinkedLookupDomainTests(unittest.TestCase):
    def remote(self, major, domain=DOMAIN, *, missing=False, custom=False):
        parent, related, edge = (
            ("x.owner", "x.choice", "x_choice_id")
            if custom
            else ("res.partner", "account.account", "property_account_payable_id")
        )
        target = target_identity_hash(
            connection_mode="remote",
            base_url="https://example.test",
            database="migration",
        )
        company = OdooCompanySelection(target, 105, "Example Wiltz")
        state = WorkspaceState(
            "lookup",
            "Lookup",
            "CSV",
            odoo_connection_mode=OdooConnectionMode.REMOTE,
            odoo_base_url="https://example.test",
            odoo_database="migration",
            intended_models=(parent,),
            odoo_company=company,
        )
        schema = SimpleNamespace(
            connection_target_hash=target,
            read_credential_binding_hash="binding",
            read_principal_hash="principal",
            read_permission_hash="permissions",
            read_context_hash=company.context_hash,
        )
        identity = OdooReadIdentity(
            target,
            "principal",
            "permissions",
            company.context_hash,
            (related,),
            "2026-11-12T10:00:00Z",
        )
        context = SimpleNamespace(
            readiness_reader=None,
            secret_store=None,
            target_credential_workspace=lambda *args, **kwargs: state,
            read_identity_probe=lambda *args: identity,
        )
        calls = []

        def transport(url, headers, body, timeout, method):
            payload = json.loads(body) if body else {}
            calls.append((url, payload))
            if url.endswith("/web/version"):
                return 200, {"version": f"{major}.0"}
            self.assertEqual(payload["context"]["allowed_company_ids"], [105])
            if url.endswith(parent + "/fields_get"):
                return 200, {
                    edge: {
                        "type": "many2one",
                        "relation": related,
                        "company_dependent": True,
                        "domain": domain,
                    }
                }
            if url.endswith(related + "/fields_get"):
                fields = {
                    "code": {"type": "char"},
                    "name": {"type": "char"},
                    "company_ids": {"type": "many2many", "relation": "res.company"},
                    "company_id": {"type": "many2one", "relation": "res.company"},
                    "account_type": {"type": "selection"},
                }
                return 200, {
                    name: fields[name]
                    for name in payload["allfields"]
                    if name in fields and not (missing and name == "code")
                }
            if url.endswith(related + "/search_read"):
                return 200, [{"id": 7, "code": "440000", "name": "Suppliers"}]
            self.fail(url)

        with (
            patch(
                "impodo.web.composition.target_readers.get_target_credential",
                return_value=SimpleNamespace(
                    binding_hash="binding", secret="fictional"
                ),
            ),
            patch(
                "impodo.web.composition.target_readers.Json2ReadConnector",
                side_effect=lambda config: Json2ReadConnector(
                    config, transport=transport
                ),
            ),
        ):
            try:
                result = _read_supporting_lookup_snapshots(
                    context,
                    state,
                    schema,
                    actor=SimpleNamespace(),
                    relation_model=related,
                    requested_fields=("code", "name"),
                    record_requests=(
                        RecordRequest(
                            related, ("code", "name"), (("code", "=", "440000"),), 2001
                        ),
                    ),
                    lookup_relationship=(parent, edge),
                )
            except (ConnectorError, WorkspaceError) as error:
                return calls, error
        return calls, result

    def test_remote_predicates_fields_and_bounds_follow_live_metadata(self):
        for major in (19, 20):
            for custom in (False, True):
                with self.subTest(major=major, custom=custom):
                    calls, result = self.remote(major, custom=custom)
                    self.assertNotIsInstance(result, Exception, str(result))
                    reads = [
                        payload
                        for url, payload in calls
                        if url.endswith("/search_read")
                    ]
                    self.assertEqual(len(reads), 1)
                    self.assertEqual(
                        reads[0]["domain"],
                        [
                            ["code", "=", "440000"],
                            ["company_ids", "parent_of", [105]],
                            ["account_type", "=", "liability_payable"],
                        ],
                    )
                    self.assertEqual(reads[0]["fields"], ["id", "code", "name"])
                    self.assertEqual(reads[0]["limit"], 500)
                    self.assertEqual(reads[0]["order"], "id asc")
                    self.assertTrue(calls[-2][0].endswith("/fields_get"))

    def test_remote_rejects_missing_fields_and_unsupported_rules_before_records(self):
        for major in (19, 20):
            for missing, domain in (
                (True, DOMAIN),
                (False, "[('company_ids', '=', unknown)]"),
            ):
                with self.subTest(major=major, missing=missing):
                    calls, result = self.remote(major, domain, missing=missing)
                    self.assertIsInstance(result, Exception)
                    self.assertFalse(
                        any(url.endswith("/search_read") for url, _ in calls)
                    )
                    if missing:
                        self.assertIsInstance(result, WorkspaceError)

    def test_custom_single_company_rule_keeps_shared_choices(self):
        domain = "['|', ('company_id', '=', False), ('company_id', 'in', [allowed_company_ids[0]])]"
        for major in (19, 20):
            calls, result = self.remote(major, domain, custom=True)
            self.assertNotIsInstance(result, Exception, str(result))
            read = next(
                payload for url, payload in calls if url.endswith("/search_read")
            )
            self.assertEqual(read["domain"][1:], linked_value_domain(domain, 105))

    def local(self, major, domain=DOMAIN, *, missing=False):
        env = MagicMock()
        env.company.id = 105
        env.cr.dbname = "migration"
        parent, related = MagicMock(), MagicMock()
        env.__getitem__.side_effect = {
            "res.partner": parent,
            "account.account": related,
        }.__getitem__
        parent.fields_get.return_value = {
            "property_account_payable_id": {
                "type": "many2one",
                "relation": "account.account",
                "company_dependent": True,
                "domain": domain,
            }
        }
        related.sudo.return_value = related
        related.with_context.return_value = related
        related._description = "Account"
        fields = {
            "code": {"type": "char"},
            "name": {"type": "char"},
            "company_ids": {"type": "many2many"},
            "company_id": {"type": "many2one"},
            "account_type": {"type": "selection"},
        }
        related.fields_get.side_effect = lambda allfields, attributes: {
            name: fields[name]
            for name in allfields
            if name in fields and not (missing and name == "code")
        }
        related.default_get.return_value = {}
        related._fields = {
            name: SimpleNamespace(type=details["type"], company_dependent=False)
            for name, details in fields.items()
        }
        related.search_read.return_value = [
            {"id": 7, "code": "440000", "name": "Suppliers"}
        ]
        script = _preflight_script(
            (MetadataRequest("account.account", ("code", "name")),),
            (
                RecordRequest(
                    "account.account",
                    ("code", "name"),
                    (("code", "=", "440000"),),
                    2001,
                ),
            ),
            validate_lookup_fields=True,
            lookup_relationship=("res.partner", "property_account_payable_id"),
        )
        output = StringIO()
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
            try:
                exec(script, {"env": env})  # noqa: S102 -- Exercise the fixed Odoo shell program against a fake environment.
            except SystemExit as error:
                self.assertEqual(error.code, 0)
        payload = json.loads(output.getvalue().split("__IMPODO_LOCAL_ODOO_JSON__")[1])
        env.cr.rollback.assert_called_once()
        return related, payload

    def test_local_uses_the_same_domain_and_rejects_before_reading(self):
        for major in (19, 20):
            for domain in (
                DOMAIN,
                "[('company_id', 'in', [allowed_company_ids[0], False])] + []",
                "[('company_ids', '=', unknown)]",
                "['|', ('name', '=', 'a')]",
                "__import__('os')",
            ):
                with self.subTest(major=major, domain=domain):
                    model, payload = self.local(major, domain)
                    expected = linked_value_domain(domain, 105)
                    if expected is None:
                        self.assertIn("lookup_error", payload)
                        model.search_read.assert_not_called()
                    else:
                        self.assertNotIn("lookup_error", payload)
                        self.assertEqual(
                            model.search_read.call_args.args[0],
                            [["code", "=", "440000"], *expected],
                        )
            model, payload = self.local(major, missing=True)
            self.assertIn("lookup_error", payload)
            model.search_read.assert_not_called()
