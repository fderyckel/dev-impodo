"""Closed staged setup of an explicitly allocated Morrow manufacturing target.

Importing performs no target I/O. Failed or unknown writes retain private
receipts without retries or cleanup. Independent readback lives separately.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from impodo.adapters.odoo.connectors import (
    Json2ReadConnector,
    Json2WriteIdentityConnector,
)
from impodo.domain.odoo.compatibility import OdooReleaseStage, recognize_odoo_version
from impodo.domain.odoo.contracts import MetadataRequest, RecordRequest
from scripts.practice_customer_seed import (
    NAMES,
    PracticeSeedClient,
    relation_id,
    require,
)
from scripts.practice_manufacturing_projection import (
    MIGRATION_MODELS,
    OPERATION_FIELDS,
    PRODUCT_FIELDS,
    READ_MODELS,
    SETUP_MODELS,
    VARIANT_FIELDS,
    WORKCENTRE_FIELDS,
    ManufacturingProjection,
    exact_index,
    relation_ids,
    unit_field,
)
from scripts.practice_manufacturing_raw_read import read_template_variant_links


class LiveManufacturingSeed(PracticeSeedClient):
    """Set up only the closed M0 and M2 records of an allocated fresh target."""

    def __init__(self, connection, corpus_root, private_directory):
        super().__init__(connection, corpus_root, private_directory)
        self.read_config = replace(
            self.read_config,
            relevant_modules=("base", "product", "stock", "mrp"),
            context={"active_test": False},
        )
        self.write_config = replace(
            self.write_config,
            relevant_modules=("base", "product", "stock", "mrp"),
            context={"active_test": False},
        )
        self.reader = Json2ReadConnector(self.read_config)
        self._permitted_creates = {}
        self._initial_category_ids: dict[tuple[str, ...], int] = {}
        self._reviewed_product_values = []
        self._assert_spec()

    def _assert_spec(self):
        require(
            self.seed_spec["companies"]
            == [
                {"company_code": code, "name": NAMES[code]}
                for code in ("NORTH", "SOUTH")
            ],
            "REVIEWED_COMPANY_SPEC_REQUIRED",
        )
        require(
            {row["name"] for row in self.seed_spec["product_tags"]}
            == {"MORROW", "TRAINING"}
            and len(self.seed_spec["product_tags"]) == 2,
            "REVIEWED_TAG_SPEC_REQUIRED",
        )
        stages = exact_index(
            self.seed_spec["stages"], lambda row: row["id"], "SEED_STAGE_DUPLICATE"
        )
        self.initial_categories = stages["M0-initial"]["categories"]
        require(
            [row["path"] for row in self.initial_categories]
            == [["Components"], ["Components", "Metals"]],
            "REVIEWED_INITIAL_CATEGORY_SPEC_REQUIRED",
        )
        self.preexisting_categories = self.seed_spec.get("preexisting_references")
        require(
            self.preexisting_categories
            == [
                {
                    "source_key": "CAT-10",
                    "path": ["Services"],
                    "xmlid": "product.product_category_services",
                    "must_already_exist": True,
                    "owned": False,
                }
            ],
            "REVIEWED_NATIVE_CATEGORY_REFERENCE_REQUIRED",
        )
        self.initial_category_projection = [
            *self.initial_categories,
            *(
                {"category_key": row["source_key"], "path": row["path"]}
                for row in self.preexisting_categories
            ),
        ]
        self.existing_products = stages["M2-product-seed"]["products"]
        require(
            {row["product_ref"] for row in self.existing_products}
            == {f"MA-P-{number:04d}" for number in range(9, 17)}
            and len(self.existing_products) == 8,
            "REVIEWED_EIGHT_PRODUCT_SPEC_REQUIRED",
        )
        centres = self.seed_spec["work_centres"]
        require(
            len(centres) == 4
            and {row["code"] for row in centres}
            == {f"MA-WC-{number:02d}" for number in range(1, 5)},
            "REVIEWED_WORKCENTRE_SPEC_REQUIRED",
        )
        for row in centres:
            expected_company = "NORTH" if int(row["code"][-2:]) % 2 else "SOUTH"
            require(
                row["company_code"] == expected_company
                and row["company_name"] == NAMES[expected_company]
                and row["name"] == f"Morrow Practice Work Centre {row['code'][-2:]}",
                "REVIEWED_WORKCENTRE_ASSIGNMENT_REQUIRED",
            )
        require(
            self.seed_spec["version_fields"][str(self.major)]["bom_unit_field"]
            == unit_field(self.major),
            "SEED_MAJOR_FIELDS_DIFFERED",
        )

    def records(self, model, fields, domain, *, company_id=None, limit=100):
        require(domain and 1 < limit <= 200, "BOUNDED_SOURCE_SCOPE_REQUIRED")
        context: dict[str, object] = {"active_test": False}
        if company_id:
            context["allowed_company_ids"] = [company_id]
        config = replace(self.read_config, context=context)
        connector = Json2ReadConnector(config)
        result = connector.get_records(
            (RecordRequest(model, tuple(fields), tuple(domain), limit),)
        )
        require(result.complete, "INCOMPLETE_READ")
        rows = tuple(result.records.get(model, ()))
        require(len(rows) < limit, "READ_LIMIT_REACHED")
        require(
            all(set(fields) <= set(row.values) for row in rows),
            "READ_FIELDS_INCOMPLETE",
        )
        if model == "product.template" and "product_variant_ids" in fields:
            rows = read_template_variant_links(connector, rows, major=self.major)
        return rows

    def validate(self):
        require(
            self.ledger["state"] in {"READ_ONLY", "VALIDATED_READ_ONLY"}
            and not self.seeded,
            "FRESH_VALIDATION_REQUIRED",
        )
        fingerprint = self.reader.get_target_fingerprint()
        observed = recognize_odoo_version(fingerprint.odoo_version)
        require(
            observed.major == self.major
            and observed.series == f"{self.major}.0"
            and observed.release_stage is OdooReleaseStage.FINAL
            and not observed.is_saas,
            "EXACT_RUNTIME_VERSION_REQUIRED",
        )
        require(
            {"base", "product", "stock", "mrp"} <= set(fingerprint.module_versions),
            "INSTALLED_MANUFACTURING_MODULES_REQUIRED",
        )
        self.reader.probe_read_identity((*READ_MODELS, "ir.model.data"))
        Json2WriteIdentityConnector(self.write_config).probe_write_identity(
            tuple(sorted(set(SETUP_MODELS + MIGRATION_MODELS))),
            tuple(sorted(set(SETUP_MODELS + MIGRATION_MODELS))),
            creatable_models=tuple(sorted(set(SETUP_MODELS + MIGRATION_MODELS))),
        )
        category_fields = (
            ("name", "parent_id", "company_id")
            if self.major == 20
            else ("name", "parent_id")
        )
        model_fields = {
            "res.company": ("name", "partner_id", "user_ids"),
            "product.category": category_fields,
            "product.tag": ("name",),
            "product.template": PRODUCT_FIELDS,
            "product.product": VARIANT_FIELDS,
            "uom.uom": ("name",),
            "mrp.workcenter": WORKCENTRE_FIELDS,
            "resource.resource": ("name", "company_id", "active"),
            "ir.model.data": ("module", "name", "model", "res_id", "noupdate"),
            "mrp.bom": (
                "code",
                "product_tmpl_id",
                "product_id",
                "product_qty",
                "type",
                "active",
                "company_id",
                unit_field(self.major),
            ),
            "mrp.bom.line": (
                "bom_id",
                "sequence",
                "product_id",
                "product_qty",
                "company_id",
                unit_field(self.major),
            ),
            "mrp.routing.workcenter": OPERATION_FIELDS,
        }
        requests = tuple(
            MetadataRequest(model, fields) for model, fields in model_fields.items()
        )
        metadata = self.reader.get_model_metadata(requests)
        require(metadata.complete, "COMPLETE_MANUFACTURING_METADATA_REQUIRED")
        for request in requests:
            require(
                request.model in metadata.models
                and set(request.fields) <= set(metadata.models[request.model].fields),
                "MANUFACTURING_FIELD_METADATA_MISSING",
            )
        writable = {
            "res.company": {"name", "user_ids"},
            "product.category": set(category_fields),
            "product.tag": {"name"},
            "product.template": set(PRODUCT_FIELDS) - {"product_variant_ids"},
            "mrp.workcenter": {"code", "name", "company_id"},
            "mrp.bom": set(model_fields["mrp.bom"]),
            "mrp.bom.line": set(model_fields["mrp.bom.line"]) - {"company_id"},
            "mrp.routing.workcenter": set(OPERATION_FIELDS) - {"company_id"},
        }
        for model, fields in writable.items():
            require(
                all(
                    not metadata.models[model].fields[name].readonly for name in fields
                ),
                "MANUFACTURING_REQUIRED_WRITABLE_FIELD_READONLY",
            )
        context = self.reader._post_read_method(
            "res.users", "context_get", {"context": {}}
        )
        self.user_id = context.get("uid")
        require(
            type(self.user_id) is int and self.user_id > 0,
            "CURRENT_USER_CONTEXT_REQUIRED",
        )
        writer_context = Json2ReadConnector(self.write_config)._post_read_method(
            "res.users", "context_get", {"context": {}}
        )
        require(
            writer_context.get("uid") == self.user_id,
            "SAME_SETUP_API_PRINCIPAL_REQUIRED",
        )
        companies = self.records(
            "res.company", ("name", "partner_id"), (("id", ">", 0),), limit=3
        )
        require(
            len(companies) == 1
            and companies[0].values["name"] not in set(NAMES.values()),
            "FRESH_SINGLE_COMPANY_TARGET_REQUIRED",
        )
        self.initial_company = companies[0]
        probes = (
            ("product.template", "default_code", "like", "MA-P-%"),
            ("product.product", "default_code", "like", "MA-P-%"),
            ("mrp.bom", "code", "like", "MA-BOM-%"),
            ("mrp.workcenter", "code", "like", "MA-WC-%"),
            (
                "mrp.workcenter",
                "name",
                "in",
                tuple(row["name"] for row in self.seed_spec["work_centres"]),
            ),
            ("product.tag", "name", "in", ("MORROW", "TRAINING")),
        )
        for model, field, operator, value in probes:
            require(
                not self.records(
                    model, (field,), ((field, operator, value),), limit=70
                ),
                "MANUFACTURING_FIXTURE_NAMESPACE_NOT_EMPTY",
            )
        self._validate_native_category_reference(category_fields)
        units = self.records("uom.uom", ("name",), (("name", "=", "Units"),), limit=3)
        require(len(units) == 1, "EXISTING_UNITS_MUST_BE_UNIQUE")
        self.unit_id = units[0].odoo_id
        self.ledger["state"] = "VALIDATED_READ_ONLY"
        self.ledger["initial_company"] = {
            "id": self.initial_company.odoo_id,
            "values": dict(self.initial_company.values),
        }
        self.ledger["observed_version"] = fingerprint.odoo_version
        self._save()
        return {"major": self.major, "fresh_fixture_scope": True, "writes": 0}

    def _validate_native_category_reference(self, category_fields):
        """Whitelist one observed stock reference without claiming ownership."""
        external = self.records(
            "ir.model.data",
            ("module", "name", "model", "res_id", "noupdate"),
            (("module", "=", "product"), ("name", "=", "product_category_services")),
            limit=3,
        )
        require(len(external) == 1, "NATIVE_CATEGORY_XMLID_MUST_BE_UNIQUE")
        identity = external[0].values
        native_id = relation_id(identity["res_id"])
        require(
            identity["module"] == "product"
            and identity["name"] == "product_category_services"
            and identity["model"] == "product.category"
            and identity["noupdate"] is True
            and native_id is not None,
            "NATIVE_CATEGORY_XMLID_DIFFERED",
        )
        native = self.records(
            "product.category", category_fields, (("id", "=", native_id),), limit=3
        )
        require(
            len(native) == 1
            and native[0].values["name"] == "Services"
            and not relation_id(native[0].values["parent_id"]),
            "NATIVE_SERVICES_ROOT_DIFFERED",
        )
        if self.major == 20:
            require(
                not relation_id(native[0].values["company_id"]),
                "NATIVE_SERVICES_MUST_BE_SHARED",
            )
        root_names = tuple(
            sorted({row["path"][0] for row in self.oracle["baseline"]["categories"]})
        )
        roots = self.records(
            "product.category",
            category_fields,
            (("name", "in", root_names), ("parent_id", "=", False)),
            limit=20,
        )
        require(
            len(roots) == 1 and roots[0].odoo_id == native_id,
            "UNREVIEWED_PRACTICE_CATEGORY_ROOT_COLLISION",
        )
        require(
            not self.records(
                "product.category",
                category_fields,
                (("parent_id", "=", native_id),),
                limit=30,
            ),
            "NATIVE_SERVICES_CHILDREN_NOT_EMPTY",
        )
        require(
            not any(
                row["id"] == native_id
                for row in self.ledger["owned"].get("product.category", ())
            ),
            "NATIVE_REFERENCE_MUST_NOT_BE_OWNED",
        )
        self.ledger["references"] = {
            "product.category": [
                {
                    "id": native_id,
                    "source_key": "CAT-10",
                    "path": ["Services"],
                    "xmlid": "product.product_category_services",
                    "noupdate": True,
                    "owned": False,
                }
            ],
        }
        self._save()
        return native_id

    def _check_create(self, model, values):
        allowed_fields = {
            "res.company": {"name", "user_ids"},
            "product.category": {"name", "parent_id"}
            | ({"company_id"} if self.major == 20 else set()),
            "product.tag": {"name"},
            "mrp.workcenter": {"code", "name", "company_id"},
            "product.template": set(PRODUCT_FIELDS) - {"product_variant_ids"},
        }
        require(
            model in allowed_fields and set(values) == allowed_fields[model],
            "SETUP_MODEL_OR_FIELDS_OUTSIDE_SCOPE",
        )
        if model == "product.template":
            require(
                self.ledger["state"] == "SEEDING_M2"
                and values in self._reviewed_product_values,
                "REVIEWED_M2_PRODUCT_VALUES_REQUIRED",
            )
            return values["default_code"]
        require(self.ledger["state"] == "SEEDING_M0", "INITIAL_SETUP_STAGE_REQUIRED")
        if model == "res.company":
            require(
                values == {"name": NAMES["SOUTH"], "user_ids": [(4, self.user_id)]},
                "REVIEWED_COMPANY_CREATE_ONLY",
            )
            return values["name"]
        if model == "product.tag":
            require(
                values["name"] in {"MORROW", "TRAINING"}, "REVIEWED_TAG_CREATE_ONLY"
            )
            return values["name"]
        if model == "mrp.workcenter":
            reviewed = [
                {
                    "code": row["code"],
                    "name": row["name"],
                    "company_id": self.company_ids[row["company_code"]],
                }
                for row in self.seed_spec["work_centres"]
            ]
            require(values in reviewed, "REVIEWED_WORKCENTRE_CREATE_ONLY")
            return values["code"]
        reviewed = []
        for row in self.initial_categories:
            segments: list[str] = list(row["path"])
            if not segments:
                raise RuntimeError("NONEMPTY_CATEGORY_PATH_REQUIRED")
            path = tuple(segments)
            if len(path) > 1 and path[:-1] not in self._initial_category_ids:
                continue
            candidate = {
                "name": segments[-1],
                "parent_id": self._initial_category_ids[path[:-1]]
                if len(path) > 1
                else False,
            }
            if self.major == 20:
                candidate["company_id"] = False
            reviewed.append(candidate)
        require(values in reviewed, "REVIEWED_INITIAL_CATEGORY_CREATE_ONLY")
        return [values["parent_id"], values["name"]]

    def _mutate(self, kind, model, values, *, identifier=None, company_id=None):
        require(
            self.ledger["state"] in {"SEEDING_M0", "SEEDING_M2"},
            "SEED_WRITE_SCOPE_CLOSED",
        )
        if kind == "write":
            require(
                self.ledger["state"] == "SEEDING_M0"
                and model == "res.company"
                and identifier == self.initial_company.odoo_id
                and values == {"name": NAMES["NORTH"]},
                "FRESH_COMPANY_RENAME_ONLY",
            )
        else:
            require(
                kind == "create"
                and identifier is None
                and model in SETUP_MODELS
                and values in self._permitted_creates.get(model, ()),
                "REVIEWED_CREATE_VALUES_REQUIRED",
            )
        business_key = (
            self._check_create(model, values) if kind == "create" else NAMES["NORTH"]
        )
        require(
            not any(
                item["kind"] == kind
                and item["model"] == model
                and item.get("business_key") == business_key
                for item in self.ledger["operations"]
            ),
            "SEED_MUTATION_ALREADY_DISPATCHED",
        )
        item = {
            "kind": kind,
            "model": model,
            "business_key": business_key,
            "id": identifier,
            "values": values,
            "status": "DISPATCHING",
        }
        self.ledger["operations"].append(item)
        self._save()
        try:
            writer = self._writer(model, values, company_id)
            if kind == "create":
                ids = writer.create_rows(model, (values,))
                require(
                    len(ids) == 1 and type(ids[0]) is int and ids[0] > 0,
                    "SEED_RECEIPT_INVALID",
                )
                item["receipt_ids"] = list(ids)
                self._own(model, ids, "explicit_seed_create")
                identifier = ids[0]
                self._permitted_creates[model].remove(values)
            else:
                if type(identifier) is not int or identifier <= 0:
                    raise RuntimeError("SEED_EXACT_ID_REQUIRED")
                writer.update_row(model, identifier, values)
        except Exception:
            item["status"] = "FAILED_OR_UNKNOWN_RETAIN_FOR_DIAGNOSIS"
            self.ledger["state"] = "FAILED_RETAIN_FOR_DIAGNOSIS"
            self._save()
            raise
        item["status"] = "ACKNOWLEDGED"
        self._save()
        return identifier

    def _create(self, model, values, *, company_id=None):
        self._permitted_creates.setdefault(model, []).append(dict(values))
        return self._mutate("create", model, values, company_id=company_id)

    def _projection(self):
        return ManufacturingProjection(
            self.records, self.major, self.company_ids, self.oracle
        )

    def seed(self, *, allow_write=False):
        require(allow_write is True, "ALLOW_WRITE_REQUIRED")
        require(
            self.ledger["state"] == "VALIDATED_READ_ONLY" and not self.seeded,
            "FRESH_VALIDATION_REQUIRED",
        )
        self.validate()
        self.seeded = True
        self.ledger["state"] = "SEEDING_M0"
        self._save()
        try:
            north = self.initial_company.odoo_id
            self._mutate(
                "write", "res.company", {"name": NAMES["NORTH"]}, identifier=north
            )
            south = self._create(
                "res.company", {"name": NAMES["SOUTH"], "user_ids": [(4, self.user_id)]}
            )
            self.company_ids = {"NORTH": north, "SOUTH": south}
            created_company = self.records(
                "res.company",
                ("name", "partner_id", "user_ids"),
                (("id", "=", south),),
                company_id=south,
                limit=2,
            )
            require(
                len(created_company) == 1
                and created_company[0].values["name"] == NAMES["SOUTH"]
                and self.user_id in relation_ids(created_company[0].values["user_ids"]),
                "SOUTH_COMPANY_GRANT_READBACK_REQUIRED",
            )
            partner = relation_id(created_company[0].values["partner_id"])
            require(partner, "GENERATED_COMPANY_PARTNER_REQUIRED")
            self._own("res.partner", (partner,), "owned_company_generated_partner")
            category_ids = {}
            for row in self.initial_categories:
                path: tuple[str, ...] = tuple(row["path"])
                values = {
                    "name": path[-1],
                    "parent_id": category_ids[path[:-1]] if len(path) > 1 else False,
                }
                if self.major == 20:
                    values["company_id"] = False
                category_ids[path] = self._create(
                    "product.category", values, company_id=north
                )
                self._initial_category_ids[path] = category_ids[path]
            for row in self.seed_spec["product_tags"]:
                self._create("product.tag", {"name": row["name"]}, company_id=north)
            for row in self.seed_spec["work_centres"]:
                company_id = self.company_ids[row["company_code"]]
                identifier = self._create(
                    "mrp.workcenter",
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "company_id": company_id,
                    },
                    company_id=company_id,
                )
                receipt = self.records(
                    "mrp.workcenter",
                    WORKCENTRE_FIELDS,
                    (("id", "=", identifier),),
                    company_id=company_id,
                    limit=2,
                )
                require(
                    len(receipt) == 1
                    and receipt[0].values["code"] == row["code"]
                    and receipt[0].values["name"] == row["name"]
                    and relation_id(receipt[0].values["company_id"]) == company_id,
                    "WORKCENTRE_CREATE_READBACK_REQUIRED",
                )
                resource = relation_id(receipt[0].values["resource_id"])
                require(resource, "GENERATED_WORKCENTRE_RESOURCE_REQUIRED")
                generated = self.records(
                    "resource.resource",
                    ("name", "company_id", "active"),
                    (("id", "=", resource),),
                    company_id=company_id,
                    limit=2,
                )
                require(
                    len(generated) == 1
                    and generated[0].values["name"] == row["name"]
                    and relation_id(generated[0].values["company_id"]) == company_id
                    and generated[0].values["active"] is True,
                    "GENERATED_RESOURCE_READBACK_REQUIRED",
                )
                self._own(
                    "resource.resource",
                    (resource,),
                    "owned_workcentre_generated_resource",
                )
            self._projection()._category_index(self.initial_category_projection)
            self._projection()._setup_relations()
            require(
                not self.records(
                    "product.template",
                    ("default_code",),
                    (("default_code", "like", "MA-P-%"),),
                    limit=70,
                ),
                "M0_MUST_NOT_SEED_PRODUCTS",
            )
            require(
                not self.records(
                    "mrp.bom", ("code",), (("code", "like", "MA-BOM-%"),), limit=20
                ),
                "M0_MUST_NOT_SEED_BOMS",
            )
        except Exception:
            self.ledger["state"] = "FAILED_RETAIN_FOR_DIAGNOSIS"
            self._save()
            raise
        self.ledger["state"] = "M0_SEEDED"
        self._save()
        return {
            "major": self.major,
            "companies": 2,
            "categories": 3,
            "available_categories": 3,
            "owned_categories": 2,
            "tags": 2,
            "workcentres": 4,
            "products": 0,
            "bom_headers": 0,
        }

    def seed_existing_products(self, *, allow_write=False):
        require(allow_write is True, "ALLOW_WRITE_REQUIRED")
        require(self.ledger["state"] == "M0_SEEDED", "M0_SEEDED_REQUIRED")
        projection = self._projection()
        categories = projection._category_index()
        unit, tags = projection._setup_relations()
        require(
            not self.records(
                "product.template",
                ("default_code",),
                (("default_code", "like", "MA-P-%"),),
                limit=70,
            ),
            "M2_FRESH_PRODUCT_SCOPE_REQUIRED",
        )
        require(
            not self.records(
                "mrp.bom", ("code",), (("code", "like", "MA-BOM-%"),), limit=20
            ),
            "M2_MUST_PRECEDE_BOMS",
        )
        self.ledger["state"] = "SEEDING_M2"
        self._save()
        try:
            for row in self.existing_products:
                require(
                    row["target_company_assignment"] is None
                    and row["uom_key"] == "Units",
                    "M2_REVIEWED_SHARED_PRODUCT_REQUIRED",
                )
                values = {
                    "default_code": row["product_ref"],
                    "name": row["name"],
                    "list_price": float(Decimal(row["price"])),
                    "categ_id": categories[tuple(row["category_path"])].odoo_id,
                    "type": row["target_type"],
                    "active": row["active"],
                    "uom_id": unit,
                    "product_tag_ids": [
                        (6, 0, sorted(tags[name] for name in row["tags"]))
                    ],
                    "company_id": False,
                }
                self._reviewed_product_values.append(values)
                template = self._create(
                    "product.template", values, company_id=self.company_ids["NORTH"]
                )
                variants = self.records(
                    "product.product",
                    VARIANT_FIELDS,
                    (("product_tmpl_id", "=", template),),
                    company_id=self.company_ids["NORTH"],
                    limit=3,
                )
                require(
                    len(variants) == 1
                    and variants[0].values["default_code"] == row["product_ref"]
                    and relation_id(variants[0].values["product_tmpl_id"]) == template,
                    "SEED_GENERATED_VARIANT_REQUIRED",
                )
                self._own(
                    "product.product",
                    (variants[0].odoo_id,),
                    "owned_template_generated_variant",
                )
            projection._products(self.existing_products, exact=True)
        except Exception:
            self.ledger["state"] = "FAILED_RETAIN_FOR_DIAGNOSIS"
            self._save()
            raise
        self.ledger["state"] = "M2_SEEDED"
        self._save()
        return {"products": 8, "variants": 8, "categories": 12}

    def verify(self, phase, company=None):
        projection = self._projection()
        phase = phase.lower().replace("_", "-")
        if phase in {"categories", "m1-category-complete"}:
            return projection.verify_categories()
        if phase == "m0-initial":
            categories = len(
                projection._category_index(self.initial_category_projection)
            )
            projection._setup_relations()
            require(
                not self.records(
                    "product.template",
                    ("default_code",),
                    (("default_code", "like", "MA-P-%"),),
                    limit=70,
                ),
                "M0_MUST_NOT_HAVE_PRODUCTS",
            )
            require(
                not self.records(
                    "mrp.bom", ("code",), (("code", "like", "MA-BOM-%"),), limit=20
                ),
                "M0_MUST_NOT_HAVE_BOMS",
            )
            return {
                "categories": categories,
                "available_categories": categories,
                "owned_categories": len(
                    self.ledger["owned"].get("product.category", ())
                ),
                "products": 0,
                "bom_headers": 0,
            }
        if phase == "m2-product-seed":
            templates, variants = projection._products(
                self.existing_products, exact=True
            )
            return {"products": len(templates), "variants": len(variants)}
        if phase in {"products-north", "products-south", "boms-north", "boms-south"}:
            selected = phase.rsplit("-", 1)[1].upper()
            require(company is None or company == selected, "PHASE_COMPANY_CONFLICT")
            company = selected
            phase = phase.split("-", 1)[0]
        if phase in {"products", "m3-all-products-complete"}:
            return projection.verify_products(company)
        if phase == "boms":
            require(company in self.company_ids, "REVIEWED_COMPANY_REQUIRED")
            return projection.verify_boms(company)
        if phase in {"all", "m4-boms-complete"}:
            result: dict[str, object] = {
                **projection.verify_categories(),
                **projection.verify_products(),
            }
            for selected in ("NORTH", "SOUTH"):
                result[selected] = projection.verify_boms(selected)
            return result
        raise RuntimeError("UNKNOWN_MANUFACTURING_VERIFICATION_PHASE")


# Stable caller-facing name; the explicit Live name remains useful in reviews.
ManufacturingSeedClient = LiveManufacturingSeed
