"""Exercise complete retained-data restores through real local services."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from impodo.application.odoo_saved_data_jobs import SavedOdooActionError
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.odoo_database_binding import database_scope_hash
from impodo.domain.odoo_saved_data import SavedOdooRestoreProfile
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import Capability
from impodo.domain.shared.models import FieldMetadata, OdooWriteIdentity, TargetRecord
from impodo.domain.workspace.workbench import OdooConnectionMode, target_identity_hash
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.composition.saved_odoo_restore import restore_saved_odoo_data
from tests.integration.web import test_saved_odoo_archive_lifecycle as archive_fixture
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase, _browser_schema
from tests.support.recipe_lifecycle import FictionalOdoo


class _RestoreOdoo(FictionalOdoo):
    def find_records(self, model, domain, fields):
        return self.read_ids(model, self.find_ids(model, domain), fields)

    def find_records_many(self, model, lookups):
        return tuple(
            self.find_records(model, item.domain, item.fields) for item in lookups
        )

    def update_rows(self, model, record_ids, values):
        for record_id in record_ids:
            self.update_row(model, record_id, values)


class SavedOdooRestoreFlowTests(ProjectSetupBrowserTestCase):
    def _version(self, major):
        return archive_fixture.SavedOdooArchiveLifecycleTests._captured_version(
            self, major
        )

    def _destination(self, major, mode, *, required=False, existing=False):
        context = self.app.state.context
        original_identity = context.read_identity_probe
        records = {("res.partner", 40): {"name": "Target only", "x_setup": "Unchanged"}}
        if existing:
            records[("res.partner", 41)] = {"name": "Alice", "x_setup": "Existing"}
        state = {"adapter": None}

        def schema(workspace, _key):
            self.assertIsNotNone(workspace.saved_restore_generation_id)
            metadata = _browser_schema(workspace)
            model = metadata.models["res.partner"]
            if required:
                model = replace(
                    model,
                    fields={
                        **model.fields,
                        "x_setup": FieldMetadata(
                            "x_setup",
                            "char",
                            "Setup",
                            required=True,
                            stored=True,
                            computed=False,
                            has_inverse=False,
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                    },
                )
            return replace(
                metadata,
                fingerprint=replace(metadata.fingerprint, odoo_version=f"{major}.0"),
                models={"res.partner": model},
            )

        def identity(workspace, key, models):
            result = original_identity(workspace, key, models)
            return replace(result, context_hash=workspace.odoo_company.context_hash)

        def match(workspace, key, metadata_requests, record_requests):
            metadata = schema(workspace, key)
            requested = {item.model: item.fields for item in record_requests}
            current = state["adapter"].records if state["adapter"] else records
            found = {
                model: tuple(
                    TargetRecord(
                        model, identifier, {name: values.get(name) for name in fields}
                    )
                    for (record_model, identifier), values in current.items()
                    if record_model == model and values["name"] in {"Alice", "Bob"}
                )
                for model, fields in requested.items()
            }
            return metadata, RecordSnapshot(metadata.fingerprint, found, requested)

        def writer(workspace, _key, scope):
            current = context.queries.get(workspace.workspace_id)
            selection = context.queries.get_source_selection(current.workspace_id)
            source = context.queries.get_odoo_schema_catalog(current.workspace_id)
            snapshot = context.transfer_execution.current_snapshot(
                current, selection, source
            )
            self.assertIsNotNone(snapshot)
            adapter = _RestoreOdoo(
                snapshot, scope, imports_external_ids=mode is OdooConnectionMode.REMOTE
            )
            adapter.records.update({key: dict(value) for key, value in records.items()})
            state["adapter"] = adapter
            return adapter

        def write_identity(workspace, _key, scope):
            return OdooWriteIdentity(
                target_hash=target_identity_hash(
                    connection_mode=mode.value,
                    base_url=workspace.odoo_base_url,
                    database=workspace.odoo_database,
                ),
                principal_hash="sha256:" + "1" * 64,
                permission_hash=content_hash("fixture write permission"),
                context_hash=workspace.odoo_company.context_hash,
                readable_models=tuple(item.model for item in scope.models),
                writable_models=tuple(
                    item.model for item in scope.models if item.write_fields
                ),
                observed_at=datetime.now(UTC).isoformat(),
            )

        return (
            state,
            records,
            {
                "schema_reader": schema,
                "read_identity_probe": identity,
                "destination_match_reader": match,
                "write_executor_factory": writer,
                "write_identity_probe": write_identity,
                "readback_reader_factory": lambda *_args: state["adapter"],
            },
        )

    def _profile(self, definition, mode, *, required=False, declare_recreated=True):
        now = datetime.now(UTC)
        profile = SavedOdooRestoreProfile(
            profile_id=str(uuid4()),
            project_id=definition.project_id,
            saved_data_id=definition.saved_data_id,
            label="Reset database",
            revision=1,
            connection_mode=mode.value,
            base_url="https://remote.example.test"
            if mode is OdooConnectionMode.REMOTE
            else "http://127.0.0.1:8069",
            database="production" if mode is OdooConnectionMode.REMOTE else "odoo-test",
            company_name="Destination company",
            credential_workspace_id=None,
            settings_json=canonical_json(
                {
                    "keys": {"res.partner": ["name"]},
                    "rules": {},
                    "defaults": {
                        "res.partner": {
                            "x_setup": {
                                "provider": "fixed_value",
                                "value": "Configured",
                            }
                        }
                    }
                    if required
                    else {},
                }
            ),
            created_at=now,
            updated_at=now,
        )
        # This fixture explicitly recreates its fictional same-endpoint target
        # once after capture. Restoring and retrying never declare generations.
        context = self.app.state.context
        identity = context.identity_resolver.resolve({"authenticated": True})

        def prepare_reset():
            source = context.queries.get(definition.source_workspace_id)
            if not declare_recreated or database_scope_hash(
                source.odoo_base_url, source.odoo_database
            ) != database_scope_hash(profile.base_url, profile.database):
                return
            current = context.odoo_database_bindings.current(
                source.workspace_id, base_url=profile.base_url,
                database=profile.database, actor=identity.actor,
                capability=Capability.SOURCE_CONFIGURE,
                scope=identity.organization_scope,
                authorization_receipt=identity.authorization,
            )
            context.odoo_database_bindings.declare_recreated(
                source.workspace_id, base_url=profile.base_url,
                database=profile.database,
                expected_generation_hash=current.generation_hash,
                operation_id=str(uuid4()),
                reason="The fixture recreated its test destination after saving clean data",
                actor=identity.actor, scope=identity.organization_scope,
                authorization_receipt=identity.authorization,
            )

        run_local_operation_sync(prepare_reset)
        return profile

    def _run(
        self,
        definition,
        version,
        profile,
        adapters,
        *,
        check_only=False,
        operation_id=None,
    ):
        context = self.app.state.context
        identity = context.identity_resolver.resolve({"authenticated": True})
        target_hash = target_identity_hash(
            connection_mode=profile.connection_mode,
            base_url=profile.base_url,
            database=profile.database,
        )
        company = OdooCompanySelection(target_hash, 7, profile.company_name)
        with (
            patch.multiple(context, **adapters) if adapters else nullcontext(),
            patch(
                "impodo.web.composition.saved_odoo_restore._company_choice_connector",
                return_value=SimpleNamespace(
                    get_company_access=lambda: SimpleNamespace(choices=(company,))
                ),
            ),
        ):
            return restore_saved_odoo_data(
                context,
                definition.project_id,
                definition.saved_data_id,
                version.version_number,
                actor=identity.actor,
                scope=identity.organization_scope,
                authorization_receipt=identity.authorization,
                operation_id=operation_id or str(uuid4()),
                profile=profile,
                api_key="destination-only-key",
                progress=lambda *_args: None,
                check_only=check_only,
            )

    def test_restore_and_repeat_keep_target_only_rows_and_protect_new_defaults(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    definition, version, _ = self._version(major)
                    state, records, adapters = self._destination(
                        major, mode, required=True, existing=True
                    )
                    result = self._run(
                        definition,
                        version,
                        self._profile(definition, mode, required=True),
                        adapters,
                    )
                    self.assertEqual(result.issues, ())
                    self.assertTrue(result.verified)
                    current = state["adapter"].records
                    self.assertEqual(len(current), 3)
                    self.assertEqual(
                        current[("res.partner", 40)], records[("res.partner", 40)]
                    )
                    self.assertEqual(
                        current[("res.partner", 41)]["x_setup"], "Existing"
                    )
                    self.assertEqual(
                        next(
                            value
                            for value in current.values()
                            if value["name"] == "Bob"
                        )["x_setup"],
                        "Configured",
                    )
                    records.clear()
                    records.update({key: dict(value) for key, value in current.items()})
                    profile = self.app.state.context.saved_odoo_data.repository.get_restore_profile(
                        definition.project_id,
                        definition.saved_data_id,
                        result.profile_id,
                    )
                    repeated = self._run(definition, version, profile, adapters)
                    self.assertTrue(repeated.verified)
                    self.assertNotEqual(repeated.workspace_id, result.workspace_id)
                    self.assertEqual(state["adapter"].records, records)

    def test_check_reads_destination_and_never_opens_writer(self):
        definition, version, _ = self._version(20)
        state, _records, adapters = self._destination(20, OdooConnectionMode.REMOTE)
        result = self._run(
            definition,
            version,
            self._profile(definition, OdooConnectionMode.REMOTE),
            adapters,
            check_only=True,
        )
        self.assertEqual(result.issues, ())
        self.assertIsNone(state["adapter"])
        self.assertFalse(result.verified)

    def test_retry_completed_operation_keeps_journal_and_exact_approved_evidence(self):
        definition, version, _ = self._version(20)
        state, _records, adapters = self._destination(20, OdooConnectionMode.REMOTE)
        profile = self._profile(definition, OdooConnectionMode.REMOTE)
        operation_id = str(uuid4())
        first = self._run(
            definition, version, profile, adapters, operation_id=operation_id
        )
        before = self.app.state.context.queries.get(first.workspace_id)
        writer = state["adapter"]
        with patch.object(
            self.app.state.context,
            "schema_reader",
            side_effect=AssertionError("Completed retry must not read Odoo"),
        ):
            repeated = self._run(
                definition, version, profile, {}, operation_id=operation_id
            )
        self.assertEqual(repeated.workspace_id, first.workspace_id)
        self.assertTrue(repeated.verified)
        self.assertEqual(self.app.state.context.queries.get(first.workspace_id), before)
        self.assertIs(state["adapter"], writer)
        repository = self.app.state.context.saved_odoo_data.repository
        bindings = repository.list_restore_bindings(
            definition.project_id, definition.saved_data_id
        )
        with (
            patch.object(
                type(self.app.state.context.reconciliation), "current", return_value=None
            ),
            self.assertRaises(SavedOdooActionError) as stopped,
        ):
            self._run(
                definition, version, replace(profile, profile_id=str(uuid4())), adapters
            )
        self.assertEqual(stopped.exception.redirect_url, first.redirect_url)
        self.assertEqual(
            repository.list_restore_bindings(
                definition.project_id, definition.saved_data_id
            ),
            bindings,
        )
        self.assertIs(state["adapter"], writer)

    def test_missing_required_default_returns_consolidated_review_before_write(self):
        definition, version, _ = self._version(19)
        state, _records, adapters = self._destination(
            19, OdooConnectionMode.REMOTE, required=True
        )
        result = self._run(
            definition,
            version,
            self._profile(definition, OdooConnectionMode.REMOTE),
            adapters,
        )
        self.assertIsNone(state["adapter"])
        self.assertTrue(
            any("x_setup" in message for message in result.issues), result.issues
        )
        self.assertEqual(result.default_rows[0]["name"], "x_setup")
