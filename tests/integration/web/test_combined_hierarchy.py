"""Carry hierarchy references from any joined input through the real workflow."""

from dataclasses import replace
from unittest.mock import patch

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
    IdentityNullPolicy,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.preparation.staging_contracts import CanonicalStagingRun
from impodo.domain.workspace.derived_entities import (
    derived_consumer_links,
    derived_dataset_links,
)
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web.test_fresh_data_controls import field
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.hierarchy_execution import compare_hierarchy
from tests.support.hierarchy_workflow import (
    PRODUCT_ROWS,
    HierarchyWorkflowFixture,
    product_csv,
)


class CombinedHierarchyTests(HierarchyWorkflowFixture):
    def combined(
        self, source_index=1, *, rows=PRODUCT_ROWS, all_blank_mode="emit_null_reference"
    ):
        files = {}
        for index in range(3):
            lines = (
                product_csv(rows if index == source_index else PRODUCT_ROWS)
                .decode()
                .splitlines()
            )
            files[f"input{index}"] = (
                lines[0]
                + f",Unused{index}\n"
                + "\n".join(line + f",value{index}" for line in lines[1:])
                + "\n"
            ).encode()
        self.combined_files = files
        workspace_id = self.freeze_files(files)
        self.schema(workspace_id)
        physical = self.context.queries.get_source_selection(workspace_id)
        inputs = tuple(sorted(physical.datasets, key=lambda dataset: dataset.name))
        columns = tuple(
            {column.source_name: column.stable_key for column in dataset.columns}
            for dataset in inputs
        )
        request = ExactJoinGroupRequest(
            output_dataset_name="products_combined",
            primary_dataset_id=inputs[0].dataset_id,
            primary_key_column_keys=(columns[0]["Reference"],),
            key_value_types=("string",),
            additions=tuple(
                ExactJoinGroupAddition(dataset.dataset_id, (keys["Reference"],))
                for dataset, keys in zip(inputs[1:], columns[1:], strict=True)
            ),
            # None of the original hierarchy levels need to be selected.
            projections=(
                ExactJoinGroupProjection(
                    inputs[0].dataset_id, columns[0]["Reference"], "Reference"
                ),
            ),
        )
        plan, _ = run_local_operation_sync(
            self.context.derived_entities.save_join_group,
            workspace_id,
            request,
            expected_parent_version=None,
            actor=self.actor,
        )
        plan, _ = run_local_operation_sync(
            self.context.derived_entities.save_hierarchy,
            workspace_id,
            output_dataset_name="categories",
            source_dataset_id=inputs[source_index].dataset_id,
            source_level_column_keys=tuple(
                columns[source_index][f"Level{i}"] for i in range(1, 5)
            ),
            target_model="product.category",
            target_name_field="name",
            external_id_namespace="fictional_workshop",
            missing_parent_mode="promote",
            missing_parent_value=None,
            missing_leaf="use_deepest",
            all_blank_mode=all_blank_mode,
            all_blank_value="Unclassified" if all_blank_mode == "fixed" else None,
            expected_parent_version=plan.version,
            actor=self.actor,
        )
        selection = self.context.queries.get_mapping_source_selection(workspace_id)
        combined = next(
            dataset
            for dataset in selection.datasets
            if dataset.name == "products_combined"
        )
        reference = next(
            column.stable_key
            for column in combined.columns
            if column.source_name == "Reference"
        )
        link = next(
            link
            for link in derived_consumer_links(plan)
            if link.consumer_dataset_id == combined.dataset_id
        )
        self.assertEqual(len(derived_dataset_links(plan)), 1)
        resolver = RelationshipResolver(
            origin=ResolverOrigin.DATASET, dataset_id=link.derived_dataset_id
        )
        revision = self.submit(
            workspace_id,
            (
                DatasetMapping(
                    combined.dataset_id,
                    "product.template",
                    source_identity_column_keys=(reference,),
                    target_identity=(
                        IdentityComponentMapping((reference,), ("default_code",)),
                    ),
                    fields=(ScalarFieldMapping("name", reference, required=True),),
                    relationships=(
                        RelationshipMapping(
                            "categ_id",
                            "many2one",
                            (link.source_column_key,),
                            resolver=resolver,
                            categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                        ),
                    ),
                ),
                DatasetMapping(
                    link.derived_dataset_id,
                    "product.category",
                    source_identity_column_keys=(link.canonical_key_column_key,),
                    target_identity=(
                        IdentityComponentMapping((link.name_column_key,), ("name",)),
                    ),
                    target_scope=(
                        IdentityComponentMapping(
                            (link.parent_key_column_key,),
                            ("parent_id",),
                            resolver=resolver,
                            null_policy=IdentityNullPolicy.EXPLICIT_SCOPE_NULL,
                        ),
                    ),
                ),
            ),
        )
        return workspace_id, revision, combined, link

    def test_primary_secondary_and_third_input_keep_full_paths_and_source_lineage(self):
        for index in range(3):
            with self.subTest(source_index=index):
                workspace_id, _, combined, link = self.combined(index)
                reader = self.context.categorical_coverage.logical_values
                selection = self.context.queries.get_mapping_source_selection(
                    workspace_id
                )
                self.assertTrue(
                    reader.supports(workspace_id, selection, combined.dataset_id)
                )
                from impodo.adapters.duckdb import grouped_exact_join_source as module

                reference = combined.columns[0].stable_key
                with patch.object(
                    module, "_create_narrow_input", wraps=module._create_narrow_input
                ) as scan:
                    values = reader.project(
                        workspace_id,
                        selection,
                        combined.dataset_id,
                        (link.source_column_key, reference),
                    )
                self.assertEqual(scan.call_count, 3)
                self.assertEqual(
                    sorted(
                        len(call.kwargs["needed_column_keys"])
                        for call in scan.call_args_list
                    ),
                    [1, 1, 5],
                )
                self.assertEqual(
                    values[reference].to_list(), [row[0] for row in PRODUCT_ROWS]
                )
                self.assertEqual(
                    values[link.source_column_key].to_list(),
                    [
                        "machines / pumps / service / seals",
                        "machines / valves / service / seals",
                        "maintenance / pumps / service / seals",
                        "machines / pumps",
                        "machines",
                        "tools",
                    ],
                )
                counts = reader.counts(
                    workspace_id,
                    selection,
                    combined.dataset_id,
                    link.source_column_key,
                    2,
                )
                self.assertEqual(
                    len(counts), 3
                )  # The choice reader preserves its overflow sentinel.
                staging, quality = self.prepare(workspace_id)
                self.assertEqual(
                    (
                        quality.ready_count,
                        quality.quarantined_count,
                        quality.blocked_count,
                    ),
                    (18, 0, 0),
                )
                products = [
                    row
                    for row in staging.rows
                    if row.target_model == "product.template"
                ]
                self.assertEqual(len(products), 6)
                historical = replace(staging, evaluator_version=9)
                self.assertEqual(
                    CanonicalStagingRun.from_json(historical.to_json()).content_hash,
                    historical.content_hash,
                )
                reconciliation = next(
                    item for item in staging.datasets if item.dataset == combined.name
                )
                self.assertEqual(
                    (reconciliation.input_rows, reconciliation.input_rows_used),
                    (18, 18),
                )
                self.assertTrue(
                    all(len(row.lineage.physical_sources) == 3 for row in products)
                )
                self.assertEqual(len({str(row.references) for row in products[:3]}), 3)

    def test_combined_reference_survives_compare_load_and_reconciliation(self):
        workspace_id, _, _combined, link = self.combined(2)
        with collect_duckdb_request_timings() as timings:
            page = self.client.get(
                f"/workspaces/{workspace_id}/mapping?mapping_dataset=1&scalar_page=1&relation_page=1"
            )
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('data-target-field="categ_id"', page.text)
        self.assertIn("Selected categories path", page.text)
        self.assertIn(link.source_column_key, page.text)
        # Measured authenticated fixture: eight connections across the page's
        # foundation/workspace owners and its narrow in-memory joined-value read.
        self.assertLessEqual(timings.connection_count, 8)
        self.assertLessEqual(timings.schema_check_count, 3)
        self.assertEqual(timings.lock_retry_count, 0)
        self.prepare(workspace_id)
        self.approve(workspace_id)
        self.expect_business_writes(workspace_id)
        target, snapshot, identities = compare_hierarchy(self, workspace_id)
        execution = self.context.execution.execute(
            workspace_id,
            expected_snapshot_hash=snapshot.semantic_hash,
            executor=target,
            actor=self.actor,
            **identities,
        )
        self.assertEqual(execution.status.value, "COMPLETED")
        paths = target.category_paths()
        products = {
            values["default_code"]: paths[values["categ_id"]]
            for (model, _), values in target.records.items()
            if model == "product.template"
        }
        self.assertEqual(products["PX101"], ("Machines", "Pumps", "Service", "Seals"))
        self.assertEqual(products["PX102"], ("Machines", "Valves", "Service", "Seals"))
        self.assertEqual(
            products["PX103"], ("Maintenance", "Pumps", "Service", "Seals")
        )
        reconciliation = self.context.reconciliation.reconcile(
            workspace_id,
            expected_execution_run_id=execution.run_id,
            reader=target,
            actor=self.actor,
            **{
                key: value
                for key, value in identities.items()
                if key.startswith("write_")
            },
        )
        self.assertEqual(reconciliation.status.value, "VERIFIED", reconciliation.rows)

    def test_unmatched_optional_input_stays_blank(self):
        workspace_id, _, _, _ = self.combined(1, rows=PRODUCT_ROWS[:-1])
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.blocked_count, 0)
        product = next(
            row
            for row in staging.rows
            if row.target_model == "product.template"
            and row.target_identity == ("PX106",)
        )
        self.assertTrue(
            all(value is None for value in product.references.values()),
            product.references,
        )

    def test_fixed_blank_policy_applies_only_to_matched_source_rows(self):
        rows = (*PRODUCT_ROWS[:4], ("PX105", "", "", "", ""))
        workspace_id, _, combined, link = self.combined(
            1, rows=rows, all_blank_mode="fixed"
        )
        reader = self.context.categorical_coverage.logical_values
        selection = self.context.queries.get_mapping_source_selection(workspace_id)
        values = reader.project(
            workspace_id, selection, combined.dataset_id, (link.source_column_key,)
        )
        self.assertEqual(values.to_series().to_list()[-2:], ["unclassified", None])
        staging, quality = self.prepare(workspace_id)
        self.assertEqual(quality.blocked_count, 0)
        products = {
            row.target_identity[0]: row
            for row in staging.rows
            if row.target_model == "product.template"
        }
        self.assertTrue(
            any(value is not None for value in products["PX105"].references.values())
        )
        self.assertTrue(
            all(value is None for value in products["PX106"].references.values())
        )

    def test_recipe_rebinds_combined_path_on_a_reordered_fresh_delivery(self):
        from datetime import UTC, datetime
        from uuid import uuid4

        context, actor = self.context, self.actor
        workspace_id, original, _, _ = self.combined(2)
        self.prepare(workspace_id)
        owner = context.migration_workspaces.get(workspace_id, actor=actor)
        publication = run_local_operation_sync(
            context.recipe_publication.publish,
            project_id=owner.project_id,
            data_version_id=owner.data_version_id,
            workspace_id=workspace_id,
            display_name="Fictional combined hierarchy",
            business_purpose="Preserve category links from any input",
            actor=actor,
        )
        setup = context.test_runs.start_setup(
            owner.project_id,
            expected_workspace_revision=context.migration_projects.get(
                owner.project_id, actor=actor
            ).optimistic_revision,
            recipe_revisions=(
                (publication.recipe.recipe_id, publication.revision.version),
            ),
            dependencies=(),
            label="Reordered combined hierarchy",
            export_as_of=datetime.now(UTC).date().isoformat(),
            operation_id=str(uuid4()),
            actor=actor,
        )
        url = f"/projects/{owner.project_id}/test-runs/{setup.run.migration_run_id}/fresh-data"
        page = self.client.get(url)
        uploaded = self.client.post(
            url + "/files",
            headers=POST_HEADERS,
            follow_redirects=False,
            data={
                "csrf_token": field(page, "csrf_token"),
                "revision": field(page, "revision"),
            },
            files=[
                (
                    "source_file",
                    (
                        name + ".csv",
                        ("\n".join((lines[0], *reversed(lines[1:]))) + "\n").encode(),
                        "text/csv",
                    ),
                )
                for name, value in self.combined_files.items()
                for lines in (value.decode().splitlines(),)
            ],
        )
        self.assertEqual(uploaded.status_code, 303, uploaded.text)
        page = self.client.get(url)
        registered = self.client.post(
            url + "/register",
            headers=POST_HEADERS,
            follow_redirects=False,
            data={
                "csrf_token": field(page, "csrf_token"),
                "revision": field(page, "revision"),
            },
        )
        self.assertEqual(registered.status_code, 303, registered.text)
        page = self.client.get(url)
        accepted = self.client.post(
            url + "/accept",
            headers=POST_HEADERS,
            follow_redirects=False,
            data={
                "csrf_token": field(page, "csrf_token"),
                "parameter_revision": "",
                "warnings_acknowledged": "1",
            },
        )
        self.assertEqual(accepted.status_code, 303, accepted.text)
        schema = self.schema(setup.setup_workspace.workspace_id)
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
            application.status.value,
            "READY",
            context.run_planning.repository.list_issues(application.application_id),
        )
        rebound = context.mapping_workspace.mappings.get_mapping_revision(
            application.workspace_id
        )
        self.assertNotEqual(
            {mapping.dataset_id for mapping in original.definition.datasets},
            {mapping.dataset_id for mapping in rebound.definition.datasets},
        )
        _, quality = self.prepare(application.workspace_id)
        self.assertEqual(
            (quality.ready_count, quality.quarantined_count, quality.blocked_count),
            (18, 0, 0),
        )
