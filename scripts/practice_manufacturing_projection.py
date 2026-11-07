"""Independent bounded readback of declared Morrow manufacturing business values."""

from __future__ import annotations

from decimal import Decimal

from scripts.practice_customer_seed import (
    relation_id,
    require,
)

PRODUCT_FIELDS = (
    "default_code",
    "name",
    "list_price",
    "categ_id",
    "type",
    "active",
    "uom_id",
    "product_tag_ids",
    "company_id",
    "product_variant_ids",
)
VARIANT_FIELDS = ("default_code", "product_tmpl_id", "active", "company_id")
WORKCENTRE_FIELDS = ("code", "name", "company_id", "resource_id", "active")
OPERATION_FIELDS = (
    "bom_id",
    "name",
    "sequence",
    "workcenter_id",
    "time_mode",
    "time_cycle_manual",
    "company_id",
    "active",
)
SETUP_MODELS = (
    "res.company",
    "product.category",
    "product.tag",
    "mrp.workcenter",
    "product.template",
)
MIGRATION_MODELS = (
    "product.category",
    "product.template",
    "mrp.bom",
    "mrp.bom.line",
    "mrp.routing.workcenter",
)
READ_MODELS = (
    *SETUP_MODELS,
    "product.product",
    "uom.uom",
    "mrp.bom",
    "mrp.bom.line",
    "mrp.routing.workcenter",
    "resource.resource",
)


def unit_field(major):
    """Keep the stock field distinction explicit for the verified target major."""
    require(major in (19, 20), "EXACT_SUPPORTED_MAJOR_REQUIRED")
    return {19: "product_uom_id", 20: "uom_id"}[major]


def relation_ids(value):
    require(isinstance(value, (list, tuple)), "RELATION_ID_LIST_REQUIRED")
    require(
        all(type(item) is int and item > 0 for item in value), "RELATION_IDS_INVALID"
    )
    require(len(value) == len(set(value)), "DUPLICATE_RELATION_IDS")
    return set(value)


def exact_index(rows, key, code):
    result = {}
    for row in rows:
        value = key(row)
        require(value not in result, code)
        result[value] = row
    return result


def decimal_equal(actual, expected, code):
    require(not isinstance(actual, bool), code)
    require(Decimal(str(actual)) == Decimal(str(expected)), code)


class ManufacturingProjection:
    """Check actual records against the separately declared business oracle.

    ``records_fn`` has the PracticeSeedClient.records call shape and returns
    TargetRecord tuples. It must include archived records and enforce complete,
    bounded reads; this wrapper also enforces a limit below every query cap.
    The same reader can be supplied by a controlled target without seed logic.
    """

    def __init__(self, records_fn, major, company_ids, oracle):
        require(major in (19, 20), "EXACT_SUPPORTED_MAJOR_REQUIRED")
        require(set(company_ids) == {"NORTH", "SOUTH"}, "REVIEWED_COMPANIES_REQUIRED")
        require(len(set(company_ids.values())) == 2, "DISTINCT_COMPANIES_REQUIRED")
        require(
            all(type(value) is int and value > 0 for value in company_ids.values()),
            "COMPANY_IDS_REQUIRED",
        )
        self.records_fn = records_fn
        self.major = major
        self.company_ids = dict(company_ids)
        self.oracle = oracle.get("baseline", oracle)
        self.uom_field = unit_field(major)

    def _read(self, model, fields, domain, *, company=None, limit=100):
        require(domain and 1 < limit <= 200, "BOUNDED_PROJECTION_REQUIRED")
        company_id = self.company_ids[company] if company else None
        rows = tuple(
            self.records_fn(
                model, tuple(fields), tuple(domain), company_id=company_id, limit=limit
            )
        )
        require(len(rows) < limit, "PROJECTION_LIMIT_REACHED")
        require(
            all(
                row.model == model and type(row.odoo_id) is int and row.odoo_id > 0
                for row in rows
            ),
            "PROJECTION_RECORD_INVALID",
        )
        require(
            len({row.odoo_id for row in rows}) == len(rows), "PROJECTION_DUPLICATE_IDS"
        )
        require(
            all(set(fields) <= set(row.values) for row in rows),
            "PROJECTION_FIELDS_INCOMPLETE",
        )
        return rows

    def _category_index(self, expected=None):
        expected = self.oracle["categories"] if expected is None else expected
        expected_paths = {tuple(row["path"]) for row in expected}
        require(len(expected_paths) == len(expected), "ORACLE_CATEGORY_PATHS_DUPLICATE")
        roots = sorted({path[0] for path in expected_paths})
        fields = (
            ("name", "parent_id", "company_id")
            if self.major == 20
            else ("name", "parent_id")
        )
        level = self._read(
            "product.category",
            fields,
            (("name", "in", roots), ("parent_id", "=", False)),
            limit=50,
        )
        result = {}
        parent_paths = {}
        for depth in range(max(map(len, expected_paths)) + 1):
            for row in level:
                parent = relation_id(row.values["parent_id"])
                require(
                    parent is None or parent in parent_paths,
                    "CATEGORY_PARENT_OUTSIDE_TREE",
                )
                path = (
                    (*parent_paths[parent], row.values["name"])
                    if parent
                    else (row.values["name"],)
                )
                require(path not in result, "CATEGORY_PATH_AMBIGUOUS")
                if self.major == 20:
                    require(
                        not relation_id(row.values["company_id"]),
                        "CATEGORY_MUST_BE_SHARED",
                    )
                result[path] = row
            parent_paths = {row.odoo_id: path for path, row in result.items()}
            if not level:
                break
            level = self._read(
                "product.category",
                fields,
                (("parent_id", "in", tuple(row.odoo_id for row in level)),),
                limit=50,
            )
            require(
                depth < max(map(len, expected_paths)) or not level,
                "CATEGORY_DEPTH_EXCEEDED",
            )
        require(set(result) == expected_paths, "CATEGORY_PATHS_DIFFERED")
        return result

    def verify_categories(self):
        return {"categories": len(self._category_index())}

    def _setup_relations(self, company=None):
        unit = self._read(
            "uom.uom", ("name",), (("name", "=", "Units"),), company=company, limit=3
        )
        require(len(unit) == 1, "EXISTING_UNITS_MUST_BE_UNIQUE")
        tags = self._read(
            "product.tag",
            ("name",),
            (("name", "in", ("MORROW", "TRAINING")),),
            company=company,
            limit=5,
        )
        tag_ids = exact_index(
            tags, lambda row: row.values["name"], "TAG_NAME_AMBIGUOUS"
        )
        require(set(tag_ids) == {"MORROW", "TRAINING"}, "REVIEWED_TAGS_REQUIRED")
        return unit[0].odoo_id, {name: row.odoo_id for name, row in tag_ids.items()}

    def _products(self, expected, *, company=None, exact=False):
        categories = self._category_index()
        unit, tags = self._setup_relations(company)
        rows = self._read(
            "product.template",
            PRODUCT_FIELDS,
            (("default_code", "like", "MA-P-%"),),
            company=company,
            limit=70,
        )
        actual = exact_index(
            rows, lambda row: row.values["default_code"], "PRODUCT_CODE_AMBIGUOUS"
        )
        known = {row["product_ref"] for row in self.oracle["products"]}
        wanted = {row["product_ref"] for row in expected}
        require(
            set(actual) <= known and wanted <= set(actual), "PRODUCT_NAMESPACE_DIFFERED"
        )
        if exact:
            require(set(actual) == wanted, "PRODUCT_SET_DIFFERED")
        for expected_row in expected:
            row = actual[expected_row["product_ref"]]
            values = row.values
            require(values["name"] == expected_row["name"], "PRODUCT_NAME_DIFFERED")
            decimal_equal(
                values["list_price"], expected_row["price"], "PRODUCT_PRICE_DIFFERED"
            )
            require(
                values["type"] == expected_row["target_type"], "PRODUCT_TYPE_DIFFERED"
            )
            require(
                values["active"] is expected_row["active"], "PRODUCT_ACTIVE_DIFFERED"
            )
            require(not relation_id(values["company_id"]), "PRODUCT_MUST_BE_SHARED")
            require(
                relation_id(values["categ_id"])
                == categories[tuple(expected_row["category_path"])].odoo_id,
                "PRODUCT_CATEGORY_DIFFERED",
            )
            require(relation_id(values["uom_id"]) == unit, "PRODUCT_UNIT_DIFFERED")
            require(
                relation_ids(values["product_tag_ids"])
                == {tags[name] for name in expected_row["tags"]},
                "PRODUCT_TAGS_DIFFERED",
            )
        template_ids = tuple(actual[code].odoo_id for code in wanted)
        variants = self._read(
            "product.product",
            VARIANT_FIELDS,
            (("product_tmpl_id", "in", template_ids),),
            company=company,
            limit=70,
        )
        by_template = exact_index(
            variants,
            lambda row: relation_id(row.values["product_tmpl_id"]),
            "SINGLE_GENERATED_VARIANT_REQUIRED",
        )
        require(set(by_template) == set(template_ids), "GENERATED_VARIANTS_MISSING")
        by_code = {}
        for expected_row in expected:
            code = expected_row["product_ref"]
            template = actual[code]
            variant = by_template[template.odoo_id]
            require(variant.values["default_code"] == code, "VARIANT_CODE_DIFFERED")
            require(
                variant.values["active"] is expected_row["active"],
                "VARIANT_ACTIVE_DIFFERED",
            )
            require(
                not relation_id(variant.values["company_id"]), "VARIANT_MUST_BE_SHARED"
            )
            require(
                relation_ids(template.values["product_variant_ids"])
                == {variant.odoo_id},
                "TEMPLATE_VARIANT_LINK_DIFFERED",
            )
            by_code[code] = variant
        return {code: actual[code] for code in wanted}, by_code

    def verify_products(self, company=None, *, initial=False):
        require(
            company is None or company in self.company_ids, "REVIEWED_COMPANY_REQUIRED"
        )
        expected = [
            row
            for row in self.oracle["products"]
            if company is None or row["source_company_code"] == company
        ]
        if initial:
            require(
                self.oracle["initial_product_state"]["active"] is True,
                "INITIAL_PRODUCT_STATE_MUST_BE_DECLARED",
            )
            expected = [{**row, "active": True} for row in expected]
        templates, variants = self._products(
            expected, company=company, exact=company is None
        )
        return {"products": len(templates), "variants": len(variants)}

    def _bom_projection(self, company):
        require(company in self.company_ids, "REVIEWED_COMPANY_REQUIRED")
        templates, variants = self._products(
            self.oracle["products"], company=company, exact=True
        )
        unit, _ = self._setup_relations(company)
        company_id = self.company_ids[company]
        expected_headers = [
            row for row in self.oracle["bom_headers"] if row["company_code"] == company
        ]
        fields = (
            "code",
            "product_tmpl_id",
            "product_id",
            "product_qty",
            "type",
            "active",
            "company_id",
            self.uom_field,
        )
        headers = self._read(
            "mrp.bom",
            fields,
            (("code", "like", "MA-BOM-%"), ("company_id", "=", company_id)),
            company=company,
            limit=20,
        )
        by_code = exact_index(
            headers, lambda row: row.values["code"], "BOM_CODE_AMBIGUOUS"
        )
        require(
            set(by_code) == {row["bom_ref"] for row in expected_headers},
            "BOM_HEADERS_DIFFERED",
        )
        for expected in expected_headers:
            row = by_code[expected["bom_ref"]]
            require(
                relation_id(row.values["company_id"]) == company_id,
                "BOM_COMPANY_DIFFERED",
            )
            require(
                relation_id(row.values["product_tmpl_id"])
                == templates[expected["finished_product_ref"]].odoo_id,
                "BOM_FINISHED_TEMPLATE_DIFFERED",
            )
            require(
                not relation_id(row.values["product_id"]),
                "BOM_VARIANT_SCOPE_UNEXPECTED",
            )
            decimal_equal(
                row.values["product_qty"],
                expected["product_qty"],
                "BOM_BASE_QUANTITY_DIFFERED",
            )
            require(
                row.values["type"] == expected["bom_type"]
                and row.values["active"] is expected["active"],
                "BOM_TYPE_OR_ACTIVE_DIFFERED",
            )
            require(
                relation_id(row.values[self.uom_field]) == unit, "BOM_UNIT_DIFFERED"
            )
        bom_ids = tuple(row.odoo_id for row in headers)
        reverse_boms = {row.odoo_id: code for code, row in by_code.items()}
        line_fields = (
            "bom_id",
            "sequence",
            "product_id",
            "product_qty",
            "company_id",
            self.uom_field,
        )
        lines = self._read(
            "mrp.bom.line",
            line_fields,
            (("bom_id", "in", bom_ids),),
            company=company,
            limit=50,
        )
        by_line = exact_index(
            lines,
            lambda row: (
                reverse_boms.get(relation_id(row.values["bom_id"])),
                row.values["sequence"],
            ),
            "BOM_LINE_SEQUENCE_AMBIGUOUS",
        )
        expected_lines = [
            row for row in self.oracle["bom_lines"] if row["company_code"] == company
        ]
        require(
            set(by_line)
            == {(row["bom_ref"], row["sequence"]) for row in expected_lines},
            "BOM_LINE_KEYS_DIFFERED",
        )
        for expected in expected_lines:
            row = by_line[(expected["bom_ref"], expected["sequence"])]
            require(
                relation_id(row.values["product_id"])
                == variants[expected["component_ref"]].odoo_id,
                "BOM_COMPONENT_VARIANT_DIFFERED",
            )
            decimal_equal(
                row.values["product_qty"],
                expected["quantity"],
                "BOM_COMPONENT_QUANTITY_DIFFERED",
            )
            require(
                relation_id(row.values[self.uom_field]) == unit,
                "BOM_LINE_UNIT_DIFFERED",
            )
            require(
                relation_id(row.values["company_id"]) == company_id,
                "BOM_LINE_COMPANY_DIFFERED",
            )
        operations = self._read(
            "mrp.routing.workcenter",
            OPERATION_FIELDS,
            (("bom_id", "in", bom_ids),),
            company=company,
            limit=30,
        )
        by_operation = exact_index(
            operations,
            lambda row: (
                reverse_boms.get(relation_id(row.values["bom_id"])),
                row.values["name"],
            ),
            "BOM_OPERATION_NAME_AMBIGUOUS",
        )
        expected_operations = [
            row for row in self.oracle["operations"] if row["company_code"] == company
        ]
        require(
            set(by_operation)
            == {(row["bom_ref"], row["name"]) for row in expected_operations},
            "BOM_OPERATION_KEYS_DIFFERED",
        )
        workcentres = self._read(
            "mrp.workcenter",
            WORKCENTRE_FIELDS,
            (("code", "like", "MA-WC-%"), ("company_id", "=", company_id)),
            company=company,
            limit=10,
        )
        by_wc = exact_index(
            workcentres, lambda row: row.values["code"], "WORKCENTRE_CODE_AMBIGUOUS"
        )
        expected_codes = {
            "NORTH": {"MA-WC-01", "MA-WC-03"},
            "SOUTH": {"MA-WC-02", "MA-WC-04"},
        }[company]
        require(set(by_wc) == expected_codes, "WORKCENTRE_SET_DIFFERED")
        for code, row in by_wc.items():
            require(
                row.values["name"] == f"Morrow Practice Work Centre {code[-2:]}",
                "WORKCENTRE_NAME_DIFFERED",
            )
            require(
                relation_id(row.values["company_id"]) == company_id
                and row.values["active"] is True,
                "WORKCENTRE_COMPANY_OR_ACTIVE_DIFFERED",
            )
            require(
                relation_id(row.values["resource_id"]), "WORKCENTRE_RESOURCE_REQUIRED"
            )
        for expected in expected_operations:
            row = by_operation[(expected["bom_ref"], expected["name"])]
            require(
                row.values["sequence"] == expected["sequence"],
                "OPERATION_ORDER_DIFFERED",
            )
            require(
                row.values["time_mode"] == expected["time_mode"],
                "OPERATION_TIME_MODE_DIFFERED",
            )
            decimal_equal(
                row.values["time_cycle_manual"],
                expected["minutes"],
                "OPERATION_MINUTES_DIFFERED",
            )
            require(
                relation_id(row.values["workcenter_id"])
                == by_wc[expected["work_centre_code"]].odoo_id,
                "OPERATION_WORKCENTRE_DIFFERED",
            )
            require(
                relation_id(row.values["company_id"]) == company_id
                and row.values["active"] is True,
                "OPERATION_COMPANY_OR_ACTIVE_DIFFERED",
            )
        # These declarations supplement the exact graph checks above, preserving
        # subassemblies and a repeated component at distinct line sequences.
        for expected in self.oracle["subassemblies"]:
            if expected["parent_bom_ref"] in by_code:
                require(
                    expected["component_bom_ref"] in by_code, "SUBASSEMBLY_BOM_MISSING"
                )
                require(
                    relation_id(
                        by_code[expected["component_bom_ref"]].values["product_tmpl_id"]
                    )
                    == templates[expected["component_ref"]].odoo_id,
                    "SUBASSEMBLY_TEMPLATE_DIFFERED",
                )
        repeated = self.oracle["intentional_repeated_component"]
        if repeated["bom_ref"] in by_code:
            for sequence, quantity in zip(
                repeated["line_sequences"], repeated["line_quantities"], strict=True
            ):
                row = by_line[(repeated["bom_ref"], sequence)]
                require(
                    relation_id(row.values["product_id"])
                    == variants[repeated["component_ref"]].odoo_id,
                    "REPEATED_COMPONENT_COLLAPSED",
                )
                decimal_equal(
                    row.values["product_qty"],
                    quantity,
                    "REPEATED_COMPONENT_QUANTITY_DIFFERED",
                )
        return {
            "headers": headers,
            "lines": lines,
            "operations": operations,
            "workcentres": workcentres,
        }

    def verify_boms(self, company):
        result = self._bom_projection(company)
        counts = {
            "bom_headers": len(result["headers"]),
            "bom_lines": len(result["lines"]),
            "operations": len(result["operations"]),
        }
        return {**counts, "records": sum(counts.values())}

    def snapshot_company(self, company):
        """Return private exact graph evidence suitable for equality comparison."""
        require(company in self.company_ids, "REVIEWED_COMPANY_REQUIRED")
        company_id = self.company_ids[company]
        fields = (
            "code",
            "product_tmpl_id",
            "product_id",
            "product_qty",
            "type",
            "active",
            "company_id",
            self.uom_field,
        )
        headers = self._read(
            "mrp.bom",
            fields,
            (("code", "like", "MA-BOM-%"), ("company_id", "=", company_id)),
            company=company,
            limit=20,
        )
        ids = tuple(row.odoo_id for row in headers)
        result = {"mrp.bom": headers}
        for model, read_fields in (
            (
                "mrp.bom.line",
                (
                    "bom_id",
                    "sequence",
                    "product_id",
                    "product_qty",
                    "company_id",
                    self.uom_field,
                ),
            ),
            ("mrp.routing.workcenter", OPERATION_FIELDS),
        ):
            result[model] = (
                self._read(
                    model,
                    read_fields,
                    (("bom_id", "in", ids),),
                    company=company,
                    limit=50,
                )
                if ids
                else ()
            )
        return {
            model: [
                {"id": row.odoo_id, "values": dict(row.values)}
                for row in sorted(rows, key=lambda item: item.odoo_id)
            ]
            for model, rows in result.items()
        }
