"""Browser evidence for reviewing Odoo relationship capture choices."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.guided_odoo_capture import (
    GuidedOdooCaptureIssueCode,
    GuidedOdooCapturePlanIssue,
    GuidedOdooCapturePlanProposal,
)
from impodo.domain.odoo.contracts import (
    ConnectorTransportError,
)
from impodo.domain.odoo_relationship_scope import (
    OdooRelationshipCaptureAction,
    OdooRelationshipScope,
    relationship_scope_decisions,
)
from impodo.domain.odoo_source_scope import propose_related_odoo_data
from impodo.domain.workspace.contracts import (
    OdooModelCatalog,
    OdooModelSummary,
    SchemaField,
)
from impodo.web.target_credentials import (
    TargetCredentialRole,
    store_target_credential,
)
from tests.support.browser_scenarios import (
    ProjectSetupBrowserTestCase,
    datetime,
    replace,
    timezone,
    uuid4,
)
from tests.support.identity import resolve_local_actor


class OdooRelationshipReviewBrowserTests(ProjectSetupBrowserTestCase):
    def _guided_relationship_capture_workspace(
        self,
        *,
        include_ambiguous_owner: bool = False,
    ):
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        fields_by_name = {field.name: field for field in schema.models[0].fields}
        name_field = replace(
            fields_by_name["name"],
            required=True,
            readonly=False,
            relation=None,
            stored=True,
            computed=False,
            related=False,
            company_dependent=False,
            exportable=True,
        )
        root_fields = [
            name_field,
            SchemaField(
                name="company_id",
                label="Company",
                type="many2one",
                required=True,
                readonly=False,
                relation="res.company",
                relation_field=None,
                selection=(),
                stored=True,
                computed=False,
                related=False,
                company_dependent=False,
                exportable=True,
            ),
            fields_by_name["write_date"],
        ]
        models = [
            replace(
                schema.models[0],
                name="x.root",
                label="Main records",
                fields=(),
                unique_constraints=(),
            ),
            replace(
                schema.models[0],
                name="res.company",
                label="Companies",
                fields=(
                    replace(name_field, label="Company Name"),
                    replace(
                        name_field,
                        name="vat",
                        label="Tax ID",
                        required=False,
                    ),
                    fields_by_name["write_date"],
                ),
                unique_constraints=(),
            ),
        ]
        if include_ambiguous_owner:
            root_fields.append(
                SchemaField(
                    name="owner_id",
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
                )
            )
            models.append(
                replace(
                    schema.models[0],
                    name="x.owner",
                    label="Owners",
                    fields=(
                        replace(name_field, label="Owner Name"),
                        replace(
                            name_field,
                            name="reference",
                            label="Owner Reference",
                        ),
                        fields_by_name["write_date"],
                    ),
                    unique_constraints=(),
                )
            )
        models[0] = replace(models[0], fields=tuple(root_fields))
        selected_schema = replace(
            schema,
            models=tuple(models),
            content_hash="sha256:" + "6" * 64,
        )
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root",),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            selected_schema,
            actor=resolve_local_actor(context),
        )
        model_catalog = OdooModelCatalog(
            workspace_id=workspace_state.workspace_id,
            connection_target_hash=selected_schema.connection_target_hash,
            policy_hash=selected_schema.policy_hash,
            captured_at=datetime.now(timezone.utc),
            captured_by=resolve_local_actor(context).identity.display_name,
            connection_mode=selected_schema.connection_mode,
            database=selected_schema.database,
            odoo_version=selected_schema.odoo_version,
            models=tuple(
                OdooModelSummary(
                    name=model.name,
                    label=model.label,
                    modules=(),
                    state="base",
                )
                for model in selected_schema.models
            ),
            content_hash="sha256:" + "7" * 64,
            read_credential_binding_hash=(selected_schema.read_credential_binding_hash),
            read_principal_hash=selected_schema.read_principal_hash,
            read_permission_hash=selected_schema.read_permission_hash,
            read_context_hash=selected_schema.read_context_hash,
        )
        context.schema_workspace.schemas.save_odoo_model_catalog(
            workspace_state.workspace_id,
            model_catalog,
            actor=resolve_local_actor(context),
        )
        return workspace_state, selected_schema, model_catalog

    def _save_company_relationship_scope(
        self,
        workspace_state,
        selected_schema,
    ):
        context = self.app.state.context
        decisions = relationship_scope_decisions(
            propose_related_odoo_data(
                selected_schema.models,
                include_selected=True,
            ),
            actions_by_model={
                "res.company": OdooRelationshipCaptureAction.MATCH_EXISTING,
            },
        )
        saved_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root", "res.company"),
            relationship_decisions=decisions,
            relationship_root_models=("x.root",),
        )
        return saved_state, decisions

    def test_odoo_source_page_proposes_related_business_scope(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        product_schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    name="product.template",
                    label="Product",
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
                            name="categ_id",
                            label="Product Category",
                            type="many2one",
                            required=True,
                            readonly=False,
                            relation="product.category",
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        SchemaField(
                            name="company_id",
                            label="Company",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation="res.company",
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        SchemaField(
                            name="activity_ids",
                            label="Activities",
                            type="one2many",
                            required=False,
                            readonly=True,
                            relation="mail.activity",
                            relation_field="res_id",
                            selection=(),
                            related=False,
                            company_dependent=False,
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
                ),
            ),
        )
        context = self.app.state.context
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
                    name="product.category",
                    label="Product Categories",
                ),
                SimpleNamespace(
                    name="res.company",
                    label="Companies",
                ),
                SimpleNamespace(
                    name="mail.activity",
                    label="Activities",
                ),
                SimpleNamespace(
                    name="x.owner",
                    label="Owners",
                ),
            ),
        )

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=product_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn("Choose the Odoo data to move", page.text)
        self.assertIn(
            "Impodo prepares safe, bounded capture plans automatically",
            page.text,
        )
        self.assertIn("Linked data needed for this migration", page.text)
        self.assertIn("Needed to keep the records meaningful", page.text)
        self.assertIn("Reuse destination setup", page.text)
        self.assertIn("Not part of the business-data move", page.text)
        self.assertIn("Safe default for an unclassified link", page.text)
        self.assertIn("Product Category", page.text)
        self.assertIn('name="related_actions"', page.text)
        self.assertIn('value="product.category::PRESERVE_LINKED"', page.text)
        self.assertIn('value="res.company::MATCH_EXISTING"', page.text)
        self.assertNotIn('value="res.company::PRESERVE_LINKED"', page.text)
        self.assertIn('value="x.owner::PRESERVE_LINKED"', page.text)
        self.assertIn("Impodo has prepared the safe relationship plan", page.text)
        self.assertIn("You do not need to confirm each record", page.text)
        self.assertIn("Keep linked value", page.text)
        self.assertIn(
            "Advanced: inspect or change relationship handling",
            page.text,
        )
        self.assertIn("Advanced: choose a different outcome", page.text)
        self.assertNotIn("load another selected record type", page.text)
        self.assertIn("Reading the source is safe and read-only", page.text)
        self.assertIn("<summary>Support details</summary>", page.text)
        self.assertIn(
            "Impodo recommends keeping the populated link without turning",
            page.text,
        )
        self.assertIn(
            "Impodo recommends reusing destination setup and blocking when",
            page.text,
        )
        self.assertIn("Transfer related records as migration data", page.text)
        self.assertIn("Use existing destination records only", page.text)
        self.assertIn("This link is required", page.text)
        self.assertIn(
            "Before transfer, Impodo must match them to existing destination settings",
            page.text,
        )
        self.assertIn("Not included", page.text)
        self.assertIn("data-linked-data-form", page.text)
        self.assertIn("data-linked-data-unsaved-notice", page.text)
        self.assertIn("Save linked-data choices", page.text)
        self.assertIn(
            'data-submitting-label="Saving linked-data choices and updating field choices..."',
            page.text,
        )
        self.assertNotIn("Review recommended supporting data", page.text)
        self.assertNotIn(
            "Related record types outside this source selection",
            page.text,
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            ),
            (),
        )

    def test_related_data_remote_read_failure_keeps_current_workspace_evidence(
        self,
    ) -> None:
        workspace_state, selected_schema, model_catalog = (
            self._guided_relationship_capture_workspace()
        )
        context = self.app.state.context
        workspace_state, _decisions = self._save_company_relationship_scope(
            workspace_state,
            selected_schema,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            selected_schema,
            actor=resolve_local_actor(context),
        )
        context.sources.define_odoo_capture_selection(
            workspace_state.workspace_id,
            dataset_name="main_records",
            model="x.root",
            field_names=("name",),
            include_archived=False,
            page_size=100,
            actor=resolve_local_actor(context),
        )
        context.sources.define_odoo_capture_selection(
            workspace_state.workspace_id,
            dataset_name="companies",
            model="res.company",
            field_names=("name",),
            include_archived=False,
            page_size=100,
            linked_only=True,
            actor=resolve_local_actor(context),
        )
        before_state = context.queries.get(workspace_state.workspace_id)
        before_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        before_schema = context.queries.get_odoo_schema_catalog(
            workspace_state.workspace_id
        )
        before_selections = context.queries.get_current_odoo_capture_selections(
            workspace_state.workspace_id
        )
        remote_failure = AsyncMock(
            side_effect=ConnectorTransportError(
                "Odoo JSON-2 fields_get read for x.root timed out after 3 attempts"
            )
        )

        with (
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
                remote_failure,
            ),
        ):
            failed = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(before_state.revision),
                    "related_actions": "res.company::CAPTURE_LINKED",
                },
            )

        after_state = context.queries.get(workspace_state.workspace_id)
        self.assertEqual(failed.status_code, 422, failed.text)
        self.assertIn(
            "Odoo JSON-2 fields_get read for x.root timed out after 3 attempts",
            failed.text,
        )
        self.assertEqual(after_state.revision, before_state.revision)
        self.assertEqual(after_state.intended_models, before_state.intended_models)
        self.assertEqual(
            context.queries.get_current_odoo_relationship_scope(
                workspace_state.workspace_id
            ),
            before_scope,
        )
        self.assertEqual(
            context.queries.get_odoo_schema_catalog(workspace_state.workspace_id),
            before_schema,
        )
        self.assertEqual(
            context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            ),
            before_selections,
        )
        remote_failure.assert_awaited_once()

    def test_missing_schema_with_retained_roots_offers_same_project_rebuild(
        self,
    ) -> None:
        workspace_state, selected_schema, model_catalog = (
            self._guided_relationship_capture_workspace()
        )
        context = self.app.state.context
        workspace_state, _decisions = self._save_company_relationship_scope(
            workspace_state,
            selected_schema,
        )
        self.assertIsNone(
            context.queries.get_odoo_schema_catalog(workspace_state.workspace_id)
        )
        retained_scope = context.queries.get_current_odoo_relationship_scope(
            workspace_state.workspace_id
        )
        self.assertIsNotNone(retained_scope)
        self.assertEqual(retained_scope.root_models, ("x.root",))

        with patch.object(
            context.queries,
            "get_odoo_model_catalog",
            return_value=model_catalog,
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )

        rebuild_action = (
            f'action="/workspaces/{workspace_state.workspace_id}'
            '/sources/odoo-related-data"'
        )
        self.assertEqual(page.status_code, 200)
        self.assertIn(rebuild_action, page.text)
        self.assertIn(
            f'name="revision" value="{workspace_state.revision}"',
            page.text,
        )
        self.assertIn("Rebuild", page.text)

    def test_relationship_conflict_shows_one_blank_exception_choice(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        shared_model = "x.shared"
        product_schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    name="product.template",
                    label="Product",
                    fields=(
                        SchemaField(
                            name="shared_id",
                            label="Primary shared record",
                            type="many2one",
                            required=False,
                            readonly=False,
                            relation=shared_model,
                            relation_field=None,
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        SchemaField(
                            name="shared_ids",
                            label="Shared records",
                            type="one2many",
                            required=False,
                            readonly=False,
                            relation=shared_model,
                            relation_field="product_id",
                            selection=(),
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                    ),
                ),
            ),
        )
        context = self.app.state.context
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=(
                SimpleNamespace(name="product.template", label="Products"),
                SimpleNamespace(name=shared_model, label="Shared records"),
            ),
        )

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=product_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )
            rejected = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                },
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn("1 relationship exception needs a decision", page.text)
        self.assertIn('id="relationship-exceptions"', page.text)
        self.assertEqual(
            page.text.count('aria-label="Relationship exception for Shared records"'),
            1,
        )
        self.assertIn(
            '<option value="" selected disabled>Choose a deliberate outcome</option>',
            page.text,
        )
        self.assertIn("Why Impodo needs your decision", page.text)
        self.assertIn(
            "<strong>Primary shared record</strong> on Product",
            page.text,
        )
        self.assertIn("&mdash; Keep linked value", page.text)
        self.assertIn("<strong>Shared records</strong> on Product", page.text)
        self.assertIn("&mdash; Do not include", page.text)
        self.assertNotIn(
            'aria-label="Relationship handling for Shared records"',
            page.text,
        )
        self.assertIn(
            '<details class="technical-details">\n'
            "            <summary>Advanced: inspect or change relationship handling</summary>",
            page.text,
        )
        self.assertNotIn(
            '<details class="technical-details" open>\n'
            "            <summary>Advanced: inspect or change relationship handling</summary>",
            page.text,
        )
        self.assertIn("data-linked-data-form", page.text)
        self.assertIn("data-linked-data-unsaved-notice", page.text)
        self.assertIn(
            "Save linked-data choices",
            page.text,
        )
        self.assertEqual(rejected.status_code, 422)

    def test_complete_root_only_review_prepares_capture_plan_automatically(
        self,
    ) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        fields_by_name = {field.name: field for field in schema.models[0].fields}
        root_schema = replace(
            schema,
            models=(
                replace(
                    schema.models[0],
                    name="x.root",
                    label="Main records",
                    fields=(
                        replace(
                            fields_by_name["name"],
                            required=True,
                            readonly=False,
                            relation=None,
                            stored=True,
                            computed=False,
                            related=False,
                            company_dependent=False,
                            exportable=True,
                        ),
                        fields_by_name["write_date"],
                    ),
                    unique_constraints=(),
                ),
            ),
            content_hash="sha256:" + "7" * 64,
        )
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("x.root",),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            root_schema,
            actor=resolve_local_actor(context),
        )
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=(SimpleNamespace(name="x.root", label="Main records"),),
        )
        capture_schema = AsyncMock(return_value=root_schema)

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=root_schema,
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
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
            )
            prepared = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                },
            )
            result = self.client.get(prepared.headers["location"])

        self.assertIn("Let Impodo prepare the capture plans", page.text)
        self.assertEqual(prepared.status_code, 303)
        self.assertEqual(
            prepared.headers["location"],
            f"/workspaces/{workspace_state.workspace_id}/sources#capture-next-action",
        )
        selections = context.queries.get_current_odoo_capture_selections(
            workspace_state.workspace_id
        )
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0].model, "x.root")
        self.assertEqual(selections[0].field_names, ("name",))
        self.assertEqual(selections[0].capture_role.value, "ROOT")
        self.assertIn("Capture-plan setup is complete", result.text)
        self.assertIn("1 main record type selected", result.text)
        self.assertIn("Impodo will read all matching main records", result.text)
        self.assertIn("Edit Main records", result.text)
        capture_schema.assert_awaited_once()

    def test_saved_capture_summary_separates_main_records_from_linked_support(
        self,
    ) -> None:
        workspace_state, selected_schema, model_catalog = (
            self._guided_relationship_capture_workspace()
        )
        context = self.app.state.context

        async def persist_selected_schema(
            _context, saved_state, _model_catalog, *, actor
        ):
            self.assertEqual(actor, resolve_local_actor(context))
            context.schema_workspace.schemas.save_odoo_schema_catalog(
                saved_state.workspace_id,
                selected_schema,
                actor=resolve_local_actor(context),
            )
            return selected_schema

        capture_schema = AsyncMock(side_effect=persist_selected_schema)

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
            prepared = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                },
            )
            self.assertEqual(prepared.status_code, 303, prepared.text)
            result = self.client.get(prepared.headers["location"])

        selections = context.queries.get_current_odoo_capture_selections(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {(item.model, item.capture_role.value) for item in selections},
            {
                ("x.root", "ROOT"),
                ("res.company", "LINKED_ONLY"),
            },
        )
        company = next(item for item in selections if item.model == "res.company")
        self.assertEqual(company.field_names, ("name",))
        self.assertIn("1 main record type selected", result.text)
        self.assertIn(
            "Impodo also prepared 1 automatic linked-value support record type",
            result.text,
        )
        self.assertIn(
            "Advanced: 1 automatic linked-value support record type",
            result.text,
        )
        self.assertIn("Companies · linked-value support", result.text)
        self.assertIn("Edit Main records", result.text)
        advanced_edit = result.text.index(
            "Advanced: edit automatic linked-value support"
        )
        company_edit = result.text.index("Edit Companies")
        self.assertLess(advanced_edit, company_edit)
        self.assertNotIn("2 Odoo capture plans", result.text)

    def test_exception_editor_shows_only_named_models_and_keeps_explicit_edits(
        self,
    ) -> None:
        workspace_state, selected_schema, model_catalog = (
            self._guided_relationship_capture_workspace(include_ambiguous_owner=True)
        )
        context = self.app.state.context

        async def persist_selected_schema(
            _context, saved_state, _model_catalog, *, actor
        ):
            self.assertEqual(actor, resolve_local_actor(context))
            context.schema_workspace.schemas.save_odoo_schema_catalog(
                saved_state.workspace_id,
                selected_schema,
                actor=resolve_local_actor(context),
            )
            return selected_schema

        capture_schema = AsyncMock(side_effect=persist_selected_schema)
        sources_url = f"/workspaces/{workspace_state.workspace_id}/sources"

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
            prepared = self._post(
                f"{sources_url}/odoo-related-data",
                {
                    "csrf_token": self.csrf,
                    "revision": str(workspace_state.revision),
                    "related_actions": "x.owner::MATCH_EXISTING",
                },
            )
            self.assertEqual(prepared.status_code, 303, prepared.text)
            exception_page = self.client.get(prepared.headers["location"])
            explicit_page = self.client.get(
                f"{sources_url}?model=res.company&edit=1#capture-plan"
            )

            set_issue = GuidedOdooCapturePlanProposal(
                drafts=(),
                issues=(
                    GuidedOdooCapturePlanIssue(
                        code=GuidedOdooCaptureIssueCode.DATASET_LIMIT,
                        model=None,
                        model_label="Capture plan set",
                        reason="The automatic capture set needs one set-level decision.",
                    ),
                ),
                skipped_models=(),
            )
            with patch(
                "impodo.web.routers.sources.propose_guided_odoo_capture_plans",
                return_value=set_issue,
            ):
                set_issue_page = self.client.get(sources_url)

        self.assertIn("model=x.owner", prepared.headers["location"])
        owner_edit_url = "?model=x.owner&amp;edit=1#capture-plan"
        company_edit_url = "?model=res.company&amp;edit=1#capture-plan"
        root_edit_url = "?model=x.root&amp;edit=1#capture-plan"
        self.assertIn("This record type needs one decision", exception_page.text)
        self.assertIn("Your saved linked-data choices", exception_page.text)
        self.assertIn(
            "<strong>Owners:</strong> Use existing destination records only",
            exception_page.text,
        )
        self.assertIn("Your saved choice", exception_page.text)
        self.assertIn("data-capture-plan-form", exception_page.text)
        self.assertIn(owner_edit_url, exception_page.text)
        self.assertNotIn(company_edit_url, exception_page.text)
        self.assertNotIn(root_edit_url, exception_page.text)
        self.assertIn(
            "Advanced: linked-value support requiring review",
            exception_page.text,
        )

        self.assertIn(owner_edit_url, explicit_page.text)
        self.assertIn(company_edit_url, explicit_page.text)
        self.assertNotIn(root_edit_url, explicit_page.text)
        self.assertIn(
            'name="model" value="res.company"',
            explicit_page.text,
        )

        self.assertIn("1 capture-plan exception remains", set_issue_page.text)
        self.assertIn(
            "The automatic capture set needs one set-level decision",
            set_issue_page.text,
        )
        self.assertNotIn(
            'aria-label="Main Odoo record types"',
            set_issue_page.text,
        )
        self.assertNotIn(
            'aria-label="Linked-value support record types"',
            set_issue_page.text,
        )
        self.assertNotIn(
            f'action="{sources_url}/odoo-selection"',
            set_issue_page.text,
        )

        completed = self._post(
            f"{sources_url}/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "owners",
                "model": "x.owner",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
                "linked_only": "1",
            },
        )

        self.assertEqual(completed.status_code, 303, completed.text)
        self.assertEqual(
            completed.headers["location"],
            f"{sources_url}#capture-next-action",
        )
        selections = context.queries.get_current_odoo_capture_selections(
            workspace_state.workspace_id
        )
        self.assertEqual(
            {(item.model, item.capture_role.value) for item in selections},
            {
                ("res.company", "LINKED_ONLY"),
                ("x.owner", "LINKED_ONLY"),
                ("x.root", "ROOT"),
            },
        )

        root_resaved = self._post(
            f"{sources_url}/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "odoo_x_root",
                "model": "x.root",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
                "linked_only": "1",
            },
        )

        self.assertEqual(root_resaved.status_code, 303, root_resaved.text)
        root_selection = next(
            item
            for item in context.queries.get_current_odoo_capture_selections(
                workspace_state.workspace_id
            )
            if item.model == "x.root"
        )
        self.assertEqual(root_selection.capture_role.value, "ROOT")

    def test_recommended_supporting_model_defaults_to_linked_only(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("product.category", "product.template"),
        )
        product = replace(
            schema.models[0],
            name="product.template",
            label="Products",
            fields=(
                *schema.models[0].fields,
                SchemaField(
                    name="categ_id",
                    label="Product Category",
                    type="many2one",
                    required=True,
                    readonly=False,
                    relation="product.category",
                    relation_field=None,
                    selection=(),
                    related=False,
                    company_dependent=False,
                    exportable=True,
                ),
            ),
        )
        category = replace(
            schema.models[0],
            name="product.category",
            label="Product Categories",
        )
        product_schema = replace(schema, models=(product, category))
        model_catalog = SimpleNamespace(
            content_hash=context.queries.get_odoo_model_catalog(
                workspace_state.workspace_id
            ).content_hash,
            models=(
                SimpleNamespace(
                    name="product.category",
                    label="Product Categories",
                ),
                SimpleNamespace(
                    name="product.template",
                    label="Products",
                ),
            ),
        )
        decisions = relationship_scope_decisions(
            propose_related_odoo_data(
                product_schema.models,
                include_selected=True,
            ),
            included_models={"product.category"},
        )
        relationship_scope = OdooRelationshipScope.create(
            scope_id=str(uuid4()),
            version=1,
            decisions=decisions,
            root_models=("product.template",),
            recorded_at=datetime.now(timezone.utc),
            recorded_by=resolve_local_actor(context).identity.display_name,
        )

        with (
            patch.object(
                context.queries,
                "get_odoo_schema_catalog",
                return_value=product_schema,
            ),
            patch.object(
                context.queries,
                "get_odoo_model_catalog",
                return_value=model_catalog,
            ),
            patch.object(
                context.queries,
                "get_current_odoo_relationship_scope",
                return_value=relationship_scope,
            ),
        ):
            page = self.client.get(
                f"/workspaces/{workspace_state.workspace_id}/sources"
                "?model=product.category&edit=1"
            )

        self.assertEqual(page.status_code, 200)
        self.assertIn('type="hidden" name="linked_only" value="1"', page.text)
        self.assertNotRegex(
            page.text,
            r'type="checkbox" name="linked_only"',
        )
        self.assertIn("Linked-value support", page.text)
        self.assertIn("Archived linked records are included", page.text)

    def test_completed_capture_action_is_inside_current_evidence(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        credential = store_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        schema = replace(
            schema,
            read_credential_binding_hash=credential.binding_hash,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            schema,
            actor=resolve_local_actor(context),
        )

        with collect_duckdb_request_timings() as selection_timings:
            selected = self._post(
                f"/workspaces/{workspace_state.workspace_id}/sources/odoo-selection",
                {
                    "csrf_token": self.csrf,
                    "dataset_name": "odoo_contacts",
                    "model": schema.models[0].name,
                    "field_names": "name",
                    "include_archived": "",
                    "page_size": "100",
                },
            )

        self.assertEqual(selected.status_code, 303)
        self.assertLessEqual(selection_timings.connection_count, 8)
        page = self.client.get(selected.headers["location"])
        current_evidence = page.text.index("Current protected evidence")
        next_action = page.text.index("Capture plans complete")
        section_end = page.text.index("</section>", current_evidence)
        self.assertLess(current_evidence, next_action)
        self.assertLess(next_action, section_end)
        self.assertIn(
            'data-submitting-label="Counting selected source records in Odoo..."',
            page.text,
        )

    def test_linked_relationship_review_is_inside_current_evidence(self) -> None:
        workspace_state, schema = self._registered_remote_schema_workspace()
        context = self.app.state.context
        workspace_state = context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=workspace_state.revision,
            permitted_models=("product.template", "product.category"),
        )
        base_model = schema.models[0]
        product = replace(
            base_model,
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
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    exportable=True,
                ),
                SchemaField(
                    name="write_date",
                    label="Last Updated",
                    type="datetime",
                    required=False,
                    readonly=True,
                    relation=None,
                    relation_field=None,
                    selection=(),
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    sortable=True,
                    exportable=True,
                ),
                SchemaField(
                    name="categ_id",
                    label="Product Category",
                    type="many2one",
                    required=True,
                    readonly=False,
                    relation="product.category",
                    relation_field=None,
                    selection=(),
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    exportable=True,
                ),
            ),
        )
        category = replace(
            base_model,
            name="product.category",
            label="Product Categories",
            fields=(
                SchemaField(
                    name="name",
                    label="Category Name",
                    type="char",
                    required=True,
                    readonly=False,
                    relation=None,
                    relation_field=None,
                    selection=(),
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    exportable=True,
                ),
                SchemaField(
                    name="write_date",
                    label="Last Updated",
                    type="datetime",
                    required=False,
                    readonly=True,
                    relation=None,
                    relation_field=None,
                    selection=(),
                    stored=True,
                    related=False,
                    company_dependent=False,
                    searchable=True,
                    sortable=True,
                    exportable=True,
                ),
            ),
        )
        credential = store_target_credential(
            self.secrets,
            workspace_state,
            TargetCredentialRole.READ,
            "read-secret",
            persistent=False,
        )
        schema = replace(
            schema,
            models=(product, category),
            read_credential_binding_hash=credential.binding_hash,
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_state.workspace_id,
            schema,
            actor=resolve_local_actor(context),
        )
        current_state = context.queries.get(workspace_state.workspace_id)
        context.workspace_states.update_schema_scope(
            workspace_state.workspace_id,
            actor=resolve_local_actor(context),
            expected_revision=current_state.revision,
            permitted_models=current_state.intended_models,
            relationship_decisions=relationship_scope_decisions(
                propose_related_odoo_data(
                    schema.models,
                    include_selected=True,
                ),
                included_models={"product.category"},
            ),
            relationship_root_models=("product.template",),
            relationship_explicit_actions=(
                (
                    "product.category",
                    OdooRelationshipCaptureAction.CAPTURE_LINKED,
                ),
            ),
        )

        root = self._post(
            f"/workspaces/{workspace_state.workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "products",
                "model": "product.template",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
            },
        )
        self.assertEqual(root.status_code, 303, root.text)
        linked = self._post(
            f"/workspaces/{workspace_state.workspace_id}/sources/odoo-selection",
            {
                "csrf_token": self.csrf,
                "dataset_name": "product_categories",
                "model": "product.category",
                "field_names": "name",
                "include_archived": "",
                "page_size": "100",
                "linked_only": "1",
            },
        )
        self.assertEqual(linked.status_code, 303, linked.text)

        page = self.client.get(linked.headers["location"])
        current_evidence = page.text.index("Current protected evidence")
        relationship_review = page.text.index(
            "Relationship fields used to find linked records"
        )
        next_action = page.text.index("Capture plans complete")
        section_end = page.text.index("</section>", current_evidence)
        self.assertLess(current_evidence, relationship_review)
        self.assertLess(relationship_review, next_action)
        self.assertLess(next_action, section_end)
        self.assertIn(
            "Products.Product Category (product.template.categ_id) refers to "
            "product.category",
            page.text,
        )
