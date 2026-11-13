"""Keep reset declarations local, scoped, explicit, and separate from rebinds."""

import asyncio
import re
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import duckdb
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.odoo_database_binding import (
    OdooDatabaseGeneration,
    OdooDatabaseGenerationConflict,
    database_scope_hash,
)
from impodo.domain.shared.access import (
    LOCAL_ACTOR,
    Actor,
    AuthorizationError,
    Capability,
)
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.routers.odoo_database_bindings import (
    build_odoo_database_bindings_router,
)
from tests.application.workspace.test_destination_matching import _workspace
from tests.integration.web.test_transfer_load_routes import _endpoint, _request
from tests.support.browser_scenarios import ProjectSetupBrowserTestCase
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import (
    MutableLocalRequestIdentityResolver,
    resolve_local_actor,
)
from tests.support.paths import REPOSITORY_ROOT


class _Generations:
    def __init__(self, events):
        self.events = events
        self.current_values = {}
        self.receipts = {}
        self.declarations = []

    def current(self, workspace_id, *, base_url, database, scope, **_kwargs):
        self.events.append("generation_read")
        key = database_scope_hash(base_url, database)
        return self.current_values.get(key) or OdooDatabaseGeneration(
            str(scope.organization_id), key, 0
        )

    def get(self, workspace_id, role, **_kwargs):
        self.events.append("binding_read")

    def declare_recreated(
        self,
        workspace_id,
        *,
        base_url,
        database,
        expected_generation_hash,
        operation_id,
        actor,
        scope,
        reason,
        authorization_receipt,
    ):
        self.events.append("declaration")
        if operation_id in self.receipts:
            return self.receipts[operation_id]
        current = self.current(
            workspace_id, base_url=base_url, database=database, scope=scope
        )
        if current.generation_hash != expected_generation_hash:
            raise OdooDatabaseGenerationConflict(
                "The database review changed. Reload before recording a reset."
            )
        new = OdooDatabaseGeneration(
            str(scope.organization_id),
            current.scope_hash,
            current.generation + 1,
            current.generation_hash,
            operation_id,
            actor.identity,
            datetime.now(UTC),
            reason,
        )
        self.current_values[current.scope_hash] = new
        self.receipts[operation_id] = new
        self.declarations.append((base_url, database, authorization_receipt))
        return new


class OdooDatabaseBindingRouteTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.project_id, self.saved_data_id = str(uuid4()), str(uuid4())
        original = _workspace(datetime.now(UTC))
        original = replace(
            original,
            odoo_company=OdooCompanySelection(
                "sha256:" + "a" * 64, 1, "Reviewed company"
            ),
        )
        fresh = replace(original, workspace_id=str(uuid4()), name="Fresh source")
        other = replace(original, workspace_id=str(uuid4()), name="Other work")
        self.states = {item.workspace_id: item for item in (original, fresh, other)}
        self.original, self.fresh = original, fresh
        self.roots = tuple(
            SimpleNamespace(
                workspace_id=item.workspace_id,
                project_id=self.project_id,
                display_name=item.name,
            )
            for item in self.states.values()
        )
        self.definition = SimpleNamespace(
            saved_data_id=self.saved_data_id,
            project_id=self.project_id,
            source_workspace_id=original.workspace_id,
            label="Saved clean data",
            revision=3,
            latest_version=2,
            config_json='{"all_fields":true}',
        )
        self.resolver = MutableLocalRequestIdentityResolver()
        self.generations = _Generations(self.events)

        def project(project_id, *, scope, authorization, capability):
            self.events.append("project_admission")
            if not authorization.authorizes(
                capability, at=datetime.now(UTC), project_id=project_id
            ):
                raise AuthorizationError("denied")
            return SimpleNamespace(
                project_id=self.project_id, display_name="Data project"
            )

        def root(workspace_id, **_kwargs):
            self.events.append("workspace_admission")
            return next(
                (item for item in self.roots if item.workspace_id == workspace_id),
                SimpleNamespace(project_id="other project", workspace_id=workspace_id),
            )

        def state(workspace_id):
            self.events.append("state:" + workspace_id)
            return self.states[workspace_id]

        self.saved = SimpleNamespace(
            get=lambda *_args, **_kwargs: self.definition,
            rebind_source=Mock(return_value=self.definition),
        )
        self.context = SimpleNamespace(
            identity_resolver=self.resolver,
            project_queries=SimpleNamespace(get_visible_project=project),
            migration_workspaces=SimpleNamespace(
                get=root, list_for_project=lambda *_args, **_kwargs: self.roots
            ),
            queries=SimpleNamespace(get=state, get_source_selection=lambda _: None),
            odoo_database_bindings=self.generations,
            saved_odoo_data=self.saved,
        )
        templates = Environment(
            loader=ChoiceLoader(
                [
                    DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                    FileSystemLoader(REPOSITORY_ROOT / "src/impodo/web/templates"),
                ]
            ),
            autoescape=True,
        )
        self.render = lambda request, template, **values: HTMLResponse(
            templates.get_template(template).render(csrf_token="csrf", **values),
            status_code=values.get("status_code", 200),
        )

    def _call(self, name, values, **arguments):
        with patch(
            "impodo.web.routers.odoo_database_bindings._render", side_effect=self.render
        ):
            return asyncio.run(
                _endpoint(build_odoo_database_bindings_router(self.context), name)(
                    _request("/database-bindings", values),
                    self.project_id,
                    **arguments,
                )
            )

    def _declaration(self, **changes):
        identity = self.resolver.resolve({"authenticated": True})
        current = self.generations.current(
            self.original.workspace_id,
            base_url=self.original.odoo_base_url,
            database=self.original.odoo_database,
            scope=identity.organization_scope,
        )
        return {
            "csrf_token": "csrf",
            "workspace_id": self.original.workspace_id,
            "role": "SOURCE",
            "expected_generation_hash": current.generation_hash,
            "operation_id": str(uuid4()),
            "confirm_database": self.original.odoo_database,
            "confirm_recreated": "yes",
            "reason": "The test database was recreated",
            **changes,
        }

    def test_read_only_page_hydrates_only_selected_state_and_explains_organization_scope(
        self,
    ):
        response = self._call(
            "database_bindings_page", {}, workspace_id=self.original.workspace_id
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("every project and company", response.body.decode())
        self.assertIn("Fresh review required", response.body.decode())
        self.assertEqual(
            [event for event in self.events if event.startswith("state:")],
            ["state:" + self.original.workspace_id],
        )
        self.assertEqual(self.generations.current_values, {})
        self.assertEqual(self.generations.declarations, [])

    def test_declares_server_derived_database_and_retries_one_operation(self):
        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    self.states[self.original.workspace_id] = replace(
                        self.original,
                        odoo_connection_mode=mode,
                        destination_verified_odoo_version=f"{major}.0",
                    )
                    data = self._declaration()
                    response = self._call("declare_recreated_database", data)
                    self.assertEqual(response.status_code, 303)
                    writes = len(self.generations.declarations)
                    repeated = self._call("declare_recreated_database", data)
                    self.assertEqual(repeated.status_code, 303)
                    self.assertEqual(len(self.generations.declarations), writes)
                    self.assertEqual(
                        self.generations.declarations[-1][:2],
                        (self.original.odoo_base_url, self.original.odoo_database),
                    )

    def test_declaration_permission_and_project_scope_fail_before_child_reads(self):
        data = self._declaration()
        base_identity = self.resolver.resolve({"authenticated": True})
        limited = replace(
            base_identity,
            authorization=replace(
                base_identity.authorization,
                project_visibility=replace(
                    base_identity.authorization.project_visibility,
                    organization_wide=False,
                    project_ids=frozenset({self.project_id}),
                ),
            ),
        )
        self.context.identity_resolver = SimpleNamespace(resolve=lambda _: limited)
        self.events.clear()
        with self.assertRaises(HTTPException) as denied:
            self._call("declare_recreated_database", data)
        self.assertEqual(denied.exception.status_code, 403)
        self.assertEqual(self.events, [])

    def test_csrf_duplicates_and_posted_endpoint_tampering_are_rejected_before_reads(
        self,
    ):
        data = self._declaration()
        for entries in (
            {**data, "csrf_token": "wrong"},
            [*data.items(), ("reason", "second reason")],
            {**data, "base_url": "https://unreviewed.example.test"},
        ):
            with self.subTest(entries=entries):
                self.events.clear()
                with self.assertRaises(HTTPException):
                    self._call("declare_recreated_database", entries)
                self.assertEqual(self.events, [])

    def test_wrong_confirmation_and_stale_generation_write_nothing(self):
        data = self._declaration()
        wrong = self._call(
            "declare_recreated_database", {**data, "confirm_database": "different"}
        )
        self.assertEqual(wrong.status_code, 422)
        self.assertEqual(self.generations.declarations, [])
        self.assertEqual(
            self._call("declare_recreated_database", data).status_code, 303
        )
        stale = self._call(
            "declare_recreated_database", {**data, "operation_id": str(uuid4())}
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(len(self.generations.declarations), 1)

    def test_unknown_workspace_never_reaches_its_engine_or_ledger(self):
        data = self._declaration(workspace_id=str(uuid4()))
        self.events.clear()
        with self.assertRaises(HTTPException) as rejected:
            self._call("declare_recreated_database", data)
        self.assertEqual(rejected.exception.status_code, 404)
        self.assertNotIn("declaration", self.events)
        self.assertFalse(any(event.startswith("state:") for event in self.events))

    def test_rebind_checks_exact_company_and_preserves_current_capture_policy(self):
        data = {
            "csrf_token": "csrf",
            "revision": "3",
            "workspace_id": self.fresh.workspace_id,
            "confirm_database": self.fresh.odoo_database,
            "confirm_company": "Reviewed company",
        }
        response = self._call(
            "rebind_saved_source", data, saved_data_id=self.saved_data_id
        )
        self.assertEqual(response.status_code, 303)
        self.saved.rebind_source.assert_called_once()
        self.assertTrue(self.saved.rebind_source.call_args.kwargs["all_fields"])
        self.assertEqual(
            self.saved.rebind_source.call_args.kwargs["expected_revision"], 3
        )
        self.saved.rebind_source.reset_mock()
        rejected = self._call(
            "rebind_saved_source",
            {**data, "confirm_company": "Another company"},
            saved_data_id=self.saved_data_id,
        )
        self.assertEqual(rejected.status_code, 422)
        self.saved.rebind_source.assert_not_called()

    def test_source_rebind_denial_reads_no_definition_or_workspace(self):
        self.context.identity_resolver = MutableLocalRequestIdentityResolver(
            Actor(LOCAL_ACTOR.identity, frozenset({Capability.PROJECT_VIEW}))
        )
        self.saved.get = Mock(
            side_effect=AssertionError("No definition read before authorization")
        )
        with self.assertRaises(AuthorizationError):
            self._call(
                "rebind_saved_source",
                {
                    "csrf_token": "csrf",
                    "revision": "3",
                    "workspace_id": self.fresh.workspace_id,
                    "confirm_database": self.fresh.odoo_database,
                    "confirm_company": "Reviewed company",
                },
                saved_data_id=self.saved_data_id,
            )
        self.saved.get.assert_not_called()
        self.assertFalse(any(event.startswith("state:") for event in self.events))


class OdooDatabaseBindingPagePerformanceTests(ProjectSetupBrowserTestCase):
    def test_physical_review_and_declaration_are_bounded_for_both_majors_and_modes(
        self,
    ):
        context = self.app.state.context
        connect = DuckDbConnectionFactory._connect_with_lock_wait
        opened = []

        def observe(factory, path, config):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            owner = connect(factory, path, config)
            opened.append((Path(path).name, owner))
            return owner

        for major in (19, 20):
            for mode in (OdooConnectionMode.LOCAL, OdooConnectionMode.REMOTE):
                with self.subTest(major=major, mode=mode):
                    workspace_id, _, _ = self._mapping_ready_workspace(
                        scalar_field_count=2, odoo_major=major, connection_mode=mode
                    )
                    root = context.migration_workspaces.get(
                        workspace_id, actor=resolve_local_actor(context)
                    )
                    location = (
                        f"/projects/{root.project_id}/odoo-database-bindings"
                        f"?workspace_id={workspace_id}&role=DESTINATION"
                    )
                    state = context.queries.get(workspace_id)
                    with patch.object(
                        context.odoo_database_bindings,
                        "declare_recreated",
                        side_effect=AssertionError("GET must not declare a reset"),
                    ):
                        for _ in range(2):
                            opened.clear()
                            with (
                                count_duckdb_statements() as statements,
                                collect_duckdb_request_timings() as timings,
                                patch.object(
                                    DuckDbConnectionFactory,
                                    "_connect_with_lock_wait",
                                    observe,
                                ),
                            ):
                                response = self.client.get(location)
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertIn(
                                "Review an Odoo database reset", response.text
                            )
                            self.assertEqual(
                                (timings.connection_count, timings.schema_check_count),
                                (4, 4),
                            )
                            self.assertEqual(timings.lock_retry_count, 0)
                            # Project and selected-workspace admission validates
                            # registry, linkage, data-version, and engine stores.
                            self.assertEqual(
                                {name for name, _ in opened},
                                {
                                    "registry.duckdb",
                                    "workspace.duckdb",
                                    "data-version.duckdb",
                                    "workspace-engine.duckdb",
                                },
                            )
                            self.assertLessEqual(len(statements), 42)
                            self.assertEqual(
                                sum("duckdb_columns()" in sql for sql in statements),
                                timings.schema_check_count,
                            )
                            for _, owner in opened:
                                with self.assertRaises(duckdb.ConnectionException):
                                    owner.execute("SELECT 1")
                    self.assertEqual(context.queries.get(workspace_id), state)
                    expected_hash = re.search(
                        r'name="expected_generation_hash" value="([^"]+)"',
                        response.text,
                    )
                    self.assertIsNotNone(expected_hash)
                    form = {
                        "csrf_token": self.csrf,
                        "workspace_id": workspace_id,
                        "role": "DESTINATION",
                        "expected_generation_hash": expected_hash[1],
                        "operation_id": str(uuid4()),
                        "confirm_database": state.odoo_database,
                        "confirm_recreated": "yes",
                        "reason": "Reviewed recreation of the isolated test database",
                    }
                    for stale in (False, True):
                        if stale:
                            form["operation_id"] = str(uuid4())
                        with (
                            count_duckdb_statements() as statements,
                            collect_duckdb_request_timings() as timings,
                        ):
                            declared = self._post(
                                f"/projects/{root.project_id}/odoo-database-bindings/declare",
                                form,
                            )
                        self.assertEqual(declared.status_code, 409 if stale else 303)
                        # A rejected CAS closes the command scope before opening
                        # a separate bounded projection for its recovery form.
                        self.assertEqual(timings.connection_count, 8 if stale else 4)
                        self.assertEqual(timings.schema_check_count, 8 if stale else 4)
                        self.assertEqual(timings.lock_retry_count, 0)
                        self.assertLessEqual(len(statements), 82 if stale else 48)
        self.assertEqual(self.connection_calls, [])
        self.assertEqual(self.schema_calls, [])
        self.assertEqual(self.model_catalog_calls, [])
        self.assertEqual(self.read_identity_calls, [])
        self.assertEqual(self.readiness_calls, [])


if __name__ == "__main__":
    unittest.main()
