"""Stateful JSON-2 boundary for synthetic Morrow customer qualification.

Only the remote boundary is replaced. Actual source, mapping, preparation,
comparison, writer, journal and read-back services remain under test.
Odoo stock field authority (both reviewed independently):
https://raw.githubusercontent.com/odoo/odoo/19.0/addons/account/models/account_payment_term.py
https://raw.githubusercontent.com/odoo/odoo/20.0/addons/account/models/account_payment_term.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from impodo.adapters.duckdb.unit_of_work import _read_databases
from tests.support.hierarchy_qualification_wire import QualificationWire


def stock_fields(major):
    """Return a fresh captured-metadata analogue for the exact qualified major."""
    if major not in (19, 20):
        raise ValueError("Only reviewed Odoo 19 and Odoo 20 models are available")
    char = lambda required=False: {"type": "char", "required": required}
    relation = lambda model, required=False: {
        "type": "many2one",
        "relation": model,
        "required": required,
    }
    fields = {
        "res.company": {"name": char(True), "parent_id": relation("res.company")},
        "res.country": {"code": char(True), "name": char(True)},
        "res.lang": {"code": char(True), "name": char(True)},
        "res.partner": {
            "ref": char(),
            "name": char(True),
            "email": char(),
            "phone": char(),
            "street": char(),
            "zip": char(),
            "city": char(),
            "vat": char(),
            "active": {"type": "boolean"},
            "company_id": relation("res.company"),
            "country_id": relation("res.country"),
            "lang": {
                "type": "selection",
                "selection": [["en_US", "English"], ["fr_FR", "French"]],
            },
            "property_payment_term_id": relation("account.payment.term"),
            "property_supplier_payment_term_id": relation("account.payment.term"),
            "property_account_position_id": relation("account.fiscal.position"),
        },
        "account.payment.term": {
            "name": char(True),
            "company_id": relation("res.company"),
            "active": {"type": "boolean"},
            "sequence": {"type": "integer", "required": True},
            "line_ids": {
                "type": "one2many",
                "relation": "account.payment.term.line",
                "relation_field": "payment_id",
            },
        },
        "account.payment.term.line": {
            "payment_id": relation("account.payment.term", True),
            "value": {
                "type": "selection",
                "required": True,
                "selection": [["percent", "Percent"], ["fixed", "Fixed"]],
            },
            "value_amount": {"type": "float", "digits": [16, 2]},
            "nb_days": {"type": "integer"},
            "delay_type": {
                "type": "selection",
                "required": True,
                "selection": [
                    ["days_after", "Days after invoice date"],
                    ["days_after_end_of_month", "Days after end of month"],
                    ["days_after_end_of_next_month", "Days after end of next month"],
                    ["days_end_of_month_on_the", "Days end of month on the"],
                ],
            },
        },
        "account.fiscal.position": {
            "name": char(True),
            "company_id": {**relation("res.company", True), "readonly": True},
            "active": {"type": "boolean"},
        },
    }
    # These reviewed majors currently have the same fields. Each call builds
    # its own metadata; callers bind it to their exact version and policy hash.
    return fields


class PracticeCustomerTarget(QualificationWire):
    """Store independent initial seed state and enforce stock default schedules."""

    def __init__(self, seed_path, *, major=19, mode="REMOTE", company="NORTH"):
        super().__init__(major=major, mode=mode)
        if company not in {"NORTH", "SOUTH"}:
            raise ValueError("Unknown synthetic operation company")
        self.fields = stock_fields(major)
        self.relations = {
            (model, name): field["relation"]
            for model, fields in self.fields.items()
            for name, field in fields.items()
            if field.get("relation")
        }
        self.records.clear()
        self.module_names = ("base", "contacts", "account")
        self.company_ids = {"NORTH": 9, "SOUTH": 19}
        self.operation_company = self.company_ids[company]
        self.lose_next_response = None
        self.before_update = None
        self.after_update = None
        self.updates = []
        seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
        for row in seed["companies"]:
            self.records[("res.company", self.company_ids[row["company_code"]])] = {
                "name": row["name"],
                "parent_id": False,
            }
        for identifier, code, name in ((41, "BE", "Belgium"), (42, "FR", "France")):
            self.records[("res.country", identifier)] = {"code": code, "name": name}
        for identifier, code, name in (
            (71, "en_US", "English"),
            (72, "fr_FR", "French"),
        ):
            self.records[("res.lang", identifier)] = {"code": code, "name": name}
        term_ids = {}
        for offset, row in enumerate(seed["payment_terms"], 51):
            term_ids[(row["company_code"], row["term_code"])] = offset
            self.records[("account.payment.term", offset)] = {
                "name": row["name"],
                "company_id": self.company_ids[row["company_code"]],
                "active": True,
                "sequence": 10,
            }
            self.records[("account.payment.term.line", 200 + offset)] = {
                "payment_id": offset,
                "value": "percent",
                "value_amount": 100.0,
                "nb_days": row["balance_line"]["days"],
                "delay_type": "days_after",
            }
        fiscal_ids = {}
        for identifier, row in enumerate(seed["fiscal_positions"], 81):
            fiscal_ids[(row["company_code"], row["name"])] = identifier
            self.records[("account.fiscal.position", identifier)] = {
                "name": row["name"],
                "company_id": self.company_ids[row["company_code"]],
                "active": True,
            }
        for identifier, row in enumerate(seed["customers"], 101):
            address = row["address"]
            self.records[("res.partner", identifier)] = {
                "ref": row["customer_ref"],
                "name": row["name"],
                "email": row["email"] or False,
                "phone": row["phone"],
                "street": address["street"],
                "zip": address["postcode"],
                "city": address["city"],
                "vat": False,
                "active": True,
                "company_id": False,
                "country_id": 41 if row["country_code"] == "BE" else 42,
                "lang": "fr_FR" if row["language_label"] == "French" else "en_US",
                "property_payment_term_id": term_ids[tuple(row["payment_term_key"])],
                "property_supplier_payment_term_id": False,
                "property_account_position_id": fiscal_ids[
                    tuple(row["fiscal_position_key"])
                ],
            }

    def value(self, model, identifier, field):
        if model == "account.payment.term" and field == "line_ids":
            return [
                key
                for (kind, key), row in self.records.items()
                if kind == "account.payment.term.line"
                and row["payment_id"] == identifier
            ]
        return super().value(model, identifier, field)

    def matches(self, model, identifier, domain):
        tokens = iter(domain)

        def expression(token):
            if token in ("|", "&"):
                left, right = expression(next(tokens)), expression(next(tokens))
                return left or right if token == "|" else left and right
            if token == "!":
                return not expression(next(tokens))
            field, operator, expected = token
            actual = self.value(model, identifier, field)
            if operator in ("=", "!="):
                equal = actual == expected or (
                    actual in (None, False) and expected in (None, False)
                )
                return equal if operator == "=" else not equal
            if operator in ("in", "not in"):
                found = actual in expected
                return found if operator == "in" else not found
            if operator in (">", "<", ">=", "<="):
                return {
                    ">": lambda: actual > expected,
                    "<": lambda: actual < expected,
                    ">=": lambda: actual >= expected,
                    "<=": lambda: actual <= expected,
                }[operator]()
            if operator in ("=ilike", "ilike", "=like", "like"):
                a, e = str(actual), str(expected)
                if "ilike" in operator:
                    a, e = a.casefold(), e.casefold()
                pattern = re.escape(e).replace("%", ".*").replace("_", ".")
                matcher = re.fullmatch if operator.startswith("=") else re.search
                return matcher(pattern, a) is not None
            if operator in ("parent_of", "child_of") and (
                model == "res.company"
                or self.relations.get((model, field)) == "res.company"
            ):
                return actual in expected
            raise AssertionError(f"Unsupported controlled-domain operator: {operator}")

        # Parse every prefix expression before combining its results.
        results = [expression(token) for token in tokens]
        return all(results)

    def _create(self, model, values, external_ids=()):
        if model == "account.payment.term.line":
            for row in values:
                parent = row.get("payment_id", row.get("payment_id/.id"))
                if parent and row.get("value", "percent") == "percent":
                    total = sum(
                        r["value_amount"]
                        for (m, _), r in self.records.items()
                        if m == model
                        and r["payment_id"] == int(parent)
                        and r["value"] == "percent"
                    )
                    if total + float(row.get("value_amount", 0)) != 100:
                        raise ValueError(
                            "Stock Odoo payment-term percentages must total100"
                        )
        identifiers = super()._create(model, values, external_ids)
        if model == "account.payment.term":
            for identifier in identifiers:
                self.records[(model, identifier)].setdefault("active", True)
                self.records[(model, identifier)].setdefault("sequence", 10)
                self.records[("account.payment.term.line", 30000 + identifier)] = {
                    "payment_id": identifier,
                    "value": "percent",
                    "value_amount": 100.0,
                    "nb_days": 0,
                    "delay_type": "days_after",
                }
        return identifiers

    def update_row(self, model, identifier, values):
        if self.before_update:
            self.before_update(model, identifier, values)
        row = self.records[(model, identifier)]
        for name, value in values.items():
            related = self.relations.get((model, name))
            if related and value not in (False, None):
                assert (related, int(value)) in self.records, (
                    "Update dependency does not exist"
                )
        row.update(values)
        self.updates.append((model, identifier, dict(values)))
        self.writes.append((model, (dict(values),), (identifier,)))
        if self.after_update:
            self.after_update(model, identifier, values)

    def transport(self, url, headers, body, timeout, method):
        assert getattr(_read_databases, "connections", None) is None, (
            "Odoo called inside a retained DuckDB scope"
        )
        if url.endswith("/web/version"):
            return 200, {"version": f"{self.major}.0"}
        model, operation = url.split("/json/2/", 1)[1].rsplit("/", 1)
        payload = json.loads(body)
        if operation == "context_get":
            return 200, {
                "uid": 7,
                "allowed_company_ids": [self.operation_company],
                "lang": "en_US",
                "tz": "UTC",
            }
        if operation == "default_get":
            defaults = (
                {"active": True, "sequence": 10}
                if model == "account.payment.term"
                else {}
            )
            return 200, {
                name: defaults[name] for name in payload["fields"] if name in defaults
            }
        if operation == "search_read" and model == "res.users":
            fields = payload["fields"]
            row = {
                "id": 7,
                "login": "fixture-user",
                "company_id": self.operation_company,
                "company_ids": [9, 19],
                "group_ids": [3],
                "share": False,
            }
            return 200, [{key: row.get(key, False) for key in {"id", *fields}}]
        if operation == "search_read" and model in self.fields:
            self.calls.append((model, operation))
            fields = payload.get("fields", ["id"])
            assert all("." not in name for name in fields), (
                "Odoo does not return dotted fields"
            )
            identifiers = sorted(
                key
                for (kind, key) in self.records
                if kind == model and self.matches(model, key, payload.get("domain", []))
            )
            if payload.get("context", {}).get("active_test") is not False:
                identifiers = [
                    key
                    for key in identifiers
                    if self.records[(model, key)].get("active", True)
                ]
            offset, limit = (
                payload.get("offset", 0),
                payload.get("limit", len(identifiers)),
            )
            return 200, [
                {"id": key, **{name: self.value(model, key, name) for name in fields}}
                for key in identifiers[offset : offset + limit]
            ]
        if operation == "write":
            self.calls.append((model, operation))
            for identifier in payload["ids"]:
                self.update_row(model, identifier, payload["vals"])
            result = (200, True)
        else:
            try:
                result = super().transport(url, headers, body, timeout, method)
            except ValueError:
                return 400, {"name": "odoo.exceptions.ValidationError"}
        if self.lose_next_response == (model, operation):
            self.lose_next_response = None
            raise TimeoutError("Controlled response loss after target mutation")
        return result
