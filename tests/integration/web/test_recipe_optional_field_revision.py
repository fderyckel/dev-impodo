"""Qualify publishing a Recipe successor after one optional Odoo field is added."""

from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from re import search
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.coverage import (
    CoverageApplicability, CoverageDeclaration, CoverageFamily, CoverageScopeRevision,
    ReferenceBundle, ReferenceDataSet, ReferenceEntry, ReferenceValueKind,
)
from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping, ReferenceLookupMapping, ScalarFieldMapping
from impodo.domain.mapping.validation.evidence import mapping_issue_fingerprint
from impodo.domain.preparation.quality import (
    QualityOutcomePolicy, QualityOwnerRole, QualityRule, QualityRuleFamily,
    QualityRuleSource, manager_quality_rule,
)
from impodo.domain.recipe.models import RecipeError, RecipeIntegrityError
from impodo.domain.recipe_parameters import RecipeParameterDefinition, RecipeParameterDefinitions, RecipeParameterType
from impodo.domain.run.contracts import RecipeApplicationStatus
from impodo.domain.run.models import MigrationRunPurpose
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Capability
from impodo.domain.shared.models import FieldMetadata
from impodo.domain.workspace.supporting_review import SupportingChoice, SupportingChoiceAction
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import OdooConnectionMode
from impodo.web.target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
    store_target_credential,
)
from tests.support.browser_scenarios import (
    POST_HEADERS,
    ProjectSetupBrowserTestCase,
    _browser_schema,
    _register_workspace_through_services,
)
from tests.support.database_probe import count_duckdb_statements
from tests.support.identity import (
    MutableLocalRequestIdentityResolver,
    resolve_local_actor,
)
from tests.support.recipe_lifecycle import (
    execute_test_application,
    metadata_for_schema,
    prepare_test_application,
)


class RecipeOptionalFieldRevisionTests(ProjectSetupBrowserTestCase):
    def test_added_field_is_published_and_verified_in_a_fresh_test(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    self._exercise_revision(major, mode)

    def test_historical_draft_restores_saved_dependencies(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    self._exercise_revision(major, mode, with_dependencies=True)

    def test_historical_draft_requires_fresh_coverage_approval(self):
        for major in (19, 20):
            for mode in OdooConnectionMode:
                with self.subTest(major=major, mode=mode):
                    self._exercise_revision(major, mode, with_dependencies=True, with_advanced=True)

    def _exercise_revision(self, major, mode, *, with_dependencies=False, with_advanced=False):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        project = context.project_authoring.create(
            actor=actor,
            display_name="Customer migration",
            source_mode="FILE",
            creation_request_id=str(uuid4()),
        )
        workspace_id = project.workspace.workspace_id
        source_file = context.intake.accept(
            workspace_id,
            actor=actor,
            expected_revision=project.workspace_state.revision,
            display_name="customers.csv",
            stream=BytesIO(
                b"Reference,Customer name,Migration note\nC001,Acme,Reviewed\n"
            ),
        )
        state = _register_workspace_through_services(
            context,
            project.workspace_state,
            connection_mode=mode,
            base_url=(
                "http://127.0.0.1:8069"
                if mode is OdooConnectionMode.LOCAL
                else "https://fictional.example.test"
            ),
            database=f"odoo{major}_authoring",
            intended_models=("res.partner",),
        )
        context.inspections.inspect_project(workspace_id, actor=actor)
        context.sources.confirm_source(
            workspace_id,
            source_file.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=actor,
        )
        context.data_version_source_projection.finalize_file_selection(
            workspace_id,
            dataset_names={(source_file.file_id, "csv"): "customers"},
            actor=actor,
        )
        source = context.queries.get_source_selection(workspace_id)
        dataset = source.datasets[0]
        ref_column, name_column, note_column = (
            column.stable_key for column in dataset.columns
        )

        template = _browser_schema(state)
        model = template.models["res.partner"]
        fields = {field: model.fields[field] for field in ("name", "ref")}
        if with_dependencies:
            fields["category_ids"] = model.fields["category_ids"]
            fields["comment"] = FieldMetadata(name="comment", label="Note", type="char")
        snapshot = replace(
            template,
            fingerprint=replace(
                template.fingerprint,
                odoo_version=f"{major}.0",
                module_versions={"base": f"{major}.0.1.0"},
            ),
            models={model.model: replace(model, fields=fields)},
        )
        read_identity = None
        binding = local_read_credential_binding_hash(state)
        if mode is OdooConnectionMode.REMOTE:
            credential = store_target_credential(
                context.secret_store,
                state,
                TargetCredentialRole.READ,
                "fictional-authoring-read",
                persistent=False,
            )
            binding = credential.binding_hash
            read_identity = context.read_identity_probe(
                state, credential.secret, (model.model,)
            )
        schema = context.schema_workspace.capture(
            workspace_id,
            snapshot,
            read_credential_binding_hash=binding,
            read_identity=read_identity,
            actor=actor,
        )
        key = BusinessKeyDefinition(
            key_id="res.partner:ref",
            model=model.model,
            key_fields=("ref",),
            description="Customer reference",
            status=BusinessKeyStatus.CONFIRMED,
        )
        context.schema_workspace.govern(workspace_id, business_keys=(key,), actor=actor)

        if with_dependencies:
            self._exercise_dependencies(project, snapshot, mode, dataset, actor, with_advanced=with_advanced)
            return

        mapping_path = f"/workspaces/{workspace_id}/mapping"
        form = {
            "csrf_token": self.csrf,
            "editable_dataset_id": dataset.dataset_id,
            "target_model_0": model.model,
            "mode_0": "upsert",
            "source_identity_0": ref_column,
            "business_key_0": key.key_id,
            "identity_source_0_0": ref_column,
        }
        self._confirm_mapping(mapping_path, form, "name", name_column)
        original = self._publish_original(project)
        recipe_url = f"/projects/{project.project.project_id}/recipes/{original.recipe.recipe_id}"
        revise_url = f"{recipe_url}/revise?version=1"
        earlier_application = None
        extra_checks = major == 19 and mode is OdooConnectionMode.LOCAL
        if extra_checks:
            earlier_application = self._apply_recipe(original, schema, mode)
            newer_workspace = context.migration_workspaces.create(
                project.project.project_id,
                actor=actor,
                expected_workspace_revision=context.migration_projects.get(
                    project.project.project_id,
                    actor=actor,
                ).optimistic_revision,
                data_version_id=project.data_version.data_version_id,
                migration_run_id=project.workspace.migration_run_id,
                display_name="Unrelated Authoring draft",
            )
            reader = replace(
                actor,
                capabilities=actor.capabilities
                - {Capability.RECIPE_PUBLISH, Capability.PROJECT_ADMIN},
            )
            with (
                patch.object(
                    context,
                    "identity_resolver",
                    MutableLocalRequestIdentityResolver(reader),
                ),
                patch.object(
                    context.recipes,
                    "read_revisions",
                    side_effect=AssertionError("Denied review must not read Recipes"),
                ),
            ):
                self.assertEqual(self.client.get(revise_url).status_code, 404)
        detail = self.client.get(recipe_url + "?version=1")
        self.assertIn(f'href="{revise_url}">Revise this Recipe', detail.text)
        with (
            collect_duckdb_request_timings() as entry_timing,
            count_duckdb_statements() as entry_sql,
            patch.object(
                context.migration_workspaces,
                "list_for_project",
                side_effect=AssertionError("Review must read its one workspace"),
            ),
        ):
            entry = self.client.get(revise_url)
        self.assertEqual(entry.status_code, 200, entry.text)
        self.assertIn("Starting from Recipe v1", entry.text)
        self.assertIn("Open Match data", entry.text)
        self.assertNotIn("Save a new Recipe version", entry.text)
        self.assertLessEqual(entry_timing.connection_count, 4, entry_timing)
        self.assertLessEqual(entry_timing.schema_check_count, 3, entry_timing)
        self.assertLessEqual(len(entry_sql), 115)
        started = self._post(revise_url, {"csrf_token": self.csrf, "action": "start"})
        self.assertEqual(started.status_code, 303, started.text)
        self.assertEqual(
            started.headers["location"],
            mapping_path + "?step=check#recipe-revision-workspace",
        )

        mapping_page = self.client.get(started.headers["location"])
        self.assertIn("Revising Recipe v1", mapping_page.text)
        self.assertIn(f'href="{revise_url}">Review Recipe changes', mapping_page.text)
        self.assertIn("Check for new or changed Odoo fields", mapping_page.text)
        note = FieldMetadata(
            "x_migration_note", "char", label="Migration note", stored=True
        )
        expanded = replace(
            snapshot,
            models={
                model.model: replace(model, fields={**fields, note.name: note}),
            },
        )
        with patch(
            "impodo.web.routers.schema._read_selected_schema_sync",
            return_value=(
                expanded,
                schema.read_credential_binding_hash,
                read_identity,
                None,
            ),
        ):
            checked = self._post(
                f"/workspaces/{workspace_id}/schema/capture",
                {
                    "csrf_token": self.csrf,
                    "return_to_mapping": "1",
                },
            )
        self.assertEqual(checked.status_code, 303, checked.text)
        review = self.client.get(checked.headers["location"])
        self.assertIn("Apply new optional fields", review.text)
        self.assertIn("Migration note", review.text)
        pending = context.queries.get_odoo_schema_catalog(workspace_id).pending_refresh
        self.assertIsNotNone(pending)
        working = context.queries.get_mapping_working_draft(workspace_id)
        applied = self._post(
            f"{mapping_path}/odoo-fields/apply",
            {
                "csrf_token": self.csrf,
                "candidate_id": pending.candidate_id,
                "working_version": str(working.version),
            },
        )
        self.assertEqual(applied.status_code, 303, applied.text)
        self.assertIn(
            "kept your field matches and matching rules",
            self.client.get(applied.headers["location"]).text,
        )
        field_page = self.client.get(f"{mapping_path}?field_query={note.name}")
        self.assertIn(f'data-target-field="{note.name}"', field_page.text)
        self._confirm_mapping(mapping_path, form, note.name, note_column)

        prepare = self.client.get(f"/workspaces/{workspace_id}/prepare")
        self.assertIn(f'href="{revise_url}">Review Recipe changes', prepare.text)
        preview = self.client.get(revise_url)
        self._assert_change_summary(preview.text)
        successor_form = self._publication_review_form(preview.text)
        if extra_checks:
            overview = self.client.get(f"/projects/{project.project.project_id}")
            self.assertIn(f'href="{revise_url}">Review Recipe changes', overview.text)
            wrong_digest = self._post(
                revise_url, {**successor_form, "draft_hash": "sha256:" + "0" * 64}
            )
            self.assertEqual(wrong_digest.status_code, 422)
            self.assertIn("The Recipe draft changed", wrong_digest.text)
            before = context.queries.get_mapping_working_draft(workspace_id)
            changed = replace(
                before.definition.datasets[0],
                fields=tuple(
                    replace(field, compare=False)
                    if field.target_field == note.name
                    else field
                    for field in before.definition.datasets[0].fields
                ),
            )
            draft = context.mapping_workspace.save_working_draft(
                workspace_id,
                datasets=(changed,),
                expected_version=before.version,
                actor=actor,
            )
            rejected = self._post(revise_url, successor_form)
            self.assertEqual(rejected.status_code, 422)
            self.assertIn("Authoring workspace or review changed", rejected.text)
            self.assertEqual(
                context.queries.get_mapping_working_draft(workspace_id), draft
            )
            self.assertNotIn("Save a new Recipe version", rejected.text)
            self._confirm_mapping(mapping_path, form, note.name, note_column)
            preview = self.client.get(revise_url)
            self._assert_change_summary(preview.text)
            successor_form = self._publication_review_form(preview.text)
        result = self._post(revise_url, successor_form)
        self.assertEqual(result.status_code, 303, result.text)
        self.assertEqual(result.headers["location"], recipe_url + "?version=2")
        replayed = self._post(revise_url, successor_form)
        self.assertEqual(replayed.status_code, 303, replayed.text)
        self.assertEqual(replayed.headers["location"], result.headers["location"])
        stale = self._post(revise_url, {**successor_form, "operation_id": str(uuid4())})
        self.assertEqual(stale.status_code, 422)
        self._assert_change_summary(self.client.get(result.headers["location"]).text)
        if extra_checks:
            read_revision = context.recipes.read_revision

            def baseline_unavailable(recipe_id, version, *, actor):
                if version == 1:
                    raise RecipeIntegrityError("Earlier test payload is unavailable")
                return read_revision(recipe_id, version, actor=actor)

            with patch.object(
                context.recipes, "read_revision", side_effect=baseline_unavailable
            ):
                saved_page = self.client.get(result.headers["location"])
                self.assertEqual(saved_page.status_code, 200, saved_page.text)
                self.assertIn("could not verify the earlier version", saved_page.text)
                self.assertIn("What this Recipe will reuse", saved_page.text)
        archived_review = self.client.get(revise_url)
        self.assertIn("Starting from Recipe v1", archived_review.text)
        self.assertIn("Create Authoring draft", archived_review.text)
        self.assertNotIn("Open Match data", archived_review.text)
        with (
            collect_duckdb_request_timings() as rejected_timing,
            count_duckdb_statements() as rejected_sql,
        ):
            rejected_start = self._post(
                revise_url, {"csrf_token": self.csrf, "action": "start"}
            )
        self.assertEqual(rejected_start.status_code, 422)
        self.assertLessEqual(rejected_timing.connection_count, 4, rejected_timing)
        self.assertLessEqual(rejected_timing.schema_check_count, 3, rejected_timing)
        self.assertLessEqual(len(rejected_sql), 115)
        if earlier_application is not None:
            self.assertEqual(
                context.migration_workspaces.get(
                    newer_workspace.workspace_id, actor=actor
                ),
                newer_workspace,
            )
            old_run = self.client.get(
                f"/projects/{project.project.project_id}/runs/{earlier_application.migration_run_id}"
            )
            self.assertEqual(old_run.status_code, 200, old_run.text)
            self.assertIn(f'href="{revise_url}">Revise this Recipe', old_run.text)
            self.assertNotIn(f'href="{recipe_url}/revise?version=2"', old_run.text)
        selection = (original.recipe.recipe_id, 2)
        revised = context.recipes.read_revisions(
            project.project.project_id, (selection,), actor=actor
        )[selection]
        self.assertEqual(revised.revision.version, 2)
        historical = context.recipes.read_revision(
            original.recipe.recipe_id, 1, actor=actor
        )
        self.assertEqual(historical, original.envelope)
        self.assertEqual(
            sorted(
                item.version
                for item in context.recipes.revisions(
                    revised.recipe.recipe_id, actor=actor
                )
            ),
            [1, 2],
        )
        self.assertEqual(context.queries.get_source_selection(workspace_id), source)
        self._assert_added_field(original.envelope, revised.envelope, note.name)
        refreshed = context.queries.get_odoo_schema_catalog(workspace_id)
        application = self._apply_recipe(revised, refreshed, mode)
        prepare_test_application(self, context, application, Path(self.temporary.name))
        _, _, records = execute_test_application(
            self,
            context,
            application,
            target_records={
                ("res.partner", 1): {
                    "ref": "C002",
                    "name": "Beta",
                    note.name: "Old note",
                },
            },
            expected_creates=1,
            expected_updates=1,
            write_mode="upsert",
        )
        self.assertEqual(records[("res.partner", 1)][note.name], "Updated note")
        self.assertEqual(
            {row["ref"]: (row["name"], row[note.name]) for row in records.values()},
            {"C002": ("Beta", "Updated note"), "C003": ("Gamma", "New note")},
        )
        if mode is OdooConnectionMode.LOCAL:
            authoring = context.migration_workspaces.get(workspace_id, actor=actor)
            context.migration_workspaces.close(
                workspace_id,
                actor=actor,
                expected_revision=authoring.optimistic_revision,
            )
            closed = self.client.get(f"{recipe_url}/revise?version=2")
            self.assertIn("Create Authoring draft", closed.text)
            self.assertNotIn("Open Match data", closed.text)
        self._exercise_new_draft(
            revise_url, project, original, expanded, mode, extra_checks
        )

    def _exercise_dependencies(self, project, snapshot, mode, dataset, actor, *, with_advanced=False):
        context = self.app.state.context
        compiler = context.recipe_authoring_drafts.compiler
        origin_id = project.workspace.workspace_id
        parameters = RecipeParameterDefinitions((
            RecipeParameterDefinition("batch", "Import batch", RecipeParameterType.INTEGER, False),
        ))
        compiler.parameters.save_parameter_definitions(origin_id, parameters, actor=actor)
        entry = ReferenceEntry(("C001",), {"name": "Acme"})
        reference = ReferenceDataSet(
            str(uuid4()), 1, "Customer names", ("code",),
            {"name": ReferenceValueKind.DISPLAY_VALUE}, (entry,),
            "Data manager", "Internal", "Original delivery",
        )
        bundle = ReferenceBundle(origin_id, (reference,))
        compiler.references.save_reference_bundle(origin_id, bundle, actor=actor)
        schema_workspace = context.schema_workspace
        packet = schema_workspace.supporting_review_inputs(origin_id, actor=actor)
        choice = SupportingChoice("res.partner", "category_ids", SupportingChoiceAction.SKIP_OPTIONAL)
        schema_workspace.supporting.save(
            packet, expected_binding_hash=packet.binding_hash, expected_version=0,
            operation_id=str(uuid4()), choices=(choice,), actor=actor,
        )
        ref_column, name_column, _ = (column.stable_key for column in dataset.columns)
        name = ScalarFieldMapping(
            target_field="name", source_column_key=ref_column,
            reference_lookup=ReferenceLookupMapping(
                reference.reference_id, reference.content_hash, (ref_column,), "name",
            ),
        )
        mapping = DatasetMapping(
            dataset.dataset_id, "res.partner",
            source_identity_column_keys=(ref_column,),
            target_identity=(IdentityComponentMapping((ref_column,), ("ref",)),),
            fields=(name, ScalarFieldMapping(target_field="comment", source_column_key=name_column)),
        )
        self._confirm_current_datasets(origin_id, (mapping,), actor)
        rule = manager_quality_rule(
            workspace_id=origin_id, dataset=dataset.name, family=QualityRuleFamily.EQUALITY,
            name="Review customer names", input_fields=("name", "comment"),
            outcome=QualityOutcomePolicy.WARNING,
        )
        ruleset = context.quality.save_manager_rules(
            context.quality.configuration(origin_id, dataset.dataset_id), (rule,), actor=actor,
        )
        if with_advanced:
            scope, advanced = self._approve_origin_coverage(origin_id, dataset, reference, bundle, ruleset, actor)
        original = self._publish_original(project)
        if with_advanced:
            changed_scope = replace(
                scope, version=2, parent_version=1,
                declarations=tuple(replace(item, rationale="Changed after publication") for item in scope.declarations),
            )
            compiler.references.save_coverage_scope(origin_id, changed_scope, expected_parent_version=1, actor=actor)
        compiler.references.save_reference_bundle(
            origin_id,
            replace(bundle, datasets=(replace(
                reference, version=2, entries=(replace(entry, values={"name": "Updated"}),),
            ),)),
            actor=actor,
        )
        compiler.parameters.save_parameter_definitions(origin_id, RecipeParameterDefinitions(), actor=actor)
        if mode is OdooConnectionMode.LOCAL:
            workspace = context.migration_workspaces.get(origin_id, actor=actor)
            context.migration_workspaces.close(
                origin_id, expected_revision=workspace.optimistic_revision, actor=actor,
            )
        revise_url = (
            f"/projects/{project.project.project_id}/recipes/{original.recipe.recipe_id}/revise?version=1"
        )
        workspace_id = self._exercise_new_draft(
            revise_url, project, original, snapshot, mode, False, with_dependencies=True,
            interrupt_seed=snapshot.fingerprint.odoo_version == "19.0" and mode is OdooConnectionMode.LOCAL,
            with_advanced=with_advanced,
        )
        self.assertEqual(compiler.parameters.get_parameter_definitions(workspace_id), parameters)
        self.assertEqual(compiler.references.get_reference_bundle(workspace_id), replace(bundle, workspace_id=workspace_id))
        review = schema_workspace.supporting.current(workspace_id, actor=actor)
        self.assertEqual(review.choices, (choice,))
        self.assertEqual(review.binding_hash, schema_workspace.supporting_review_inputs(
            workspace_id, actor=actor,
        ).binding_hash)
        working = context.queries.get_mapping_working_draft(workspace_id)
        lookup = next(
            item.reference_lookup for item in working.definition.datasets[0].fields
            if item.target_field == "name"
        )
        self.assertEqual(lookup.reference_content_hash, reference.content_hash)
        revision = self._confirm_current_datasets(workspace_id, working.definition.datasets, actor)
        if with_advanced:
            self.assertIsNone(context.quality.current_ruleset(workspace_id))
            planned, _ = context.quality.preparation_inputs(
                workspace_id, revision.definition, context.queries.get_source_selection(workspace_id),
            )
            self.assertIn(advanced.name, {item.name for item in planned.rules})
            context.preparation.prepare(workspace_id, actor=actor)
            quality = context.quality.current_run(workspace_id)
            self.assertIn("ADVANCED_CHECK_FAILED", {item.reason_code for item in quality.issues})
        self.assertIn("Review customer names", self.client.get(
            f"/workspaces/{workspace_id}/mapping?step=check",
        ).text)
        selection = context.queries.get_source_selection(workspace_id)
        restored = context.quality.editable_ruleset(workspace_id, revision.definition, selection)
        self.assertEqual(
            replace(restored.manager_rules[0], rule_id=rule.rule_id), rule,
        )
        configuration = context.quality.configuration(workspace_id, selection.datasets[0].dataset_id)
        changed = replace(restored.manager_rules[0], name="Revised business check")
        restored_ruleset = context.quality.save_manager_rules(configuration, (changed,), actor=actor)
        self.assertEqual(restored_ruleset.manager_rules, (changed,))
        if with_advanced:
            new_scope = compiler.references.get_coverage_scope(workspace_id)
            self.assertEqual(new_scope.declarations, scope.declarations)
            self.assertNotEqual(new_scope.scope_id, scope.scope_id)
            self.assertEqual(new_scope.approved_by, actor.identity)
            self.assertGreater(new_scope.approved_at, scope.approved_at)
            self.assertEqual(new_scope.source_selection_hash, selection.content_hash)
            self.assertEqual(restored_ruleset.coverage_scope_hash, new_scope.content_hash)
            self.assertEqual(restored_ruleset.reference_bundle_hash, replace(bundle, workspace_id=workspace_id).content_hash)
            restored_check = next(item for item in restored_ruleset.rules if item.name == advanced.name)
            self.assertEqual(replace(restored_check, rule_id=advanced.rule_id), advanced)
            self.assertEqual(compiler.references.get_coverage_scope(origin_id), changed_scope)
        self.assertEqual(context.quality.current_ruleset(origin_id).manager_rules, (rule,))
        published = self.client.get(revise_url)
        self.assertIn("Changes from Recipe v1", published.text)
        saved = self._post(revise_url, self._publication_review_form(published.text))
        self.assertEqual(saved.status_code, 303, saved.text)
        self.assertTrue(saved.headers["location"].endswith("?version=2"))
        if with_advanced and mode is OdooConnectionMode.LOCAL:
            compiler.references.save_coverage_scope(
                workspace_id, replace(new_scope, version=2, parent_version=1),
                expected_parent_version=1, actor=actor,
            )
            with self.assertRaisesRegex(WorkspaceError, "approved coverage scope"):
                context.preparation.prepare(workspace_id, actor=actor)

    def _approve_origin_coverage(self, workspace_id, dataset, reference, bundle, ruleset, actor):
        context = self.app.state.context
        selection = context.queries.get_source_selection(workspace_id)
        declarations = tuple(
            CoverageDeclaration(
                family,
                CoverageApplicability.APPLICABLE if family is CoverageFamily.TC_14 else CoverageApplicability.INAPPLICABLE,
                "Use the approved customer codes" if family is CoverageFamily.TC_14 else "Outside this import",
                (dataset.name,) if family is CoverageFamily.TC_14 else (),
            )
            for family in CoverageFamily
        )
        scope = CoverageScopeRevision(
            str(uuid4()), workspace_id, 1, None, selection.content_hash,
            declarations, actor.identity, datetime.now(UTC),
        )
        compiler = context.recipe_authoring_drafts.compiler
        compiler.references.save_coverage_scope(workspace_id, scope, expected_parent_version=None, actor=actor)
        advanced = QualityRule(
            rule_id=content_hash("Approved customer codes"), dataset=dataset.name,
            family=QualityRuleFamily.APPROVED_CODE_LIST, name="Approved customer codes",
            explanation="Check customer values against the approved list.",
            input_fields=("name",),
            parameters={"reference_id": reference.reference_id, "reference_content_hash": reference.content_hash},
            outcome=QualityOutcomePolicy.WARNING, owner_role=QualityOwnerRole.DATA_MANAGER,
            source=QualityRuleSource.SCOPE_APPROVED,
        )
        context.quality.publish_ruleset(workspace_id, replace(
            ruleset, version=ruleset.version + 1, parent_version=ruleset.version,
            rules=tuple(sorted((*ruleset.rules, advanced), key=lambda item: item.rule_id)),
            coverage_scope_hash=scope.content_hash, reference_bundle_hash=bundle.content_hash,
        ), actor=actor)
        return scope, advanced

    def _confirm_current_datasets(self, workspace_id, datasets, actor):
        context = self.app.state.context
        current = context.queries.get_mapping_revision(workspace_id)
        working = context.queries.get_mapping_working_draft(workspace_id)
        revision, validation = context.mapping_workspace.check_definition(
            workspace_id, datasets=datasets, expected_parent_version=current.version if current else None,
            expected_working_draft_version=working.version if working else None, actor=actor,
        )
        working = context.queries.get_mapping_working_draft(workspace_id)
        context.mapping_workspace.submit_current(
            workspace_id, datasets=revision.definition.datasets, expected_version=revision.version,
            expected_working_draft_version=working.version, actor=actor,
            warning_acknowledgements=tuple(mapping_issue_fingerprint(item) for item in validation.issues if item.severity == "warning"),
        )
        return revision

    def _exercise_new_draft(
        self, revise_url, project, original, snapshot, mode, extra_checks, *,
        with_dependencies=False, interrupt_seed=False, with_advanced=False,
    ):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        project_id = project.project.project_id
        origin_id = project.workspace.workspace_id
        original_mapping = context.queries.get_mapping_working_draft(origin_id)
        original_target = context.migration_run_target_setup.get(
            project.run.migration_run_id, actor=actor
        )
        workspace_count = len(
            context.migration_workspaces.list_for_project(project_id, actor=actor)
        )
        form = {"csrf_token": self.csrf, "action": "create_draft"}
        if extra_checks:
            with patch.object(
                context.recipe_authoring_drafts.projections,
                "materialize",
                side_effect=RecipeError("Interrupted source projection"),
            ):
                self.assertEqual(self._post(revise_url, form).status_code, 422)
        with (
            collect_duckdb_request_timings() as timing,
            count_duckdb_statements() as statements,
        ):
            created = self._post(revise_url, form)
        self.assertEqual(created.status_code, 303, created.text)
        # Registry plus source and the workspaces consulted during provisioning.
        self.assertLessEqual(timing.connection_count, 6, timing)
        self.assertLessEqual(timing.schema_check_count, 5, timing)
        # Checking a diverged origin also compiles its current dependency declarations.
        self.assertLessEqual(len(statements), 395 if with_advanced else 380 if with_dependencies else 365)
        target_url = created.headers["location"]
        self.assertTrue(target_url.endswith("/target"), target_url)
        workspace_id = target_url.split("/")[2]
        self.assertNotEqual(workspace_id, origin_id)
        with (
            collect_duckdb_request_timings() as replay_timing,
            count_duckdb_statements() as replay_sql,
        ):
            replayed = self._post(revise_url, form)
        self.assertEqual(replayed.headers["location"], target_url)
        self.assertLessEqual(replay_timing.connection_count, 1)
        self.assertEqual(replay_timing.schema_check_count, 0)
        self.assertLessEqual(len(replay_sql), 16)
        self.assertEqual(
            len(context.migration_workspaces.list_for_project(project_id, actor=actor)),
            workspace_count + 1,
        )
        draft = context.migration_workspaces.get(workspace_id, actor=actor)
        self.assertEqual(draft.data_version_id, project.data_version.data_version_id)
        self.assertIsNone(draft.recipe_application_id)
        self.assertNotEqual(draft.migration_run_id, project.run.migration_run_id)
        self.assertIs(
            context.migration_runs.get(draft.migration_run_id, actor=actor).purpose,
            MigrationRunPurpose.AUTHORING,
        )
        self.assertEqual(
            self.client.get(target_url, follow_redirects=False).status_code, 200
        )
        self.assertIsNone(context.queries.get_odoo_schema_catalog(workspace_id))
        self.assertIsNone(context.queries.get_mapping_working_draft(workspace_id))
        state = context.queries.get(workspace_id)
        self.assertIsNone(
            get_target_credential(
                context.secret_store, state, TargetCredentialRole.READ
            )
        )

        # Use the normal metadata capture route with controlled Odoo evidence.
        credential = None
        read_identity = None
        binding = local_read_credential_binding_hash(state)
        if mode is OdooConnectionMode.REMOTE:
            credential = store_target_credential(
                context.secret_store,
                state,
                TargetCredentialRole.READ,
                "new-draft-read",
                persistent=False,
            )
            binding = credential.binding_hash
            read_identity = context.read_identity_probe(
                state, credential.secret, state.intended_models
            )
        with patch(
            "impodo.web.routers.schema._read_selected_schema_sync",
            return_value=(snapshot, binding, read_identity, None),
        ):
            checked = self._post(
                f"/workspaces/{workspace_id}/schema/capture", {"csrf_token": self.csrf}
            )
        self.assertEqual(checked.status_code, 303, checked.text)
        checked_page = self.client.get(checked.headers["location"])
        seed_form = {"csrf_token": self.csrf, "action": "start"}
        if with_advanced:
            self.assertIn(f'href="{revise_url}"', checked_page.text)
            self.assertIn("Review advanced checks", checked_page.text)
            self.assertNotIn("Approve coverage and open Match data", checked_page.text)
            references = context.recipe_authoring_drafts.compiler.references
            self.assertIsNone(references.get_coverage_scope(workspace_id))
            self.assertEqual(self._post(revise_url, seed_form).status_code, 422)
            review = self.client.get(revise_url)
            self.assertIn("Approved customer codes", review.text)
            self.assertIn("Use the approved customer codes", review.text)
            self.assertNotIn("Changed after publication", review.text)
            token = search(r'name="coverage_review_hash" value="([^"]+)"', review.text).group(1)
            seed_form.update(action="approve_coverage", coverage_review_hash=token)
            stale = self._post(revise_url, {**seed_form, "coverage_review_hash": content_hash("stale")})
            self.assertEqual(stale.status_code, 422)
            self.assertIsNone(references.get_coverage_scope(workspace_id))
        else:
            self.assertIn(f'action="{revise_url}"', checked_page.text)
            self.assertIn("Open Match data", checked_page.text)
        if interrupt_seed:
            with patch.object(
                context.mapping_workspace, "save_working_draft",
                side_effect=RecipeError("Interrupted draft seed"),
            ):
                interrupted = self._post(revise_url, seed_form)
            self.assertEqual(interrupted.status_code, 422, interrupted.text)
            self.assertIsNone(context.queries.get_mapping_working_draft(workspace_id))
        with (
            collect_duckdb_request_timings() as seed_timing,
            count_duckdb_statements() as seed_sql,
        ):
            started = self._post(revise_url, seed_form)
        self.assertEqual(started.status_code, 303, started.text)
        self.assertIn(
            f"/workspaces/{workspace_id}/mapping", started.headers["location"]
        )
        # Reference history adds the origin's foundation and engine stores.
        self.assertLessEqual(seed_timing.connection_count, 6 if with_dependencies else 4, seed_timing)
        self.assertLessEqual(seed_timing.schema_check_count, 5 if with_dependencies else 3, seed_timing)
        # A fresh approval adds one scope read, its audit write, and source validation.
        self.assertLessEqual(len(seed_sql), 330 if with_advanced else 295 if with_dependencies else 200)
        self.assertEqual(seed_timing.lock_retry_count, 0)
        saved = context.queries.get_mapping_working_draft(workspace_id)
        self.assertEqual(
            [field.target_field for field in saved.definition.datasets[0].fields],
            ["comment", "name"] if with_dependencies else ["name"],
        )
        self.assertNotEqual(saved.mapping_id, original_mapping.mapping_id)
        self.assertIsNone(context.queries.get_mapping_revision(workspace_id))
        self.assertIn(
            "Revising Recipe v1", self.client.get(started.headers["location"]).text
        )
        dataset_mapping = saved.definition.datasets[0]
        edited = context.mapping_workspace.save_working_draft(
            workspace_id,
            datasets=(
                replace(
                    dataset_mapping,
                    fields=(replace(dataset_mapping.fields[0], compare=False), *dataset_mapping.fields[1:]),
                ),
            ),
            expected_version=saved.version,
            actor=actor,
        )
        repeated = self._post(revise_url, {"csrf_token": self.csrf, "action": "start"})
        self.assertEqual(repeated.headers["location"], started.headers["location"])
        self.assertEqual(
            context.queries.get_mapping_working_draft(workspace_id), edited
        )
        if extra_checks:
            source = context.queries.get_source_selection(workspace_id).datasets[0]
            ref, _, note = (column.stable_key for column in source.columns)
            governance = context.queries.get_schema_governance(workspace_id)
            self._confirm_mapping(
                f"/workspaces/{workspace_id}/mapping",
                {
                    "csrf_token": self.csrf,
                    "editable_dataset_id": source.dataset_id,
                    "target_model_0": "res.partner",
                    "mode_0": "upsert",
                    "source_identity_0": ref,
                    "business_key_0": governance.business_keys[0].key_id,
                    "identity_source_0_0": ref,
                },
                "name",
                note,
            )
            page = self.client.get(revise_url)
            self.assertIn("Changes from Recipe v1", page.text)
            self.assertIn("Changes to the latest version, Recipe v2", page.text)
            response = self._post(revise_url, self._publication_review_form(page.text))
            self.assertEqual(response.status_code, 303, response.text)
            self.assertTrue(response.headers["location"].endswith("?version=3"))
            latest = context.recipes.read_revision(
                original.recipe.recipe_id, 3, actor=actor
            )
            self.assertEqual(latest["provenance"]["origin_workspace_id"], workspace_id)
            self.assertEqual(latest["provenance"]["revision_base_version"], 1)
        self.assertEqual(
            context.queries.get_mapping_working_draft(origin_id), original_mapping
        )
        self.assertEqual(
            context.migration_run_target_setup.get(
                project.run.migration_run_id, actor=actor
            ),
            original_target,
        )
        self.assertEqual(
            context.recipes.read_revision(original.recipe.recipe_id, 1, actor=actor),
            original.envelope,
        )
        return workspace_id

    def _apply_recipe(self, revised, schema, mode):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        project_id = revised.recipe.project_id
        recipe_selection = (revised.recipe.recipe_id, revised.revision.version)
        setup = context.test_runs.start_setup(
            project_id,
            expected_workspace_revision=context.migration_projects.get(
                project_id, actor=actor
            ).optimistic_revision,
            recipe_revisions=(recipe_selection,),
            dependencies=(),
            label="Test the customer note",
            export_as_of="2026-11-08",
            operation_id=str(uuid4()),
            actor=actor,
        )
        run_id = setup.run.migration_run_id
        url = f"/projects/{project_id}/test-runs/{run_id}/fresh-data"
        page = self.client.get(url)
        revision = search(r'name="revision" value="([^"]+)"', page.text)
        self.assertIsNotNone(revision)
        uploaded = self.client.post(
            f"{url}/files",
            data={"csrf_token": self.csrf, "revision": revision.group(1)},
            files={
                "source_file": (
                    "customers.csv",
                    b"Reference,Customer name,Migration note\nC002,Beta,Updated note\nC003,Gamma,New note\n",
                    "text/csv",
                )
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        page = self.client.get(url)
        revision = search(r'name="revision" value="([^"]+)"', page.text)
        self.assertIsNotNone(revision)
        registered = self._post(
            f"{url}/register",
            {
                "csrf_token": self.csrf,
                "revision": revision.group(1),
            },
        )
        self.assertEqual(registered.status_code, 303, registered.text)
        accepted = self._post(
            f"{url}/accept",
            {
                "csrf_token": self.csrf,
                "parameter_revision": "",
                "warnings_acknowledged": "1",
            },
        )
        self.assertEqual(accepted.status_code, 303, accepted.text)

        workspace_id = setup.setup_workspace.workspace_id
        state = context.queries.get(workspace_id)
        state = context.workspace_states.update_target(
            workspace_id,
            actor=actor,
            expected_revision=state.revision,
            odoo_connection_mode=mode.value,
            odoo_base_url="http://127.0.0.1:8070"
            if mode is OdooConnectionMode.LOCAL
            else "https://fictional-test.example.test",
            odoo_database="customer_test",
            intended_applications=(),
            intended_models=("res.partner",),
        )
        credential = store_target_credential(
            context.secret_store,
            state,
            TargetCredentialRole.READ,
            "fictional-test-read",
            persistent=False,
        )
        identity = context.read_identity_probe(
            state, credential.secret, ("res.partner",)
        )
        metadata = metadata_for_schema(schema)
        metadata = replace(
            metadata,
            fingerprint=replace(
                metadata.fingerprint,
                target_hash=identity.target_hash,
                database=state.odoo_database,
                snapshot_timestamp=datetime.now(UTC).isoformat(),
            ),
        )
        captured = context.schema_workspace.capture(
            workspace_id,
            metadata,
            read_credential_binding_hash=credential.binding_hash,
            read_identity=identity,
            actor=actor,
        )
        activated = context.test_runs.activate(
            project_id,
            run_id,
            expected_workspace_revision=context.migration_projects.get(
                project_id, actor=actor
            ).optimistic_revision,
            target_schema=captured,
            target_reference_bundle=None,
            credential_generation=captured.read_credential_binding_hash,
            operation_id=str(uuid4()),
            actor=actor,
        )
        application = activated.applications[0]
        self.assertEqual(
            (application.recipe_id, application.recipe_revision), recipe_selection
        )
        self.assertEqual(
            application.status,
            RecipeApplicationStatus.READY,
            context.run_planning.repository.list_issues(application.application_id),
        )
        return application

    def _confirm_mapping(self, path, form, target, column):
        context = self.app.state.context
        workspace_id = path.split("/")[2]
        schema = context.queries.get_odoo_schema_catalog(workspace_id)
        index = next(
            i for i, field in enumerate(schema.models[0].fields) if field.name == target
        )
        current = context.queries.get_mapping_revision(workspace_id)
        working = context.queries.get_mapping_working_draft(workspace_id)
        fields = {
            **form,
            "visible_scalar_target_0": target,
            f"scalar_value_source_0_{index}": "source",
            f"scalar_source_0_{index}": column,
            f"scalar_compare_0_{index}": "1",
        }
        saved = self._post(
            f"{path}/save?step=check&field_query={target}",
            {
                **fields,
                "action": "draft",
                "expected_parent_version": str(current.version) if current else "",
                "expected_working_draft_version": str(working.version)
                if working
                else "",
            },
        )
        self.assertEqual(saved.status_code, 303, saved.text)
        revision = context.queries.get_mapping_revision(workspace_id)
        working = context.queries.get_mapping_working_draft(workspace_id)
        confirmed = self._post(
            f"{path}/save?step=confirm",
            {
                **fields,
                "action": "submit",
                "expected_parent_version": str(revision.version),
                "expected_working_draft_version": str(working.version),
            },
        )
        self.assertEqual(confirmed.status_code, 303, confirmed.text)
        self.assertEqual(
            confirmed.headers["location"], f"/workspaces/{workspace_id}/prepare"
        )
        saved_checks = self._post(
            f"{path}/quality",
            {
                "csrf_token": self.csrf,
                "quality_dataset_id": form["editable_dataset_id"],
            },
        )
        self.assertEqual(saved_checks.status_code, 303, saved_checks.text)

    def _publish_original(self, project):
        context = self.app.state.context
        actor = resolve_local_actor(context)
        project_url = f"/projects/{project.project.project_id}"
        overview = self.client.get(project_url)
        self.assertIn("Ready to save", overview.text)
        operation = search(r'name="operation_id" value="([^"]+)"', overview.text)
        self.assertIsNotNone(operation)
        form = {
            "csrf_token": self.csrf,
            "operation_id": operation.group(1),
            "data_version_id": project.data_version.data_version_id,
            "workspace_id": project.workspace.workspace_id,
            "recipe_id": "",
            "expected_recipe_revision": "",
            "display_name": "Customer import",
            "business_purpose": "Reuse reviewed customer rules",
        }
        path = f"{project_url}/recipes"
        result = self._post(path, form)
        self.assertEqual(result.status_code, 303, result.text)
        recipe_id = result.headers["location"].split("#recipe-")[1]
        selection = (recipe_id, 1)
        return context.recipes.read_revisions(
            project.project.project_id, (selection,), actor=actor
        )[selection]

    def _publication_review_form(self, html):
        form = {"csrf_token": self.csrf, "action": "publish"}
        for name in (
            "review_id",
            "operation_id",
            "expected_recipe_revision",
            "draft_hash",
            "working_hash",
        ):
            field = search(rf'name="{name}" value="([^"]*)"', html)
            self.assertIsNotNone(field, name)
            form[name] = field.group(1)
        return form

    def _assert_change_summary(self, html):
        for wording in (
            "Changes from Recipe v1",
            "Added 1 mapped Odoo field",
            "res.partner.x_migration_note",
            "Uses 1 additional source column",
            "Migration note",
            "Use one source column",
            "Use on create and eligible updates",
            "Send a blank prepared value to Odoo",
        ):
            self.assertIn(wording, html)

    def _assert_added_field(self, original, revised, field_name):
        old = original["recipe"]
        new = revised["recipe"]
        old_mapping = old["mapping"]["datasets"][0]
        new_mapping = new["mapping"]["datasets"][0]
        new_fields = new_mapping["fields"]
        self.assertEqual(
            [field["target_field"] for field in new_fields], ["name", field_name]
        )
        self.assertEqual({**new_mapping, "fields": new_fields[:-1]}, old_mapping)
        old_columns = old["source_shape"]["datasets"][0]["columns"]
        new_columns = new["source_shape"]["datasets"][0]["columns"]
        self.assertEqual(new_columns[:-1], old_columns)
        self.assertEqual(new_columns[-1]["source_name"], "Migration note")
        self.assertEqual(
            new_fields[-1]["provider"]["source_column_ids"],
            [new_columns[-1]["logical_column_id"]],
        )
