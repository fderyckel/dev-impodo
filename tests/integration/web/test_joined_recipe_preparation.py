"""Verify joined Many2one aliases through preparation and saved Recipe reuse."""

from datetime import UTC, datetime
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from starlette.datastructures import FormData

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.mapping.validation.evidence import mapping_issue_fingerprint
from impodo.domain.odoo.company import OdooCompanySelection
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.exact_join_forms import (
    parse_exact_join_group_form,
    resolve_exact_join_group_request,
)
from tests.integration.web import test_exact_join_workflow as fixtures
from tests.integration.web.test_fresh_data_controls import field as form_value
from tests.support.browser_scenarios import POST_HEADERS, _browser_schema
from tests.support.identity import resolve_local_actor


class JoinedRecipePreparationTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.ExactJoinBrowserTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.context = self.fixture.app.state.context
        self.actor = resolve_local_actor(self.context)

    def _schema(self, workspace_id):
        context, actor = self.context, self.actor
        state = context.queries.get(workspace_id)
        state = context.workspace_states.update_target(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            odoo_connection_mode="LOCAL",
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_applications=(),
            intended_models=("x_record",),
        )

        def field(name, field_type="char", relation=None):
            return SchemaField(name, name, field_type, False, False, relation, None, ())

        schema = OdooSchemaCatalog(
            workspace_id=workspace_id,
            policy_hash=content_hash("policy"),
            captured_at=datetime.now(UTC),
            captured_by="Fixture",
            connection_mode="LOCAL",
            database="odoo19_local",
            odoo_version="19.0",
            content_hash=content_hash((workspace_id, "schema")),
            models=(
                SchemaModel(
                    "x_record",
                    "Record",
                    (field("ref"), field("x_list_id", "many2one", "x_list")),
                ),
                SchemaModel("x_list", "List", (field("ref"),)),
            ),
            origin=SchemaOrigin.LIVE_API,
            connection_target_hash=_browser_schema(state).fingerprint.target_hash,
            read_credential_binding_hash=content_hash("credential"),
            read_principal_hash=content_hash("principal"),
            read_permission_hash=content_hash("permissions"),
            read_context_hash=content_hash("context"),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        keys = tuple(
            BusinessKeyDefinition(
                f"{model}:ref", model, ("ref",), status=BusinessKeyStatus.CONFIRMED
            )
            for model in ("x_record", "x_list")
        )
        context.schema_workspace.govern(workspace_id, business_keys=keys, actor=actor)
        return schema

    def _prepare(self, workspace_id):
        source = self.context.preparation.grouped_exact_joins
        with (
            patch.object(
                self.context,
                "readiness_reader",
                side_effect=AssertionError("Prepare data must not contact Odoo"),
            ),
            patch.object(
                source, "iter_batches", wraps=source.iter_batches
            ) as batch_reader,
            collect_duckdb_request_timings() as timings,
        ):
            self.context.preparation.prepare(workspace_id, actor=self.actor)
        batch_reader.assert_called_once()
        self.assertEqual(
            len(batch_reader.call_args.kwargs["group"].participant_dataset_ids), 4
        )
        summary = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        run = self.context.preparation.staging.get_canonical_staging_run(
            workspace_id, summary.run_id, expected_content_hash=summary.content_hash
        )
        self.assertEqual(run.reconciliation.blocked_rows, 0, run.issues)
        self.assertTrue(all(len(row.lineage.physical_sources) == 4 for row in run.rows))
        references = {
            row.target_identity: row.references["x_list_id"].key
            if row.references["x_list_id"] is not None
            else None
            for row in run.rows
        }
        self.assertTrue(
            all(
                row.references["x_list_id"] is None
                or row.references["x_list_id"].model == "x_list"
                for row in run.rows
            )
        )
        self.assertEqual(timings.lock_retry_count, 0)
        return references

    def test_saved_joined_many2one_recipe_prepares_fresh_files_with_the_same_aliases(
        self,
    ):
        self._exercise_recipe_reuse(uncovered_choice=False)

    def test_reused_joined_recipe_blocks_a_new_unmatched_many2one_choice(self):
        self._exercise_recipe_reuse(uncovered_choice=True)

    def test_joined_many2one_leave_blank_survives_preparation_and_recipe_reuse(self):
        self._exercise_recipe_reuse(uncovered_choice=False, blank_choice=True)

    def test_joined_recipe_rebinds_company_in_a_fresh_run_without_copying_odoo_ids(
        self,
    ):
        self._exercise_recipe_reuse(uncovered_choice=False, company_binding=True)

    def _exercise_recipe_reuse(
        self, *, uncovered_choice, blank_choice=False, company_binding=False
    ):
        context, actor, fixture = self.context, self.actor, self.fixture
        workspace_id, physical, by_name = fixture._frozen_customer_workspace(
            several_tables=True
        )
        authoring_schema = self._schema(workspace_id)
        if company_binding:
            state = context.queries.get(workspace_id)
            company = OdooCompanySelection(
                authoring_schema.connection_target_hash,
                105,
                "Fictional authoring factory",
            )
            context.workspace_states.select_odoo_company(
                workspace_id,
                actor=actor,
                expected_revision=state.revision,
                selection=company,
            )
            context.schema_workspace.schemas.save_odoo_create_defaults(
                workspace_id,
                replace(authoring_schema, read_context_hash=company.context_hash),
                expected_content_hash=authoring_schema.content_hash,
                expected_read_credential_binding_hash=authoring_schema.read_credential_binding_hash,
                expected_company_context_hash=company.context_hash,
                actor=actor,
            )
        form = fixture._join_group_form(physical, by_name)
        items = [
            (name, item)
            for name, value in form.items()
            for item in (value if isinstance(value, list) else [value])
        ]
        request = resolve_exact_join_group_request(
            parse_exact_join_group_form(FormData(items)), physical
        )
        run_local_operation_sync(
            context.derived_entities.save_join_group,
            workspace_id,
            request,
            expected_parent_version=None,
            actor=actor,
        )
        effective = context.queries.get_mapping_source_selection(workspace_id)
        dataset = effective.datasets[0]
        columns = {column.source_name: column.stable_key for column in dataset.columns}
        aliases = (
            ValueMapping("Fictional Retail", "B"),
            ValueMapping("United Caps", "A"),
        )
        if blank_choice:
            aliases = aliases[1:]
        mapping = DatasetMapping(
            dataset_id=dataset.dataset_id,
            target_model="x_record",
            source_identity_column_keys=(columns["account_number"],),
            target_identity=(
                IdentityComponentMapping(
                    (columns["account_number"],), ("ref",), "string"
                ),
            ),
            relationships=(
                RelationshipMapping(
                    target_field="x_list_id",
                    kind="many2one",
                    source_column_keys=(columns["name"],),
                    categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
                    blank_source_values=("Fictional Retail",) if blank_choice else (),
                    resolver=RelationshipResolver(
                        origin=ResolverOrigin.TARGET_CATALOG,
                        model="x_list",
                        key_mappings=(ReferenceKeyMapping(columns["name"], "ref"),),
                        value_mappings=aliases,
                    ),
                ),
            ),
        )
        revision, validation = run_local_operation_sync(
            context.mapping_workspace.check_definition,
            workspace_id,
            datasets=(mapping,),
            expected_parent_version=None,
            expected_working_draft_version=None,
            actor=actor,
        )
        self.assertNotEqual(validation.status.value, "INVALID", validation.issues)
        draft = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        run_local_operation_sync(
            context.mapping_workspace.submit_current,
            workspace_id,
            datasets=revision.definition.datasets,
            expected_version=revision.version,
            expected_working_draft_version=draft.version,
            warning_acknowledgements=tuple(
                mapping_issue_fingerprint(issue)
                for issue in validation.issues
                if issue.severity == "warning"
            ),
            actor=actor,
        )
        self.assertEqual(
            self._prepare(workspace_id),
            {("C001",): ("A",), ("C002",): None if blank_choice else ("B",)},
        )
        owner = context.migration_workspaces.get(workspace_id, actor=actor)
        recipe_draft = context.recipe_publication.draft(
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            actor=actor,
        )
        self.assertTrue(recipe_draft.can_publish, recipe_draft.issues)
        publication = run_local_operation_sync(
            context.recipe_publication.publish,
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            display_name="Joined generic records",
            business_purpose="Preserve the approved classification matches",
            actor=actor,
        )
        envelope = context.recipes.repository.read_recipe_revision(
            publication.recipe.recipe_id, publication.revision.version
        )
        definition = envelope["recipe"]
        if company_binding:
            self.assertNotIn("odoo_company", str(definition))
            self.assertNotIn("company_binding_hash", str(definition))
            self.assertNotIn(company.name, str(definition))
        saved = definition["mapping"]["datasets"][0]["relationships"][0]
        self.assertEqual(
            saved["blank_source_values"], ["Fictional Retail"] if blank_choice else []
        )
        self.assertEqual(
            saved["value_matches"],
            [
                {"source_value": item.source_value, "target_value": item.target_value}
                for item in aliases
            ],
        )
        self.assertNotIn(dataset.dataset_id, str(definition))
        self.assertNotIn(request.group_id, str(definition))

        export_as_of = datetime.now(UTC).date().isoformat()
        setup = context.test_runs.start_setup(
            owner.project_id,
            expected_workspace_revision=context.migration_projects.get(
                owner.project_id, actor=actor
            ).optimistic_revision,
            recipe_revisions=(
                (publication.recipe.recipe_id, publication.revision.version),
            ),
            dependencies=(),
            label="Fresh joined records",
            export_as_of=export_as_of,
            operation_id=str(uuid4()),
            actor=actor,
        )
        url = f"/projects/{owner.project_id}/test-runs/{setup.run.migration_run_id}/fresh-data"
        page = fixture.client.get(url)
        files = [
            ("source_file", (name, values, "text/csv"))
            for name, values in (
                (
                    "accounts.csv",
                    b"Partition,Party,AccountNum,Company\npart-2,1000,C101,Co\npart-2,2000,C102,Co\npart-2,3000,C103,Co\n",
                ),
                (
                    "details.csv",
                    b"Partition,RecId,Name,Email\npart-2,1000,Fictional Retail,one@example.test\npart-2,2000,United Caps,two@example.test\npart-2,3000,Fictional Retail,three@example.test\npart-2,9999,Unused,new@example.test\n",
                ),
                (
                    "addresses.csv",
                    b"Partition,Party,City\npart-2,1000,One\npart-2,2000,Two\npart-2,3000,Three\n",
                ),
                (
                    "terms.csv",
                    b"Partition,Party,PaymentTerm\npart-2,1000,30D\npart-2,2000,45D\npart-2,3000,30D\n",
                ),
            )
        ]
        if uncovered_choice:
            files = [
                (
                    key,
                    (
                        name,
                        values.replace(b"Fictional Retail,one", b"New choice,one"),
                        media_type,
                    ),
                )
                for key, (name, values, media_type) in files
            ]
        uploaded = fixture.client.post(
            url + "/files",
            data={
                "csrf_token": form_value(page, "csrf_token"),
                "revision": form_value(page, "revision"),
            },
            files=files,
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        page = fixture.client.get(url)
        registered = fixture.client.post(
            url + "/register",
            data={
                "csrf_token": form_value(page, "csrf_token"),
                "revision": form_value(page, "revision"),
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(registered.status_code, 303, registered.text)
        page = fixture.client.get(url)
        accepted = fixture.client.post(
            url + "/accept",
            data={
                "csrf_token": form_value(page, "csrf_token"),
                "parameter_revision": "",
                "warnings_acknowledged": "1",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(accepted.status_code, 303, accepted.text)
        fresh_schema = self._schema(setup.setup_workspace.workspace_id)
        if company_binding:
            fresh_id = setup.setup_workspace.workspace_id
            state = context.queries.get(fresh_id)
            self.assertIsNone(state.odoo_company)
            keys = context.schema_workspace.schemas.get_schema_governance(
                fresh_id
            ).business_keys
            state = context.workspace_states.update_target(
                fresh_id,
                actor=actor,
                expected_revision=state.revision,
                odoo_connection_mode="LOCAL",
                odoo_base_url=state.odoo_base_url,
                odoo_database="fictional_rehearsal",
                intended_applications=(),
                intended_models=state.intended_models,
            )
            new_company = OdooCompanySelection(
                _browser_schema(state).fingerprint.target_hash,
                7001,
                "Fictional rehearsal factory",
            )
            context.workspace_states.select_odoo_company(
                fresh_id,
                actor=actor,
                expected_revision=state.revision,
                selection=new_company,
            )
            fresh_schema = replace(
                fresh_schema,
                database=state.odoo_database,
                connection_target_hash=new_company.target_hash,
                read_context_hash=new_company.context_hash,
                content_hash=content_hash(
                    (fresh_schema.content_hash, "fresh-company-target")
                ),
            )
            context.schema_workspace.schemas.save_odoo_schema_catalog(
                fresh_id, fresh_schema, actor=actor
            )
            context.schema_workspace.govern(fresh_id, business_keys=keys, actor=actor)
        evaluator = context.run_planning.compiler.preparation.group_evaluator
        original_review = evaluator.review

        def review(**kwargs):
            with collect_duckdb_request_timings() as timings:
                preview = original_review(**kwargs)
            self.assertEqual(timings.connection_count, 1)
            self.assertEqual(timings.schema_check_count, 0)
            self.assertEqual(timings.lock_retry_count, 0)
            self.assertEqual(preview.physical_snapshot_scans, 4)
            self.assertTrue(preview.bounded_execution_plan_verified)
            return preview

        with patch.object(evaluator, "review", side_effect=review) as join_review:
            activated = run_local_operation_sync(
                context.test_runs.activate,
                owner.project_id,
                setup.run.migration_run_id,
                expected_workspace_revision=context.migration_projects.get(
                    owner.project_id, actor=actor
                ).optimistic_revision,
                target_schema=fresh_schema,
                target_reference_bundle=None,
                credential_generation=fresh_schema.read_credential_binding_hash,
                operation_id=str(uuid4()),
                actor=actor,
            )
        join_review.assert_called_once()
        application = activated.applications[0]
        if company_binding:
            applied_state = context.queries.get(application.workspace_id)
            self.assertEqual(applied_state.odoo_company, new_company)
            self.assertEqual(
                applied_state.odoo_operation_context, {"allowed_company_ids": [7001]}
            )
            with self.assertRaisesRegex(ValueError, "fixed for this activated run"):
                context.workspace_states.select_odoo_company(
                    application.workspace_id,
                    actor=actor,
                    expected_revision=applied_state.revision,
                    selection=replace(
                        new_company, company_id=9003, name="Another company"
                    ),
                )
        if uncovered_choice:
            self.assertEqual(application.status, RecipeApplicationStatus.BLOCKED)
            issues = context.run_planning.repository.list_issues(
                application.application_id
            )
            self.assertIn(
                "MAPPING_CATEGORICAL_COVERAGE_INCOMPLETE",
                {item.code for item in issues},
            )
            self.assertIsNone(
                context.preparation.staging.get_current_staging_summary(
                    application.workspace_id
                )
            )
            return
        self.assertEqual(
            application.status,
            RecipeApplicationStatus.READY,
            context.run_planning.repository.list_issues(application.application_id),
        )
        fresh = context.queries.get_mapping_source_selection(application.workspace_id)
        self.assertNotEqual(fresh.datasets[0].dataset_id, dataset.dataset_id)
        fresh_plan = context.derived_entities.get_derived_entity_plan(
            application.workspace_id
        )
        fresh_physical = context.derived_entities.sources.get_source_selection(
            application.workspace_id
        )
        self.assertNotEqual(fresh_plan.rules[0].group_id, request.group_id)
        self.assertTrue(fresh_plan.join_reviews)
        self.assertTrue(
            all(
                item.source_selection_hash == fresh_physical.content_hash
                and item.review.actual_output_rows == 3
                for item in fresh_plan.join_reviews
            )
        )
        rebound = context.mapping_workspace.mappings.get_mapping_revision(
            application.workspace_id
        )
        self.assertEqual(
            rebound.definition.datasets[0].relationships[0].resolver.value_mappings,
            aliases,
        )
        self.assertEqual(
            rebound.definition.datasets[0].relationships[0].blank_source_values,
            ("Fictional Retail",) if blank_choice else (),
        )
        self.assertEqual(
            self._prepare(application.workspace_id),
            {
                ("C101",): None if blank_choice else ("B",),
                ("C102",): ("A",),
                ("C103",): None if blank_choice else ("B",),
            },
        )
