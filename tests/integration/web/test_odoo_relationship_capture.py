"""Browser evidence for Odoo relationship closure and capture safety."""

from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.odoo.contracts import (
    ConnectorAuthorizationError,
    ConnectorTransportError,
    MetadataSnapshot,
)
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
)
from impodo.domain.shared.models import (
    OdooReadIdentity,
)
from impodo.domain.workspace.contracts import (
    OdooModelSummary,
    SchemaField,
)
from impodo.web.target_credentials import (
    TargetCredentialRole,
    store_target_credential,
)
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
    replace,
)
from tests.support.identity import resolve_local_actor


class OdooRelationshipCaptureBrowserTests(ProjectSetupBrowserTestCase):
    def _incremental_relationship_capture_workspace(self):
        """Build a three-level graph backed by exact remote-read fixtures."""

        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("product.template",),
        )
        name = SchemaField(
            name="name",
            label="Name",
            type="char",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            stored=True,
            computed=False,
            related=False,
            company_dependent=False,
            exportable=True,
        )
        product = replace(
            schema.models[0],
            name="product.template",
            label="Products",
            fields=(
                name,
                SchemaField(
                    name="x_owner_id",
                    label="Owner",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="x.owner",
                    relation_field=None,
                    selection=(),
                    stored=True,
                    computed=False,
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
            unique_constraints=(),
        )
        owner = replace(
            product,
            name="x.owner",
            label="Owners",
            fields=(
                name,
                SchemaField(
                    name="calendar_id",
                    label="Working Hours",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="resource.calendar",
                    relation_field=None,
                    selection=(),
                    stored=True,
                    computed=False,
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
            unique_constraints=(),
        )
        calendar = replace(
            product,
            name="resource.calendar",
            label="Working Hours",
            fields=(name,),
            unique_constraints=(),
        )
        root_schema = replace(
            schema,
            models=(product,),
            content_hash="sha256:" + "8" * 64,
        )
        model_catalog = replace(
            context.queries.get_odoo_model_catalog(workspace_state.workspace_id),
            models=tuple(
                OdooModelSummary(
                    name=model.name,
                    label=model.label,
                    modules=(),
                    state="base",
                )
                for model in (product, owner, calendar)
            ),
            content_hash="sha256:" + "9" * 64,
        )
        context.schema_workspace.schemas.save_odoo_model_catalog(
            workspace_state.workspace_id,
            model_catalog,
            actor=resolve_local_actor(context),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            root_schema,
            actor=resolve_local_actor(context),
        )
        store_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
            "incremental-read-secret",
            persistent=False,
        )

        raw_models = {
            model.name: self._metadata_model(model)
            for model in (product, owner, calendar)
        }
        return workspace_state, root_schema, raw_models

    def test_odoo_source_page_saves_unprofiled_related_data_without_backtracking(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("product.template",),
        )
        product_schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    name="product.template",
                    label="Products",
                    fields=(
                        SchemaField(
                            name="name",
                            label="Product Name",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                            exportable=True,
                        ),
                        SchemaField(
                            name="x_owner_id",
                            label="Owner",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation="x.owner",
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        SchemaField(
                            name="x_reviewer_id",
                            label="Reviewer",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation="x.reviewer",
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                    ),
                ),
            ),
        )
        owner_schema = replace(
            product_schema,
            models=(
                product_schema.models[0],
                replace(
                    schema.models[0],
                    name="x.owner",
                    label="Owners",
                    fields=(
                        SchemaField(
                            name="name",
                            label="Owner Name",
                            type="char",
                            required=True,
                            readonly=False,
                            relation=None,
                            relation_field=None,
                            selection=(),
                            exportable=True,
                        ),
                    ),
                ),
            ),
        )
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=(
                SimpleNamespace(
                    name="product.template",
                    label="Products",
                ),
                SimpleNamespace(
                    name="x.owner",
                    label="Owners",
                ),
            ),
        )

        async def capture_reachable_schema(
            _context, staged_state, _model_catalog, *, actor
        ):
            self.assertEqual(actor, resolve_local_actor(context))
            if "x.owner" in staged_state.intended_models:
                return owner_schema
            return product_schema

        capture_schema = AsyncMock(side_effect=capture_reachable_schema)
        saved_schema_reader = context.queries.get_odoo_schema_catalog

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=lambda workspace_id: (
                    saved_schema_reader(workspace_id) or product_schema
                ),
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                capture_schema,
            ),
        ):
            with collect_duckdb_request_timings() as related_data_timings:
                saved = self._post(
                    f"/workspaces/{workspace_state.workspace_id}"
                    "/sources/odoo-related-data",
                    {
                        "csrf_token": self.csrf,
                        "revision": str(workspace_state.revision),
                        "related_actions": "x.owner::CAPTURE_LINKED",
                    },
                )
            saved_state = context.queries.get(workspace_state.workspace_id)
            repeated = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(saved_state.revision),
                    "related_actions": "x.owner::CAPTURE_LINKED",
                },
            )
            after_repeated = context.queries.get(workspace_state.workspace_id)
            overridden_scope = context.queries.get_current_odoo_relationship_scope(
                workspace_state.workspace_id
            )
            reset_to_recommendation = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(after_repeated.revision),
                    "related_actions": "x.owner::PRESERVE_LINKED",
                },
            )
            review_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertEqual(repeated.status_code, 303)
        self.assertEqual(reset_to_recommendation.status_code, 303)
        # Two bounded schema-closure passes plus one atomic publication stay
        # within a fixed request-level connection budget.
        self.assertLessEqual(related_data_timings.connection_count, 10)
        self.assertEqual(
            saved.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/sources#related-data-scope",
        )
        self.assertEqual(
            context.queries.get(workspace_state.workspace_id).intended_models,
            ("product.template", "x.owner"),
        )
        self.assertEqual(
            after_repeated.revision,
            saved_state.revision,
        )
        self.assertIs(
            overridden_scope.decisions[0].action,
            OdooRelationshipCaptureAction.CAPTURE_LINKED,
        )
        relationship_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertIsNotNone(relationship_scope)
        self.assertEqual(relationship_scope.version, 2)
        self.assertEqual(len(relationship_scope.decisions), 1)
        self.assertEqual(
            relationship_scope.decisions[0].identity,
            ("product.template", "x_owner_id"),
        )
        self.assertIs(
            relationship_scope.decisions[0].action,
            OdooRelationshipCaptureAction.PRESERVE_LINKED,
        )
        self.assertIn("relationship-review-unavailable", review_page.text)
        self.assertIn(
            "will not treat an unavailable relationship as excluded",
            review_page.text,
        )
        self.assertNotIn("Continue with Impodo's plan", review_page.text)
        self.assertEqual(capture_schema.await_count, 6)

    def test_related_data_review_preserves_an_explicit_root_also_reached_by_an_edge(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        product = replace(
            schema.models[0],
            name="product.template",
            label="Products",
            fields=(
                SchemaField(
                    name="name",
                    label="Product Name",
                    type="char",
                    required=True,
                    readonly=False,
                    relation=None,
                    relation_field=None,
                    selection=(),
                    exportable=True,
                ),
                SchemaField(
                    name="x_owner_id",
                    label="Owner",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="x.owner",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        owner = replace(
            product,
            name="x.owner",
            label="Owners",
            fields=(product.fields[0],),
        )
        selected_schema = replace(schema, models=(product, owner))
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("product.template", "x.owner"),
        )
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=(
                SimpleNamespace(name="product.template", label="Products"),
                SimpleNamespace(name="x.owner", label="Owners"),
            ),
        )
        capture_schema = AsyncMock(return_value=selected_schema)

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=selected_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                capture_schema,
            ),
        ):
            first = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::MATCH_EXISTING",
                },
            )
            after_first = context.queries.get(workspace_state.workspace_id)
            second = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(after_first.revision),
                    "related_actions": "x.owner::MATCH_EXISTING",
                },
            )

        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 303)
        self.assertEqual(
            context.queries.get(workspace_state.workspace_id).intended_models,
            ("product.template", "x.owner"),
        )
        relationship_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertEqual(
            relationship_scope.root_models,
            ("product.template", "x.owner"),
        )

    def test_related_data_defaults_close_new_graph_levels_in_one_action(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("product.template",),
        )
        base = schema.models[0]
        name = SchemaField(
            name="name",
            label="Name",
            type="char",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            exportable=True,
        )
        product = replace(
            base,
            name="product.template",
            label="Products",
            fields=(
                name,
                SchemaField(
                    name="x_owner_id",
                    label="Owner",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="x.owner",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        owner = replace(
            base,
            name="x.owner",
            label="Owners",
            fields=(
                name,
                SchemaField(
                    name="calendar_id",
                    label="Working Hours",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="resource.calendar",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        calendar = replace(
            base,
            name="resource.calendar",
            label="Working Hours",
            fields=(name,),
        )
        initial_schema = replace(schema, models=(product,))
        owner_schema = replace(schema, models=(product, owner))
        complete_schema = replace(schema, models=(product, owner, calendar))
        current = {"schema": initial_schema}
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=tuple(
                SimpleNamespace(name=model.name, label=model.label)
                for model in complete_schema.models
            ),
        )

        async def capture_next(_context, saved_state, _model_catalog, *, actor):
            self.assertEqual(actor, resolve_local_actor(context))
            current["schema"] = (
                owner_schema
                if "resource.calendar" not in saved_state.intended_models
                else complete_schema
            )
            return current["schema"]

        capture_schema = AsyncMock(side_effect=capture_next)
        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=lambda _workspace_id: current["schema"],
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                capture_schema,
            ),
        ):
            first = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::CAPTURE_LINKED",
                },
            )
            first_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        self.assertEqual(first.status_code, 303)
        self.assertIn("relationship-review-complete", first_page.text)
        self.assertEqual(
            frozenset(
                context.queries.get(workspace_state.workspace_id).intended_models
            ),
            frozenset({"product.template", "x.owner", "resource.calendar"}),
        )
        scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {item.identity for item in scope.decisions},
            {
                ("product.template", "x_owner_id"),
                ("x.owner", "calendar_id"),
            },
        )
        self.assertEqual(
            {item.identity: item.action for item in scope.decisions},
            {
                ("product.template", "x_owner_id"): (
                    OdooRelationshipCaptureAction.CAPTURE_LINKED
                ),
                ("x.owner", "calendar_id"): (
                    OdooRelationshipCaptureAction.PRESERVE_LINKED
                ),
            },
        )
        self.assertEqual(capture_schema.await_count, 2)

    def test_unreachable_relationships_do_not_create_false_model_conflicts(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root",),
        )
        base = schema.models[0]
        name = SchemaField(
            name="name",
            label="Name",
            type="char",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            stored=True,
            computed=False,
            related=False,
            company_dependent=False,
            exportable=True,
        )

        def relationship(
            name: str,
            label: str,
            field_type: str,
            relation: str,
            *,
            required: bool = False,
        ) -> SchemaField:
            return SchemaField(
                name=name,
                label=label,
                type=field_type,
                required=required,
                readonly=False,
                relation=relation,
                relation_field=None,
                selection=(),
                stored=True,
                computed=False,
                related=False,
                company_dependent=False,
                exportable=True,
            )

        root = replace(
            base,
            name="x.root",
            label="Main records",
            fields=(
                name,
                relationship("owner_id", "Owner", "many2one", "x.owner"),
                relationship(
                    "employee_ids",
                    "Employees",
                    "many2many",
                    "hr.employee",
                ),
            ),
            unique_constraints=(),
        )
        owner = replace(
            base,
            name="x.owner",
            label="Owners",
            fields=(
                name,
                relationship(
                    "bridge_id",
                    "Bridge",
                    "many2one",
                    "x.bridge",
                    required=True,
                ),
                relationship(
                    "employee_id",
                    "Employee",
                    "many2one",
                    "hr.employee",
                ),
            ),
            unique_constraints=(),
        )
        bridge = replace(
            base,
            name="x.bridge",
            label="Bridges",
            fields=(
                name,
                relationship(
                    "owner_ids",
                    "Owners",
                    "one2many",
                    "x.owner",
                ),
            ),
            unique_constraints=(),
        )
        employee = replace(
            base,
            name="hr.employee",
            label="Employees",
            fields=(name,),
            unique_constraints=(),
        )
        models_by_name = {
            model.name: model for model in (root, owner, bridge, employee)
        }
        current = {"schema": replace(schema, models=(root,))}
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=tuple(
                SimpleNamespace(name=model.name, label=model.label)
                for model in models_by_name.values()
            ),
        )

        async def capture_candidate(_context, saved_state, _model_catalog, *, actor):
            self.assertEqual(actor, resolve_local_actor(context))
            current["schema"] = replace(
                schema,
                models=tuple(
                    models_by_name[model] for model in saved_state.intended_models
                ),
            )
            return current["schema"]

        capture_schema = AsyncMock(side_effect=capture_candidate)
        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                side_effect=lambda _workspace_id: current["schema"],
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch(
                "impodo.web.routers.sources._open_incremental_related_schema_session",
                return_value=None,
            ),
            patch(
                "impodo.web.routers.sources._prepare_related_schema_candidate",
                capture_schema,
            ),
        ):
            response = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                },
            )

        self.assertEqual(response.status_code, 303, response.text)
        saved_state = context.queries.get(workspace_state.workspace_id)
        self.assertEqual(
            frozenset(saved_state.intended_models),
            frozenset({"x.root", "x.owner", "x.bridge"}),
        )
        scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {item.identity: item.action for item in scope.decisions},
            {
                ("x.owner", "bridge_id"): (
                    OdooRelationshipCaptureAction.PRESERVE_LINKED
                ),
                ("x.root", "employee_ids"): (
                    OdooRelationshipCaptureAction.DO_NOT_CAPTURE
                ),
                ("x.root", "owner_id"): (OdooRelationshipCaptureAction.PRESERVE_LINKED),
            },
        )
        self.assertEqual(capture_schema.await_count, 3)

    def test_related_data_closure_reads_each_record_type_once_and_binds_full_access(
        self,
    ) -> None:
        workspace_state, _root_schema, raw_models = (
            self._incremental_relationship_capture_workspace()
        )
        context = self.app.state.context
        fingerprint = _browser_schema(workspace_state).fingerprint
        full_scope = (
            "product.template",
            "resource.calendar",
            "x.owner",
        )
        full_permission_hash = "sha256:" + "f" * 64
        schema_read_scopes: list[tuple[str, ...]] = []
        identity_scopes: list[tuple[str, ...]] = []
        opened_sessions: list[tuple[str, str]] = []

        class RecordingSchemaSession:
            def read_models(self, models):
                scope = tuple(sorted(models))
                schema_read_scopes.append(scope)
                return MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={model: raw_models[model] for model in scope},
                )

            def verify_identity(self, models):
                scope = tuple(sorted(models))
                identity_scopes.append(scope)
                return OdooReadIdentity(
                    target_hash=fingerprint.target_hash,
                    principal_hash="sha256:" + "1" * 64,
                    permission_hash=full_permission_hash,
                    context_hash="sha256:" + "4" * 64,
                    readable_models=scope,
                    observed_at="2026-10-12T08:00:00Z",
                )

        session = RecordingSchemaSession()

        def open_session(saved_state, api_key):
            opened_sessions.append((saved_state.workspace_id, api_key))
            return session

        with patch.object(
            context,
            "schema_read_session_factory",
            new=open_session,
        ):
            saved = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::CAPTURE_LINKED",
                },
            )

        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertEqual(
            schema_read_scopes,
            [
                ("product.template",),
                ("x.owner",),
                ("resource.calendar",),
            ],
        )
        self.assertEqual(identity_scopes, [full_scope])
        self.assertEqual(
            opened_sessions,
            [(workspace_state.workspace_id, "incremental-read-secret")],
        )
        persisted_schema = context.queries.get_odoo_schema_catalog(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {model.name for model in persisted_schema.models},
            set(full_scope),
        )
        self.assertEqual(
            persisted_schema.read_permission_hash,
            full_permission_hash,
        )

    def test_related_data_rejects_an_overlapping_save_and_releases_after_success(
        self,
    ) -> None:
        workspace_state, _root_schema, raw_models = (
            self._incremental_relationship_capture_workspace()
        )
        context = self.app.state.context
        fingerprint = _browser_schema(workspace_state).fingerprint
        first_read_started = Event()
        release_first_read = Event()

        class BlockingSchemaSession:
            def read_models(self, models):
                scope = tuple(sorted(models))
                if not first_read_started.is_set():
                    first_read_started.set()
                    if not release_first_read.wait(10):
                        raise AssertionError(
                            "Timed out waiting to release the first linked-data save"
                        )
                return MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={model: raw_models[model] for model in scope},
                )

            def verify_identity(self, models):
                scope = tuple(sorted(models))
                return OdooReadIdentity(
                    target_hash=fingerprint.target_hash,
                    principal_hash="sha256:" + "1" * 64,
                    permission_hash="sha256:" + "f" * 64,
                    context_hash="sha256:" + "4" * 64,
                    readable_models=scope,
                    observed_at="2026-10-12T08:00:00Z",
                )

        first_client = type(self.client)(
            self.app,
            cookies=dict(self.client.cookies),
        )
        first_responses = []
        first_errors = []
        path = f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data"
        form = {
            "csrf_token": self.csrf,
            "revision": str(workspace_state.revision),
            "related_actions": "x.owner::CAPTURE_LINKED",
        }

        def submit_first_save() -> None:
            try:
                first_responses.append(
                    first_client.post(
                        path,
                        data=form,
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                )
            except BaseException as error:  # pragma: no cover - assertion aid
                first_errors.append(error)

        with patch.object(
            context,
            "schema_read_session_factory",
            new=lambda _saved_state, _api_key: BlockingSchemaSession(),
        ):
            first_thread = Thread(target=submit_first_save, daemon=True)
            first_thread.start()
            try:
                self.assertTrue(
                    first_read_started.wait(10),
                    "The first linked-data save did not reach its Odoo read",
                )
                overlapping = self._post(path, form)
            finally:
                release_first_read.set()
                first_thread.join(15)
                first_client.close()

            self.assertFalse(first_thread.is_alive())
            self.assertEqual(first_errors, [])
            self.assertEqual(len(first_responses), 1)
            self.assertEqual(first_responses[0].status_code, 303)
            self.assertEqual(overlapping.status_code, 409)
            self.assertEqual(
                overlapping.text,
                "Impodo is already checking linked data for this project. "
                "Wait for it to finish, then refresh.",
            )

            current_state = context.queries.get(workspace_state.workspace_id)
            after_success = self._post(
                path,
                {
                    **form,
                    "revision": str(current_state.revision),
                },
            )

        self.assertEqual(after_success.status_code, 303, after_success.text)

    def test_related_data_releases_the_in_flight_gate_after_failure(self) -> None:
        workspace_state, _root_schema, _raw_models = (
            self._incremental_relationship_capture_workspace()
        )
        context = self.app.state.context
        first_read_started = Event()
        release_first_read = Event()

        class FailingBlockingSchemaSession:
            def read_models(self, _models):
                if not first_read_started.is_set():
                    first_read_started.set()
                    if not release_first_read.wait(10):
                        raise AssertionError(
                            "Timed out waiting to release the first linked-data save"
                        )
                raise ConnectorTransportError(
                    "Odoo stopped responding during the linked-data check"
                )

            def verify_identity(self, _models):  # pragma: no cover - defensive
                raise AssertionError("A failed field read cannot reach identity check")

        first_client = type(self.client)(
            self.app,
            cookies=dict(self.client.cookies),
        )
        first_responses = []
        first_errors = []
        path = f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data"
        form = {
            "csrf_token": self.csrf,
            "revision": str(workspace_state.revision),
            "related_actions": "x.owner::CAPTURE_LINKED",
        }

        def submit_first_save() -> None:
            try:
                first_responses.append(
                    first_client.post(
                        path,
                        data=form,
                        headers=POST_HEADERS,
                        follow_redirects=False,
                    )
                )
            except BaseException as error:  # pragma: no cover - assertion aid
                first_errors.append(error)

        with patch.object(
            context,
            "schema_read_session_factory",
            new=lambda _saved_state, _api_key: FailingBlockingSchemaSession(),
        ):
            first_thread = Thread(target=submit_first_save, daemon=True)
            first_thread.start()
            try:
                self.assertTrue(
                    first_read_started.wait(10),
                    "The first linked-data save did not reach its Odoo read",
                )
                overlapping = self._post(path, form)
            finally:
                release_first_read.set()
                first_thread.join(15)
                first_client.close()

            self.assertFalse(first_thread.is_alive())
            self.assertEqual(first_errors, [])
            self.assertEqual(len(first_responses), 1)
            self.assertEqual(first_responses[0].status_code, 422)
            self.assertEqual(overlapping.status_code, 409)

            after_failure = self._post(path, form)

        self.assertEqual(after_failure.status_code, 422)
        self.assertIn(
            "Odoo stopped responding during the linked-data check",
            after_failure.text,
        )

    def test_failed_final_related_data_access_check_publishes_nothing(
        self,
    ) -> None:
        workspace_state, root_schema, raw_models = (
            self._incremental_relationship_capture_workspace()
        )
        context = self.app.state.context
        fingerprint = _browser_schema(workspace_state).fingerprint
        full_scope = (
            "product.template",
            "resource.calendar",
            "x.owner",
        )
        schema_read_scopes: list[tuple[str, ...]] = []
        identity_scopes: list[tuple[str, ...]] = []
        opened_sessions: list[tuple[str, str]] = []
        before_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        before_selections = context.queries.get_current_odoo_capture_selections(
            workspace_state.workspace_id
        )

        class FailingFinalSchemaSession:
            def read_models(self, models):
                scope = tuple(sorted(models))
                schema_read_scopes.append(scope)
                return MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={model: raw_models[model] for model in scope},
                )

            def verify_identity(self, models):
                scope = tuple(sorted(models))
                identity_scopes.append(scope)
                raise ConnectorAuthorizationError(
                    "Odoo access changed before linked data could be saved"
                )

        session = FailingFinalSchemaSession()

        def open_session(saved_state, api_key):
            opened_sessions.append((saved_state.workspace_id, api_key))
            return session

        with patch.object(
            context,
            "schema_read_session_factory",
            new=open_session,
        ):
            failed = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::CAPTURE_LINKED",
                },
            )

        self.assertEqual(failed.status_code, 422, failed.text)
        self.assertIn(
            "Odoo access changed before linked data could be saved",
            failed.text,
        )
        self.assertEqual(
            schema_read_scopes,
            [
                ("product.template",),
                ("x.owner",),
                ("resource.calendar",),
            ],
        )
        self.assertEqual(identity_scopes, [full_scope])
        self.assertEqual(
            opened_sessions,
            [(workspace_state.workspace_id, "incremental-read-secret")],
        )
        after_state = context.queries.get(workspace_state.workspace_id)
        self.assertEqual(after_state.revision, workspace_state.revision)
        self.assertEqual(
            after_state.intended_models,
            workspace_state.intended_models,
        )
        self.assertEqual(
            context.queries.get_current_odoo_relationship_scope(
                workspace_state.workspace_id
            ),
            before_scope,
        )
        self.assertEqual(
            context.queries.get_odoo_schema_catalog(workspace_state.workspace_id),
            root_schema,
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            ),
            before_selections,
        )

    def test_later_relationship_conflict_is_saved_as_resumable_checkpoint(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root",),
        )
        base = schema.models[0]
        write_date = next(field for field in base.fields if field.name == "write_date")
        name = SchemaField(
            name="name",
            label="Name",
            type="char",
            required=True,
            readonly=False,
            relation=None,
            relation_field=None,
            selection=(),
            stored=True,
            company_dependent=False,
            exportable=True,
        )
        root = replace(
            base,
            name="x.root",
            label="Main records",
            fields=(
                name,
                write_date,
                SchemaField(
                    name="calendar_id",
                    label="Working Hours",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="resource.calendar",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
                SchemaField(
                    name="workcenter_id",
                    label="Work Center",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="mrp.workcenter",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        workcenter = replace(
            base,
            name="mrp.workcenter",
            label="Work Centers",
            fields=(
                name,
                SchemaField(
                    name="resource_calendar_id",
                    label="Working Hours",
                    type="many2one",
                    required=False,
                    readonly=False,
                    relation="resource.calendar",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        calendar = replace(
            base,
            name="resource.calendar",
            label="Working Hours",
            fields=(name,),
        )
        root_schema = replace(
            schema,
            models=(root,),
            content_hash="sha256:" + "8" * 64,
        )
        complete_schema = replace(
            schema,
            models=(root, workcenter, calendar),
            content_hash="sha256:" + "9" * 64,
        )
        stored_model_catalog = context.queries.get_odoo_model_catalog(
            workspace_state.workspace_id
        )
        model_catalog = replace(
            stored_model_catalog,
            models=tuple(
                OdooModelSummary(
                    name=model.name,
                    label=model.label,
                    modules=(),
                    state="base",
                )
                for model in complete_schema.models
            ),
            content_hash="sha256:" + "b" * 64,
        )
        context.schema_workspace.schemas.save_odoo_model_catalog(
            workspace_state.workspace_id,
            model_catalog,
            actor=resolve_local_actor(context),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            root_schema,
            actor=resolve_local_actor(context),
        )
        store_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
            "checkpoint-read-secret",
            persistent=False,
        )
        fingerprint = _browser_schema(workspace_state).fingerprint
        raw_models = {
            model.name: self._metadata_model(model) for model in complete_schema.models
        }
        existing_capture = context.sources.define_odoo_capture_selection(
            workspace_state.workspace_id,
            dataset_name="main_records",
            model="x.root",
            field_names=("name",),
            include_archived=False,
            page_size=100,
            actor=resolve_local_actor(context),
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            ),
            (existing_capture,),
        )

        sessions = []

        class RecordingSchemaSession:
            def __init__(self):
                self.schema_reads = []
                self.identity_checks = []

            def read_models(self, models):
                selected = tuple(sorted(models))
                self.schema_reads.append(selected)
                return MetadataSnapshot(
                    fingerprint=fingerprint,
                    models={model: raw_models[model] for model in selected},
                )

            def verify_identity(self, models):
                selected = tuple(sorted(models))
                self.identity_checks.append(selected)
                return OdooReadIdentity(
                    target_hash=fingerprint.target_hash,
                    principal_hash="sha256:" + "1" * 64,
                    permission_hash="sha256:" + "3" * 64,
                    context_hash="sha256:" + "4" * 64,
                    readable_models=selected,
                    observed_at="2026-10-12T08:00:00Z",
                )

        def open_session(saved_state, api_key):
            self.assertEqual(saved_state.workspace_id, workspace_state.workspace_id)
            self.assertEqual(api_key, "checkpoint-read-secret")
            session = RecordingSchemaSession()
            sessions.append(session)
            return session

        with patch.object(
            context,
            "schema_read_session_factory",
            new=open_session,
        ):
            checkpoint = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": [
                        "resource.calendar::PRESERVE_LINKED",
                        "mrp.workcenter::CAPTURE_LINKED",
                    ],
                },
            )
            checkpoint_state = context.queries.get(workspace_state.workspace_id)
            checkpoint_scope = context.queries.get_current_odoo_relationship_scope(
                workspace_state.workspace_id
            )
            checkpoint_schema = context.queries.get_odoo_schema_catalog(
                workspace_state.workspace_id
            )
            checkpoint_capture_selections = (
                context.queries.get_current_odoo_capture_selections(
                    workspace_state.workspace_id
                )
            )
            review_page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )
            resumed = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(checkpoint_state.revision),
                    "related_actions": [
                        "resource.calendar::PRESERVE_LINKED",
                        "mrp.workcenter::CAPTURE_LINKED",
                    ],
                },
            )

        self.assertEqual(len(sessions), 2)
        self.assertEqual(
            [session.schema_reads for session in sessions],
            [
                [
                    ("x.root",),
                    ("mrp.workcenter", "resource.calendar"),
                ],
                [
                    ("x.root",),
                    ("mrp.workcenter", "resource.calendar"),
                ],
            ],
        )
        self.assertEqual(
            [session.identity_checks for session in sessions],
            [
                [("mrp.workcenter", "x.root")],
                [
                    (
                        "mrp.workcenter",
                        "resource.calendar",
                        "x.root",
                    )
                ],
            ],
        )
        self.assertEqual(checkpoint.status_code, 303, checkpoint.text)
        self.assertGreater(checkpoint_state.revision, workspace_state.revision)
        self.assertEqual(
            checkpoint.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/sources#related-data-scope",
        )
        self.assertEqual(review_page.status_code, 200)
        self.assertNotIn("We could not complete that action", review_page.text)
        self.assertIn("1 relationship exception needs a decision", review_page.text)
        self.assertIn(
            'aria-label="Relationship exception for Working Hours"',
            review_page.text,
        )
        self.assertIn(
            "<strong>Working Hours</strong> on Main records",
            review_page.text,
        )
        self.assertIn("&mdash; Keep linked value", review_page.text)
        self.assertIn(
            "<strong>Working Hours</strong> on Work Centers",
            review_page.text,
        )
        self.assertIn(
            "&mdash; Transfer related records as migration data",
            review_page.text,
        )
        self.assertEqual(
            {
                item.identity
                for item in checkpoint_scope.decisions
                if item.relation_model == "resource.calendar"
            },
            set(),
        )
        self.assertEqual(
            frozenset(checkpoint_state.intended_models),
            frozenset({"x.root", "mrp.workcenter"}),
        )
        self.assertEqual(
            {model.name for model in checkpoint_schema.models},
            {"x.root", "mrp.workcenter"},
        )
        self.assertEqual(checkpoint_capture_selections, ())
        self.assertEqual(resumed.status_code, 303, resumed.text)
        resumed_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertEqual(
            resumed_scope.explicit_actions,
            {
                "mrp.workcenter": (OdooRelationshipCaptureAction.CAPTURE_LINKED),
                "resource.calendar": (OdooRelationshipCaptureAction.PRESERVE_LINKED),
            },
        )
        self.assertEqual(
            {
                item.action
                for item in resumed_scope.decisions
                if item.relation_model == "resource.calendar"
            },
            {OdooRelationshipCaptureAction.PRESERVE_LINKED},
        )
