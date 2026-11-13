"""Serve ordinary self-parent metadata through the real JSON-2 read adapter."""

import json


def odoo_hierarchy_domain(company="company_id", rule="[]"):
    """Use the check_company wrapper emitted by Odoo 19/20 fields_get.

    Source: odoo/orm/fields_relational.py::_Relational._description_domain.
    The ordinary-field branch uses and/or; company-property fields do not.
    """
    return (
        f"({company} and [('{company}', 'in', [{company}] + [False])] "
        f"or [('{company}', '=', False)]) + ({rule})"
    )


class HierarchyParentTransport:
    def __init__(
        self,
        major,
        model="x.tree.node",
        parent="container",
        company="x_legal_entity",
        domain=None,
        before_call=lambda: None,
    ):
        self.major, self.model, self.parent, self.company = (
            major,
            model,
            parent,
            company,
        )
        self.before_call, self.calls = before_call, []
        self.fields = {
            "name": {"type": "char"},
            parent: {
                "type": "many2one",
                "relation": model,
                "company_dependent": False,
                "domain": domain,
            },
            company: {"type": "many2one", "relation": "res.company"},
            "active": {"type": "boolean"},
            "kind": {"type": "char"},
        }

    def __call__(self, url, headers, body, timeout, method):
        self.before_call()
        payload = json.loads(body) if body else {}
        self.calls.append((url, payload))
        if url.endswith("/web/version"):
            return 200, {"version": f"{self.major}.0"}
        if url.endswith(self.model + "/fields_get"):
            requested = payload["allfields"] or self.fields
            return 200, {
                name: self.fields[name] for name in requested if name in self.fields
            }
        if url.endswith("res.company/search_read"):
            return 200, [{"id": 7, "name": "Example"}]
        if url.endswith(self.model + "/search_read"):
            ancestor = ["id", "in", [10]] in payload["domain"]
            return 200, [
                {
                    "id": 10 if ancestor else 11,
                    "name": "Physical" if ancestor else "Stock",
                    self.parent: False if ancestor else 10,
                    self.company: False if ancestor else 7,
                    "active": True,
                    "kind": "folder",
                }
            ]
        raise AssertionError(f"Unexpected Odoo method: {url}")
