"""Verify saved-data configure and interrupted refresh against physical stores."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock, patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_database_binding import OdooDatabaseBindingRole
from impodo.domain.odoo_source_policy import odoo_source_policy_hash
from impodo.domain.shared.access import Capability
from impodo.domain.shared.models import OdooReadIdentity
from impodo.web.composition.database_bindings import review_database_binding
from impodo.web.composition.saved_odoo_data import saved_capture_access
from impodo.web.target_credentials import TargetCredentialRole, store_target_credential
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    _browser_model_catalog,
    _browser_schema,
    _BrowserOdooCaptureGateway,
)
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import resolve_local_actor


class _RefreshGateway(_BrowserOdooCaptureGateway):
    def __init__(self, context, state, metadata, initial_schema):
        self.application_context = context
        self.metadata = metadata
        current = context.queries.get_odoo_schema_catalog(state.workspace_id)
        super().__init__(state, current or initial_schema)

    def probe_identity(self, request, *, cancellation=None):
        self.schema = self.application_context.queries.get_odoo_schema_catalog(
            self.workspace_state.workspace_id
        )
        self.identity_context_hash = self.schema.read_context_hash
        return super().probe_identity(request, cancellation=cancellation)

    def probe_schema(self, request, context, *, cancellation=None):
        return self.metadata


class SavedOdooDataRefreshLifecycleTests(ProjectSetupBrowserTestCase):
    def _configured(self, major):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        state, schema = self._registered_remote_schema_workspace()
        schema = replace(
            schema,
            odoo_version=f"{major}.0",
            policy_hash=odoo_source_policy_hash(major),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            state.workspace_id, schema, actor=actor
        )
        request_identity = context.identity_resolver.resolve({"authenticated": True})
        review_database_binding(
            context,
            state,
            OdooDatabaseBindingRole.SOURCE,
            actor=actor,
            scope=request_identity.organization_scope,
            authorization_receipt=request_identity.authorization,
        )
        response = self._post(
            f"/workspaces/{state.workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "contacts",
                "model": "res.partner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(response.status_code, 303, response.text)
        company = OdooCompanySelection(
            schema.connection_target_hash, 1, "Source company"
        )
        store_target_credential(
            context.secret_store,
            state,
            TargetCredentialRole.READ,
            "fictional-source-read-key",
            persistent=False,
        )

        def read(fresh, credential_owner):
            metadata = _browser_schema(fresh)
            metadata = replace(
                metadata,
                fingerprint=replace(
                    metadata.fingerprint,
                    odoo_version=f"{major}.0",
                    module_versions={"base": f"{major}.0.1.0"},
                ),
            )
            identity = OdooReadIdentity(
                target_hash=metadata.fingerprint.target_hash,
                principal_hash=schema.read_principal_hash,
                permission_hash=schema.read_permission_hash,
                context_hash=company.context_hash,
                readable_models=("res.partner",),
                observed_at=datetime.now(UTC).isoformat(),
            )
            catalog = _browser_model_catalog(fresh)
            catalog = replace(catalog, fingerprint=metadata.fingerprint)
            with (
                patch.object(
                    context,
                    "read_identity_probe",
                    side_effect=lambda target, key, models: replace(
                        identity, readable_models=models
                    ),
                ) as identities,
                patch.object(context, "schema_reader", return_value=metadata),
                patch.object(context, "model_catalog_reader", return_value=catalog),
                patch.object(
                    context,
                    "source_capture_factory",
                    return_value=_RefreshGateway(context, fresh, metadata, schema),
                ),
            ):
                access = saved_capture_access(
                    context,
                    fresh,
                    credential_owner,
                    actor=actor,
                    scope=request_identity.organization_scope,
                    authorization_receipt=request_identity.authorization,
                )
                self.assertEqual(
                    [call.args[2] for call in identities.call_args_list],
                    [("ir.model",), ("res.partner",)],
                )
                return access

        access_factory = Mock(side_effect=read)
        service = context.saved_odoo_data
        operation = str(uuid4())
        definition = service.configure(
            state.workspace_id,
            actor=actor,
            label=f"Clean contacts {major}",
            source_company=company,
            all_fields=False,
            operation_id=operation,
            access_factory=access_factory,
        )
        access_factory.reset_mock()
        self.assertEqual(
            service.configure(
                state.workspace_id,
                actor=actor,
                label=f"Clean contacts {major}",
                source_company=company,
                all_fields=False,
                operation_id=operation,
                access_factory=access_factory,
            ),
            definition,
        )
        access_factory.assert_not_called()
        return service, definition, access_factory, actor

    def test_update_requires_current_source_review_before_enqueuing(self):
        context = self.app.state.context
        for major in (19, 20):
            for problem in ("unbound", "recreated"):
                with self.subTest(major=major, problem=problem):
                    service, definition, factory, actor = self._configured(major)
                    if problem == "unbound":
                        database = service.repository.database
                        with database.connect(database.registry_path) as connection:
                            connection.execute(
                                "DELETE FROM saved_odoo_source_binding WHERE saved_data_id = ?",
                                [definition.saved_data_id],
                            )
                    else:
                        identity = context.identity_resolver.resolve(
                            {"authenticated": True}
                        )
                        state = context.queries.get(definition.source_workspace_id)
                        current = context.odoo_database_bindings.current(
                            state.workspace_id,
                            base_url=state.odoo_base_url,
                            database=state.odoo_database,
                            actor=actor,
                            capability=Capability.PROJECT_ADMIN,
                            scope=identity.organization_scope,
                            authorization_receipt=identity.authorization,
                        )
                        context.odoo_database_bindings.declare_recreated(
                            state.workspace_id,
                            base_url=state.odoo_base_url,
                            database=state.odoo_database,
                            actor=actor,
                            expected_generation_hash=current.generation_hash,
                            operation_id=str(uuid4()),
                            reason="Isolated test recreation",
                            scope=identity.organization_scope,
                            authorization_receipt=identity.authorization,
                        )
                    with (
                        patch.object(context.saved_odoo_jobs, "enqueue") as enqueue,
                        patch.object(context, "source_capture_factory") as remote,
                        count_duckdb_statements() as statements,
                        collect_duckdb_request_timings() as timings,
                    ):
                        response = self._post(
                            f"/projects/{definition.project_id}/saved-odoo-data/{definition.saved_data_id}/update",
                            {
                                "csrf_token": self.csrf,
                                "revision": str(definition.revision),
                                "operation_id": str(uuid4()),
                            },
                        )
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertIn("fresh", response.text)
                    self.assertIn(
                        "Review a database reset or rebind the source", response.text
                    )
                    enqueue.assert_not_called()
                    remote.assert_not_called()
                    factory.assert_not_called()
                    self.assertEqual(
                        service.get(
                            definition.project_id, definition.saved_data_id, actor=actor
                        ),
                        definition,
                    )
                    self.assertEqual(
                        timings.connection_count, 5 if problem == "unbound" else 8
                    )
                    self.assertEqual(
                        timings.schema_check_count, 5 if problem == "unbound" else 8
                    )
                    self.assertEqual(timings.lock_retry_count, 0)
                    self.assertLessEqual(
                        len(statements), 79 if problem == "unbound" else 116
                    )

    def test_physical_capture_acceptance_interruption_recovers_without_source(self):
        for major in (19, 20):
            with self.subTest(major=major):
                service, definition, factory, actor = self._configured(major)
                operation = str(uuid4())
                accept = service.projections.accept_odoo_capture
                with (
                    patch.object(
                        service.projections,
                        "accept_odoo_capture",
                        side_effect=RuntimeError("accept interrupted"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "accept interrupted"),
                ):
                    service.refresh(
                        definition.project_id,
                        definition.saved_data_id,
                        actor=actor,
                        expected_revision=definition.revision,
                        operation_id=operation,
                        access_factory=factory,
                    )
                self.assertIsNone(
                    service.get(
                        definition.project_id, definition.saved_data_id, actor=actor
                    ).latest_version
                )
                factory.side_effect = AssertionError(
                    "Offline recovery must not read source Odoo"
                )
                self.assertEqual(service.projections.accept_odoo_capture, accept)
                version = service.refresh(
                    definition.project_id,
                    definition.saved_data_id,
                    actor=actor,
                    expected_revision=definition.revision,
                    operation_id=operation,
                    access_factory=factory,
                )
                self.assertEqual(version.version_number, 1)
                self.assertEqual(version.row_counts, (("res.partner", 2),))
                self.assertEqual(
                    service.refresh(
                        definition.project_id,
                        definition.saved_data_id,
                        actor=actor,
                        expected_revision=definition.revision,
                        operation_id=operation,
                        access_factory=factory,
                    ),
                    version,
                )

    def test_physical_accepted_refresh_survives_failed_version_promotion(self):
        for major in (19, 20):
            with self.subTest(major=major):
                service, definition, factory, actor = self._configured(major)
                first = service.refresh(
                    definition.project_id,
                    definition.saved_data_id,
                    actor=actor,
                    expected_revision=definition.revision,
                    operation_id=str(uuid4()),
                    access_factory=factory,
                )
                current = service.get(
                    definition.project_id, definition.saved_data_id, actor=actor
                )
                operation = str(uuid4())
                with (
                    patch.object(
                        service.repository,
                        "append_version",
                        side_effect=RuntimeError("promotion interrupted"),
                    ),
                    self.assertRaisesRegex(RuntimeError, "promotion interrupted"),
                ):
                    service.refresh(
                        definition.project_id,
                        definition.saved_data_id,
                        actor=actor,
                        expected_revision=current.revision,
                        operation_id=operation,
                        access_factory=factory,
                    )
                self.assertEqual(
                    service.get(
                        definition.project_id, definition.saved_data_id, actor=actor
                    ).latest_version,
                    1,
                )
                factory.side_effect = AssertionError(
                    "Accepted capture recovery must work offline"
                )
                second = service.refresh(
                    definition.project_id,
                    definition.saved_data_id,
                    actor=actor,
                    expected_revision=current.revision,
                    operation_id=operation,
                    access_factory=factory,
                )
                self.assertEqual(second.version_number, 2)
                self.assertNotEqual(second.workspace_id, first.workspace_id)
                self.assertEqual(
                    service.repository.get_version(
                        definition.project_id, definition.saved_data_id, 1
                    ),
                    first,
                )
