"""Bind the practice exercises to an explicitly allocated disposable target.

This module starts no service, provisions no database, and opens no browser.
Source acceptance and every persisted workflow boundary use the normal local
Impodo application; LOCAL reads use the actual native Odoo reader.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from impodo.adapters.odoo.local_stack import LocalStackService, probe_local_stack
from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.domain.mapping.contracts import TargetFieldDisposition, TargetFieldHandling
from impodo.domain.mapping.create_field_policy import (
    CreateFieldCoverage,
    evaluate_create_field,
)
from impodo.domain.odoo.compatibility import OdooReleaseStage, recognize_odoo_version
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.web.app import create_local_app
from impodo.web.composition.odoo_company import read_company_access
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.composition.target_readers import _read_readiness_snapshots
from impodo.web.routers.schema import (
    _capture_selected_schema_sync,
    _refresh_model_catalog_sync,
)
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    store_target_credential,
)
from scripts.practice_customer_workflow import COMPANY_NAMES, PracticeCustomerWorkflow
from tests.support.browser_scenarios import _csrf
from tests.support.identity import resolve_local_actor

MODELS = (
    "res.partner",
    "account.payment.term",
    "account.payment.term.line",
    "res.company",
)


@dataclass(frozen=True)
class PracticeConnection:
    """Read only a named disposable allocation and role-separated key files."""

    base_url: str
    database: str = field(repr=False)
    mode: str
    major: int
    local_config: Path | None
    python: Path | None
    odoo_bin: Path | None
    read_key: str = field(repr=False)
    write_key: str = field(repr=False)
    source_commit: str = ""
    postgres_version: str = ""

    @classmethod
    def read(cls, path, *, expected_major):
        payload = json.loads(Path(path).read_text())
        required = {
            "contract_version",
            "disposable",
            "purpose",
            "major",
            "mode",
            "base_url",
            "database",
            "read_api_key_file",
            "write_api_key_file",
        }
        optional = {
            "local_config",
            "python",
            "odoo_bin",
            "source_commit",
            "postgres_version",
            "exercise_case",
            "template_database",
        }
        if (
            not isinstance(payload, dict)
            or not required <= payload.keys() <= required | optional
        ):
            raise ValueError("TARGET_DESCRIPTOR_INVALID")
        if (
            payload["contract_version"] != 1
            or payload["disposable"] is not True
            or payload["purpose"] != "morrow_practice_qualification"
            or payload["major"] != expected_major
            or expected_major not in (19, 20)
            or payload["mode"] not in ("LOCAL", "REMOTE")
            or not re.fullmatch(r"impodo_scenario_[a-z0-9_]{1,48}", payload["database"])
        ):
            raise ValueError("DISPOSABLE_TARGET_REQUIRED")
        url = urlsplit(payload["base_url"])
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("TARGET_DESCRIPTOR_INVALID")
        if payload["mode"] == "LOCAL":
            if url.hostname not in ("127.0.0.1", "::1") or url.scheme not in (
                "http",
                "https",
            ):
                raise ValueError("LOCAL_LOOPBACK_REQUIRED")
        elif url.scheme != "https":
            raise ValueError("REMOTE_HTTPS_REQUIRED")
        paths = {
            name: Path(payload[name]).expanduser().absolute()
            if payload.get(name)
            else None
            for name in ("local_config", "python", "odoo_bin")
        }
        # Execute the selected venv entry point; resolving its Python symlink
        # would silently discard that environment's installed dependencies.
        for selected_path in paths.values():
            if selected_path is not None:
                selected_path.resolve(strict=True)
        if payload["mode"] == "LOCAL" and not all(paths.values()):
            raise ValueError("LOCAL_PROFILE_REQUIRED")
        if payload["mode"] == "REMOTE" and any(paths.values()):
            raise ValueError("REMOTE_LOCAL_PROFILE_FORBIDDEN")
        keys = [
            Path(payload[name]).resolve(strict=True).read_text().strip()
            for name in ("read_api_key_file", "write_api_key_file")
        ]
        if not all(keys):
            raise ValueError("TARGET_CREDENTIALS_REQUIRED")
        return cls(
            payload["base_url"].rstrip("/"),
            payload["database"],
            payload["mode"],
            expected_major,
            **paths,
            read_key=keys[0],
            write_key=keys[1],
            source_commit=payload.get("source_commit", ""),
            postgres_version=payload.get("postgres_version", ""),
        )


class LivePracticeCustomerWorkflow(PracticeCustomerWorkflow):
    """Use authenticated in-process routes and real target readers and writers."""

    def __init__(self, connection, storage):
        super().__init__(methodName="runTest")
        self.connection = connection
        self.storage = Path(storage)
        self.temporary = SimpleNamespace(name=str(self.storage))
        self.secrets = MemorySecretStore()
        self._open()

    def _open(self):
        def probe(profile):
            if (
                profile.base_url.rstrip("/") != self.connection.base_url
                or profile.database_hint != self.connection.database
            ):
                raise ValueError("LOCAL_PROFILE_TARGET_MISMATCH")
            return probe_local_stack(
                replace(
                    profile,
                    python_path=self.connection.python,
                    odoo_bin_path=self.connection.odoo_bin,
                )
            )

        self.app = create_local_app(
            self.storage,
            launch_token="morrow-private-qualification",
            secret_store=self.secrets,
            local_stack_service=LocalStackService(probe=probe),
        )
        self.context = self.app.state.context
        self.actor = resolve_local_actor(self.context)
        self.client = TestClient(self.app)
        self.assertEqual(
            self.client.get(
                "/launch?token=morrow-private-qualification", follow_redirects=False
            ).status_code,
            303,
        )
        self.csrf = _csrf(self.client.get("/projects").text)

    def close(self):
        self.client.close()
        for name in (
            "load_jobs",
            "preparation_jobs",
            "odoo_capture_jobs",
            "recipe_run_jobs",
            "preflight_jobs",
        ):
            manager = getattr(self.context, name, None)
            if manager is not None:
                manager.shutdown()

    def reopen(self, **dependencies):
        if dependencies:
            raise ValueError("Unexpected target override")
        self.close()
        self._open()

    def bind_local(self, workspace_id):
        if self.connection.mode == "LOCAL":
            selected = self.context.local_stack.select_config(
                workspace_id, self.connection.local_config
            )
            self.assertIsNotNone(selected.profile)

    def install_practice_schema(self, workspace_id, *, target, major, mode, company):
        del target
        self.assertEqual((major, mode), (self.connection.major, self.connection.mode))
        context, actor = self.context, self.actor
        state = run_local_operation_sync(
            lambda: context.workspace_states.update_target(
                workspace_id,
                actor=actor,
                expected_revision=context.queries.get(workspace_id).revision,
                odoo_connection_mode=mode,
                odoo_base_url=self.connection.base_url,
                odoo_database=self.connection.database,
                intended_applications=(),
                intended_models=MODELS,
            )
        )
        self.bind_local(workspace_id)
        owner = run_local_operation_sync(
            context.target_credential_workspace,
            workspace_id,
            workspace_state=state,
            actor=actor,
        )
        store_target_credential(
            self.secrets,
            owner,
            TargetCredentialRole.READ,
            self.connection.read_key,
            persistent=True,
        )
        if mode == "LOCAL":
            profile = context.local_stack.get(workspace_id).profile
            access = context.local_odoo_reader.get_company_access(state, profile)
        else:
            access = read_company_access(state, self.connection.read_key)
        choices = [
            choice for choice in access.choices if choice.name == COMPANY_NAMES[company]
        ]
        self.assertEqual(len(choices), 1, "COMPANY_SCOPE_AMBIGUOUS")
        state = run_local_operation_sync(
            context.workspace_states.select_odoo_company,
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            selection=choices[0],
        )
        _refresh_model_catalog_sync(context, state, actor=actor)
        schema = _capture_selected_schema_sync(context, state, actor=actor)
        version = recognize_odoo_version(schema.odoo_version)
        self.assertEqual(
            (version.major, version.release_stage), (major, OdooReleaseStage.FINAL)
        )
        run_local_operation_sync(
            context.schema_workspace.govern,
            workspace_id,
            actor=actor,
            business_keys=(
                BusinessKeyDefinition(
                    "practice:company-name",
                    "res.company",
                    ("name",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "practice:partner-ref",
                    "res.partner",
                    ("ref",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "practice:term-name-company",
                    "account.payment.term",
                    ("name",),
                    scope_fields=("company_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
                BusinessKeyDefinition(
                    "practice:sole-term-line",
                    "account.payment.term.line",
                    ("payment_id",),
                    status=BusinessKeyStatus.CONFIRMED,
                ),
            ),
        )
        return schema

    def submit(self, workspace_id, mappings, **kwargs):
        schema = run_local_operation_sync(
            self.context.queries.get_odoo_schema_catalog, workspace_id
        )
        models = {model.name: model for model in schema.models}
        completed = []
        for mapping in mappings:
            provided = {
                item.target_field for item in (*mapping.fields, *mapping.relationships)
            }
            provided.update(
                field
                for item in (*mapping.target_identity, *mapping.target_scope)
                for field in item.target_fields
            )
            defaults = tuple(
                TargetFieldDisposition(item.name, TargetFieldHandling.ODOO_DEFAULT)
                for item in models[mapping.target_model].fields
                if evaluate_create_field(
                    item,
                    provided=item.name in provided,
                    handling=None,
                    target_model=mapping.target_model,
                    odoo_version=schema.odoo_version,
                ).coverage
                is CreateFieldCoverage.DEFAULT_AVAILABLE
            )
            completed.append(
                replace(
                    mapping,
                    target_field_dispositions=(
                        *mapping.target_field_dispositions,
                        *defaults,
                    ),
                )
            )
        return run_local_operation_sync(
            super().submit, workspace_id, completed, **kwargs
        )

    def compare(self, workspace_id):
        state = run_local_operation_sync(self.context.queries.get, workspace_id)
        return self.context.preflight.compare(
            workspace_id,
            actor=self.actor,
            reader=lambda request: _read_readiness_snapshots(
                self.context, state, request, actor=self.actor
            ),
            local_operation=run_local_operation_sync,
        )

    def ports(self, workspace_id, preview):
        state = run_local_operation_sync(self.context.queries.get, workspace_id)
        owner = run_local_operation_sync(
            self.context.target_credential_workspace,
            workspace_id,
            workspace_state=state,
            actor=self.actor,
        )
        credential = get_target_credential(
            self.secrets, owner, TargetCredentialRole.WRITE
        )
        if credential is None:
            credential = store_target_credential(
                self.secrets,
                owner,
                TargetCredentialRole.WRITE,
                self.connection.write_key,
                persistent=True,
            )
        remote = state.odoo_connection_mode.value == "REMOTE"
        read = (
            self.context.read_identity_probe(
                state, self.connection.read_key, preview.snapshot.readable_models
            )
            if remote
            else None
        )
        write = (
            self.context.write_identity_probe(
                state, credential.secret, preview.api_scope
            )
            if remote
            else None
        )
        return (
            self.context.write_executor_factory(
                state, credential.secret, preview.api_scope
            ),
            self.context.readback_reader_factory(
                state, credential.secret, preview.api_scope
            ),
            {
                "read_identity": read,
                "read_credential_binding_hash": preview.snapshot.read_credential_binding_hash
                if read
                else "",
                "write_identity": write,
                "write_credential_binding_hash": credential.binding_hash
                if write
                else "",
            },
        )
