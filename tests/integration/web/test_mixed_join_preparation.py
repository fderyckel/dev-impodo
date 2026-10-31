"""Exercise multiple combined outputs through the real preparation worker."""

from asyncio import get_running_loop
from dataclasses import replace
import re
from datetime import UTC, datetime
from threading import get_ident
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.derived_entities import (
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
)
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
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.web.composition.page_reads import run_local_operation_sync
from impodo.web.composition.preparation_job_manager import PreparationCancelled
from tests.integration.web import test_exact_join_workflow as fixtures
from tests.integration.web import test_joined_recipe_preparation as recipe_fixtures
from tests.integration.web.test_fresh_data_controls import field as form_value
from tests.support.browser_scenarios import POST_HEADERS, _wait_for_preparation
from tests.support.identity import resolve_local_actor


def delivery(row_counts=(4753, 44387, 16159), *, offset=0):
    """Return 262 source columns, collisions, blanks, and three shared inputs."""

    result = []
    for name, headers, count, width in (
        (
            "cust02.csv",
            ("Partition", "Party", "AccountNum", "Company"),
            row_counts[0],
            88,
        ),
        ("cust01.csv", ("Partition", "RecId", "Name", "Email"), row_counts[1], 87),
        ("cust03.csv", ("Partition", "Party", "City"), row_counts[2], 87),
    ):
        lines = [
            ",".join((*headers, *(f"Empty_{i}" for i in range(width - len(headers)))))
        ]
        for index in range(1, count + 1):
            key = index + offset
            if name == "cust02.csv":
                values = ("partition", str(key), f"P{key}", "A")
            elif name == "cust01.csv":
                values = (
                    "partition",
                    str(key),
                    ("A", "B", "TO_DEFINE")[index % 3],
                    f"u{key}@example.test",
                )
            else:
                values = ("partition", str(key), f"S{key}")
            lines.append(",".join((*values, *("" for _ in range(width - len(values))))))
        result.append((name, ("\n".join(lines) + "\n").encode()))
    return result


class MixedJoinPreparationTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.ExactJoinBrowserTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.context = self.fixture.app.state.context
        self.actor = resolve_local_actor(self.context)

    def _setup(self, row_counts=(4753, 44387, 16159)):
        context, actor = self.context, self.actor
        workspace_id, physical, by_name = self.fixture._frozen_customer_workspace(
            source_files=delivery(row_counts)
        )
        self.assertEqual(sum(d.row_count for d in physical.datasets), sum(row_counts))
        self.assertEqual(sum(len(d.columns) for d in physical.datasets), 262)
        schema = self._schema(workspace_id)
        if getattr(self, "company_binding", False):
            self._confirm_company(workspace_id, schema, 105)
        primary, details, addresses = (
            by_name[name]
            for name in (
                "customer_accounts",
                "party_details",
                "customer_addresses",
            )
        )
        column = self.fixture._column

        def projection(dataset, name, output):
            return ExactJoinGroupProjection(
                dataset.dataset_id, column(dataset, name).stable_key, output
            )

        for index, (name, additions, projections) in enumerate(
            (
                (
                    "records_complete",
                    ((details, "RecId"), (addresses, "Party")),
                    (
                        projection(primary, "AccountNum", "ID"),
                        projection(details, "Name", "Choice"),
                    ),
                ),
                (
                    "secondary_complete",
                    ((addresses, "Party"),),
                    (
                        projection(addresses, "City", "ID"),
                        projection(primary, "AccountNum", "Parent"),
                    ),
                ),
            )
        ):
            request = ExactJoinGroupRequest(
                output_dataset_name=name,
                primary_dataset_id=primary.dataset_id,
                primary_key_column_keys=(column(primary, "Party").stable_key,),
                key_value_types=("integer",),
                additions=tuple(
                    ExactJoinGroupAddition(d.dataset_id, (column(d, key).stable_key,))
                    for d, key in additions
                ),
                projections=projections,
            )
            run_local_operation_sync(
                context.derived_entities.save_join_group,
                workspace_id,
                request,
                expected_parent_version=index or None,
                actor=actor,
            )
        self._map(workspace_id)
        return workspace_id, row_counts[0]

    def _schema(self, workspace_id):
        context, actor = self.context, self.actor
        schema = recipe_fixtures.JoinedRecipePreparationTests._schema(
            self, workspace_id
        )
        schema = replace(
            schema,
            models=(
                *schema.models,
                SchemaModel(
                    "x_secondary",
                    "Secondary",
                    (
                        SchemaField(
                            "ref", "Reference", "char", False, False, None, None, ()
                        ),
                        SchemaField(
                            "x_parent_id",
                            "Parent",
                            "many2one",
                            False,
                            False,
                            "x_record",
                            None,
                            (),
                        ),
                    ),
                ),
            ),
        )
        context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=actor
        )
        context.schema_workspace.govern(
            workspace_id,
            business_keys=tuple(
                BusinessKeyDefinition(
                    f"{model}:ref", model, ("ref",), status=BusinessKeyStatus.CONFIRMED
                )
                for model in ("x_record", "x_secondary", "x_list")
            ),
            actor=actor,
        )
        return schema

    def _confirm_company(self, workspace_id, schema, company_id):
        state = self.context.queries.get(workspace_id)
        company = OdooCompanySelection(
            schema.connection_target_hash, company_id, f"Fictional factory {company_id}"
        )
        self.context.workspace_states.select_odoo_company(
            workspace_id,
            actor=self.actor,
            expected_revision=state.revision,
            selection=company,
        )
        refreshed = replace(schema, read_context_hash=company.context_hash)
        self.context.schema_workspace.schemas.save_odoo_create_defaults(
            workspace_id,
            refreshed,
            expected_content_hash=schema.content_hash,
            expected_read_credential_binding_hash=schema.read_credential_binding_hash,
            expected_company_context_hash=company.context_hash,
            actor=self.actor,
        )
        return refreshed

    def _map(self, workspace_id):
        context, actor = self.context, self.actor
        effective = context.queries.get_mapping_source_selection(workspace_id)
        self.assertEqual(len(effective.datasets), 2)
        mappings = []
        for dataset in effective.datasets:
            keys = {c.source_name: c.stable_key for c in dataset.columns}
            primary_output = dataset.name == "records_complete"
            relation = RelationshipMapping(
                target_field="x_list_id" if primary_output else "x_parent_id",
                kind="many2one",
                source_column_keys=(keys["Choice" if primary_output else "Parent"],),
                categorical_policy=(
                    CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH
                    if primary_output
                    else CategoricalCoveragePolicy.EXACT_BUSINESS_KEY
                ),
                blank_source_values=("TO_DEFINE",) if primary_output else (),
                resolver=RelationshipResolver(
                    origin=ResolverOrigin.TARGET_CATALOG
                    if primary_output
                    else ResolverOrigin.DATASET,
                    model="x_list" if primary_output else None,
                    dataset_id=None
                    if primary_output
                    else next(
                        d.dataset_id
                        for d in effective.datasets
                        if d.name == "records_complete"
                    ),
                    key_mappings=(ReferenceKeyMapping(keys["Choice"], "ref"),)
                    if primary_output
                    else (),
                    value_mappings=(
                        ValueMapping("A", "Alpha"),
                        ValueMapping("B", "Beta"),
                    )
                    if primary_output
                    else (),
                ),
            )
            mappings.append(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="x_record" if primary_output else "x_secondary",
                    source_identity_column_keys=(keys["ID"],),
                    target_identity=(
                        IdentityComponentMapping((keys["ID"],), ("ref",), "string"),
                    ),
                    relationships=(relation,),
                )
            )
        revision, validation = run_local_operation_sync(
            context.mapping_workspace.check_definition,
            workspace_id,
            datasets=tuple(mappings),
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
                mapping_issue_fingerprint(i)
                for i in validation.issues
                if i.severity == "warning"
            ),
            actor=actor,
        )

    def _worker(self, workspace_id, expected_rows, physical_rows=65299):
        if getattr(self, "company_binding", False):
            state = self.context.queries.get(workspace_id)
            schema = self.context.queries.get_odoo_schema_catalog(workspace_id)
            self.assertIsNotNone(state.odoo_company)
            self.assertEqual(schema.read_context_hash, state.odoo_company.context_hash)
        # These traps cover in-process paths. The durable large-run manifest
        # independently disables materialized fallback inside spawned workers.
        page = self.fixture.client.get(f"/workspaces/{workspace_id}/summary")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn(f"{physical_rows:,} source rows", page.text)
        with patch(
            "impodo.application.workspace.preparation.preparation_service.stage_browser_mapping",
            side_effect=AssertionError("whole-run reconstruction"),
        ):
            started = self.fixture.client.post(
                f"/workspaces/{workspace_id}/summary/check",
                data={"csrf_token": self.fixture.csrf},
                headers=POST_HEADERS,
                follow_redirects=False,
            )
            self.assertEqual(started.status_code, 303, started.text)
            finished = _wait_for_preparation(
                self.fixture.client, started.headers["location"], timeout=120
            )
        self.assertEqual(finished["status"], "SUCCEEDED", finished)
        summary = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        run = self.context.preparation.staging.get_canonical_staging_run(
            workspace_id, summary.run_id, expected_content_hash=summary.content_hash
        )
        self.assertEqual(len(run.rows), expected_rows * 2)
        self.assertEqual(run.reconciliation.blocked_rows, 0, run.issues)
        quality = self.context.quality.current_summary(workspace_id)
        self.assertEqual(quality.blocked_count, 0, quality)
        self.assertEqual(quality.ready_count, expected_rows * 2, quality)
        ledger = self.context.quality.quality.get_quality_run(
            workspace_id, quality.run_id
        )
        self.assertEqual(len(ledger.source_accounting), physical_rows)
        self.assertTrue(
            any(len(item.canonical_row_ids) == 2 for item in ledger.source_accounting)
        )
        self.assertEqual(
            sum(
                row.references.get("x_list_id") is None
                for row in run.rows
                if row.dataset == "records_complete"
            ),
            (expected_rows + 1) // 3,
        )
        self.assertTrue(
            all(
                len(row.lineage.physical_sources)
                == (3 if row.dataset == "records_complete" else 2)
                for row in run.rows
            )
        )
        normalization = self.context.normalization.current_summary(workspace_id)
        approved = self.fixture.client.post(
            f"/workspaces/{workspace_id}/normalization/approve",
            data={
                "csrf_token": self.fixture.csrf,
                "run_id": normalization.run_id,
                "lifecycle_version": str(normalization.lifecycle_version),
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(approved.status_code, 303, approved.text)
        self.assertTrue(self.context.normalization.current_summary(workspace_id).frozen)
        review = self.fixture.client.get(f"/workspaces/{workspace_id}/summary")
        self.assertEqual(review.status_code, 200, review.text)
        self.assertIn("Final review", review.text)
        self.assertIn("Compare the approved data with Odoo", review.text)
        self.assertNotIn("Reduce the source size", review.text)
        return run

    def test_65299_rows_with_second_combined_output_and_recipe_reuse(self):
        self.company_binding = True
        workspace_id, count = self._setup()
        self._worker(workspace_id, count)
        self._reuse(workspace_id, count)

    def test_100000_rows_with_multiple_combined_outputs(self):
        workspace_id, count = self._setup((8000, 70000, 22000))
        self._worker(workspace_id, count, 100000)

    def test_start_page_reads_off_loop_and_failed_repeat_preserves_good_result(self):
        workspace_id, count = self._setup((3, 4, 4))
        caller = get_ident()
        reads = []
        original = self.context.queries.get_mapping_revision

        def read(*args, **kwargs):
            try:
                get_running_loop()
            except RuntimeError:
                pass
            else:
                raise AssertionError(
                    "Preparation page queried DuckDB on the event loop"
                )
            reads.append(get_ident())
            return original(*args, **kwargs)

        with (
            patch.object(
                self.context.queries, "get_mapping_revision", side_effect=read
            ),
            collect_duckdb_request_timings() as timings,
        ):
            page = self.fixture.client.get(f"/workspaces/{workspace_id}/prepare")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertTrue(reads)
        self.assertTrue(all(thread != caller for thread in reads))
        self.assertLessEqual(timings.connection_count, 8)
        self.assertLessEqual(timings.schema_check_count, 6)
        prepared = self.context.preparation.prepare(workspace_id, actor=self.actor)
        before = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        with patch.object(
            self.context.preparation.quality,
            "evaluate_and_publish",
            side_effect=OSError("injected storage exhaustion"),
        ):
            with self.assertRaises(OSError):
                self.context.preparation.prepare(workspace_id, actor=self.actor)
        self.assertEqual(
            self.context.preparation.staging.get_current_staging_summary(workspace_id),
            before,
        )
        self.assertEqual(
            self.context.normalization.current_summary(workspace_id).run_id,
            prepared.run_id,
        )
        cancelled = False

        def stop(completed, total):
            nonlocal cancelled
            if completed:
                cancelled = True
                raise PreparationCancelled("cancel after a bounded producer page")

        with self.assertRaises(PreparationCancelled):
            self.context.preparation.prepare(
                workspace_id,
                actor=self.actor,
                progress=lambda phase, completed, total, message: stop(
                    completed, total
                ),
            )
        self.assertTrue(cancelled)
        self.assertEqual(
            self.context.normalization.current_summary(workspace_id).run_id,
            prepared.run_id,
        )

    def _reuse(self, workspace_id, count, *, stable_lookup=False, delivery_factory=delivery):
        context, actor, client = self.context, self.actor, self.fixture.client
        owner = context.migration_workspaces.get(workspace_id, actor=actor)
        publication = run_local_operation_sync(
            context.recipe_publication.publish,
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            display_name="Generic combined records",
            business_purpose="Reuse aliases, blanks, and incoming links",
            actor=actor,
        )
        envelope = context.recipes.repository.read_recipe_revision(
            publication.recipe.recipe_id, publication.revision.version
        )
        self.assertEqual(len(envelope["recipe"]["mapping"]["datasets"]), 2)
        saved = next(
            d
            for d in envelope["recipe"]["mapping"]["datasets"]
            if d["target_model"] == "x_record"
        )
        self.assertEqual(
            next(
                relation
                for relation in saved["relationships"]
                if relation["target_field"] == "x_list_id"
            )["blank_source_values"],
            ["TO_DEFINE"],
        )
        if stable_lookup:
            relation = next(r for r in saved["relationships"] if r["target_field"] == "x_generated_id")
            self.assertNotIn("blank_source_action", relation)
            self.assertFalse(relation["required"])
            self.assertFalse(relation["required_on_create"])
        setup = context.test_runs.start_setup(
            owner.project_id,
            expected_workspace_revision=context.migration_projects.get(
                owner.project_id, actor=actor
            ).optimistic_revision,
            recipe_revisions=(
                (publication.recipe.recipe_id, publication.revision.version),
            ),
            dependencies=(),
            label="Fresh mixed delivery",
            export_as_of=datetime.now(UTC).date().isoformat(),
            operation_id=str(uuid4()),
            actor=actor,
        )
        url = f"/projects/{owner.project_id}/test-runs/{setup.run.migration_run_id}/fresh-data"
        for action in ("files", "register", "accept"):
            page = client.get(url)
            data = {"csrf_token": form_value(page, "csrf_token")}
            options = {}
            if action in {"files", "register"}:
                data["revision"] = form_value(page, "revision")
            if action == "files":
                options["files"] = [
                    ("source_file", (name, values, "text/csv"))
                    for name, values in delivery_factory(offset=100000)
                ]
            if action == "accept":
                data.update(parameter_revision="", warnings_acknowledged="1")
                if stable_lookup:
                    match = re.search(r'<select[^>]*name="(match_[0-9]+)"[^>]*>.*?<option value="([^"]+)"[^>]*>cust03\.csv', page.text, re.S)
                    self.assertIsNotNone(match, "The key-only address input needs an explicit table choice")
                    data[match.group(1)] = match.group(2)
            response = client.post(
                url + "/" + action,
                data=data,
                headers=POST_HEADERS,
                follow_redirects=False,
                **options,
            )
            self.assertEqual(response.status_code, 303, response.text)
        schema = self._schema(setup.setup_workspace.workspace_id)
        if getattr(self, "company_binding", False):
            self.assertIsNone(
                context.queries.get(setup.setup_workspace.workspace_id).odoo_company
            )
            schema = self._confirm_company(setup.setup_workspace.workspace_id, schema, 707)
        activated = run_local_operation_sync(
            context.test_runs.activate,
            owner.project_id,
            setup.run.migration_run_id,
            expected_workspace_revision=context.migration_projects.get(
                owner.project_id, actor=actor
            ).optimistic_revision,
            target_schema=schema,
            target_reference_bundle=None,
            credential_generation=schema.read_credential_binding_hash,
            operation_id=str(uuid4()),
            actor=actor,
        )
        application = activated.applications[0]
        self.assertEqual(
            application.status,
            RecipeApplicationStatus.READY,
            context.run_planning.repository.list_issues(application.application_id),
        )
        run = self._worker(application.workspace_id, count)
        self.assertTrue(
            all(
                str(row.target_identity[0])[1:].isdigit()
                and int(str(row.target_identity[0])[1:]) > 100000
                for row in run.rows
                if not stable_lookup or row.dataset == "records_complete"
            )
        )
