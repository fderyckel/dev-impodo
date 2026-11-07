"""Closed seed/projection helper for an already allocated Morrow disposable lab.

Import only: this module performs no action until validate(), seed(), or a
projection method is called. It never starts services, retries writes, adopts
an existing fixture by name, cleans a database, or prints credentials/IDs.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    Json2WriteIdentityConnector,
    target_record_read_config,
)
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.odoo.compatibility import OdooReleaseStage, recognize_odoo_version
from impodo.domain.odoo.contracts import MetadataRequest, RecordRequest

MODELS = (
    "account.payment.term",
    "account.payment.term.line",
    "res.company",
    "res.partner",
)
NAMES = {"NORTH": "Morrow Assembly North", "SOUTH": "Morrow Assembly South"}
CONTACT_FIELDS = (
    "ref",
    "name",
    "email",
    "phone",
    "street",
    "zip",
    "city",
    "company_id",
    "country_id",
    "property_payment_term_id",
)
TERM_FIELDS = ("name", "company_id", "active", "sequence")
LINE_FIELDS = ("payment_id", "value", "value_amount", "nb_days", "delay_type")


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def relation_id(value):
    if isinstance(value, (tuple, list)):
        value = value[0] if value else False
    return value if type(value) is int and value > 0 else None


class PracticeSeedClient:
    """Read before setup; record each permitted mutation in a private ledger."""

    def __init__(self, connection, corpus_root, private_directory):
        self.connection_path = Path(connection).resolve(strict=True)
        payload = json.loads(self.connection_path.read_text(encoding="utf-8"))
        require(
            payload.get("contract_version") == 1
            and payload.get("disposable") is True
            and payload.get("purpose") == "morrow_practice_qualification",
            "DISPOSABLE_DESCRIPTOR_REQUIRED",
        )
        self.major = payload.get("major")
        require(
            self.major in (19, 20) and payload.get("mode") in {"LOCAL", "REMOTE"},
            "EXACT_LAB_MODE_REQUIRED",
        )
        require(
            re.fullmatch(
                rf"impodo_scenario_morrow{self.major}_[a-z0-9_]+",
                payload.get("database", ""),
            ),
            "DISPOSABLE_DATABASE_PREFIX_REQUIRED",
        )
        self.root = Path(corpus_root).resolve(strict=True)
        self.seed_spec = json.loads(
            (self.root / "target-seed-projection.json").read_text()
        )
        self.oracle = json.loads(
            (self.root / "expected-business-results.json").read_text()
        )
        self.private = Path(private_directory).resolve()
        self.private.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(self.private, 0o700)
        self.ledger_path = self.private / "owned-records.json"
        self.ledger = {
            "contract_version": 1,
            "database": payload["database"],
            "major": self.major,
            "purpose": "morrow_practice_qualification",
            "operations": [],
            "owned": {},
            "state": "READ_ONLY",
        }

        def key(role):
            path = Path(payload[f"{role}_api_key_file"]).resolve(strict=True)
            path.relative_to(self.connection_path.parent)
            require(path.is_file(), "PRIVATE_KEY_FILE_REQUIRED")
            value = path.read_text(encoding="utf-8").strip()
            require(bool(value), "PRIVATE_KEY_FILE_EMPTY")
            return value

        common = {
            "base_url": payload["base_url"],
            "database": payload["database"],
            "connection_mode": payload["mode"],
            "retries": 0,
            "relevant_modules": ("base", "contacts", "account"),
        }
        self.read_config = target_record_read_config(
            Json2Config(api_key=key("read"), **common)
        )
        self.write_config = Json2Config(api_key=key("write"), **common)
        self.reader = Json2ReadConnector(self.read_config)
        self.company_ids = {}
        self.seeded = False
        self._save()

    def _save(self):
        self.ledger_path.write_text(
            json.dumps(self.ledger, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(self.ledger_path, 0o600)

    def records(self, model, fields, domain, *, company_id=None, limit=100):
        require(domain, "BOUNDED_SOURCE_SCOPE_REQUIRED")
        config = (
            replace(self.read_config, context={"allowed_company_ids": [company_id]})
            if company_id
            else self.read_config
        )
        result = Json2ReadConnector(config).get_records(
            (RecordRequest(model, tuple(fields), tuple(domain), limit),)
        )
        require(result.complete, "INCOMPLETE_READ")
        rows = result.records.get(model, ())
        require(len(rows) < limit, "READ_LIMIT_REACHED")
        return tuple(rows)

    def _writer(self, model, fields, company_id=None):
        names = tuple(sorted(fields))
        scope = OdooApiScope(
            "sha256:" + "8" * 64,
            (OdooModelScope(model, names, names, names),),
            (model,),
        )
        config = (
            replace(self.write_config, context={"allowed_company_ids": [company_id]})
            if company_id
            else self.write_config
        )
        return Json2WriteExecutor(config, scope)

    def _own(self, model, ids, origin):
        self.ledger["owned"].setdefault(model, []).extend(
            {"id": identifier, "origin": origin} for identifier in ids
        )
        self._save()

    def _mutate(self, kind, model, values, *, identifier=None, company_id=None):
        require(self.ledger["state"] == "SEEDING", "SEED_WRITE_SCOPE_CLOSED")
        allowed = {
            "res.company": {"name", "user_ids"},
            "account.payment.term": {"name", "company_id"},
            "account.payment.term.line": {"nb_days"},
            "res.partner": set(CONTACT_FIELDS),
        }
        require(
            model in allowed and set(values) <= allowed[model],
            "SETUP_MODEL_OR_FIELD_OUTSIDE_SCOPE",
        )
        if kind == "write":
            permitted = {item["id"] for item in self.ledger["owned"].get(model, [])}
            if model == "res.company":
                require(
                    identifier == self.initial_company.odoo_id
                    and values == {"name": NAMES["NORTH"]},
                    "FRESH_COMPANY_RENAME_ONLY",
                )
            else:
                require(
                    model == "account.payment.term.line"
                    and identifier in permitted
                    and values == {"nb_days": 30},
                    "OWNED_SCHEDULE_UPDATE_ONLY",
                )
        elif model == "res.company":
            require(
                values == {"name": NAMES["SOUTH"], "user_ids": [(4, self.user_id)]},
                "REVIEWED_COMPANY_CREATE_ONLY",
            )
        elif model == "account.payment.term":
            require(
                values.get("name") == "Morrow Net 30"
                and values.get("company_id") in self.company_ids.values(),
                "NET30_SEED_ONLY",
            )
        else:
            require(
                model == "res.partner"
                and values.get("ref")
                in {row["customer_ref"] for row in self.seed_spec["customers"]},
                "REVIEWED_CONTACT_SEED_ONLY",
            )
        item = {
            "kind": kind,
            "model": model,
            "id": identifier,
            "values": values,
            "status": "DISPATCHING",
        }
        self.ledger["operations"].append(item)
        self._save()
        writer = self._writer(model, values, company_id)
        try:
            if kind == "create":
                ids = writer.create_rows(model, (values,))
                require(len(ids) == 1, "SEED_RECEIPT_INVALID")
                item["receipt_ids"] = list(ids)
                self._own(model, ids, "explicit_seed_create")
                result = ids[0]
            else:
                require(
                    type(identifier) is int and identifier > 0, "SEED_EXACT_ID_REQUIRED"
                )
                writer.update_row(model, identifier, values)
                result = identifier
        except Exception:
            item["status"] = "FAILED_OR_UNKNOWN_RETAIN_FOR_DIAGNOSIS"
            self._save()
            raise
        item["status"] = "ACKNOWLEDGED"
        self._save()
        return result

    def validate(self):
        observed = recognize_odoo_version(
            self.reader.get_target_fingerprint().odoo_version
        )
        require(
            observed.major == self.major
            and observed.series == f"{self.major}.0"
            and observed.release_stage is OdooReleaseStage.FINAL
            and not observed.is_saas,
            "EXACT_RUNTIME_VERSION_REQUIRED",
        )
        self.reader.probe_read_identity((*MODELS, "res.country"))
        Json2WriteIdentityConnector(self.write_config).probe_write_identity(
            MODELS,
            MODELS,
            creatable_models=("account.payment.term", "res.company", "res.partner"),
        )
        requests = tuple(
            MetadataRequest(model, fields)
            for model, fields in (
                ("res.company", ("name", "user_ids", "partner_id")),
                ("res.partner", CONTACT_FIELDS),
                ("account.payment.term", TERM_FIELDS),
                ("account.payment.term.line", LINE_FIELDS),
            )
        )
        metadata = self.reader.get_model_metadata(requests)
        for request in requests:
            require(
                request.model in metadata.models
                and set(request.fields) <= set(metadata.models[request.model].fields),
                "SEED_FIELD_METADATA_MISSING",
            )
        context = self.reader._post_read_method(
            "res.users", "context_get", {"context": {}}
        )
        require(
            type(context.get("uid")) is int and context["uid"] > 0,
            "CURRENT_USER_CONTEXT_REQUIRED",
        )
        self.user_id = context["uid"]
        write_context = Json2ReadConnector(self.write_config)._post_read_method(
            "res.users", "context_get", {"context": {}}
        )
        require(
            write_context.get("uid") == self.user_id,
            "SAME_SETUP_API_PRINCIPAL_REQUIRED",
        )
        companies = self.records(
            "res.company", ("name", "partner_id"), (("id", ">", 0),), limit=3
        )
        require(len(companies) == 1, "FRESH_SINGLE_COMPANY_TARGET_REQUIRED")
        require(
            companies[0].values["name"] not in set(NAMES.values()),
            "TARGET_ALREADY_SEEDED",
        )
        self.initial_company = companies[0]
        refs = tuple(
            row["customer_ref"] for row in self.oracle["baseline"]["customers"]
        )
        require(
            not self.records("res.partner", ("ref",), (("ref", "in", refs),), limit=30),
            "FIXTURE_REFS_NOT_EMPTY",
        )
        require(
            not self.records(
                "account.payment.term",
                TERM_FIELDS,
                (
                    (
                        "name",
                        "in",
                        ("Morrow Net 30", "Morrow Net 45", "Morrow Immediate"),
                    ),
                ),
                limit=20,
            ),
            "FIXTURE_TERMS_NOT_EMPTY",
        )
        countries = self.records(
            "res.country", ("code",), (("code", "in", ("BE", "FR")),), limit=4
        )
        self.country_ids = {row.values["code"]: row.odoo_id for row in countries}
        require(set(self.country_ids) == {"BE", "FR"}, "STANDARD_COUNTRIES_REQUIRED")
        self.ledger["state"] = "VALIDATED_READ_ONLY"
        self.ledger["initial_company"] = {
            "id": self.initial_company.odoo_id,
            "values": dict(self.initial_company.values),
        }
        self._save()
        return {"major": self.major, "fresh_fixture_scope": True, "writes": 0}

    def seed(self, *, allow_write=False):
        require(allow_write is True, "ALLOW_WRITE_REQUIRED")
        require(
            self.ledger["state"] == "VALIDATED_READ_ONLY" and not self.seeded,
            "FRESH_VALIDATION_REQUIRED",
        )
        self.validate()
        self.seeded = True
        self.ledger["state"] = "SEEDING"
        self._save()
        north = self.initial_company.odoo_id
        self._mutate("write", "res.company", {"name": NAMES["NORTH"]}, identifier=north)
        south = self._mutate(
            "create",
            "res.company",
            {"name": NAMES["SOUTH"], "user_ids": [(4, self.user_id)]},
        )
        self.company_ids = {"NORTH": north, "SOUTH": south}
        company_rows = self.records(
            "res.company",
            ("partner_id",),
            (("id", "=", south),),
            company_id=south,
            limit=2,
        )
        require(len(company_rows) == 1, "CREATED_COMPANY_READBACK_REQUIRED")
        generated_partner = relation_id(company_rows[0].values["partner_id"])
        if generated_partner:
            self._own(
                "res.partner", (generated_partner,), "owned_company_generated_partner"
            )
        term_ids = {}
        for company, company_id in self.company_ids.items():
            term = self._mutate(
                "create",
                "account.payment.term",
                {"name": "Morrow Net 30", "company_id": company_id},
                company_id=company_id,
            )
            term_ids[company] = term
            lines = self.records(
                "account.payment.term.line",
                LINE_FIELDS,
                (("payment_id", "=", term),),
                company_id=company_id,
                limit=3,
            )
            require(
                len(lines) == 1
                and lines[0].values["value"] == "percent"
                and lines[0].values["value_amount"] == 100
                and lines[0].values["nb_days"] == 0,
                "STOCK_DEFAULT_SCHEDULE_DIFFERED",
            )
            self._own(
                "account.payment.term.line",
                (lines[0].odoo_id,),
                "owned_parent_generated_default",
            )
            self._mutate(
                "write",
                "account.payment.term.line",
                {"nb_days": 30},
                identifier=lines[0].odoo_id,
                company_id=company_id,
            )
        for row in self.seed_spec["customers"]:
            company_id = self.company_ids[row["company_code"]]
            address = row["address"]
            values = {
                "ref": row["customer_ref"],
                "name": row["name"],
                "email": row["email"] or False,
                "phone": row["phone"],
                "street": address["street"],
                "zip": address["postcode"],
                "city": address["city"],
                "country_id": self.country_ids[row["country_code"]],
                "company_id": False,
                "property_payment_term_id": term_ids[row["company_code"]],
            }
            self._mutate("create", "res.partner", values, company_id=company_id)
        self.ledger["state"] = "SEEDED"
        self._save()
        self.verify_seed()
        return {
            "major": self.major,
            "seed_contacts": 4,
            "seed_terms": 2,
            "seed_term_lines": 2,
            "companies": 2,
            "net45_seeded": False,
        }

    def contact_projection(self, refs, company):
        return self.records(
            "res.partner",
            CONTACT_FIELDS,
            (("ref", "in", tuple(refs)),),
            company_id=self.company_ids[company],
            limit=len(refs) + 1,
        )

    def verify_seed(self):
        refs = tuple(row["customer_ref"] for row in self.seed_spec["customers"])
        by_ref = {
            row.values["ref"]: row for row in self.contact_projection(refs, "NORTH")
        }
        require(set(by_ref) == set(refs), "SEED_CONTACT_REFS_DIFFERED")
        for expected in self.seed_spec["customers"]:
            actual = by_ref[expected["customer_ref"]].values
            require(
                actual["name"] == expected["name"]
                and (actual["email"] or "") == (expected["email"] or "")
                and actual["phone"] == expected["phone"],
                "SEED_CONTACT_VALUES_DIFFERED",
            )
        for company in self.company_ids:
            self.verify_terms(company=company, expected={"Morrow Net 30": 30})
        return True

    def verify_customers(
        self, *, company="NORTH", fresh=False, immediate_refs=(), with_terms=False
    ):
        section = "fresh_customer_delivery" if fresh else "baseline"
        expected = [
            row
            for row in self.oracle[section]["customers"]
            if row["company_code"] == company
            and (not immediate_refs or row["customer_ref"] in immediate_refs)
        ]
        actual = self.contact_projection(
            [row["customer_ref"] for row in expected], company
        )
        by_ref = {row.values["ref"]: row for row in actual}
        require(
            len(actual) == len(expected) and len(by_ref) == len(expected),
            "CUSTOMER_PROJECTION_GRAIN_DIFFERED",
        )
        for row in expected:
            values = by_ref[row["customer_ref"]].values
            require(
                all(
                    (values.get(field) or "") == (row[field] or "")
                    for field in ("name", "email", "phone")
                ),
                "CUSTOMER_PROJECTION_VALUES_DIFFERED",
            )
            require(
                (values["street"], values["zip"], values["city"])
                == (
                    row["address"]["street"],
                    row["address"]["postcode"],
                    row["address"]["city"],
                ),
                "CUSTOMER_ADDRESS_DIFFERED",
            )
            if with_terms:
                term_id = relation_id(values.get("property_payment_term_id"))
                require(term_id is not None, "CUSTOMER_TERM_MISSING")
                terms = self.records(
                    "account.payment.term",
                    TERM_FIELDS,
                    (("id", "=", term_id),),
                    company_id=self.company_ids[company],
                    limit=2,
                )
                name = (
                    "Morrow Immediate"
                    if immediate_refs
                    else "Morrow Net " + row["payment_term_key"][1][3:]
                )
                require(
                    len(terms) == 1
                    and terms[0].values["name"] == name
                    and relation_id(terms[0].values["company_id"])
                    == self.company_ids[company],
                    "CUSTOMER_TERM_SCOPE_DIFFERED",
                )
        return {"verified_contacts": len(expected)}

    def verify_terms(self, *, company="NORTH", expected=None):
        expected = expected or {"Morrow Net 30": 30, "Morrow Net 45": 45}
        company_id = self.company_ids[company]
        terms = self.records(
            "account.payment.term",
            TERM_FIELDS,
            (
                ("company_id", "=", company_id),
                ("name", "in", ("Morrow Net 30", "Morrow Net 45", "Morrow Immediate")),
            ),
            company_id=company_id,
            limit=5,
        )
        require(
            len(terms) == len(expected)
            and {r.values["name"] for r in terms} == set(expected),
            "TERM_NAMES_OR_SCOPE_DIFFERED",
        )
        for term in terms:
            lines = self.records(
                "account.payment.term.line",
                LINE_FIELDS,
                (("payment_id", "=", term.odoo_id),),
                company_id=company_id,
                limit=3,
            )
            require(len(lines) == 1, "TERM_SCHEDULE_GRAIN_DIFFERED")
            line = lines[0].values
            require(
                (
                    line["value"],
                    line["value_amount"],
                    line["nb_days"],
                    line["delay_type"],
                )
                == ("percent", 100, expected[term.values["name"]], "days_after"),
                "TERM_SCHEDULE_VALUES_DIFFERED",
            )
        return {"verified_terms": len(expected)}

    def snapshot_excluded_company(self, *, company="SOUTH"):
        refs = [
            row["customer_ref"]
            for row in self.seed_spec["customers"]
            if row["company_code"] == company
        ]
        rows = self.contact_projection(refs, company)
        return {
            row.values["ref"]: {"id": row.odoo_id, "values": dict(row.values)}
            for row in rows
        }

    def verify_excluded_company(self, before, *, company="SOUTH"):
        require(
            self.snapshot_excluded_company(company=company) == before,
            "EXCLUDED_COMPANY_CHANGED",
        )
        self.verify_terms(company=company, expected={"Morrow Net 30": 30})
        return {"excluded_company_unchanged": True}
