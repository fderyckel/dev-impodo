"""Qualify ordinary hierarchy relations through JSON-2 and the local shell."""

import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from impodo.adapters.odoo.connectors import Json2Config, Json2ReadConnector
from impodo.adapters.odoo.local_reader import _preflight_script
from impodo.domain.odoo.company_domains import hierarchy_parent_domain
from impodo.domain.odoo.contracts import ConnectorError, MetadataRequest, RecordRequest
from tests.support.hierarchy_transport import HierarchyParentTransport, odoo_hierarchy_domain


class HierarchyParentDomainTests(unittest.TestCase):
    def test_remote_ordinary_parents_keep_the_native_rule_and_shared_company_scope(
        self,
    ):
        for major in (19, 20):
            for model, parent, company in (
                ("stock.location", "location_id", "company_id"),
                ("x.tree.node", "container", "x_legal_entity"),
            ):
                for expression in (
                    None,
                    "[]",
                    "[('kind', '=', 'folder')]",
                    f"[('{company}', 'in', [False, {company}])]",
                    f"[('{company}', 'in', allowed_company_ids)]",
                    odoo_hierarchy_domain(company),
                    odoo_hierarchy_domain(company, "[('kind', '=', 'folder')]"),
                ):
                    with self.subTest(major=major, model=model, expression=expression):
                        wire = HierarchyParentTransport(
                            major, model, parent, company, expression
                        )
                        reader = Json2ReadConnector(
                            Json2Config(
                                "https://example.test",
                                "migration",
                                "fictional",
                                context={"allowed_company_ids": [7]},
                            ),
                            transport=wire,
                        )
                        captured = reader.get_model_metadata(
                            (MetadataRequest(model, (), all_fields=True),),
                            include_create_defaults=False,
                        )
                        metadata_calls = len(wire.calls)
                        domain = reader.hierarchy_lookup_domain(
                            captured, model, parent, company
                        )
                        self.assertEqual(len(wire.calls), metadata_calls)
                        self.assertEqual(
                            list(domain),
                            hierarchy_parent_domain(expression, 7, company),
                        )
                        reader.get_records(
                            (
                                RecordRequest(
                                    model, ("name", parent, company), domain, 51
                                ),
                            )
                        )
                        request = wire.calls[-1][1]
                        self.assertEqual(request["domain"], list(domain))
                        self.assertEqual(request["context"]["allowed_company_ids"], [7])
                        self.assertEqual(request["limit"], 51)
                        self.assertEqual(request["order"], "id asc")
                        self.assertNotIn("kind", request["fields"])
                        # Existing company-property lookups retain their own
                        # strict contract; the hierarchy does not weaken it.
                        with self.assertRaises(ConnectorError):
                            reader.linked_lookup_domain(model, parent, model)

    def test_remote_unknown_rules_and_invalid_relationships_stop_before_record_reads(
        self,
    ):
        for major in (19, 20):
            for invalid in (
                "unknown_rule",
                "foreign_parent",
                "missing_company",
                "no_company_context",
            ):
                with self.subTest(major=major, invalid=invalid):
                    wire = HierarchyParentTransport(
                        major,
                        domain="[('kind', '=', unknown)]"
                        if invalid == "unknown_rule"
                        else "[]",
                    )
                    if invalid == "foreign_parent":
                        wire.fields[wire.parent]["relation"] = "x.other"
                    if invalid == "missing_company":
                        del wire.fields[wire.company]
                    reader = Json2ReadConnector(
                        Json2Config(
                            "https://example.test",
                            "migration",
                            "fictional",
                            context={}
                            if invalid == "no_company_context"
                            else {"allowed_company_ids": [7]},
                        ),
                        transport=wire,
                    )
                    captured = reader.get_model_metadata(
                        (MetadataRequest(wire.model, (), all_fields=True),),
                        include_create_defaults=False,
                    )
                    with self.assertRaises(ConnectorError):
                        reader.hierarchy_lookup_domain(
                            captured, wire.model, wire.parent, wire.company
                        )
                    self.assertFalse(
                        any(url.endswith("/search_read") for url, _ in wire.calls)
                    )

    def local(self, major, expression, *, hierarchy=True, invalid_company=False):
        wire = HierarchyParentTransport(major, domain=expression)
        env, model = MagicMock(), MagicMock()
        env.company.id, env.cr.dbname = 7, "migration"
        env.__getitem__.side_effect = {wire.model: model}.__getitem__
        model.sudo.return_value = model
        model.with_context.return_value = model
        model._description = "Tree node"
        if invalid_company:
            wire.fields[wire.company]["relation"] = "x.other"
        model.fields_get.side_effect = lambda allfields, attributes: {
            name: wire.fields[name] for name in allfields if name in wire.fields
        }
        model._fields = {
            name: SimpleNamespace(type=field["type"], company_dependent=False)
            for name, field in wire.fields.items()
        }
        model.search_read.return_value = [
            {"id": 11, "name": "Stock", wire.parent: 10, wire.company: 7}
        ]
        script = _preflight_script(
            (MetadataRequest(wire.model, ("name", wire.parent, wire.company)),),
            (
                RecordRequest(
                    wire.model,
                    ("name", wire.parent, wire.company),
                    (("name", "ilike", "Stock"),),
                    51,
                ),
            ),
            validate_lookup_fields=True,
            lookup_relationship=(wire.model, wire.parent),
            hierarchy_company_field=wire.company if hierarchy else None,
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
                exec(script, {"env": env})  # noqa: S102 -- Exercise the fixed, isolated Odoo shell program.
            except SystemExit as error:
                self.assertEqual(error.code, 0)
        env.cr.rollback.assert_called_once()
        return model, json.loads(
            output.getvalue().split("__IMPODO_LOCAL_ODOO_JSON__")[1]
        )

    def test_local_scope_matches_remote_and_preserves_the_company_property_guard(self):
        for major in (19, 20):
            for expression in (
                None,
                "[]",
                "[('kind', '=', 'folder')]",
                "[('x_legal_entity', 'in', [False, x_legal_entity])]",
                "[('x_legal_entity', 'in', allowed_company_ids)]",
                odoo_hierarchy_domain("x_legal_entity"),
                odoo_hierarchy_domain("x_legal_entity", "[('kind', '=', 'folder')]"),
                "x_legal_entity and [('kind', '=', 'folder')] or unknown",
                "x_legal_entity and [('kind', '=', 'folder')] or dangerous()",
                "[('kind', '=', unknown)]",
                "__import__('os')",
            ):
                with self.subTest(major=major, expression=expression):
                    model, payload = self.local(major, expression)
                    expected = hierarchy_parent_domain(expression, 7, "x_legal_entity")
                    if expected is None:
                        self.assertIn("lookup_error", payload)
                        model.search_read.assert_not_called()
                    else:
                        self.assertNotIn("lookup_error", payload)
                        self.assertEqual(
                            model.search_read.call_args.args[0],
                            [["name", "ilike", "Stock"], *expected],
                        )
                        self.assertEqual(
                            model.search_read.call_args.kwargs["limit"], 51
                        )
            for options in ({"hierarchy": False}, {"invalid_company": True}):
                model, payload = self.local(major, "[]", **options)
                self.assertIn("lookup_error", payload)
                model.search_read.assert_not_called()
