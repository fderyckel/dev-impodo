"""Independent controlled JSON-2 boundary for the manufacturing corpus.

Only transport is replaced. Native metadata below is a selected-field model
of independently captured 19/20 fields_get evidence in the task lab; it is
not a complete native schema. Live full schema/default checks remain separate.
Initial records come from the business seed, never from prepared output.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from impodo.adapters.duckdb.unit_of_work import _read_databases
from impodo.domain.execution.odoo_readback import ExternalIdBinding
from tests.support.hierarchy_qualification_wire import QualificationWire
from tests.support.practice_customer_target import PracticeCustomerTarget


def _field(kind, *, required=False, readonly=False, store=True, **kwargs):
    return {
        "type": kind,
        "required": required,
        "readonly": readonly,
        "store": store,
        **kwargs,
    }


def _relation(model, *, required=False, readonly=False, store=True):
    return _field(
        "many2one", required=required, readonly=readonly, store=store, relation=model
    )


def stock_fields(major):
    """Selected metadata matches actual captured stock fields for this major.

    Deliberately omit unmodeled full-capture required fields such as tracking,
    store_by, base_unit_count and delegated resource_id. They remain covered
    by LIVE full capture/default checks, not fabricated defaults here.
    """
    if major not in (19, 20):
        raise ValueError("Only independently reviewed Odoo19/20 are supported")
    char = lambda required=False, **kw: _field("char", required=required, **kw)
    boolean = lambda: _field("boolean")
    integer = lambda: _field("integer")
    decimal = lambda required=False: _field("float", required=required, digits=[16, 2])
    unit = "product_uom_id" if major == 19 else "uom_id"
    fields = {
        "res.company": {"name": char(True), "parent_id": _relation("res.company")},
        "product.category": {
            "name": char(True),
            "parent_id": _relation("product.category"),
        },
        "product.template": {
            "name": char(True),
            "default_code": char(),
            "type": _field(
                "selection",
                required=True,
                selection=[
                    ["consu", "Goods"],
                    ["service", "Service"],
                    ["combo", "Combo"],
                ],
            ),
            "list_price": _field("float", digits=False),
            "active": boolean(),
            "categ_id": _relation("product.category"),
            "uom_id": _relation("uom.uom", required=True),
            "company_id": _relation("res.company"),
            "product_tag_ids": _field("many2many", relation="product.tag"),
            "product_variant_id": _relation(
                "product.product", readonly=True, store=False
            ),
            "product_variant_ids": _field(
                "one2many",
                required=True,
                relation="product.product",
                relation_field="product_tmpl_id",
            ),
        },
        "product.product": {
            "name": char(True, store=False),
            "default_code": char(),
            "product_tmpl_id": _relation("product.template", required=True),
            "uom_id": _relation("uom.uom", required=True, store=False),
            "company_id": _relation("res.company", store=False),
            "active": boolean(),
        },
        "product.tag": {"name": char(True)},
        "uom.uom": {"name": char(True), "active": boolean()},
        "mrp.workcenter": {
            "name": char(),
            "code": char(),
            "company_id": _relation("res.company"),
            "resource_id": _relation("resource.resource", required=True),
            "active": boolean(),
        },
        "mrp.bom": {
            "code": char(),
            "product_tmpl_id": _relation("product.template", required=True),
            "product_id": _relation("product.product"),
            "product_qty": decimal(True),
            unit: _relation("uom.uom", required=True),
            "type": _field(
                "selection",
                required=True,
                selection=[["normal", "Manufacture this product"], ["phantom", "Kit"]],
            ),
            "active": boolean(),
            "company_id": _relation("res.company"),
            "bom_line_ids": _field(
                "one2many", relation="mrp.bom.line", relation_field="bom_id"
            ),
            "operation_ids": _field(
                "one2many", relation="mrp.routing.workcenter", relation_field="bom_id"
            ),
        },
        "mrp.bom.line": {
            "bom_id": _relation("mrp.bom", required=True),
            "sequence": integer(),
            "product_id": _relation("product.product", required=True),
            "product_qty": decimal(True),
            unit: _relation("uom.uom", required=True),
            "company_id": _relation("res.company", readonly=True),
        },
        "mrp.routing.workcenter": {
            "name": char(True),
            "bom_id": _relation("mrp.bom", required=True),
            "workcenter_id": _relation("mrp.workcenter", required=True),
            "sequence": integer(),
            "time_mode": _field(
                "selection", selection=[["manual", "Fixed"], ["auto", "Computed"]]
            ),
            "time_cycle_manual": _field("float"),
            "time_cycle": _field("float", readonly=True, store=False),
            "active": boolean(),
            "company_id": _relation("res.company", readonly=True, store=False),
        },
    }
    if major == 20:
        fields["product.category"]["company_id"] = _relation("res.company")
    return fields


class PracticeManufacturingTarget(PracticeCustomerTarget):
    """Model only native behaviors used by this reviewed small corpus.

    Real source/mapping/preparation/comparison/snapshot/execution/read-back
    services still run. Template creates generate one variant internally;
    generated target records never appear as extra client writes.
    """

    def __init__(self, seed_path, *, major=19, mode="REMOTE", company="NORTH"):
        QualificationWire.__init__(self, major=major, mode=mode)
        self.fields = stock_fields(major)
        self.relations = {
            (model, name): field["relation"]
            for model, fields in self.fields.items()
            for name, field in fields.items()
            if field.get("relation")
        }
        self.records.clear()
        self.module_names = ("base", "product", "stock", "mrp")
        self.company_ids = {"NORTH": 9, "SOUTH": 19}
        self.select_company(company)
        self.lose_next_response = None
        self.before_update = self.after_update = None
        self.updates = []
        self._next_id = 1000
        self.seed_path = Path(seed_path)
        self.seed = json.loads(self.seed_path.read_text(encoding="utf-8"))
        for row in self.seed["companies"]:
            self.records[("res.company", self.company_ids[row["company_code"]])] = {
                "name": row["name"],
                "parent_id": False,
            }
        self.records[("uom.uom", 1)] = {"name": "Units", "active": True}
        self.records[("product.category", 60)] = {
            "name": "Services",
            "parent_id": False,
            **({"company_id": False} if major == 20 else {}),
        }
        for identifier, row in enumerate(self.seed["product_tags"], 31):
            self.records[("product.tag", identifier)] = {"name": row["name"]}
        for identifier, row in enumerate(self.seed["work_centres"], 41):
            self.records[("mrp.workcenter", identifier)] = {
                "name": row["name"],
                "code": row["code"],
                "company_id": self.company_ids[row["company_code"]],
                "active": True,
                "resource_id": 60000 + identifier,
            }
        initial = next(
            stage for stage in self.seed["stages"] if stage["id"] == "M0-initial"
        )
        path_ids = {}
        for identifier, row in enumerate(initial["categories"], 101):
            path = tuple(row["path"])
            if path[:-1] and path[:-1] not in path_ids:
                raise ValueError("Initial category seed is not parent-first")
            self.records[("product.category", identifier)] = {
                "name": path[-1],
                "parent_id": path_ids.get(path[:-1], False),
                **({"company_id": False} if major == 20 else {}),
            }
            path_ids[path] = identifier

    def select_company(self, company):
        if company not in self.company_ids:
            raise ValueError("Unknown fictional operation company")
        self.operation_company = self.company_ids[company]

    def _defaults(self, model):
        unit = "product_uom_id" if self.major == 19 else "uom_id"
        defaults = {
            "product.template": {
                "active": True,
                "type": "consu",
                "list_price": 1.0,
                "uom_id": 1,
                "company_id": False,
                "product_tag_ids": [],
            },
            "product.category": {
                "parent_id": False,
                **({"company_id": False} if self.major == 20 else {}),
            },
            "mrp.bom": {
                "active": True,
                "type": "normal",
                "product_qty": 1.0,
                unit: 1,
                "company_id": self.operation_company,
                "product_id": False,
            },
            "mrp.bom.line": {"sequence": 1, "product_qty": 1.0, unit: 1},
            "mrp.routing.workcenter": {
                "active": True,
                "sequence": 100,
                "time_mode": "manual",
                "time_cycle_manual": 60.0,
            },
        }
        return copy.deepcopy(defaults.get(model, {}))

    def value(self, model, identifier, field):
        if model == "product.template" and field in {
            "product_variant_id",
            "product_variant_ids",
        }:
            variants = sorted(
                key
                for (kind, key), row in self.records.items()
                if kind == "product.product"
                and row.get("product_tmpl_id") == identifier
            )
            return (
                (variants[0] if variants else False)
                if field == "product_variant_id"
                else variants
            )
        if model == "product.category" and field == "complete_name":
            return " / ".join(self.category_paths()[identifier])
        if model == "mrp.bom" and field in {"bom_line_ids", "operation_ids"}:
            kind = (
                "mrp.bom.line" if field == "bom_line_ids" else "mrp.routing.workcenter"
            )
            return sorted(
                key
                for (candidate, key), row in self.records.items()
                if candidate == kind and row.get("bom_id") == identifier
            )
        if (
            model in {"mrp.bom.line", "mrp.routing.workcenter"}
            and field == "company_id"
        ):
            return self.value(
                "mrp.bom", self.records[(model, identifier)]["bom_id"], "company_id"
            )
        if model == "mrp.routing.workcenter" and field == "time_cycle":
            row = self.records[(model, identifier)]
            if row.get("time_mode", "manual") != "manual":
                raise ValueError(
                    "Automatic work-order duration is outside this controlled model"
                )
            return row.get("time_cycle_manual", 60.0)
        return super().value(model, identifier, field)

    def _m2m(self, model, name, value, previous=()):
        if value in (None, False, ""):
            result = []
        elif isinstance(value, str):
            result = [int(item) for item in value.split(",") if item]
        elif isinstance(value, (list, tuple)) and all(
            type(item) is int for item in value
        ):
            result = list(value)
        else:
            result = list(previous)
            for command in value:
                operation = command[0]
                if operation == 6:
                    result = list(command[2])
                elif operation == 5:
                    result = []
                elif operation == 4:
                    result.append(command[1])
                elif operation == 3:
                    result = [item for item in result if item != command[1]]
                else:
                    raise ValueError("Unsupported controlled many2many command")
        related = self.relations[(model, name)]
        if any(
            type(item) is not int or (related, item) not in self.records
            for item in result
        ):
            raise ValueError("Controlled many2many dependency does not exist")
        return sorted(set(result))

    def _normalize(self, model, raw, previous=None):
        result = {}
        for field, value in raw.items():
            external = field.endswith("/id")
            if field.endswith("/.id"):
                field = field[:-4]
            elif external:
                field = field[:-3]
                if value:
                    value = self.external_ids[value].odoo_id
            metadata = self.fields[model].get(field)
            if metadata is None or metadata.get("readonly"):
                raise ValueError(
                    "Controlled write includes an absent or readonly field"
                )
            kind = metadata["type"]
            if kind == "many2many":
                value = self._m2m(model, field, value, (previous or {}).get(field, ()))
            elif kind == "many2one":
                value = False if value in (None, False, "") else int(value)
                if (
                    value is not False
                    and (metadata["relation"], value) not in self.records
                ):
                    raise ValueError("Controlled many2one dependency does not exist")
            elif kind == "boolean":
                if isinstance(value, str):
                    if value.casefold() not in {"true", "false", "1", "0"}:
                        raise ValueError("Invalid controlled boolean")
                    value = value.casefold() in {"true", "1"}
                elif type(value) is not bool:
                    raise ValueError("Invalid controlled boolean")
            elif kind in {"float", "integer"}:
                value = float(value) if kind == "float" else int(value)
                if not math.isfinite(value):
                    raise ValueError("Invalid controlled number")
            elif kind == "selection" and value not in {
                item[0] for item in metadata["selection"]
            }:
                raise ValueError("Invalid controlled selection")
            result[field] = value
        return result

    def _validate(self, model, row):
        for field, metadata in self.fields[model].items():
            if model == "product.template" and field == "product_variant_ids":
                # The native create hook supplies this inverse after its parent.
                continue
            if metadata.get("required") and field not in row:
                raise ValueError("Controlled create is missing a required native field")
            if (
                metadata.get("required")
                and metadata["type"] in {"char", "many2one", "selection"}
                and not row.get(field)
            ):
                raise ValueError("Controlled create has an empty required native field")
        if model == "mrp.bom" and row["product_qty"] <= 0:
            raise ValueError("Native BoM header quantity must be positive")
        if model == "mrp.bom.line" and row["product_qty"] < 0:
            raise ValueError("Native BoM component quantity must be nonnegative")
        if model == "mrp.routing.workcenter":
            parent_company = self.records[("mrp.bom", row["bom_id"])]["company_id"]
            center_company = self.records[("mrp.workcenter", row["workcenter_id"])].get(
                "company_id", False
            )
            if center_company and center_company != parent_company:
                raise ValueError(
                    "Native operation work center belongs to another company"
                )

    def _sync_variant(self, identifier, *, creating=False, changed_fields=()):
        row = self.records[("product.template", identifier)]
        variant_id = 50000 + identifier
        previous = self.records.get(("product.product", variant_id))
        self.records[("product.product", variant_id)] = {
            "product_tmpl_id": identifier,
            **{
                name: copy.deepcopy(row.get(name, False))
                for name in (
                    "default_code",
                    "name",
                    "uom_id",
                    "company_id",
                    "active",
                    "type",
                    "categ_id",
                    "product_tag_ids",
                    "list_price",
                )
            },
        }
        if self.major == 20:
            # Native20 _set_default_code lacks the archived-variant fallback.
            # The independently audited inactive create leaves its variant
            # code False. Later archiving must preserve an established code,
            # and unrelated writes must not silently repair the failed case.
            if creating and row.get("active") is False:
                self.records[("product.product", variant_id)]["default_code"] = False
            elif previous is not None and (
                "default_code" not in changed_fields or row.get("active") is False
            ):
                self.records[("product.product", variant_id)]["default_code"] = (
                    previous.get("default_code", False)
                )

    def _create(self, model, values, external_ids=()):
        if self.before_create:
            self.before_create()
        rows = []
        for raw in values:
            row = {**self._defaults(model), **self._normalize(model, raw)}
            if self.before_row:
                self.before_row(model, row)
            self._validate(model, row)
            rows.append(row)
        if external_ids and any(value in self.external_ids for value in external_ids):
            raise ValueError("A controlled external-ID create was duplicated")
        identifiers = []
        for index, row in enumerate(rows):
            identifier = self._next_id
            self._next_id += 1
            self.records[(model, identifier)] = row
            identifiers.append(identifier)
            if model == "product.template":
                self._sync_variant(identifier, creating=True)
            if external_ids:
                external_id = external_ids[index]
                self.external_ids[external_id] = ExternalIdBinding(
                    external_id, model, identifier
                )
                module, _, name = external_id.partition(".")
                self.records[("ir.model.data", len(self.external_ids))] = {
                    "module": module,
                    "name": name,
                    "model": model,
                    "res_id": identifier,
                }
        self.writes.append(
            (model, tuple(dict(row) for row in values), tuple(identifiers))
        )
        if self.after_create:
            self.after_create()
        return tuple(identifiers)

    def update_row(self, model, identifier, values):
        if self.before_update:
            self.before_update(model, identifier, values)
        previous = self.records[(model, identifier)]
        normalized = self._normalize(model, values, previous)
        row = {**previous, **normalized}
        self._validate(model, row)
        self.records[(model, identifier)] = row
        if model == "product.template":
            self._sync_variant(identifier, changed_fields=frozenset(normalized))
        self.updates.append((model, identifier, dict(values)))
        self.writes.append((model, (dict(values),), (identifier,)))
        if self.after_update:
            self.after_update(model, identifier, values)

    def seed_existing_products(self):
        """Seed exactly eight business-defined templates after category loading."""
        expected = json.loads(
            (self.seed_path.parent / "expected-business-results.json").read_text(
                encoding="utf-8"
            )
        )["baseline"]
        required_paths = {tuple(row["path"]) for row in expected["categories"]}
        actual = self.category_paths()
        if set(actual.values()) != required_paths or len(actual) != 12:
            raise ValueError(
                "Seed products only after all twelve category paths are verified"
            )
        by_path = {path: identifier for identifier, path in actual.items()}
        by_tag = {
            row["name"]: identifier
            for (model, identifier), row in self.records.items()
            if model == "product.tag"
        }
        stage = next(
            stage for stage in self.seed["stages"] if stage["id"] == "M2-product-seed"
        )
        if any(model == "product.template" for model, _ in self.records):
            raise ValueError(
                "Existing-product seed requires an otherwise empty template namespace"
            )
        rows = tuple(
            {
                "default_code": row["product_ref"],
                "name": row["name"],
                "list_price": float(row["price"]),
                "type": row["target_type"],
                "active": row["active"],
                "company_id": False,
                "uom_id": 1,
                "categ_id": by_path[tuple(row["category_path"])],
                "product_tag_ids": [[6, 0, [by_tag[name] for name in row["tags"]]]],
            }
            for row in stage["products"]
        )
        if len(rows) != 8:
            raise ValueError(
                "Independent product seed must contain exactly eight templates"
            )
        return self._create("product.template", rows)

    def transport(self, url, headers, body, timeout, method):
        assert getattr(_read_databases, "connections", None) is None, (
            "Odoo called inside a retained DuckDB scope"
        )
        assert method == "POST"
        if "/json/2/" in url:
            model, operation = url.split("/json/2/", 1)[1].rsplit("/", 1)
            if operation == "default_get":
                self.calls.append((model, operation))
                defaults = self._defaults(model)
                return 200, {
                    name: defaults[name]
                    for name in json.loads(body)["fields"]
                    if name in defaults
                }
        try:
            return super().transport(url, headers, body, timeout, method)
        except ValueError:
            return 400, {"name": "odoo.exceptions.ValidationError"}
