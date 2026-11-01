"""Validate one allocated hierarchy target and manage only its exact fixture records.

The target is an already-running disposable Odoo database. This module never
creates, resets, starts, or stops a database or service. Credentials use the
existing role- and target-bound vault envelopes. Portable receipts are owned
by the runner; exact target IDs stay in its private evidence directory.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from uuid import UUID

from impodo.adapters.odoo.connectors import (
    Json2Config,
    Json2ReadConnector,
    Json2WriteIdentityConnector,
    target_record_read_config,
)
from impodo.adapters.odoo.local_reader import LocalOdooMetadataReader
from impodo.adapters.odoo.local_stack import read_odoo_config
from impodo.adapters.odoo.writer import Json2WriteExecutor
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.odoo.compatibility import OdooReleaseStage, recognize_odoo_version
from impodo.domain.odoo.contracts import RecordRequest
from impodo.domain.run.setup import OdooConnectionMode
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.workspace.workbench import WorkspaceState
from impodo.web.target_credentials import TargetCredentialRole, get_target_credential
from tests.support.hierarchy_cases import (
    EXPECTED_CATEGORY_PATHS,
    EXPECTED_PRODUCT_PATHS,
)
from tests.support.hierarchy_workflow import EXISTING_CATEGORY_PATHS

MODELS = ("product.category", "product.template")
CATEGORY_NAMES = tuple(
    sorted({part for path in EXPECTED_CATEGORY_PATHS for part in path})
)
PRODUCT_REFS = tuple(EXPECTED_PRODUCT_PATHS)
MODULES = ("base", "contacts", "mrp", "product")
ROOT = Path(__file__).resolve().parents[1]


SAFE_FAILURE_CODES = frozenset(
    {
        "ALLOW_WRITE_REQUIRED",
        "SCENARIO_INVALID",
        "TARGET_DESCRIPTOR_INVALID",
        "DISPOSABLE_TARGET_REQUIRED",
        "TARGET_MATRIX_INVALID",
        "DISPOSABLE_DATABASE_NAME_REQUIRED",
        "LOCAL_PROFILE_REQUIRED",
        "REMOTE_LOCAL_PROFILE_FORBIDDEN",
        "LOCAL_PROFILE_FILES_REQUIRED",
        "LAB_PIN_MISMATCH",
        "SOURCE_PIN_INVALID",
        "LOCAL_SOURCE_PIN_MISMATCH",
        "LOCAL_PROFILE_TARGET_MISMATCH",
        "TARGET_CREDENTIALS_REQUIRED",
        "RUNTIME_VERSION_MISMATCH",
        "VAULT_ENTRY_ALREADY_EXISTS",
        "UNOWNED_VAULT_DELETE",
        "FIXTURE_READ_TRUNCATED",
        "LOCAL_READER_TARGET_MISMATCH",
        "FIXTURE_MODULES_REQUIRED",
        "CLEANUP_PERMISSION_REQUIRED",
        "FIXTURE_SCOPE_NOT_EMPTY",
        "EXECUTION_INCOMPLETE",
        "RECEIPT_INVALID",
        "CATEGORY_LINEAGE_INVALID",
        "CATEGORY_PATHS_DIFFER",
        "PRODUCT_PATHS_DIFFER",
        "PRODUCT_VARIANTS_DIFFER",
        "CLEANUP_UNCERTAIN",
        "CLEANUP_OWNERSHIP_MISMATCH",
        "CLEANUP_FOREIGN_DEPENDENT",
        "CLEANUP_EXTERNAL_ID_MISMATCH",
        "CLEANUP_CATEGORY_CYCLE",
        "CLEANUP_ABSENCE_UNPROVEN",
        "CLEANUP_VARIANTS_REMAIN",
        "CLEANUP_EXTERNAL_IDS_REMAIN",
        "LOCAL_PROFILE_UNAVAILABLE",
        "COMPANY_SCOPE_AMBIGUOUS",
        "INTERRUPTION_NOT_DURABLE",
        "LOAD_INCOMPLETE",
        "REQUESTED_FAULT_NOT_REACHED",
        "REPEAT_COMPARISON_DIFFERED",
    }
)


class QualificationError(RuntimeError):
    """Allow only registered static codes into portable qualification receipts."""

    @property
    def safe_code(self):
        return (
            str(self)
            if str(self) in SAFE_FAILURE_CODES
            else "QUALIFICATION_PHASE_FAILED"
        )


def require(condition, code):
    if not condition:
        raise QualificationError(code)


@dataclass(frozen=True)
class TargetDescriptor:
    base_url: str
    database: str = field(repr=False)
    credential_owner_id: str = field(repr=False)
    mode: str
    major: int
    local_config: Path | None = None
    python: Path | None = None
    odoo_bin: Path | None = None
    lab_target: str | None = None
    source_commit: str | None = None

    @classmethod
    def read(cls, path, *, mode, major):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        required = {
            "contract_version",
            "disposable",
            "purpose",
            "base_url",
            "database",
            "credential_owner_id",
        }
        optional = {"local_config", "python", "odoo_bin", "lab_target", "source_commit"}
        require(
            isinstance(payload, dict)
            and required <= set(payload) <= required | optional,
            "TARGET_DESCRIPTOR_INVALID",
        )
        require(
            type(payload["contract_version"]) is int
            and payload["contract_version"] == 1
            and payload["disposable"] is True
            and payload["purpose"] == "hierarchy_qualification",
            "DISPOSABLE_TARGET_REQUIRED",
        )
        require(
            mode in {"LOCAL", "REMOTE"} and major in {19, 20}, "TARGET_MATRIX_INVALID"
        )
        require(
            isinstance(payload["database"], str)
            and len(payload["database"]) <= 63
            and re.fullmatch(
                r"impodo_(?:hierarchy|scenario)_[a-z0-9_]+", payload["database"]
            ),
            "DISPOSABLE_DATABASE_NAME_REQUIRED",
        )
        owner = str(UUID(payload["credential_owner_id"]))
        config = Json2Config(
            payload["base_url"],
            payload["database"],
            "descriptor-validation",
            connection_mode=mode,
            retries=0,
        )
        paths = {
            name: Path(payload[name]).expanduser().resolve(strict=True)
            if payload.get(name)
            else None
            for name in ("local_config", "python", "odoo_bin")
        }
        require(mode != "LOCAL" or all(paths.values()), "LOCAL_PROFILE_REQUIRED")
        require(
            mode != "REMOTE" or not any(paths.values()),
            "REMOTE_LOCAL_PROFILE_FORBIDDEN",
        )
        require(
            all(path.is_file() for path in paths.values() if path is not None),
            "LOCAL_PROFILE_FILES_REQUIRED",
        )
        lab = payload.get("lab_target")
        commit = payload.get("source_commit")
        if lab is not None:
            manifest = json.loads(
                (ROOT / "fixtures/odoo-compatibility/lab-targets.json").read_text(
                    encoding="utf-8"
                )
            )
            expected = "baseline" if major == 19 else "odoo20"
            require(
                lab == expected and commit == manifest["targets"][expected]["commit"],
                "LAB_PIN_MISMATCH",
            )
        elif commit is not None:
            require(
                isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit),
                "SOURCE_PIN_INVALID",
            )
        result = cls(
            config.base_url,
            config.database,
            owner,
            mode,
            major,
            **paths,
            lab_target=lab,
            source_commit=commit,
        )
        if mode == "LOCAL":
            result.profile()
            if commit:
                if result.odoo_bin is None:
                    raise QualificationError("LOCAL_PROFILE_REQUIRED")
                actual = subprocess.check_output(
                    ["git", "-C", str(result.odoo_bin.parent), "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                ).strip()
                require(actual == commit, "LOCAL_SOURCE_PIN_MISMATCH")
        return result

    def profile(self):
        if self.local_config is None:
            raise QualificationError("LOCAL_PROFILE_REQUIRED")
        profile = read_odoo_config(self.local_config)
        require(
            profile.base_url.rstrip("/") == self.base_url.rstrip("/")
            and profile.database_hint == self.database,
            "LOCAL_PROFILE_TARGET_MISMATCH",
        )
        return replace(profile, python_path=self.python, odoo_bin_path=self.odoo_bin)

    def workspace(self):
        return WorkspaceState(
            workspace_id=self.credential_owner_id,
            name="Disposable hierarchy qualification",
            source_system="Fictional hierarchy",
            odoo_connection_mode=OdooConnectionMode(self.mode),
            odoo_base_url=self.base_url,
            odoo_database=self.database,
            intended_models=MODELS,
        )

    def credentials(self, vault):
        state = self.workspace()
        read = get_target_credential(vault, state, TargetCredentialRole.READ)
        write = get_target_credential(vault, state, TargetCredentialRole.WRITE)
        if read is None or write is None:
            raise QualificationError("TARGET_CREDENTIALS_REQUIRED")
        return read.secret, write.secret

    def config(self, key, *, context=None):
        return Json2Config(
            self.base_url,
            self.database,
            key,
            connection_mode=self.mode,
            retries=0,
            context=context or {},
            relevant_modules=MODULES,
        )


def require_runtime(version, major):
    observed = recognize_odoo_version(version)
    require(
        observed.problem is None
        and observed.major == major
        and observed.series == f"{major}.0"
        and not observed.is_saas
        and observed.release_stage is OdooReleaseStage.FINAL,
        "RUNTIME_VERSION_MISMATCH",
    )
    return observed.raw


class OwnedVault:
    """Track new fixture keys without adopting or deleting an existing vault entry."""

    def __init__(self, vault, ledger_path):
        self.vault, self.ledger_path = vault, ledger_path
        self.owned = set()

    def get(self, credential_id):
        return self.vault.get(credential_id)

    def set(self, credential_id, secret, *, persistent):
        require(
            credential_id in self.owned or self.vault.get(credential_id) is None,
            "VAULT_ENTRY_ALREADY_EXISTS",
        )
        self.owned.add(credential_id)
        self.ledger_path.write_text(json.dumps(sorted(self.owned)), encoding="utf-8")
        self.vault.set(credential_id, secret, persistent=persistent)

    def delete(self, credential_id):
        require(credential_id in self.owned, "UNOWNED_VAULT_DELETE")
        self.vault.delete(credential_id)
        self.owned.remove(credential_id)

    def cleanup(self):
        for credential_id in tuple(self.owned):
            if ":protected:" not in credential_id:
                self.delete(credential_id)
        self.ledger_path.write_text(json.dumps(sorted(self.owned)), encoding="utf-8")


class FixtureTarget:
    """Use bulk reads and exact owned IDs; never infer ownership from a name alone."""

    def __init__(self, descriptor, read_key, write_key, private):
        self.descriptor = descriptor
        self.read_config, self.write_config = (
            target_record_read_config(descriptor.config(read_key)),
            descriptor.config(write_key),
        )
        self.reader = Json2ReadConnector(self.read_config)
        scope = OdooApiScope(
            preview_hash="sha256:" + "7" * 64,
            models=(
                OdooModelScope(
                    "product.category",
                    write_fields=("name", "parent_id"),
                    read_fields=("name", "parent_id"),
                    lookup_fields=("name", "parent_id"),
                ),
            ),
        )
        self.seeder = Json2WriteExecutor(self.write_config, scope)
        self.private = private
        self.owned = {model: set() for model in MODELS}
        self.external_ids = ()
        self.seeded = False

    def records(self, model, fields, domain, limit=100):
        rows = self.reader.get_records(
            (RecordRequest(model, tuple(fields), domain=tuple(domain), limit=limit),)
        ).records.get(model, ())
        require(len(rows) < limit, "FIXTURE_READ_TRUNCATED")
        return tuple(rows)

    def scope_records(self):
        categories = self.records(
            "product.category", ("name", "parent_id"), (("name", "in", CATEGORY_NAMES),)
        )
        products = self.records(
            "product.template",
            ("default_code", "categ_id", "name"),
            (("default_code", "in", PRODUCT_REFS),),
        )
        return categories, products

    def validate(self):
        fingerprint = self.reader.get_target_fingerprint()
        version = fingerprint.odoo_version
        require_runtime(version, self.descriptor.major)
        if self.descriptor.mode == "LOCAL":
            local = LocalOdooMetadataReader().get_target_fingerprint(
                self.descriptor.workspace(), self.descriptor.profile()
            )
            require_runtime(local.odoo_version, self.descriptor.major)
            require(
                local.target_hash == fingerprint.target_hash,
                "LOCAL_READER_TARGET_MISMATCH",
            )
        modules = self.records(
            "ir.module.module",
            ("name", "state"),
            (("name", "in", MODULES),),
            len(MODULES) + 1,
        )
        require(
            {
                row.values.get("name")
                for row in modules
                if row.values.get("state") == "installed"
            }
            == set(MODULES),
            "FIXTURE_MODULES_REQUIRED",
        )
        self.reader.probe_read_identity(
            (*MODELS, "ir.model", "product.product", "ir.model.data")
        )
        Json2WriteIdentityConnector(self.write_config).probe_write_identity(
            (*MODELS, "product.product", "ir.model.data"),
            MODELS,
            creatable_models=MODELS,
        )
        for model in (*MODELS, "product.product", "ir.model.data"):
            require(
                Json2ReadConnector(self.write_config)._post_read_method(
                    model,
                    "has_access",
                    {"ids": [], "operation": "unlink", "context": {}},
                )
                is True,
                "CLEANUP_PERMISSION_REQUIRED",
            )
        categories, products = self.scope_records()
        require(not categories and not products, "FIXTURE_SCOPE_NOT_EMPTY")
        return version

    def _save_owned(self):
        (self.private / "owned-records.json").write_text(
            json.dumps(
                {
                    "records": {
                        model: sorted(ids) for model, ids in self.owned.items()
                    },
                    "external_ids": self.external_ids,
                }
            ),
            encoding="utf-8",
        )

    def seed(self):
        self.seeded = True
        paths = {}
        for depth in range(1, 5):
            level = [path for path in EXISTING_CATEGORY_PATHS if len(path) == depth]
            if not level:
                continue
            identifiers = self.seeder.create_rows(
                "product.category",
                tuple(
                    {"name": path[-1], "parent_id": paths.get(path[:-1], False)}
                    for path in level
                ),
            )
            self.owned["product.category"].update(identifiers)
            self._save_owned()
            paths.update(zip(level, identifiers, strict=True))

    def adopt_verified(self, run):
        require(
            run.status.value == "COMPLETED" and run.committed_count == 13,
            "EXECUTION_INCOMPLETE",
        )
        for row in run.rows:
            if row.status.value == "COMMITTED":
                require(
                    row.target_model in self.owned
                    and type(row.odoo_id) is int
                    and row.odoo_id > 0,
                    "RECEIPT_INVALID",
                )
                self.owned[row.target_model].add(row.odoo_id)
        self.external_ids = tuple(
            row.proposed_external_id for row in run.rows if row.proposed_external_id
        )
        self._save_owned()

    def verify_business(self):
        categories, products = self.scope_records()
        by_id = {row.odoo_id: row.values for row in categories}

        def path(identifier, seen=()):
            require(
                identifier not in seen and identifier in by_id,
                "CATEGORY_LINEAGE_INVALID",
            )
            values = by_id[identifier]
            parent = relation_id(values.get("parent_id"))
            return (
                (*path(parent, (*seen, identifier)), values["name"])
                if parent
                else (values["name"],)
            )

        paths = {identifier: path(identifier) for identifier in by_id}
        require(
            len(categories) == 12 and set(paths.values()) == EXPECTED_CATEGORY_PATHS,
            "CATEGORY_PATHS_DIFFER",
        )
        actual = {
            row.values["default_code"]: paths.get(
                relation_id(row.values.get("categ_id")) or 0
            )
            for row in products
        }
        require(
            len(products) == 6 and actual == EXPECTED_PRODUCT_PATHS,
            "PRODUCT_PATHS_DIFFER",
        )
        variants = self.records(
            "product.product",
            ("product_tmpl_id",),
            (("product_tmpl_id", "in", tuple(row.odoo_id for row in products)),),
        )
        require(
            len(variants) == 6
            and {relation_id(row.values.get("product_tmpl_id")) for row in variants}
            == {row.odoo_id for row in products},
            "PRODUCT_VARIANTS_DIFFER",
        )
        self.variant_count = len(variants)
        return categories, products

    def _unlink(self, model, identifiers):
        if not identifiers:
            return
        status, result = self.seeder.transport(
            self.write_config.base_url + "/json/2/" + model + "/unlink",
            {
                "Authorization": "bearer " + self.write_config.api_key,
                "Content-Type": "application/json",
                "X-Odoo-Database": self.write_config.database,
            },
            canonical_json_bytes({"ids": sorted(identifiers), "context": {}}),
            self.write_config.timeout_seconds,
            "POST",
        )
        require(status == 200 and result is True, "CLEANUP_UNCERTAIN")

    def cleanup(self):
        categories, products = self.verify_business()
        require(
            {row.odoo_id for row in categories} == self.owned["product.category"]
            and {row.odoo_id for row in products} == self.owned["product.template"],
            "CLEANUP_OWNERSHIP_MISMATCH",
        )
        linked = self.records(
            "product.template",
            ("categ_id",),
            (("categ_id", "in", tuple(self.owned["product.category"])),),
        )
        children = self.records(
            "product.category",
            ("parent_id",),
            (("parent_id", "in", tuple(self.owned["product.category"])),),
        )
        require(
            {row.odoo_id for row in linked} <= self.owned["product.template"]
            and {row.odoo_id for row in children} <= self.owned["product.category"],
            "CLEANUP_FOREIGN_DEPENDENT",
        )
        # Unlink may also cascade to bindings outside the import namespace.
        # Refuse foreign bindings before deleting any owned business record.
        for model in MODELS:
            attached = self.records(
                "ir.model.data",
                ("module", "name"),
                (("model", "=", model), ("res_id", "in", tuple(self.owned[model]))),
            )
            require(
                all(
                    row.values["module"] + "." + row.values["name"] in self.external_ids
                    for row in attached
                ),
                "CLEANUP_EXTERNAL_ID_MISMATCH",
            )
        bindings = []
        if self.external_ids and self.descriptor.mode == "REMOTE":
            modules = tuple(
                sorted({value.split(".", 1)[0] for value in self.external_ids})
            )
            names = tuple(value.split(".", 1)[1] for value in self.external_ids)
            bindings = self.records(
                "ir.model.data",
                ("module", "name", "model", "res_id"),
                (("module", "in", modules), ("name", "in", names)),
            )
            require(
                all(
                    row.values["module"] + "." + row.values["name"] in self.external_ids
                    and row.values["res_id"] in self.owned.get(row.values["model"], ())
                    for row in bindings
                ),
                "CLEANUP_EXTERNAL_ID_MISMATCH",
            )
        self._unlink("ir.model.data", {row.odoo_id for row in bindings})
        self._unlink("product.template", self.owned["product.template"])
        remaining = {
            row.odoo_id: relation_id(row.values.get("parent_id")) for row in categories
        }
        while remaining:
            parents = set(remaining.values())
            leaves = set(remaining) - parents
            require(leaves, "CLEANUP_CATEGORY_CYCLE")
            self._unlink("product.category", leaves)
            for identifier in leaves:
                remaining.pop(identifier)
        require(not any(self.scope_records()), "CLEANUP_ABSENCE_UNPROVEN")
        variants = self.records(
            "product.product",
            ("product_tmpl_id",),
            (("product_tmpl_id", "in", tuple(self.owned["product.template"])),),
        )
        require(not variants, "CLEANUP_VARIANTS_REMAIN")
        if bindings:
            remaining_bindings = self.records(
                "ir.model.data",
                ("module", "name"),
                (("id", "in", tuple(row.odoo_id for row in bindings)),),
            )
            require(not remaining_bindings, "CLEANUP_EXTERNAL_IDS_REMAIN")


def relation_id(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else False
    return value if type(value) is int and value > 0 else None
